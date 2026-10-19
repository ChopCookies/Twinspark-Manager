# Aider with two DGX Sparks

[Aider](https://aider.chat/) edits code from the terminal. *Integrations → Aider → Create connection files*
writes `.aider.conf.yml` for you; by hand it is three lines. You need an app key (*Keys & usage*) and the
gateway address (see [the overview](README.md#three-things-every-app-needs)).

```bash
export OPENAI_API_BASE=http://<node-a>:8000/v1
export OPENAI_API_KEY="$TWINSPARK_API_KEY"      # the app key; aider reads it from here
aider --model openai/coder                       # openai/<alias>
```

or in `.aider.conf.yml`:

```yaml
model: openai/coder
weak-model: openai/secondary        # optional: commit messages and summaries on a smaller model
openai-api-base: http://<node-a>:8000/v1
```

## Good to know

- Aider warns that it does not know the model's context window. Tell it in `.aider.model.metadata.json`:

  ```json
  {"openai/coder": {"max_input_tokens": 131072, "max_output_tokens": 8192,
                    "input_cost_per_token": 0, "output_cost_per_token": 0, "litellm_provider": "openai",
                    "mode": "chat"}}
  ```

- Aider's edit formats are text; native tool calling is not required.
