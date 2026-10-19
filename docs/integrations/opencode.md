# OpenCode with two DGX Sparks

[OpenCode](https://opencode.ai/docs/) is a coding agent in the terminal. *Integrations → OpenCode → Create
connection files* writes `opencode.json` with the context limits of the running profile. By hand:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "twinspark": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "TwinSpark",
      "options": {"baseURL": "http://<node-a>:8000/v1", "apiKey": "{env:TWINSPARK_API_KEY}"},
      "models": {
        "coder": {"name": "TwinSpark coder", "limit": {"context": 131072, "output": 8192}},
        "secondary": {"name": "TwinSpark small", "limit": {"context": 32768, "output": 4096}}
      }
    }
  },
  "model": "twinspark/coder",
  "agent": {"plan": {"model": "twinspark/secondary"}}
}
```

Export the app key (*Keys & usage*) as `TWINSPARK_API_KEY` before starting `opencode`.

## Good to know

- OpenCode is an agent: it depends on tool calls. The profile needs tool calling on and a tool-call parser;
  *Integrations → Run compatibility check* tests both.
- `limit.context` should be what the profile serves; a larger number makes OpenCode send more than fits.
- With one copy of the model on each Spark, a session's requests reach the copy that holds its history in
  the prefix cache only when they carry a session id (`session-id` header) or `prompt_cache_key`
  ([docs/gateway.md](../gateway.md#picking-a-backend)); otherwise the less busy copy answers.
