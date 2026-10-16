"""Small, bounded checks of the inference gateway, run from the machine where the client app runs.

The controller's compatibility job (:mod:`twinspark.controller.compatibility`) proves that a *recipe*
handles chat, streaming and tools inside the cluster. These checks answer a different question: does this
endpoint, with this key and alias, work *from here* — through the SSH forward or the tailnet, the way the
app will call it? Each failure is placed in a layer so the page can say what to fix:

* transport – nothing answered (forward down, wrong port, firewall, TLS)
* auth      – the gateway rejected the inference key (401/403)
* model     – the alias is not served, the route is switching or the runtime failed (404, 5xx)
* parser    – a tool call was missing or malformed (tool parser or chat template)

Every request has a deadline and a response-size limit, the one tool is an inert echo that runs in this
function (never on the model's say-so), and no key value appears in any result.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import httpx

from ...controller.integrations import normalize_url
from ...remote.bundle import Redactor
from ...remote.logs import clean
from .base import Check

REQUEST_TIMEOUT = 45.0
MAX_RESPONSE_BYTES = 128 * 1024          # the controller's compatibility checks use the same bound
CHAT_TOKENS = 256                        # room for a short reasoning preamble before "ready"
TOOL_TOKENS = 512
ECHO_TOOL = {"type": "function", "function": {
    "name": "twinspark_echo", "description": "Return a supplied value without any external action.",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                   "required": ["value"], "additionalProperties": False}}}

TITLES = {
    "transport": "Gateway reachable",
    "auth": "Inference key accepted",
    "alias": "Model alias served",
    "context": "Deployed context",
    "chat": "Chat completion",
    "streaming": "Streaming chat",
    "tools": "Tool-call round trip",
}


class _Failure(Exception):
    def __init__(self, layer: str, detail: str, status: str = "fail"):
        super().__init__(detail)
        self.layer = layer
        self.status = status


def _refused(exc: BaseException) -> bool:
    """httpx reports "All connection attempts failed"; the refusal itself sits in the exception chain."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, ConnectionRefusedError) or "refused" in str(exc).lower():
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


class _Request:
    """One bounded request; turns every transport-level problem into a ``_Failure`` with a plain sentence."""

    def __init__(self, http: httpx.AsyncClient, base: str, key: Optional[str], timeout: float,
                 redact: Redactor):
        self.http, self.base, self.key, self.timeout, self.redact = http, base, key, timeout, redact

    def _error_text(self, data: bytes) -> str:
        try:
            message = json.loads(data)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = data[:200].decode("utf-8", "replace")
        text = clean(str(message)).strip().replace("\n", " ")[:200]
        return f" The gateway said: {self.redact(text)}" if text else ""

    async def send(self, method: str, path: str, body: Optional[dict] = None, *,
                   purpose: str) -> tuple[int, httpx.Headers, bytes]:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        data = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                async with self.http.stream(method, self.base + path, json=body, headers=headers) as response:
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise _Failure("model", f"The {purpose} response was larger than "
                                                    f"{MAX_RESPONSE_BYTES // 1024} KiB, so the check stopped reading.")
                    return response.status_code, response.headers, bytes(data)
        except _Failure:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            reason = "the connection was refused" if _refused(exc) else "no connection could be made"
            raise _Failure("transport", f"Could not connect to {self.base}: {reason}. Start the TwinSpark "
                                        "connection (SSH forward or tailnet) on this computer and check the port.") \
                from None
        except (TimeoutError, httpx.TimeoutException):
            layer = "transport" if purpose == "model list" else "model"
            raise _Failure(layer, f"The {purpose} request got no complete answer within {self.timeout:.0f} s. "
                                  + ("Check that the forward points at the gateway."
                                     if layer == "transport" else "The model may be loading or overloaded; "
                                     "check the alias in TwinSpark and try again.")) from None
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
            raise _Failure("transport", f"The connection dropped during the {purpose} request "
                                        f"({type(exc).__name__}). Check the forward and try again.") from None
        except (httpx.UnsupportedProtocol, httpx.InvalidURL, httpx.ProxyError):
            raise _Failure("transport", f"{self.base} cannot be used as an HTTP(S) gateway address.") from None
        except httpx.HTTPError as exc:
            raise _Failure("transport", f"The {purpose} request failed ({type(exc).__name__}).") from None

    def raise_for_status(self, status: int, data: bytes, *, purpose: str, tools: bool = False) -> None:
        if status == 200:
            return
        said = self._error_text(data)
        if status in (401, 403):
            raise _Failure("auth", f"The gateway rejected the inference key (HTTP {status}). Copy the inference key "
                                   "from TwinSpark again; the management key does not work for inference." + said)
        if tools and status in (400, 422):
            raise _Failure("parser", f"The gateway refused the tool request (HTTP {status}). The recipe needs tool "
                                     "calling enabled with a parser that suits the model." + said)
        if status == 404:
            raise _Failure("model", f"The gateway answered HTTP 404 for the {purpose} request: the alias or "
                                    "endpoint is unknown. Activate a recipe with this alias in TwinSpark." + said)
        if status == 503:
            raise _Failure("model", "The alias is switching or not loaded (HTTP 503). Wait until TwinSpark shows "
                                    "it as serving, then check again." + said)
        raise _Failure("model", f"The gateway answered HTTP {status} for the {purpose} request. Check the alias "
                                "and its recipe in TwinSpark." + said)


def _json(data: bytes, purpose: str) -> dict:
    try:
        result = json.loads(data)
    except ValueError:
        raise _Failure("model", f"The {purpose} response was not JSON.") from None
    if not isinstance(result, dict):
        raise _Failure("model", f"The {purpose} response was not a JSON object.")
    return result


def _message(result: dict) -> dict:
    try:
        message = result["choices"][0]["message"]
        if isinstance(message, dict):
            return message
    except (KeyError, IndexError, TypeError):
        pass
    raise _Failure("model", "The response did not contain a Chat Completions message.")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _reasoning(obj: dict) -> str:
    return _text(obj.get("reasoning_content")) or _text(obj.get("reasoning"))


async def validate_endpoint(base_url: str, api_key: Optional[str], alias: str, *,
                            http: Optional[httpx.AsyncClient] = None, min_context: Optional[int] = None,
                            timeout: float = REQUEST_TIMEOUT, tools: bool = True) -> list[Check]:
    """Check ``base_url`` (an OpenAI-compatible ``…/v1``) for ``alias`` with ``api_key``, from this host.

    Order: transport → auth → alias (with its ``max_model_len`` and TwinSpark revision) → context (when
    ``min_context`` is given) → chat → streaming → tool round trip. After a transport, auth or alias failure
    the remaining checks are reported as ``skip``. Pass ``http`` to reuse a client (tests use an in-process
    ASGI transport); without it a short-lived client is created that ignores proxy variables, because the
    endpoint is a loopback forward or a tailnet address, never something behind an HTTP proxy.
    """
    checks: list[Check] = []
    redact = Redactor([api_key] if api_key else [])

    def add(check_id: str, status: str, layer: str, detail: str, **data) -> None:
        checks.append(Check(check_id, TITLES[check_id], layer, status, redact(detail), data))

    def skip_rest(after: str, reason: str) -> list[Check]:
        order = ["transport", "auth", "alias", "context", "chat", "streaming", "tools"]
        for check_id in order[order.index(after) + 1:]:
            if check_id == "context" and min_context is None or check_id == "tools" and not tools:
                continue
            add(check_id, "skip", {"transport": "transport", "auth": "auth", "tools": "parser"}.get(check_id, "model"),
                reason)
        return checks

    try:
        base = normalize_url(base_url)
    except ValueError as exc:
        add("transport", "fail", "transport", f"{exc}.")
        return skip_rest("transport", "Skipped because the gateway address is not usable.")
    own = http is None
    client = http or httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)))
    req = _Request(client, base, api_key, timeout, redact)
    try:
        # transport + auth + alias from one GET /v1/models
        try:
            status, _, data = await req.send("GET", "/models", purpose="model list")
        except _Failure as exc:
            add("transport", "fail", exc.layer, str(exc))
            return skip_rest("transport", "Skipped because TwinSpark could not reach the gateway from here.")
        add("transport", "pass", "transport", f"The gateway at {base} answered from this computer.")
        if status in (401, 403):
            try:
                req.raise_for_status(status, data, purpose="model list")
            except _Failure as exc:
                add("auth", "fail", "auth", str(exc))
            return skip_rest("auth", "Skipped because the inference key was rejected.")
        try:
            req.raise_for_status(status, data, purpose="model list")
            listing = _json(data, "model list").get("data")
            if not isinstance(listing, list):
                raise _Failure("model", "The model list did not contain a data array; this is not an "
                                        "OpenAI-compatible gateway address.")
        except _Failure as exc:
            add("auth", "skip", "auth", "Not confirmed because the model list failed.")
            add("alias", "fail", exc.layer, str(exc))
            return skip_rest("alias", "Skipped because the model list failed.")
        add("auth", "pass", "auth", "The inference key was accepted." if api_key
            else "The gateway answered without an inference key.")
        entries = [e for e in listing if isinstance(e, dict)]
        served = [str(e.get("id")) for e in entries if isinstance(e.get("id"), str)]
        entry = next((e for e in entries if e.get("id") == alias), None)
        if entry is None:
            shown = ", ".join(served[:12]) or "none"
            add("alias", "fail", "model", f"The gateway does not serve the alias {alias} (served: {shown}). "
                                          "Activate a recipe with this alias in TwinSpark, or choose a served alias.",
                served_aliases=served[:50])
            return skip_rest("alias", "Skipped because the alias is not served.")
        meta = entry.get("twinspark") if isinstance(entry.get("twinspark"), dict) else {}
        max_len = entry.get("max_model_len") or meta.get("max_model_len")
        max_len = max_len if isinstance(max_len, int) and not isinstance(max_len, bool) else None
        revision = meta.get("revision") if isinstance(meta.get("revision"), str) else None
        route = meta.get("status") if isinstance(meta.get("status"), str) else None
        facts = [f"revision {revision}" if revision else "", f"context {max_len:,} tokens" if max_len else ""]
        facts_text = ", ".join(f for f in facts if f)
        info = dict(served_aliases=served[:50], max_model_len=max_len, revision_id=revision,
                    served_model=entry.get("root") if isinstance(entry.get("root"), str) else None,
                    route_status=route)
        if route and route != "serving":
            add("alias", "warn", "model", f"The alias {alias} is listed but its route is {route}. Wait until "
                                          "TwinSpark shows it as serving.", **info)
        else:
            add("alias", "pass", "model", f"The alias {alias} is served" + (f" ({facts_text})." if facts_text
                                                                             else "."), **info)
        if min_context is not None:
            if max_len is None:
                add("context", "warn", "model", f"The gateway did not report a context limit for {alias}. Make sure "
                                                f"its recipe serves at least {min_context:,} tokens.")
            elif max_len < min_context:
                add("context", "fail", "model", f"The alias {alias} is deployed with {max_len:,} tokens of context; "
                                                f"at least {min_context:,} are needed. Choose a recipe with a larger "
                                                "deployed context; raising the number in the client does not help.",
                    max_model_len=max_len)
            else:
                add("context", "pass", "model", f"{max_len:,} tokens deployed; {min_context:,} needed.",
                    max_model_len=max_len)

        prompt = [{"role": "user", "content": "Reply with the word ready."}]
        for check_id in ("chat", "streaming"):
            body = {"model": alias, "messages": prompt, "max_tokens": CHAT_TOKENS, "temperature": 0}
            if check_id == "streaming":
                body["stream"] = True
            try:
                status, headers, data = await req.send("POST", "/chat/completions", body, purpose=check_id)
                req.raise_for_status(status, data, purpose=check_id)
                if check_id == "chat":
                    message = _message(_json(data, "chat"))
                    text, thought = _text(message.get("content")).strip(), _reasoning(message).strip()
                else:
                    text, thought = _parse_stream(headers, data)
            except _Failure as exc:
                add(check_id, exc.status, exc.layer, str(exc))
                if exc.layer in ("transport", "auth"):
                    return skip_rest(check_id, "Skipped after the previous request failed.")
                continue
            if text:
                add(check_id, "pass", "model", "Chat Completions returned text through the alias." if check_id == "chat"
                    else "Text deltas and the [DONE] marker arrived over server-sent events.")
            elif thought:
                add(check_id, "warn", "model", f"The model produced only reasoning text within {CHAT_TOKENS} tokens. "
                                               "The endpoint works, but the check could not see a final answer.")
            else:
                add(check_id, "fail", "model", "The model returned no text. Check the recipe's chat template and "
                                               "the alias in TwinSpark.")
        if tools:
            try:
                detail = await _tool_round_trip(req, alias)
                add("tools", "pass", "parser", detail)
            except _Failure as exc:
                add("tools", exc.status, exc.layer, str(exc))
        return checks
    finally:
        if own:
            await client.aclose()


def _parse_stream(headers: httpx.Headers, data: bytes) -> tuple[str, str]:
    if "text/event-stream" not in headers.get("content-type", ""):
        raise _Failure("model", "The streaming response did not use server-sent events.")
    done, text, thought = False, [], []
    for raw in data.decode("utf-8", "replace").splitlines():
        if not raw.startswith("data:"):
            continue
        value = raw[5:].strip()
        if value == "[DONE]":
            done = True
            break
        try:
            event = json.loads(value)
        except ValueError:
            raise _Failure("model", "The stream contained an event that was not JSON.") from None
        if not isinstance(event, dict):
            continue
        if "error" in event:
            raise _Failure("model", "The stream reported a server error. Check the alias in TwinSpark.")
        for choice in event.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                text.append(_text(delta.get("content")))
                thought.append(_reasoning(delta))
    if not done:
        raise _Failure("model", "The stream ended without its [DONE] marker.")
    return "".join(text).strip(), "".join(thought).strip()


async def _tool_round_trip(req: _Request, alias: str) -> str:
    messages = [{"role": "user", "content": "Call twinspark_echo with value twinspark, then report the tool's result."}]
    body = {"model": alias, "messages": messages, "max_tokens": TOOL_TOKENS, "temperature": 0,
            "tools": [ECHO_TOOL], "tool_choice": "auto"}
    status, _, data = await req.send("POST", "/chat/completions", body, purpose="tool call")
    req.raise_for_status(status, data, purpose="tool call", tools=True)
    message = _message(_json(data, "tool call"))
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise _Failure("parser", "The model answered without a tool call. The recipe needs tool calling enabled "
                                 "with the tool parser and chat template that suit this model.")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or function.get("name") != "twinspark_echo":
        raise _Failure("parser", "The tool call did not name the offered twinspark_echo tool. Check the recipe's "
                                 "tool parser.")
    if not isinstance(call.get("id"), str) or not call["id"]:
        raise _Failure("parser", "The tool call had no call ID, so a client cannot send the result back. Check the "
                                 "recipe's tool parser.")
    try:
        arguments = json.loads(function.get("arguments") or "")
    except (TypeError, ValueError):
        raise _Failure("parser", "The tool call's arguments were not valid JSON. The recipe's tool parser does not "
                                 "match the model's output format.") from None
    if not isinstance(arguments, dict) or not isinstance(arguments.get("value"), str):
        raise _Failure("parser", "The tool call's arguments did not match the tool's schema (a value string).")
    # The echo runs here, inertly: the model's request is answered with its own value and nothing else runs.
    echoed = arguments["value"][:200]
    body["messages"] = messages + [
        {"role": "assistant", "content": message.get("content"), "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": echoed}]
    body["tool_choice"] = "none"
    status, _, data = await req.send("POST", "/chat/completions", body, purpose="tool result")
    req.raise_for_status(status, data, purpose="tool result", tools=True)
    final = _message(_json(data, "tool result"))
    if final.get("tool_calls"):
        raise _Failure("parser", "After the tool result the model asked for another tool call instead of "
                                 "answering. Check the recipe's chat template for tool messages.")
    if not _text(final.get("content")).strip():
        raise _Failure("parser", "After the tool result the model returned no text. Check the recipe's chat "
                                 "template for tool messages.")
    extra = "" if len(calls) == 1 else f" The model asked for {len(calls)} calls; the first was answered."
    return ("A tool call with valid JSON arguments came back, the local echo result was accepted and the model "
            "answered. No external tool ran." + extra)
