# LiteLLM with two DGX Sparks

[LiteLLM](https://docs.litellm.ai/)'s proxy puts several providers behind one endpoint. The Sparks are one
more OpenAI-compatible provider. You need an app key (*Keys & usage*) and the gateway address
(see [the overview](README.md#three-things-every-app-needs)).

```yaml
# config.yaml for `litellm --config config.yaml`
model_list:
  - model_name: spark-default            # what LiteLLM's clients ask for
    litellm_params:
      model: openai/default              # openai/<alias>: the alias on the Sparks
      api_base: http://<node-a>:8000/v1  # with /v1
      api_key: os.environ/TWINSPARK_API_KEY
  - model_name: spark-coder
    litellm_params:
      model: openai/coder
      api_base: http://<node-a>:8000/v1
      api_key: os.environ/TWINSPARK_API_KEY
```

## Good to know

- LiteLLM can fall back to another provider when a model fails. The Sparks can also fall back themselves
  while a switch runs ([docs/gateway.md](../gateway.md#fallback-while-switching)); use one of the two for a
  given alias, not both, so it is clear who answered.
- Give LiteLLM its own key on the Sparks: its usage then shows as one line in *Keys & usage*, and LiteLLM's
  own spend tracking does the per-user split.
- `api_base` ends at `/v1`; LiteLLM adds `/chat/completions` itself.
