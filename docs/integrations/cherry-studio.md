# Cherry Studio with two DGX Sparks

[Cherry Studio](https://docs.cherry-ai.com/) is a desktop chat client. You need an app key (*Keys & usage*)
and the gateway address (see [the overview](README.md#three-things-every-app-needs)).

1. *Settings → Model Services → + Add*. Name: `TwinSpark`, provider type: *OpenAI*.
2. Switch the new provider on. **API Key**: the app key.
3. **API address**: `http://<node-a>:8000`. Cherry Studio adds `/v1/chat/completions` itself; the preview
   under the field shows the full address it will call — it should end in `/v1/chat/completions`.
4. Under the models, **+ Add** (or *Manage* to pick from the list) and enter the alias as the model ID:
   `default`, `secondary`, `coder`, ….
5. *Check* sends a test request.

## Good to know

- If the preview shows `/v1/v1/…`, remove the `/v1` you typed.
- Each alias is one model entry; a switch on the Sparks keeps the alias, so the entries stay valid.
