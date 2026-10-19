"""What a pinned image can serve, asked of the image itself (master plan §3.1).

A successful pull says nothing about whether the image knows a flag, a tool-call parser, a reasoning
parser, a speculative method or the model's architecture. Before the old model is stopped the controller
runs a fixed script per engine inside the image (``docker run --rm --network none``, no GPU, no mounts)
that prints what the engine's own registries contain. The scripts are part of TwinSpark, never sent by
the controller; every lookup is wrapped so that one moved module leaves the rest of the answer intact.
"""

from __future__ import annotations

import json
from typing import Any

_COMMON = r'''
import json, sys, typing
out = {}
def keys(obj):
    for name in ("tool_parsers", "lazy_parsers", "_lazy_parsers", "reasoning_parsers", "lazy_reasoning_parsers",
                 "DetectorMap", "ToolCallParserEnum"):
        v = getattr(obj, name, None)
        if isinstance(v, dict):
            yield from v.keys()
def flat(t):
    args = typing.get_args(t)
    if not args:
        return [t] if isinstance(t, str) else []
    return [x for a in args for x in flat(a)]
def attempt(key, fn):
    try:
        out[key] = fn()
    except Exception as e:  # noqa: BLE001
        out.setdefault("errors", {})[key] = f"{type(e).__name__}: {e}"[:200]
'''

VLLM = _COMMON + r'''
out["engine"] = "vllm"
attempt("version", lambda: __import__("vllm").__version__)
def flags():
    from vllm.entrypoints.openai.cli_args import make_arg_parser
    try:
        from vllm.utils.argparse_utils import FlexibleArgumentParser
    except ImportError:
        from vllm.utils import FlexibleArgumentParser
    p = make_arg_parser(FlexibleArgumentParser())
    return sorted({o[2:] for a in p._actions for o in a.option_strings if o.startswith("--")})
attempt("flags", flags)
def tools():
    for path in ("vllm.tool_parsers", "vllm.entrypoints.openai.tool_parsers"):
        try:
            m = __import__(path, fromlist=["ToolParserManager"])
            return sorted(set(keys(m.ToolParserManager)))
        except (ImportError, AttributeError):
            continue
    raise ImportError("ToolParserManager not found")
attempt("tool_parsers", tools)
def reasoning():
    from vllm.reasoning import ReasoningParserManager
    return sorted(set(keys(ReasoningParserManager)))
attempt("reasoning_parsers", reasoning)
def spec():
    for path in ("vllm.config.speculative", "vllm.config"):
        try:
            m = __import__(path, fromlist=["SpeculativeMethod"])
            return sorted(set(flat(m.SpeculativeMethod)))
        except (ImportError, AttributeError):
            continue
    raise ImportError("SpeculativeMethod not found")
attempt("speculative_methods", spec)
def archs():
    from vllm import ModelRegistry
    return sorted(ModelRegistry.get_supported_archs())
attempt("architectures", archs)
print("TWINSPARK-PROBE " + json.dumps(out))
'''

SGLANG = _COMMON + r'''
out["engine"] = "sglang"
attempt("version", lambda: __import__("sglang").__version__)
def flags():
    import argparse
    from sglang.srt.server_args import ServerArgs
    p = argparse.ArgumentParser()
    ServerArgs.add_cli_args(p)
    return sorted({o[2:] for a in p._actions for o in a.option_strings if o.startswith("--")})
attempt("flags", flags)
def tools():
    from sglang.srt.function_call.function_call_parser import FunctionCallParser
    return sorted(set(keys(FunctionCallParser)))
attempt("tool_parsers", tools)
def reasoning():
    for path in ("sglang.srt.parser.reasoning_parser", "sglang.srt.reasoning_parser"):
        try:
            m = __import__(path, fromlist=["ReasoningParser"])
            return sorted(set(keys(m.ReasoningParser)))
        except (ImportError, AttributeError):
            continue
    raise ImportError("ReasoningParser not found")
attempt("reasoning_parsers", reasoning)
def spec():
    from sglang.srt.speculative.spec_info import SpeculativeAlgorithm
    return sorted(m.name for m in SpeculativeAlgorithm)
attempt("speculative_methods", spec)
def archs():
    from sglang.srt.models.registry import ModelRegistry
    return sorted(ModelRegistry.get_supported_archs())
attempt("architectures", archs)
print("TWINSPARK-PROBE " + json.dumps(out))
'''

SCRIPTS = {"vllm": VLLM, "sglang": SGLANG}
_LISTS = ("flags", "tool_parsers", "reasoning_parsers", "speculative_methods", "architectures")


def parse(output: str) -> dict[str, Any]:
    """The probe's answer from the container's output, bounded; ``{"error": ...}`` when there is none."""
    line = next((ln for ln in reversed((output or "").splitlines()) if ln.startswith("TWINSPARK-PROBE ")), None)
    if line is None:
        return {"error": "the image printed no probe result: " + (output or "").strip()[-300:]}
    try:
        doc = json.loads(line[len("TWINSPARK-PROBE "):])
    except ValueError:
        return {"error": "the probe result was not JSON"}
    if not isinstance(doc, dict):
        return {"error": "the probe result was not an object"}
    out: dict[str, Any] = {"engine": str(doc.get("engine") or "")[:20], "version": str(doc.get("version") or "")[:60]}
    for key in _LISTS:
        v = doc.get(key)
        if isinstance(v, list):
            out[key] = [str(x)[:120] for x in v[:4000] if isinstance(x, (str, int))]
    errors = doc.get("errors")
    if isinstance(errors, dict):
        out["errors"] = {str(k)[:40]: str(v)[:200] for k, v in list(errors.items())[:10]}
    return out
