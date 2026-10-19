# Open WebUI with two DGX Sparks

[Open WebUI](https://docs.openwebui.com/) is a chat interface in the browser. It talks to TwinSpark like to any
OpenAI-compatible server. You need an app key (*Keys & usage*) and the gateway address
(see [the overview](README.md#three-things-every-app-needs)).

## In the settings

1. *Settings → Admin Settings → Connections*, under *OpenAI API*: **Add Connection**.
2. URL: `http://<node-a>:8000/v1` (with `/v1`). If Open WebUI runs in Docker on the workstation, the URL is
   the Spark's address, not `localhost`.
3. Authentication: *Bearer*, the app key.
4. Save. The aliases appear in the model selector (`default`, `secondary`, …).

## When starting the container

```bash
docker run -d -p 3000:8080 --name open-webui \
  -e OPENAI_API_BASE_URL=http://<node-a>:8000/v1 \
  -e OPENAI_API_KEY="$TWINSPARK_API_KEY" \
  -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:main
```

Open WebUI keeps connections in its own database after the first start; later changes go through the
settings page.

## Good to know

- Open WebUI asks for the model list when it starts and when you open the selector; a switch on the Sparks
  keeps the alias, so nothing needs to change there.
- Several people chatting at once: with one copy of the model on each Spark the gateway sends each new
  request to the less busy copy.
- Title generation and tags send extra requests to the same alias; with a small second model on node B
  (a split profile), point *Admin Settings → Interface → Task Model* at that alias.
