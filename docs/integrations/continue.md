# Continue with two DGX Sparks

[Continue](https://docs.continue.dev/) is a coding assistant for VS Code and JetBrains. You need an app key
(*Keys & usage*) and the gateway address (see [the overview](README.md#three-things-every-app-needs)).

```yaml
# Continue's config.yaml
name: TwinSpark
version: 1.0.0
schema: v1
models:
  - name: Spark coder
    provider: openai
    model: coder                         # the alias
    apiBase: http://<node-a>:8000/v1
    apiKey: <the app key>
    roles: [chat, edit, apply]
    defaultCompletionOptions:
      contextLength: 131072              # the profile's context length
      maxTokens: 4096
  - name: Spark autocomplete
    provider: openai
    model: secondary                     # a small, fast model, e.g. on node B in a split profile
    apiBase: http://<node-a>:8000/v1
    apiKey: <the app key>
    roles: [autocomplete]
```

## Good to know

- Set `contextLength` to what the profile serves (*Profile → Overview*, or `tsm status`); Continue trims what
  it sends to fit.
- Autocomplete sends a request on almost every keystroke. A big model on both Sparks answers well but not
  fast enough for that; a small model on node B of a split profile is the usual pairing.
- Agent mode uses tool calls: the profile needs tool calling on and a tool-call parser.
