"""Import eugr/spark-vllm-docker recipes (``recipes/*.yaml``) as TwinSpark drafts.

An eugr recipe is a ``vllm serve`` command template plus defaults, env, mods and
a container name. The importer renders the template exactly like
``run-recipe.py`` does (``str.format`` with ``defaults``), tokenises the
command, lifts every flag TwinSpark understands into structured settings,
drops the manager-owned ones (host/port/parallel wiring/api key — TwinSpark sets
those itself), and keeps everything else verbatim and in order as raw vLLM
arguments. Nothing is lost silently: the returned report lists what went where.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Optional

import yaml

from ..schemas.enums import DistributedBackend, Quantization, RuntimeAdapter, Topology, VerificationStatus
from ..schemas.profile import MANAGER_OWNED, ProfileDraft, canonical_flag, flag_base

_NUMBER = re.compile(r"^-?[0-9.]+([eE][-+]?[0-9]+)?$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAMPLING = {"temperature", "top_p", "top_k", "min_p", "repetition_penalty"}


class RecipeImportError(ValueError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return (s or "recipe")[:63]


def guess_quantization(model: str, declared: Optional[str] = None) -> tuple[Quantization, bool]:
    if declared:
        try:
            return Quantization(declared.lower()), False
        except ValueError:
            pass
    m = model.lower()
    for key, q in (("nvfp4", Quantization.NVFP4), ("mxfp4", Quantization.MXFP4),
                   ("autoround", Quantization.AUTOROUND), ("int4", Quantization.INT4),
                   ("awq", Quantization.AWQ), ("gptq", Quantization.GPTQ),
                   ("fp8", Quantization.FP8), ("bf16", Quantization.BF16)):
        if key in m:
            return q, False
    return Quantization.FP8, True


def _split_command(command: str) -> list[str]:
    text = re.sub(r"\\\s*\n", " ", command)
    try:
        return shlex.split(text, comments=False)
    except ValueError as exc:
        raise RecipeImportError(f"cannot tokenise the recipe command: {exc}") from exc


def _flag_pairs(tokens: list[str]) -> list[tuple[str, Optional[str], str]]:
    """[(raw flag token, value or None, canonical base)] in order."""
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or _NUMBER.match(tok):
            raise RecipeImportError(f"unexpected positional argument {tok!r} in the vLLM command")
        if "=" in tok:
            out.append((tok, None, flag_base(tok)))
            i += 1
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if nxt is not None and (not nxt.startswith("-") or _NUMBER.match(nxt)):
            out.append((tok, nxt, flag_base(tok)))
            i += 2
        else:
            out.append((tok, None, flag_base(tok)))
            i += 1
    return out


def _value(tok: str, val: Optional[str]) -> Optional[str]:
    if val is not None:
        return val
    return tok.split("=", 1)[1] if "=" in tok else None


def _json(v: Optional[str]) -> Any:
    if v is None:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def _int(v: Optional[str], what: str) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        raise RecipeImportError(f"{what} must be an integer, got {v!r}") from None


def _parallel_size(v: Optional[str], what: str) -> int:
    size = _int(v, what)
    if size < 1:
        raise RecipeImportError(f"{what} must be a positive integer, got {v!r}")
    return size


def _validate_document(doc: dict[str, Any]) -> None:
    """Validate YAML shapes before template expansion and collection operations."""
    if not isinstance(doc["command"], str) or not doc["command"].strip():
        raise RecipeImportError("recipe command must be a non-empty string")
    for key in ("defaults", "env"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, dict) or
                                  any(not isinstance(k, str) for k in value)):
            raise RecipeImportError(f"recipe {key} must be a mapping with string keys")
    for key in ("mods", "build_args"):
        value = doc.get(key)
        if value is not None and (not isinstance(value, list) or
                                  any(not isinstance(item, str) for item in value)):
            raise RecipeImportError(f"recipe {key} must be a list of strings")
    if doc.get("quantization") is not None and not isinstance(doc["quantization"], str):
        raise RecipeImportError("recipe quantization must be a string")


def parse_eugr_recipe(text: str, profile_name: Optional[str] = None,
                      overrides: Optional[dict[str, Any]] = None,
                      source_ref: Optional[str] = None) -> tuple[ProfileDraft, dict[str, Any]]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeImportError(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or "command" not in doc:
        raise RecipeImportError("an eugr recipe needs at least 'name' and 'command'")
    _validate_document(doc)
    report: dict[str, Any] = {"mapped": {}, "dropped": [], "raw": [], "notes": []}
    params = {**(doc.get("defaults") or {}), **(overrides or {})}
    params.setdefault("host", "0.0.0.0")
    params.setdefault("port", 8000)
    try:
        rendered = str(doc["command"]).format(**params)
    except KeyError as exc:
        raise RecipeImportError(f"recipe command uses {exc} but defaults do not define it") from exc
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise RecipeImportError(f"cannot render recipe command template: {exc}") from exc
    tokens = _split_command(rendered)
    env = {str(k): str(v) for k, v in (doc.get("env") or {}).items()}
    while tokens and _ENV_ASSIGN.match(tokens[0]):
        k, _, v = tokens.pop(0).partition("=")
        env[k] = v
    model: Optional[str] = None
    if tokens[:2] == ["vllm", "serve"]:
        tokens = tokens[2:]
        if tokens and not tokens[0].startswith("-"):
            model = tokens.pop(0)
    elif len(tokens) >= 3 and tokens[0].startswith("python") and tokens[1] == "-m" and \
            "vllm" in tokens[2]:
        tokens = tokens[3:]
    else:
        raise RecipeImportError("only vLLM recipes (`vllm serve ...`) can be imported; "
                                f"command starts with {' '.join(tokens[:3])!r}")
    pairs = _flag_pairs(tokens)

    simple: dict[str, Any] = {"thinking": False, "tool_calling": False}
    behaviour: dict[str, Any] = {}
    adv: dict[str, Any] = {"prefix_cache": False}
    raw: list[str] = []
    tp, pp = 1, 1
    expert_parallel = False
    backend = DistributedBackend.MP
    for tok, val, base in pairs:
        v = _value(tok, val)
        dotted = "." in canonical_flag(tok.split("=", 1)[0])
        if base == "model" and not dotted:
            model = model or v
            continue
        if base == "tensor-parallel-size":
            tp = _parallel_size(v, "tensor parallel size")
            continue
        if base == "pipeline-parallel-size":
            pp = _parallel_size(v, "pipeline parallel size")
            continue
        if base == "data-parallel-size":
            if _parallel_size(v, "data parallel size") > 1:
                raise RecipeImportError("data-parallel recipes are not supported on two Sparks yet")
            continue
        if base == "distributed-executor-backend":
            try:
                backend = DistributedBackend((v or "").lower())
            except ValueError as exc:
                raise RecipeImportError(f"unsupported distributed-executor-backend {v!r}; "
                                        "choose auto, native, mp or ray") from exc
            report["mapped"]["distributed-executor-backend"] = backend.value
            continue
        if base == "gpu-memory-utilization":
            try:
                utilization = float(v)
            except (TypeError, ValueError) as exc:
                raise RecipeImportError(f"gpu-memory-utilization must be a number, got {v!r}") from exc
            if not math.isfinite(utilization) or not 0 < utilization <= 0.95:
                raise RecipeImportError("gpu-memory-utilization must be greater than 0 and at most 0.95; "
                                        f"got {v!r}")
            adv["gpu_memory_utilization"] = utilization
            report["mapped"][base] = utilization
            continue
        if base in MANAGER_OWNED:
            report["dropped"].append(tok if v is None else f"{tok.split('=')[0]} {v}")
            continue
        if dotted:
            raw.append(tok)
            if val is not None:
                raw.append(val)
            if base == "default-chat-template-kwargs":
                behaviour["manage_thinking_kwarg"] = False
                key = canonical_flag(tok.split("=", 1)[0]).split(".", 1)[1]
                if key in ("thinking", "enable_thinking") and str(v).lower() == "true":
                    simple["thinking"] = True
            continue
        mapped = True
        if base == "max-model-len":
            simple["context_length"] = "auto" if str(v).lower() == "auto" else _int(v, "max model len")
        elif base == "max-num-seqs":
            n = _int(v, "max num seqs")
            simple["concurrency"] = max(1, min(n, 512))
            adv["max_num_seqs"] = n
        elif base == "max-num-batched-tokens":
            adv["max_num_batched_tokens"] = _int(v, "max num batched tokens")
        elif base == "kv-cache-dtype":
            adv["kv_dtype"] = v
        elif base == "block-size":
            adv["block_size"] = _int(v, "block size")
        elif base == "enable-prefix-caching" and v is None:
            adv["prefix_cache"] = True
        elif base == "no-enable-prefix-caching":
            adv["prefix_cache"] = False
        elif base == "enable-chunked-prefill" and v is None:
            adv["chunked_prefill"] = True
        elif base == "no-enable-chunked-prefill":
            adv["chunked_prefill"] = False
        elif base == "enforce-eager" and v is None:
            adv["eager_mode"] = True
        elif base == "trust-remote-code" and v is None:
            adv["trust_remote_code"] = True
        elif base == "enable-expert-parallel" and v is None:
            expert_parallel = True
        elif base == "dtype":
            adv["dtype"] = v
        elif base == "attention-backend":
            adv["attention_backend"] = v
        elif base == "load-format":
            adv["weight_loader"] = v
        elif base == "tokenizer-mode":
            adv["tokenizer_mode"] = v
        elif base == "speculative-config" and isinstance(_json(v), dict):
            adv["speculative_config"] = _json(v)
        elif base == "compilation-config" and isinstance(_json(v), dict):
            adv["compilation_config"] = _json(v)
        elif base == "reasoning-parser":
            behaviour["reasoning_parser"] = v
            simple["thinking"] = True          # reasoning models think unless told otherwise
        elif base == "tool-call-parser":
            behaviour["tool_call_parser"] = v
        elif base == "enable-auto-tool-choice" and v is None:
            simple["tool_calling"] = True
        elif base == "chat-template":
            behaviour["chat_template"] = v
        elif base == "default-chat-template-kwargs" and isinstance(_json(v), dict):
            behaviour["chat_template_kwargs"] = _json(v)
            behaviour["manage_thinking_kwarg"] = False
            kw = _json(v)
            if kw.get("enable_thinking") is not None or kw.get("thinking") is not None:
                simple["thinking"] = bool(kw.get("enable_thinking", kw.get("thinking")))
        elif base == "override-generation-config" and isinstance(_json(v), dict) \
                and set(_json(v)) <= _SAMPLING | {"max_new_tokens"}:
            for k, val2 in _json(v).items():
                behaviour["max_tokens" if k == "max_new_tokens" else k] = val2
        else:
            mapped = False
        if mapped:
            report["mapped"][base] = v if v is not None else True
        else:
            raw.append(tok)
            if val is not None:
                raw.append(val)
    if not model:
        raise RecipeImportError("the recipe command does not name a model")
    if doc.get("model") and doc["model"] != model and "/" in str(doc["model"]):
        report["notes"].append(f"command serves {model!r}; recipe 'model' says {doc['model']!r} — "
                               "using the command's model")
    if "/" not in model or model.startswith("/"):
        raise RecipeImportError(f"model {model!r} is a local path; TwinSpark needs a Hugging Face "
                                "repo id (org/name) so it can pin and stage the weights")
    # ---- topology --------------------------------------------------------------------------
    cluster_only = bool(doc.get("cluster_only"))
    if tp * pp > 2:
        raise RecipeImportError(f"recipe needs tp={tp} x pp={pp} = {tp * pp} GPUs; a TwinSpark pair "
                                "has two (4x/8x Spark recipes are not supported)")
    if tp == 2:
        topo = Topology.TP_EP if expert_parallel else Topology.TP2
    elif pp == 2:
        topo = Topology.PP2
    else:
        topo = Topology.SINGLE_A
        if cluster_only:
            report["notes"].append("recipe is cluster_only but asks for one GPU — imported as single-a")
    if expert_parallel and topo != Topology.TP_EP:
        raw.append("--enable-expert-parallel")
    quant, guessed = guess_quantization(model, doc.get("quantization"))
    if guessed:
        report["notes"].append("quantization label guessed as fp8 (only used for sizing labels; "
                               "pinning reads the real checkpoint size)")
    simple.update(model=model, quantization=quant.value, topology=topo.value)
    simple.setdefault("context_length", 32768)
    simple.setdefault("concurrency", 1)
    if simple["tool_calling"] and not behaviour.get("tool_call_parser"):
        simple["tool_calling"] = False
        report["notes"].append("--enable-auto-tool-choice without a parser — tool calling disabled")
    # the recipe command is the source of truth: never add a thinking kwarg it lacks
    behaviour["manage_thinking_kwarg"] = False
    adv["extra_vllm_args"] = raw
    adv["env"] = env
    adv["mods"] = [PurePosixPath(str(m)).name for m in (doc.get("mods") or [])]
    report["raw"] = raw
    name = profile_name or slugify(str(doc.get("name") or model.split("/")[-1]))
    source = {"format": "eugr", "recipe": doc.get("name"), "description": doc.get("description"),
              "ref": source_ref, "container": doc.get("container"),
              "build_args": doc.get("build_args") or [], "cluster_only": cluster_only,
              "recipe_version": doc.get("recipe_version")}
    if doc.get("build_args"):
        report["notes"].append(f"eugr builds this image with {' '.join(doc['build_args'])} — "
                               f"pin the matching local image ({doc.get('container')})")
    if adv["mods"]:
        report["notes"].append(f"needs mods on both nodes: {', '.join(adv['mods'])} "
                               f"(`tsm mods import-eugr <spark-vllm-docker>/mods`)")
    draft = ProfileDraft(
        name=name, description=str(doc.get("description") or doc.get("name") or "")[:300],
        simple=simple, behaviour=behaviour, advanced=adv,
        runtime_adapter=RuntimeAdapter.EUGR, distributed_backend=backend,
        verification=VerificationStatus.COMMUNITY,
        tags=["eugr", "imported"], image_hint=doc.get("container"), source=source,
    )
    return draft, report
