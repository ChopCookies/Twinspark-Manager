"""Client setup generated from recipe aliases, without copying credentials."""
from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

import yaml

CLIENTS = [
    {"id": "opencode", "title": "OpenCode (stable)", "kind": "agent",
     "description": "Coding agent with optional separate planning and coding models.",
     "docs_url": "https://opencode.ai/docs/providers/#custom-provider",
     "requirements": ["Chat completions and streaming; tool calls for agent tools.",
                      "Uses the stable OpenCode provider configuration format."]},
    {"id": "aider", "title": "Aider", "kind": "agent",
     "description": "Coding assistant with an optional second model for lightweight tasks.",
     "docs_url": "https://aider.chat/docs/llms/openai-compat.html",
     "requirements": ["Chat completions; native tool calling is not required."]},
    {"id": "langgraph", "title": "LangGraph / LangChain", "kind": "framework",
     "description": "Python agent starter using the Chat Completions API.",
     "docs_url": "https://docs.langchain.com/oss/python/langchain/agents",
     "requirements": ["Chat completions; tool calling when you add agent tools.",
                      "Install langchain, langchain-openai and langgraph in your client environment."]},
    {"id": "openclaw", "title": "OpenClaw", "kind": "agent",
     "description": "Custom provider fragment for your existing OpenClaw configuration.",
     "docs_url": "https://docs.openclaw.ai/gateway/config-tools/custom-providers",
     "requirements": ["Chat completions, streaming and tools for agent workflows.",
                      "Merge this fragment into your existing configuration."]},
    {"id": "hermes", "title": "Hermes Agent", "kind": "agent",
     "description": "Custom endpoint configuration for long-context tool workflows.",
     "docs_url": "https://hermes-agent.nousresearch.com/docs/integrations/providers",
     "requirements": ["At least 64,000 tokens of configured or observed context.",
                      "Chat completions, streaming and verified tool calling."]},
    {"id": "lm-eval", "title": "LM Evaluation Harness", "kind": "harness",
     "description": "Small generation-only GSM8K evaluation with revision-labelled results.",
     "docs_url": "https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/config_files.md",
     "requirements": ["Chat completions; install lm-eval with its API dependencies on the client.",
                      "20 samples are a smoke test, not a publishable benchmark.",
                      "Log-likelihood tasks require a separate tokenizer and logprobs integration."]},
]


def gateway_url(ctrl) -> str:
    listener = ctrl.config.gateway_listener
    host = listener.bind
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{'https' if listener.tls_cert else 'http'}://{host}:{listener.port}/v1"


def normalize_url(value: str) -> str:
    if len(value) > 2048 or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("use an HTTP(S) gateway URL without spaces or control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.hostname in ("0.0.0.0", "::")
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("invalid gateway URL")
    except ValueError as exc:
        raise ValueError("use an HTTP(S) gateway URL without credentials, query or fragment") from exc
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def alias_rows(ctrl) -> list[dict]:
    rows = {}
    profiles = ctrl.list_profiles()

    def row(profile, rev, part, alias, route=None):
        context = part.simple.context_length
        if route and route.max_model_len is not None:
            context = route.max_model_len
        revision_id = rev.revision_id if rev else None
        check = ctrl.store.kv_get(f"compatibility:{alias}")
        if not check or not revision_id or check.get("revision_id") != revision_id:
            check = None
        return {"alias": alias, "profile": profile.name, "revision_id": revision_id,
                "topology": (rev.draft if rev else profile.working_draft()).simple.topology.value,
                "node": {"single-a": "A", "single-b": "B"}.get(part.simple.topology.value, "A+B"),
                "configured_tools": part.simple.tool_calling,
                "tool_parser": part.behaviour.tool_call_parser,
                "context_length": context, "context_source": "observed" if route and
                route.max_model_len is not None else "recipe",
                "status": route.status if route else "planned", "latest_check": check}

    for profile in profiles:
        rev = profile.get_revision("pinned")
        draft = rev.draft if rev else profile.working_draft()
        if not draft:
            continue
        for part in draft.parts():
            for alias in part.simple.aliases:
                if alias in rows:
                    rows[alias]["ambiguous"] = True
                else:
                    rows[alias] = row(profile, rev, part, alias)
    # Routes always describe the exact revision in service, including older saves.
    for alias, route in ctrl.gateway.routes.items():
        if not route.revision_id or not route.backends:
            continue
        found = False
        for profile in profiles:
            rev = profile.get_revision(route.revision_id)
            if not rev:
                continue
            for part in rev.draft.parts():
                if alias in part.simple.aliases:
                    rows[alias] = row(profile, rev, part, alias, route)
                    found = True
                    break
            if found:
                break
        if not found:
            rows.pop(alias, None)  # Never label an unknown serving route with a planned recipe.
    return [rows[a] for a in sorted(rows)]


def catalog(ctrl) -> dict:
    return {"gateway": {"suggested_base_url": gateway_url(ctrl),
                        "auth_required": ctrl.gateway.inference_api_key is not None},
            "clients": CLIENTS, "aliases": alias_rows(ctrl)}


def select_alias(ctrl, alias: str) -> dict:
    item = next((r for r in alias_rows(ctrl) if r["alias"] == alias), None)
    if not item:
        raise ValueError("model alias not found — create or activate a recipe first")
    if item.get("ambiguous"):
        raise ValueError("multiple planned recipes use this alias — give them distinct aliases or activate one")
    return item


def export_client(ctrl, client_id: str, alias: str, base_url: str,
                  secondary_alias: str | None = None) -> dict:
    if client_id not in {c["id"] for c in CLIENTS}:
        raise ValueError("unknown integration client")
    primary = select_alias(ctrl, alias)
    models = [primary]
    if secondary_alias:
        if secondary_alias == alias:
            raise ValueError("choose a different second model alias")
        if client_id not in ("opencode", "aider", "langgraph", "openclaw"):
            raise ValueError("this client export uses one model; export each alias separately")
        models.append(select_alias(ctrl, secondary_alias))
    url = normalize_url(base_url)
    notes = ["Set TWINSPARK_API_KEY in the client environment to your inference key.",
             "Use a gateway hostname reachable from the client; localhost refers to the client's own machine.",
             "Configuration is not proof of model compatibility. Run checks on the serving alias."]
    if any(m["status"] == "planned" for m in models):
        notes.append("At least one alias is planned. Activate its recipe before using this configuration.")
    if client_id in ("opencode", "openclaw", "hermes"):
        if any(not isinstance(m["context_length"], int) for m in models):
            raise ValueError("context is auto and unmeasured — activate the recipe or set an explicit context length")
    if client_id == "hermes" and primary["context_length"] < 64000:
        raise ValueError("Hermes requires at least 64,000 tokens; choose a recipe that actually supports this context")

    def file(name, content, language):
        return {"name": name, "content": content, "language": language}

    def output(m):
        return min(4096, m["context_length"] // 4)

    if client_id == "opencode":
        config = {"$schema": "https://opencode.ai/config.json", "provider": {"twinspark": {
            "npm": "@ai-sdk/openai-compatible", "name": "TwinSpark",
            "options": {"baseURL": url, "apiKey": "{env:TWINSPARK_API_KEY}"},
            "models": {m["alias"]: {"name": f"TwinSpark {m['alias']}",
                                   "limit": {"context": m["context_length"], "output": output(m)}} for m in models}}},
            "model": f"twinspark/{alias}"}
        if secondary_alias:
            config["agent"] = {"build": {"model": f"twinspark/{alias}"},
                               "plan": {"model": f"twinspark/{secondary_alias}"}}
        files = [file("opencode.json", json.dumps(config, indent=2) + "\n", "json")]
    elif client_id == "aider":
        config = {"model": f"openai/{alias}", "openai-api-base": url}
        if secondary_alias:
            config["weak-model"] = f"openai/{secondary_alias}"
        notes.append("Set OPENAI_API_KEY to the same inference key before starting aider; YAML does not expand it.")
        files = [file(".aider.conf.yml", yaml.safe_dump(config, sort_keys=False), "yaml")]
    elif client_id == "langgraph":
        max_tokens = min(1024, primary["context_length"] // 4) \
            if isinstance(primary["context_length"], int) else 256
        code = ("import os\nfrom langchain_openai import ChatOpenAI\n"
                "from langchain.agents import create_agent\n\n"
                "def model(alias):\n    return ChatOpenAI(\n        model=alias,\n"
                f"        base_url={url!r},\n        api_key=os.environ['TWINSPARK_API_KEY'],\n"
                "        use_responses_api=False, stream_usage=False,\n"
                f"        temperature=0, max_tokens={max_tokens}, timeout=180, max_retries=0,\n    )\n\n"
                f"primary = model({alias!r})\n")
        if secondary_alias:
            code += f"reviewer = model({secondary_alias!r})  # Available for your own graph node.\n"
        code += ("agent = create_agent(model=primary, tools=[])\n"
                 "result = agent.invoke({'messages': [{'role': 'user', 'content': 'Explain this experiment.'}]})\n"
                 "print(result['messages'][-1].content)\n")
        files = [file("twinspark_agent.py", code, "python")]
        notes.append("The starter has no external tools. Add your own after validating tool calling.")
    elif client_id == "openclaw":
        config = {"agents": {"defaults": {"model": {"primary": f"twinspark/{alias}"}}},
                  "models": {"mode": "merge", "providers": {"twinspark": {
                      "baseUrl": url, "apiKey": "${TWINSPARK_API_KEY}", "api": "openai-completions",
                      "models": [{"id": m["alias"], "name": f"TwinSpark {m['alias']}",
                                  "input": ["text"], "contextWindow": m["context_length"],
                                  "maxTokens": output(m)} for m in models]}}}}
        files = [file("openclaw-twinspark.json", json.dumps(config, indent=2) + "\n", "json")]
        notes.append("Merge this fragment into OpenClaw's existing configuration.")
    elif client_id == "hermes":
        config = {"model": {"provider": "custom", "default": alias, "base_url": url,
                            "key_env": "TWINSPARK_API_KEY", "context_length": primary["context_length"]}}
        files = [file("hermes-twinspark.yaml", yaml.safe_dump(config, sort_keys=False), "yaml")]
        notes.append("Merge the model section into your Hermes configuration.")
    else:
        revision = primary["revision_id"] or "unversioned"
        max_tokens = min(1024, primary["context_length"] // 4) \
            if isinstance(primary["context_length"], int) else 256
        config = {"model": "local-chat-completions", "model_args": {
            "model": alias, "base_url": url + "/chat/completions", "tokenizer_backend": None,
            "tokenized_requests": False, "num_concurrent": 1, "max_retries": 0},
            "tasks": ["gsm8k"], "apply_chat_template": True, "num_fewshot": 0,
            "batch_size": 1, "limit": 20, "gen_kwargs": {"temperature": 0, "max_gen_toks": max_tokens},
            "output_path": f"./twinspark-eval-results/{alias}-{revision}", "log_samples": True}
        manifest = {"alias": alias, "revision_id": primary["revision_id"], "profile": primary["profile"],
                    "gateway_url": url, "output_path": config["output_path"], "evidence": "reported"}
        files = [file("twinspark-eval.yaml", yaml.safe_dump(config, sort_keys=False), "yaml"),
                 file("twinspark-evaluation.json", json.dumps(manifest, indent=2) + "\n", "json")]
        notes += ["Set OPENAI_API_KEY to the inference key, then run: lm-eval run --config twinspark-eval.yaml",
                  "This generation-only smoke test does not enable log-likelihood tasks such as MMLU.",
                  "Import results through /api/v1/integrations/evaluations with the exact recipe revision."]
    return {"client": client_id, "alias": alias, "secondary_alias": secondary_alias,
            "base_url": url, "revision_id": primary["revision_id"], "files": files, "notes": notes}
