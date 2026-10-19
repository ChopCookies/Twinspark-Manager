# The gateway: keys, routing, fallbacks, its own process

Apps talk to one OpenAI-compatible endpoint on node A (`http://<node-a>:8000/v1`) and use an *alias* as the
model name. This page is about who may use it (keys), how the gateway picks a backend for an alias, what
answers while it is switching, and the process it runs in.

## One key per app

Each app gets a key of its own: Hermes, OpenClaw, a friend's laptop. A key can be limited to some aliases
and can expire; revoking one leaves every other app working. Node A keeps only the SHA-256 hash of a key
and its first characters (to tell keys apart): a new key is shown once.

```bash
tsm keys create "OpenClaw on the laptop" --alias coder --expires 90    # prints the key once
tsm keys                                                                # name, aliases, state, last use
tsm keys revoke key-1a2b3c4d5e                                          # 401 for that key from now on
tsm usage --days 30                                                     # requests and tokens per key and alias
tsm usage --days 30 --csv -o usage.csv
```

The same on the **Keys & usage** page, and `GET/POST /api/v1/keys`, `POST /api/v1/keys/{id}/revoke`,
`DELETE /api/v1/keys/{id}` (revoked or expired only), `GET /api/v1/usage?days=30[&format=csv]`.

- The single key of 0.6 (`inference_api_key` in the vault) became the key **default** on the first start of
  0.7, so apps configured with it keep working. Revoke it once every app has its own.
- A key limited to aliases sees only those in `/v1/models`; any other alias answers `403`.
- **Hermes** gets its own key: the workstation client asks node A for "Hermes on <computer>" the first time
  it configures Hermes, keeps it in its credential store, and makes a new one if it was revoked.
- **Usage** is counted by the gateway per day, key and alias: requests, prompt and completion tokens from
  each response's `usage`. A stream that did not ask for `stream_options.include_usage` is counted by its
  events and marked *estimated* (no prompt tokens). Totals are kept 90 days.

## Picking a backend

Only replicated profiles (one copy of the model on each node) have a choice. In this order:

1. **Session affinity** (on by default). A request with a `session-id` (or `x-session-id`) header, or a
   `prompt_cache_key` in the body, goes to the same copy as the session's earlier requests, whose prefix
   cache holds its history. Round robin would throw that away every other request. The choice is a
   weighted rendezvous hash, so it needs no table and survives restarts; when that copy fails, only its
   sessions move.
2. **Least in flight.** Without a session the copy with fewer requests in flight gets the request.
3. **Weights.** Ties are shared by the weight of each node (`node A × 2` sends twice as many idle requests
   to A; weight 0 sends nothing there while the other copy is up).

A copy that just refused a connection is tried last for 10 s. `round-robin` restores 0.6's plain rotation.

## Fallback while switching

A switch drains an alias: new requests used to get `503` with `Retry-After`. An alias can instead fall back
to

- **another alias** of this gateway (`default` → `secondary` in a split deployment), used only while that
  alias is serving itself; or
- **an external OpenAI-compatible endpoint** (`https://…/v1` and the model name to ask it for). Its key is
  set separately (`tsm gateway fallback-key`, kept in the vault slot `fallback_api_key`). The cluster's own
  keys are never sent there.

Responses from a fallback carry `X-TwinSpark-Fallback: alias:<name>` or `external`. The drain of the
switching alias does not wait for requests that went to the fallback. The same applies while the watchdog
has marked an alias down.

## Settings

The defaults come from `gateway:` in controller.yaml:

```yaml
gateway:
  affinity: true
  balance: least-inflight        # or round-robin
  weights: {A: 1, B: 1}
  fallbacks:
    default: {alias: secondary}
    coder: {url: "https://api.example.com/v1", model: "small-coder"}
```

They can be changed at runtime, without a restart (the next request uses them): the dashboard's
**Routing…** button, or

```bash
tsm gateway                                         # what is in effect, and where it comes from
tsm gateway set --weight A=2 --affinity on
tsm gateway set --fallback default=alias:secondary
tsm gateway set --fallback coder=url:https://api.example.com/v1,model:small-coder
tsm gateway fallback-key < key.txt                  # the external endpoint's key
tsm gateway set --fallback default=none
tsm gateway reset                                   # back to controller.yaml
```

`GET/PUT/DELETE /api/v1/gateway`, `PUT /api/v1/gateway/fallback-key`. The routing summary is also in
`GET /api/v1/status` (`gateway`), and each route reports its in-flight requests per node.

## Its own process

With `gateway_mode: standalone` (the default for new installs since 0.7) the gateway runs as its own
service, `twinspark-gateway.service` (`tsm serve gateway`). A controller restart, crash or update then no
longer drops the connections of running streams: an agent in the middle of a long answer keeps it.

- The controller writes the routing (routes, settings, the keys' hashes) to `gateway_state_path` atomically,
  with a rising version, at every change. The gateway applies a new file in one step: a request sees the
  old or the new routing, never half of it. A damaged file is ignored and the last state stays.
- The gateway answers the controller on `gateway_control_socket` (a Unix socket for the service user only):
  which version it applied, the requests in flight per route, the token counters. A switch drains only
  once the gateway has applied the draining state; a gateway that does not answer has nothing in flight.
- After a restart the controller takes over the routes the gateway is serving instead of clearing them.
- The process is small (no controller, FastAPI or pydantic; `MemoryMax=384M`) and always restarted.
- `tsm status` and the dashboard say when it does not answer.

`gateway_mode: in-process` runs the gateway inside the controller as before. An installation set up with 0.6
keeps that (its controller.yaml has no `gateway_mode`). To change it: add `gateway_mode: standalone` to
`/etc/twinspark/controller.yaml`, then `sudo tsm setup` (it writes and starts `twinspark-gateway.service`
for that mode, and restarts the controller).
