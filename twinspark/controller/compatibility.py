"""Small, bounded agent protocol checks through the stable gateway."""
from __future__ import annotations

import asyncio
import json
import secrets

import httpx

from ..gateway.app import build_gateway_app
from ..schemas.enums import JobState
from ..schemas.job import Job
from .controller import BusyError
from .integrations import select_alias

CHECKS = ("chat", "streaming", "tools", "structured", "responses")
DEFAULT_CHECKS = CHECKS[:4]
CHECK_TIMEOUT = 45
MAX_RESPONSE_BYTES = 128 * 1024


class CheckProblem(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


async def _request(client, path, body, *, streaming=False):
    data = bytearray()
    async with client.stream("POST", path, json=body) as response:
        if response.status_code != 200:
            status = "unsupported" if response.status_code in (400, 404, 405, 422, 501) else "fail"
            raise CheckProblem(status, f"Gateway returned HTTP {response.status_code}; review model/runtime support.")
        if streaming and "text/event-stream" not in response.headers.get("content-type", ""):
            raise CheckProblem("fail", "Streaming response did not use the SSE protocol.")
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise CheckProblem("fail", "Response exceeded the compatibility check size limit.")
    if streaming:
        return data.decode("utf-8")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise CheckProblem("fail", "Response was not a JSON object.")
    return result


def _message(data):
    try:
        message = data["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError
        return message
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CheckProblem("fail", "Response did not contain a Chat Completions message.") from exc


def _content(message):
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise CheckProblem("fail", "Model returned no text content within the check's token budget.")
    return content


async def probe(client, alias, check, info):
    body = {"model": alias, "messages": [{"role": "user", "content": "Reply with the word ready."}],
            "max_tokens": 64, "temperature": 0}
    endpoint = "/v1/chat/completions"
    if check == "chat":
        _content(_message(await _request(client, endpoint, body)))
        return "Chat Completions returned text through the selected alias."
    if check == "streaming":
        body["stream"] = True
        stream = await _request(client, endpoint, body, streaming=True)
        done, delta = False, False
        for line in stream.splitlines():
            if not line.startswith("data:"):
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                done = True
                continue
            event = json.loads(value)
            if "error" in event:
                raise CheckProblem("fail", "Stream contained a server error.")
            for choice in event.get("choices", []):
                text = choice.get("delta", {}).get("content")
                delta = delta or (isinstance(text, str) and bool(text))
        if not done or not delta:
            raise CheckProblem("fail", "Stream lacked text deltas or its completion marker.")
        return "SSE text deltas and the completion marker were received."
    if check == "tools":
        if not info["configured_tools"] or not info["tool_parser"]:
            raise CheckProblem("unsupported", "Recipe needs tool calling and a model-appropriate tool parser.")
        body.update(max_tokens=128, tool_choice="auto", tools=[{"type": "function", "function": {
            "name": "twinspark_echo", "description": "Return a supplied value without any external action.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                           "required": ["value"], "additionalProperties": False}}}])
        body["messages"] = [{"role": "user", "content":
                             "Call twinspark_echo with value twinspark, then report the tool's result."}]
        message = _message(await _request(client, endpoint, body))
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise CheckProblem("fail", "Model did not return exactly one tool call.")
        call = calls[0]
        function = call.get("function", {})
        if (call.get("type") != "function" or function.get("name") != "twinspark_echo"
                or not isinstance(call.get("id"), str) or not call["id"]
                or json.loads(function.get("arguments", "{}")) != {"value": "twinspark"}):
            raise CheckProblem("fail", "Tool name, arguments or call ID did not match the inert check tool.")
        body["messages"] += [message, {"role": "tool", "tool_call_id": call["id"], "content": "twinspark"}]
        body["tool_choice"] = "none"
        text = _content(_message(await _request(client, endpoint, body)))
        if "twinspark" not in text.lower():
            raise CheckProblem("fail", "Final reply did not report the local tool result.")
        return "Tool arguments and a second turn with the local echo result were verified. No external tool ran."
    if check == "structured":
        body["messages"] = [{"role": "user", "content": 'Return JSON with ok set to true.'}]
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "twinspark_check", "strict": True, "schema": {"type": "object",
                "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}
        result = json.loads(_content(_message(await _request(client, endpoint, body))))
        if not isinstance(result, dict) or set(result) != {"ok"} or result["ok"] is not True:
            raise CheckProblem("fail", "Output did not satisfy the requested JSON shape and value.")
        return "Strict JSON output matched the requested schema and value."
    result = await _request(client, "/v1/responses", {
        "model": alias, "input": "Reply with the word ready.", "max_output_tokens": 64})
    texts = [content.get("text") for item in result.get("output", []) if item.get("type") == "message"
             for content in item.get("content", []) if content.get("type") == "output_text"]
    if not any(isinstance(t, str) and t.strip() for t in texts):
        raise CheckProblem("fail", "Responses output did not contain a text message.")
    return "Native Responses POST returned text; retrieval, cancellation and full agent lifecycle remain unverified."


async def _cancellable(ctrl, job, coro):
    task = asyncio.create_task(coro)
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            while not task.done():
                if job.job_id in ctrl._cancel:
                    raise asyncio.CancelledError
                await asyncio.wait({task}, timeout=0.05)
            if job.job_id in ctrl._cancel:
                raise asyncio.CancelledError
            return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def check_alias(ctrl, alias: str, checks=None) -> Job:
    checks = list(DEFAULT_CHECKS if checks is None else checks)
    if not checks or len(checks) > len(CHECKS) or any(c not in CHECKS for c in checks):
        raise ValueError("choose one or more supported compatibility checks")
    checks = list(dict.fromkeys(checks))
    if ctrl.busy():
        raise BusyError("cluster is busy — wait before checking model compatibility")
    info = select_alias(ctrl, alias)
    if info["status"] != "serving" or not info["revision_id"]:
        raise ValueError("activate the recipe and wait for its alias to serve before running checks")
    profile = ctrl.get_profile(info["profile"])
    revision = profile.get_revision(info["revision_id"])
    part = next(p for p in revision.parts() if alias in p.draft.simple.aliases)
    if any(n not in ctrl.agents for n in part.required_nodes()):
        raise ValueError("agents for this alias are not configured")
    job = Job(job_id=f"compatibility-{secrets.token_hex(5)}", kind="compatibility",
              profile_revision=info["revision_id"], payload={
                  "alias": alias, "profile": info["profile"], "revision_id": info["revision_id"],
                  "dry_run": None, "checks": [], "requested_checks": checks, "compatible": None})
    await ctrl._lock.acquire()
    ctrl.current_job = job.job_id
    ctrl.busy_profile = info["profile"]
    try:
        ctrl._persist(job)
    except BaseException:
        ctrl.current_job = None
        ctrl.busy_profile = None
        ctrl._lock.release()
        raise

    async def run():
        try:
            step = job.begin_step("runtime")
            ctrl._persist(job)

            async def runtime_modes():
                return [await ctrl.agents[n].call("hardware_facts", timeout=5) for n in part.required_nodes()]

            facts = await _cancellable(ctrl, job, runtime_modes())
            modes = {f.get("runtime_mode") for f in facts}
            if modes not in ({"docker"}, {"dry-run"}):
                raise RuntimeError("runtime mode could not be confirmed consistently; no inference checks were sent")
            dry_run = modes == {"dry-run"}
            job.payload["dry_run"] = dry_run
            for node, fact in zip(part.required_nodes(), facts, strict=True):
                ctrl.store.kv_set(f"hardware:{node}", fact)
            job.finish_step(step, "Simulated runtime; no inference will be sent." if dry_run else "Runtime confirmed.")
            headers = {"Authorization": f"Bearer {ctrl.gateway.inference_api_key}"} \
                if ctrl.gateway.inference_api_key is not None else {}
            app = build_gateway_app(ctrl.gateway, max_response_bytes=MAX_RESPONSE_BYTES)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://twinspark-gateway", headers=headers) as client:
                for check in checks:
                    if job.job_id in ctrl._cancel:
                        raise asyncio.CancelledError
                    step = job.begin_step(check)
                    ctrl._persist(job)
                    status = "simulated" if dry_run else "pass"
                    try:
                        message = "Dry-run only: protocol support has not been measured." if dry_run else \
                            await _cancellable(ctrl, job, probe(client, alias, check, info))
                    except CheckProblem as exc:
                        status, message = exc.status, str(exc)
                    except TimeoutError:
                        status, message = "fail", "Compatibility check exceeded its time limit."
                    except (ValueError, KeyError, TypeError, AttributeError):
                        status, message = "fail", "Response did not match the requested protocol."
                    except httpx.HTTPError:
                        status, message = "fail", "Gateway transfer failed or was interrupted."
                    job.payload["checks"].append({"name": check, "status": status, "message": message})
                    job.finish_step(step, message)
                    ctrl._persist(job)
            job.payload["compatible"] = None if dry_run else all(
                r["status"] == "pass" for r in job.payload["checks"])
            job.state = JobState.COMPLETED
            job.stage = "completed"
        except asyncio.CancelledError:
            job.state, job.error = JobState.FAILED, "Compatibility checks cancelled."
            job.payload["cancelled"] = True
        except Exception:
            job.state, job.error = JobState.FAILED, "Could not confirm the runtime or finish compatibility checks."
            job.guidance = ["Check the node agents and serving alias, then retry. No client readiness was confirmed."]
        finally:
            try:
                if job.state == JobState.FAILED and job.steps and job.steps[-1].status == "running":
                    job.fail_step(job.steps[-1], job.error)
                job.touch()
                ctrl._persist(job)
                ctrl.store.kv_set(f"compatibility:{alias}", {"job_id": job.job_id, "state": job.state.value,
                    "checks": job.payload["checks"], "dry_run": job.payload["dry_run"],
                    "revision_id": info["revision_id"], "compatible": job.payload["compatible"]})
            finally:
                ctrl._cancel.discard(job.job_id)
                ctrl.current_job = None
                ctrl.busy_profile = None
                ctrl._lock.release()
            ctrl._audit("user", "integration.check", f"alias/{alias}",
                        {"job": job.job_id, "revision_id": info["revision_id"], "state": job.state.value})

    ctrl._spawn(run())
    return job
