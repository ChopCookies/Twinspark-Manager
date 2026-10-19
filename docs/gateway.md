# The gateway: routing, fallbacks

Apps talk to one OpenAI-compatible endpoint on node A (`http://<node-a>:8000/v1`) and use an *alias* as the
model name. This page is about how the gateway picks a backend for an alias and what answers while it is
switching.

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
