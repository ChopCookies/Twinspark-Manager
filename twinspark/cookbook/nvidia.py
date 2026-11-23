"""Import NVIDIA's DGX Spark recipes (github.com/NVIDIA/local-ai-inference-recipes, ``models/**/recipe.yaml``).

An NVIDIA recipe is a complete launch written out for people: the container image, the ``docker run`` that
starts it on every node, and the ``vllm serve`` command of each node rank. TwinSpark starts the containers and
wires the two nodes itself, so the importer takes what is about the model and leaves the rest:

* the image (``runtime.image``) and the checkpoint;
* the environment the ``vllm serve`` command exports;
* tuning variables the ``docker run`` sets (``-e NCCL_BUFFSIZE=1048576``). The node's network wiring
  (interfaces, RoCE devices, head address, rank) and the Hugging Face token stay TwinSpark's: they come from
  ``controller.yaml`` and the node's vault;
* every ``vllm serve`` flag except the per-node ones (``--nnodes``, ``--node-rank``, ``--master-addr``,
  ``--headless``, host and port), which TwinSpark sets for each rank.

The result goes through the eugr importer as an eugr recipe, so both formats are read by the same rules and its
report says what went where. Only vLLM container recipes for one or two DGX Sparks can be imported; a recipe for
four or eight Sparks, llama.cpp or another machine is refused with the reason.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Optional

from ..schemas.enums import RuntimeAdapter, VerificationStatus
from ..schemas.profile import ProfileDraft, flag_base
from .eugr import RecipeImportError, _split_command, parse_eugr_document, slugify

# hardware id -> Sparks: what a TwinSpark pair can run
HARDWARE = {"dgx-spark": 1, "dgx-spark-2gpu": 2}
# variables of a recipe's `docker run` that are this pair's wiring or a secret: TwinSpark sets them per node
WIRING = frozenset({
    "NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "TP_SOCKET_IFNAME", "UCX_NET_DEVICES",
    "OMPI_MCA_btl_tcp_if_include", "MN_IF_NAME", "VLLM_HOST_IP", "NCCL_IB_HCA", "NCCL_IB_GID_INDEX",
    "NCCL_IB_DISABLE",
    "HEAD_IP", "MASTER_ADDR", "MASTER_PORT", "NODE_RANK", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_HOME",
    "HF_HUB_OFFLINE",
})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,199}(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?$")
_SERVE = re.compile(r"^[ \t]*vllm[ \t]+serve\b", re.MULTILINE)


def is_nvidia_recipe(doc: Any) -> bool:
    return (isinstance(doc, dict) and isinstance(doc.get("serve"), dict) and isinstance(doc.get("runtime"), dict)
            and "hardware" in doc and "schema_version" in doc)


def _exports(prefix: str, where: str) -> dict[str, str]:
    """``export NAME=value`` lines and ``NAME=value \\`` assignments that prefix the command (comments allowed).
    Anything else before ``vllm serve`` is refused: TwinSpark runs no shell."""
    env: dict[str, str] = {}
    for line in re.sub(r"\\[ \t]*\n", " ", prefix).splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError as exc:
            raise RecipeImportError(f"cannot read the {where} line {line.strip()[:60]!r}: {exc}") from exc
        assignments = words[1:] if words[:1] == ["export"] else words
        if not words or (words == ["export"]):
            continue
        for word in assignments:
            key, eq, value = word.partition("=")
            if not eq or not _ENV_NAME.match(key):
                raise RecipeImportError(f"the {where} runs {' '.join(words)[:60]!r} before vllm serve; only "
                                        "`export NAME=value` lines can be imported")
            if "$" in value:
                raise RecipeImportError(f"the {where} sets {key} from a shell variable; TwinSpark starts vLLM "
                                        "without a shell and cannot expand it")
            env[key] = value
    return env


def _serve(command: Any, where: str) -> tuple[dict[str, str], str]:
    """(exported environment, the ``vllm serve ...`` text) of a node's command."""
    if not isinstance(command, str) or not command.strip():
        raise RecipeImportError(f"the {where} has no command")
    if len(command) > 65536:
        raise RecipeImportError(f"the {where} command is too long")
    m = _SERVE.search(command)
    if m is None:
        raise RecipeImportError(f"the {where} does not run `vllm serve`")
    return _exports(command[:m.start()], where), command[m.start():]


def _setup_env(setup: Any, notes: list[str]) -> dict[str, str]:
    """``-e NAME=value`` of the recipe's ``docker run``, without this pair's wiring and secrets."""
    if not isinstance(setup, str) or not setup.strip():
        return {}
    try:
        tokens = shlex.split(re.sub(r"\\\s*\n", " ", setup), comments=True)
    except ValueError:
        notes.append("the recipe's container setup could not be read; its environment was not imported")
        return {}
    env: dict[str, str] = {}
    left: list[str] = []
    for i, tok in enumerate(tokens):
        if tok in ("-e", "--env") and i + 1 < len(tokens):
            item = tokens[i + 1]
        elif tok.startswith("--env="):
            item = tok.removeprefix("--env=")
        else:
            continue
        key, eq, value = item.partition("=")
        if not eq or not _ENV_NAME.match(key):
            continue                                # passes a host variable through (-e HF_TOKEN): not a value
        if key in WIRING or "$" in value or "<" in value:
            left.append(key)
            continue
        env[key] = value
    if env:
        notes.append(f"from the recipe's docker run: {', '.join(f'{k}={v}' for k, v in env.items())}")
    if left:
        notes.append(f"left to TwinSpark (this pair's wiring and secrets): {', '.join(dict.fromkeys(left))}")
    return env


def _flags(serve_text: str) -> list[str]:
    """The flags of a ``vllm serve`` command without the per-node ones, for comparing two ranks."""
    from ..schemas.profile import MANAGER_OWNED
    tokens = _split_command(serve_text)[3:]
    out, skip = [], False
    for i, tok in enumerate(tokens):
        if skip:
            skip = False
            continue
        if tok.startswith("-") and flag_base(tok) in MANAGER_OWNED:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            skip = "=" not in tok and nxt is not None and not nxt.startswith("--")
            continue
        out.append(tok)
    return out


def _text(v: Any, limit: int = 300) -> Optional[str]:
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def parse_nvidia_document(doc: dict[str, Any], profile_name: Optional[str] = None,
                          overrides: Optional[dict[str, Any]] = None,
                          source_ref: Optional[str] = None) -> tuple[ProfileDraft, dict[str, Any]]:
    if not is_nvidia_recipe(doc):
        raise RecipeImportError("not an NVIDIA recipe (needs schema_version, hardware, runtime and serve)")
    hardware = str(doc.get("hardware"))
    if doc.get("backend") != "vllm":
        raise RecipeImportError(f"this recipe runs {doc.get('backend')!r}; TwinSpark imports vLLM recipes")
    if hardware not in HARDWARE:
        raise RecipeImportError(f"this recipe is for {hardware}; a TwinSpark pair is two DGX Sparks "
                                f"({' or '.join(HARDWARE)} recipes can be imported)")
    runtime, serve = doc["runtime"], doc["serve"]
    if runtime.get("kind") != "container":
        raise RecipeImportError("this recipe does not run in a container; TwinSpark starts vLLM in one")
    image = runtime.get("image")
    if not isinstance(image, str) or not _IMAGE.match(image.strip()):
        raise RecipeImportError("the recipe names no usable container image (runtime.image)")
    image = image.strip()
    topology: dict[str, Any] = doc["topology"] if isinstance(doc.get("topology"), dict) else {}
    nodes = topology.get("node_count", HARDWARE[hardware])
    if not isinstance(nodes, int) or isinstance(nodes, bool) or not 1 <= nodes <= 2:
        raise RecipeImportError(f"this recipe needs {nodes} nodes; a TwinSpark pair has two")

    notes: list[str] = []
    env, serve_text = _serve(serve.get("command"), "head's" if nodes > 1 else "recipe's")
    extras = serve.get("additional_commands")
    for extra in extras if isinstance(extras, list) else []:
        if not isinstance(extra, dict) or extra.get("target") != "workers":
            continue
        w_env, w_text = _serve(extra.get("command"), "worker's")
        if w_env != env or _flags(w_text) != _flags(serve_text):
            notes.append("the worker's command differs from the head's beyond the per-node flags; TwinSpark "
                         "starts both ranks with the head's settings")
    env = {**_setup_env(runtime.get("setup_command"), notes), **env}
    if overrides:
        notes.append("an NVIDIA recipe has no template values: the overrides were not used")

    checkpoint: dict[str, Any] = doc["checkpoint"] if isinstance(doc.get("checkpoint"), dict) else {}
    model = _text(checkpoint.get("target_label"), 200)
    eugr_doc: dict[str, Any] = {
        "name": _text(doc.get("title"), 200) or model or "nvidia-recipe",
        "description": _text(doc.get("title")),
        "container": image,
        "env": env,
        # the command is plain shell, not an eugr template: its braces are JSON
        "command": serve_text.replace("{", "{{").replace("}", "}}"),
        "cluster_only": nodes > 1,
    }
    if model:
        eugr_doc["model"] = model
    precision: dict[str, Any] = doc["precision"] if isinstance(doc.get("precision"), dict) else {}
    label = str(precision.get("label") or "").lower()
    if "nvfp4" in label or label == "fp4-local":       # FP4-Local: NVIDIA's mixed NVFP4 (modelopt_mixed)
        eugr_doc["quantization"] = "nvfp4"
    elif "mxfp4" in label:
        eugr_doc["quantization"] = "mxfp4"
    draft, report = parse_eugr_document(eugr_doc, profile_name, None, source_ref)
    report["format"] = "nvidia"
    report["notes"] = notes + report["notes"]
    adv = draft.advanced
    shell = [a for a in adv.extra_vllm_args if "$" in a]
    if shell:
        raise RecipeImportError(f"the command passes {shell[0]!r} to vLLM; TwinSpark starts vLLM without a shell "
                                "and cannot expand the variable")
    if (nodes == 2) != (draft.simple.topology.value in ("tp2", "tp-ep", "pp2")):
        raise RecipeImportError(f"the recipe runs on {nodes} Spark(s) but its command splits the model "
                                f"as {draft.simple.topology.value}")
    draft_label = _text(checkpoint.get("draft_label"), 200)
    if draft_label and draft_label not in adv.extra_models:
        report["notes"].append(f"the recipe lists the drafter {draft_label}, but its command does not load it "
                               "from speculative-config; check the speculative settings")

    status = str(doc.get("status") or "")
    validation: dict[str, Any] = doc["validation"] if isinstance(doc.get("validation"), dict) else {}
    limitations = doc.get("limitations")
    lims = [x.strip()[:1000] for x in (limitations if isinstance(limitations, list) else [])
            if isinstance(x, str) and x.strip()][:20]
    note_text = _text(doc.get("notes"), 2000)
    source_notes = lims + ([note_text] if note_text else [])
    if runtime.get("label") and "mutable" in str(runtime["label"]).lower():
        source_notes.append(f"NVIDIA marks the image as mutable ({_text(runtime['label'], 120)}): Pin fixes the "
                            "digest you test with; a later pull of the same tag can be a different build.")
    source: dict[str, Any] = {
        "format": "nvidia", "title": _text(doc.get("title")), "ref": source_ref,
        "author": "NVIDIA local-ai-inference-recipes",
        "url": _github_page(source_ref), "hardware": hardware, "status": status or None,
        "validated_at": _text(validation.get("validated_at"), 40),
        "backend_version": _text(doc.get("backend_version"), 80),
        "requirements": [f"Image {image} on both nodes — Pin resolves its digest." if nodes > 1
                         else f"Image {image} — Pin resolves its digest."],
        "notes": source_notes,
    }
    adapter = (RuntimeAdapter.NGC if image.startswith("nvcr.io/nvidia/") else
               RuntimeAdapter.EUGR if image.startswith("eugr/") else RuntimeAdapter.CUSTOM)
    name = profile_name or slugify(f"{(model or draft.simple.model).split('/')[-1]}-"
                                   f"{draft.simple.topology.value}" if nodes > 1 else
                                   (model or draft.simple.model).split("/")[-1])
    draft = draft.model_copy(update={
        "name": name, "description": (_text(doc.get("title")) or draft.description)[:300],
        "runtime_adapter": adapter, "image_hint": image, "tags": ["nvidia", "imported"],
        "verification": (VerificationStatus.EXPERIMENTAL if status == "experimental"
                         else VerificationStatus.COMMUNITY),
        "source": {k: v for k, v in source.items() if v not in (None, [], "")},
    }, deep=True)
    return ProfileDraft.model_validate(draft.model_dump()), report


def _github_page(ref: Optional[str]) -> Optional[str]:
    """The page people read for a raw.githubusercontent.com download (the blob URL)."""
    m = re.match(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$", ref or "")
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{m.group(3)}"
    return ref if (ref or "").startswith("https://") else None
