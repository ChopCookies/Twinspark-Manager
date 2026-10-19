# Using the Sparks from your apps

TwinSpark serves one OpenAI-compatible endpoint on node A, `http://<node-a>:8000/v1`. Every app that can talk
to "an OpenAI-compatible server" or "a custom OpenAI base URL" works with it. The pages here are the short
version for apps people ask about:

| App | What it is | Page |
|---|---|---|
| Open WebUI | chat in the browser, for you or a household | [open-webui.md](open-webui.md) |
| n8n | workflows and agents | [n8n.md](n8n.md) |
| LiteLLM | one proxy in front of several providers | [litellm.md](litellm.md) |
| Continue | coding assistant in VS Code / JetBrains | [continue.md](continue.md) |
| Cherry Studio | desktop chat client | [cherry-studio.md](cherry-studio.md) |
| Aider | coding in the terminal | [aider.md](aider.md) |
| OpenCode | coding agent in the terminal | [opencode.md](opencode.md) |

Hermes, OpenClaw, LangGraph and lm-evaluation-harness are covered in
[docs/agent-integrations.md](../agent-integrations.md) — TwinSpark writes their configuration for you.

## Three things every app needs

1. **The address.** `http://<node-a>:8000/v1` — node A's address as the app's computer sees it. With the
   workstation client, apps on that computer use the client's local address instead (the *Integrations*
   page shows it). From a Docker container on the same machine as the app, `localhost` is the container:
   use the host's LAN address or `host.docker.internal`.
2. **A key of its own.** *Keys & usage → New key*, or `tsm keys create "Open WebUI" --alias default`. One
   key per app: revoking it later leaves every other app working, and *Keys & usage* shows each app's tokens.
   The examples below expect it in `TWINSPARK_API_KEY` or paste it where the app asks for an API key.
3. **The model name: an alias.** Apps ask for `default` (or `secondary`, `coder`, … — whatever the profile's
   `api_alias` is), not for a model repository. A switch to another profile keeps the alias, so the app does
   not need a change. `curl -H "Authorization: Bearer $TWINSPARK_API_KEY" http://<node-a>:8000/v1/models`
   lists what the key may use.

**Try it first** in the GUI's *Playground*: the same endpoint, with time to first token and tokens per
second, and *Copy as curl* for a request that works.

## When an app has trouble

| What you see | Usually |
|---|---|
| `401` | the key is wrong, revoked or expired — *Keys & usage* says which |
| `403` for one alias | the key is limited to other aliases |
| `503` with `Retry-After` | a switch is running; the app should retry (most do). A fallback alias avoids the pause — [docs/gateway.md](../gateway.md#fallback-while-switching) |
| answers cut off, "context length exceeded" | the app assumes a larger context than the profile serves: set it to the profile's context length |
| tool calls fail | the profile needs tool calling on and a tool-call parser (*Profile → Settings*); *Integrations → Run compatibility check* tests it |
| a long session gets slower to start each answer | with a copy on each Spark, requests without a session id alternate between them and miss the prefix cache: send a `session-id` header or `prompt_cache_key` ([docs/gateway.md](../gateway.md#picking-a-backend)) |
