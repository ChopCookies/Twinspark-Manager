# n8n with two DGX Sparks

In [n8n](https://docs.n8n.io/) the Sparks are an OpenAI credential with another base URL. You need an app key
(*Keys & usage*) and the gateway address (see [the overview](README.md#three-things-every-app-needs)).

## The credential

1. *Credentials → Add credential → OpenAI*.
2. **API Key**: the app key.
3. **Base URL**: `http://<node-a>:8000/v1` (with `/v1`). n8n in Docker on another machine needs the Spark's
   LAN address.
4. Save; n8n tests the credential by listing the models.

## In a workflow

- **AI Agent** with an **OpenAI Chat Model** sub-node: choose the credential, then the model — the list shows
  the aliases the key may use (`default`, `coder`, …). Choose *By ID* and type the alias if the list is empty.
- The plain **OpenAI** node (*Message a model*) works the same way.
- Agent tools need a profile with tool calling on and a tool-call parser; *Integrations → Run compatibility
  check* tests that before n8n depends on it.

## Good to know

- A switch on the Sparks answers `503` with `Retry-After` for a moment. Turn on *Retry On Fail* in the node's
  settings, or give the alias a fallback ([docs/gateway.md](../gateway.md#fallback-while-switching)).
- One key for n8n shows its token use separately under *Keys & usage* — useful for scheduled workflows.
