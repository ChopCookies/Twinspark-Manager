"""Community results for recipes: did it work, how fast, what did people change (master plan §4.1).

Reporting is **opt-in and previewed**. Nothing is sent unless ``cookbook.community_url`` is set and the
user confirms the exact document (``tsm cookbook report <profile>`` or the Cookbook's *Share result*).
A report is built from an allowlist, never by stripping a full record:

* the recipe's name, the SHA-256 of the recipe file and the mode — which recipe, exactly;
* the outcome (``works``, ``works-with-changes``, ``fails`` with the stage it failed in);
* the TwinSpark, engine and engine versions, the topology;
* numbers from the newest valid benchmark of each preset (tok/s, TTFT, concurrency; numbers only);
* the settings changed against the recipe — only known tuning fields, only numbers, booleans and short
  plain words (no paths, addresses, model names, environment variables, raw arguments);
* an optional comment the user typed (500 characters), shown in the preview like everything else;
* a random reporter id (not derived from anything on the machine) so a later report replaces the earlier
  one instead of counting twice; ``--anonymous`` leaves it out.

No hostnames, IP addresses, user names, paths, keys or tokens are part of it; :func:`check_report` refuses
a document with anything else, on both ends. The server side lives in ``community/strato`` (PHP + MySQL).
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .. import __version__
from .tracking import settings

FORMAT = "twinspark-report/1"
OUTCOMES = ("works", "works-with-changes", "fails")
STAGES = ("download", "pull", "plan", "start", "health", "smoke", "serving", "other")
MAX_COMMENT = 500
MAX_CHANGES = 40
STATS_TTL = 1800.0

# settings whose changed value says something about tuning a recipe; nothing else is ever reported
_TUNING = {
    "simple.context_length", "simple.memory_strategy", "simple.concurrency", "simple.thinking",
    "simple.tool_calling", "simple.quantization", "simple.topology",
    "behaviour.reasoning_parser", "behaviour.tool_call_parser", "behaviour.temperature", "behaviour.top_p",
    "behaviour.top_k", "behaviour.min_p", "behaviour.repetition_penalty", "behaviour.max_tokens",
    "advanced.dtype", "advanced.kv_dtype", "advanced.block_size", "advanced.attention_backend",
    "advanced.weight_loader", "advanced.tokenizer_mode", "advanced.eager_mode", "advanced.chunked_prefill",
    "advanced.prefix_cache", "advanced.max_num_seqs", "advanced.max_num_batched_tokens",
    "advanced.gpu_memory_utilization", "advanced.head_start_delay_s",
    "advanced.speculative_config.method", "advanced.speculative_config.num_speculative_tokens",
    "engine", "distributed_backend",
}
_TUNING_PREFIXES = ("advanced.extra_vllm_flags.", "advanced.compilation_config.")
_BENCH_FIELDS = ("peak_output_tok_s", "at_concurrency", "knee_output_tok_s", "ttft_p95_ms", "tpot_p95_ms",
                 "max_concurrency", "output_tok_s", "longest", "needle_score_min", "needle_score", "tools_ok")
_WORD = re.compile(r"^[A-Za-z0-9_.:+-]{1,48}$")
_LOOKS_LIKE_ADDRESS = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{1,4}:){2,}")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REPORTER = re.compile(r"^[0-9a-f]{32}$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")
_FIELD = re.compile(r"^[a-z_][a-z0-9_.-]{0,95}$")


class ReportError(ValueError):
    pass


def _plain(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return abs(value) < 1e12
    return isinstance(value, str) and bool(_WORD.match(value)) and not _LOOKS_LIKE_ADDRESS.search(value)


def _flatten(doc: Any, path: str = "") -> dict[str, Any]:
    if isinstance(doc, dict):
        out: dict[str, Any] = {}
        for k, v in doc.items():
            out.update(_flatten(v, f"{path}.{k}" if path else str(k)))
        return out or ({path: None} if path else {})
    return {path: doc}


def tuning_changes(baseline: Optional[dict], current: dict) -> list[dict[str, Any]]:
    """Fields of the allowlist whose value differs from the recipe's, with the new value."""
    if not isinstance(baseline, dict):
        return []
    a, b = _flatten(baseline), _flatten(current)
    out = []
    for field in sorted(set(a) | set(b)):
        if field not in _TUNING and not field.startswith(_TUNING_PREFIXES):
            continue
        before, after = a.get(field), b.get(field)
        if before == after or not _plain(after) or not _FIELD.match(field):
            continue
        out.append({"field": field, "value": after})
    return out[:MAX_CHANGES]


def _bench_numbers(benchmarks: list[dict]) -> dict[str, dict[str, Any]]:
    """The newest valid result of each preset, numbers only."""
    out: dict[str, dict[str, Any]] = {}
    for b in sorted(benchmarks, key=lambda x: str(x.get("at") or ""), reverse=True):
        preset = b.get("preset")
        if not b.get("valid") or not isinstance(preset, str) or not _WORD.match(preset) or preset in out:
            continue
        s = b.get("summary") if isinstance(b.get("summary"), dict) else {}
        nums = {k: s[k] for k in _BENCH_FIELDS if isinstance(s.get(k), (int, float, bool)) and _plain(s[k])}
        if nums:
            out[preset] = nums
    return out


def build_report(record: dict, *, outcome: Optional[str] = None, failure_stage: Optional[str] = None,
                 comment: str = "", reporter: Optional[str] = None) -> dict[str, Any]:
    """A report from a validation record (:func:`twinspark.controller.validation.build_record`)."""
    rev = record.get("revision") or {}
    recipe, sha = rev.get("recipe"), rev.get("recipe_sha256")
    if not recipe or not str(rev.get("recipe_ref") or "").startswith("builtin:"):
        raise ReportError("only profiles imported from a Cookbook recipe can be reported (others have no shared "
                          "recipe to compare with)")
    evidence = (record.get("evidence") or {}).get("class")
    proc = record.get("procedure") or {}
    if evidence == "dry-run" or proc.get("dry_run"):
        raise ReportError("this revision only ran with simulated containers — a dry run is not a result")
    if outcome is None:
        if proc.get("state") == "completed":
            outcome = "works"
        elif proc.get("state") == "failed":
            outcome = "fails"
        else:
            raise ReportError("this revision has not been activated yet: say how it went (--outcome)")
    if outcome == "fails" and failure_stage is None:
        failed = [s.get("stage") for s in proc.get("stages") or [] if s.get("status") == "failed"]
        failure_stage = _stage(failed[-1] if failed else None)
    parts = (record.get("deployment") or {}).get("parts") or [{}]
    first = parts[0]
    doc: dict[str, Any] = {
        "format": FORMAT, "recipe": recipe, "recipe_sha256": sha, "mode": record.get("mode"),
        "outcome": outcome, "failure_stage": failure_stage if outcome == "fails" else None,
        "twinspark": __version__, "engine": record.get("engine") or "vllm",
        "engine_version": first.get("vllm_version"), "topology": first.get("topology"),
        "hardware": "2x DGX Spark",
        "benchmarks": _bench_numbers(((record.get("results") or {}).get("benchmarks")) or []),
        "changes": record.get("changes") or [],
        "comment": (comment or "").strip()[:MAX_COMMENT],
    }
    if reporter:
        doc["reporter"] = reporter
    return check_report(doc)


def _stage(stage: Optional[str]) -> str:
    s = str(stage or "")
    for name, words in (("download", ("download", "stage-weights", "weights")), ("pull", ("pull", "image")),
                        ("plan", ("plan", "validate", "preflight")), ("start", ("start", "launch", "container")),
                        ("health", ("health", "ready")), ("smoke", ("smoke",)), ("serving", ("serv", "route"))):
        if any(w in s for w in words):
            return name
    return "other"


def check_report(doc: Any) -> dict[str, Any]:
    """The same rules the server applies: exactly these fields, only these kinds of values."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ReportError(f"not a {FORMAT} document")
    allowed = {"format", "recipe", "recipe_sha256", "mode", "outcome", "failure_stage", "twinspark", "engine",
               "engine_version", "topology", "hardware", "benchmarks", "changes", "comment", "reporter"}
    extra = set(doc) - allowed
    if extra:
        raise ReportError(f"unexpected fields: {', '.join(sorted(extra))}")
    if not _NAME.match(str(doc.get("recipe") or "")) or not _SHA.match(str(doc.get("recipe_sha256") or "")):
        raise ReportError("recipe and recipe_sha256 are required")
    if doc.get("mode") is not None and not re.match(r"^[a-z][a-z0-9-]{0,30}$", str(doc["mode"])):
        raise ReportError("invalid mode")
    if doc.get("outcome") not in OUTCOMES:
        raise ReportError(f"outcome must be one of {', '.join(OUTCOMES)}")
    if doc.get("failure_stage") is not None and doc["failure_stage"] not in STAGES:
        raise ReportError(f"failure_stage must be one of {', '.join(STAGES)}")
    for key in ("twinspark", "engine_version"):
        if doc.get(key) is not None and not _VERSION.match(str(doc[key])):
            raise ReportError(f"invalid {key}")
    if doc.get("engine") not in ("vllm", "sglang"):
        raise ReportError("invalid engine")
    if doc.get("topology") is not None and not _WORD.match(str(doc["topology"])):
        raise ReportError("invalid topology")
    if doc.get("hardware") != "2x DGX Spark":
        raise ReportError("invalid hardware")
    benches = doc.get("benchmarks")
    if not isinstance(benches, dict) or len(benches) > 8:
        raise ReportError("invalid benchmarks")
    for preset, nums in benches.items():
        if not _WORD.match(str(preset)) or not isinstance(nums, dict) or set(nums) - set(_BENCH_FIELDS) \
                or not all(isinstance(v, (int, float, bool)) and _plain(v) for v in nums.values()):
            raise ReportError(f"invalid benchmark numbers for {preset!r}")
    changes = doc.get("changes")
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ReportError("invalid changes")
    for c in changes:
        field = str(c.get("field")) if isinstance(c, dict) else ""
        if not isinstance(c, dict) or set(c) != {"field", "value"} or not _FIELD.match(field) \
                or not (field in _TUNING or field.startswith(_TUNING_PREFIXES)) or not _plain(c["value"]):
            raise ReportError(f"a changed setting is not reportable: {str(c)[:80]}")
    comment = doc.get("comment")
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
        raise ReportError(f"comment: text up to {MAX_COMMENT} characters")
    if _LOOKS_LIKE_ADDRESS.search(comment) or re.search(r"(?:^|\s)(?:/home/|/Users/|~/|[A-Za-z]:\\)", comment):
        raise ReportError("the comment contains an address or a path — leave those out")
    if doc.get("reporter") is not None and not _REPORTER.match(str(doc["reporter"])):
        raise ReportError("invalid reporter id")
    return doc


def report_for_profile(ctrl, name: str, **kw: Any) -> dict[str, Any]:
    """Validation record + the profile's tuning changes against its recipe → a report (node A)."""
    from ..controller.validation import build_record
    try:
        record = build_record(ctrl, name)
    except ValueError as exc:
        raise ReportError(str(exc)) from None
    p = ctrl.get_profile(name)
    rev = p.get_revision(p.pinned_revision) if p.pinned_revision else p.latest()
    draft = rev.draft if rev is not None else p.working_draft()
    src = draft.source or {}
    baseline = (src.get("receipt") or {}).get("baseline")
    record = {**record, "mode": src.get("mode"), "engine": draft.engine,
              "changes": tuning_changes(baseline, settings(draft))}
    return build_report(record, **kw)


def reporter_id(home: Path) -> str:
    """A random id for this installation, kept in ``home/community-reporter`` (made on first use)."""
    path = Path(home) / "community-reporter"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if _REPORTER.match(text):
            return text
    except OSError:
        pass
    value = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _endpoint(base: str, name: str) -> str:
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise ReportError("cookbook.community_url must be an https:// URL")
    return base.rstrip("/") + "/" + name


def send_report(base: str, doc: dict[str, Any], client: Optional[httpx.Client] = None) -> dict[str, Any]:
    check_report(doc)
    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                               headers={"user-agent": f"twinspark-manager/{__version__}"})
    try:
        r = c.post(_endpoint(base, "report.php"), json=doc)
    except httpx.HTTPError as exc:
        raise ReportError(f"the community server is not reachable ({type(exc).__name__})") from None
    finally:
        if own:
            c.close()
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code not in (200, 201):
        raise ReportError(str(body.get("error") or f"the community server answered HTTP {r.status_code}")[:300])
    return body if isinstance(body, dict) else {}


_stats_cache: dict[str, tuple[float, dict]] = {}


def community_stats(base: Optional[str], client: Optional[httpx.Client] = None,
                    refresh: bool = False) -> dict[str, Any]:
    """Totals per recipe from the community server (cached 30 min); ``{}`` when none is configured."""
    if not base:
        return {}
    hit = _stats_cache.get(base)
    if hit and not refresh and time.time() - hit[0] < STATS_TTL:
        return hit[1]
    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                               headers={"user-agent": f"twinspark-manager/{__version__}"})
    try:
        r = c.get(_endpoint(base, "stats.php"))
        doc = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": f"community totals unavailable ({type(exc).__name__})"}
    finally:
        if own:
            c.close()
    if not isinstance(doc, dict) or not isinstance(doc.get("recipes"), dict):
        return {"error": "the community server sent no totals"}
    out = {"recipes": {}, "generated_at": doc.get("generated_at")}
    for name, s in list(doc["recipes"].items())[:1000]:
        if _NAME.match(str(name)) and isinstance(s, dict):
            out["recipes"][name] = _clean_stats(s)
    _stats_cache[base] = (time.time(), out)
    return out


def _clean_stats(s: dict) -> dict[str, Any]:
    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e12 else None
    modes = {}
    for m, ms in (s.get("modes") or {}).items() if isinstance(s.get("modes"), dict) else []:
        if isinstance(ms, dict) and re.match(r"^[a-z][a-z0-9-]{0,30}$|^-$", str(m)):
            modes[str(m)] = {k: num(ms.get(k)) for k in ("reports", "works", "fails", "median_tok_s")}
    changes = []
    for c in (s.get("top_changes") or [])[:10] if isinstance(s.get("top_changes"), list) else []:
        if isinstance(c, dict) and _FIELD.match(str(c.get("field") or "")) and _plain(c.get("value")):
            changes.append({"field": c["field"], "value": c.get("value"), "count": num(c.get("count"))})
    return {"reports": num(s.get("reports")) or 0, "works": num(s.get("works")) or 0,
            "works_with_changes": num(s.get("works_with_changes")) or 0, "fails": num(s.get("fails")) or 0,
            "success_rate": num(s.get("success_rate")), "median_tok_s": num(s.get("median_tok_s")),
            "last_report": s.get("last_report") if isinstance(s.get("last_report"), str) else None,
            "modes": modes, "top_changes": changes}
