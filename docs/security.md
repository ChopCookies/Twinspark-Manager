# Security model and known limits

TwinSpark Manager is meant for two machines on a private QSFP link plus a laptop that reaches the GUI
over an SSH tunnel or Tailscale. It is not designed to face the internet.

## Who can do what

| Component | Listens on | Authenticated by | Can do |
|---|---|---|---|
| Web GUI / management API | node A `127.0.0.1:8443` | management key (`tsm init --show`) | everything below |
| Gateway (OpenAI API) | node A `:8000` | inference key | chat/completions only |
| Agent | node A `127.0.0.1:9443`, node B QSFP address | per-cluster bearer token | a fixed list of typed actions |
| `tsm-privd` | Unix socket, root | socket permissions | allowlisted root operations (drop page cache, headless switch, maintenance reboot) and — only when `remote-policy.json` allows — reboot, power-off, boot-next, Wake-on-LAN |
| `tsm-termd` (opt-in) | same address as the agent, node port 9444 | agent bearer token, plus a one-time ticket from the controller for browsers | a recorded shell as the service user |

There is no generic "run this command" action anywhere. The controller refuses to send an action that
is not in `ALLOWED_AGENT_ACTIONS`, and the agent refuses anything it does not implement.

## What is enforced

- Constant-time token checks; authentication happens before request bodies are read; failed attempts
  are rate-limited separately from normal operator traffic.
- The API serves no interactive docs or OpenAPI schema and returns sanitised validation errors.
- Secrets are injected into a container only for the documented variable names; they travel through a
  0600 env file, never argv, and profiles/recipes only ever hold the `${secret:slot}` reference, not the value.
- Containers never pull implicitly (`--pull never`); image identity is the digest recorded when the
  profile was pinned. Mount paths are validated and resolved on the agent.
- Recipe/cookbook downloads accept only public `https://` URLs (private, loopback, link-local and
  metadata addresses are refused, including after redirects).
- Mods are untrusted archives: bounded, link-free, installed atomically. A mod's `run.sh` does run
  inside the container, so install mods you have read.
- Every state-changing call is written to the audit log with the actor.
- Secrets are encrypted at rest (Fernet), but the key (`.master.key`) lives in the same directory: what
  actually protects them is that the directory is 0700 and its files 0600, owned by the service user.
  Commands run with `sudo` write vault files owned by that user, so the services can still read them.
- With `management_auth: none` (allowed only on a loopback listener) the API answers only requests for
  `localhost` / `127.x` / `[::1]`, so a web page cannot reach it through DNS rebinding.
- Gateway request bodies are capped at 32 MiB; management API bodies at 4 MiB (mod uploads 100 MiB).
- Remote management is **fail-closed and decided on each node**: `/etc/twinspark/remote-policy.json`
  (root-owned, in a root-owned directory chain) switches terminal, reboot, power-off, boot-next and
  Wake-on-LAN individually; a missing or malformed file, wrong owner, or any value but JSON `true` means
  off. The root helper re-reads it on every call. Terminal sessions are recorded, time-limited and capped;
  the browser reaches them through a one-time 30 s ticket bound to a node, an `Origin` check and a bridge
  limit; power actions need a typed phrase and are refused while the cluster is busy. Wake-on-LAN and
  smart-plug calls come from the config file only (an API caller cannot choose a URL or MAC), and the plug
  token lives in the vault. Details: [remote-management.md](remote-management.md).
- The QSFP link is changed **only by a person running `sudo tsm qsfp apply` (or `revert`) on that machine**.
  No agent action, API route or GUI button can do it, and nothing runs as part of a service. It writes one
  root-owned netplan file (0600) that it recognises by a marker line, refuses to edit anyone else's, refuses
  a port that carries the default route or your remote session (SSH or the Remote terminal), validates with
  `netplan generate` and puts the previous file back if the addresses do not come up. `tsm qsfp scan --identify` uses key-only SSH
  (`BatchMode`), a validated user name and no password prompt. Details: [qsfp-link.md](qsfp-link.md).

## Known limits (deliberately not fixed yet)

- **The service user is effectively root.** It is in the `docker` group (the agent starts containers),
  and anyone who can start a container with a host mount is root on that machine. By default the
  service user is the account that ran `sudo ./install.sh`. So a compromised agent or controller, or a
  management-key holder on a node with the terminal switched on, can get root there. That includes
  rewriting `remote-policy.json`: the root-owned policy file keeps a *running service* from switching
  features on through TwinSpark's own API, not someone who is already root through Docker. Rootless
  Docker or a Docker socket proxy would close this; neither is supported yet.
- The terminal is a shell as the service user (see above: effectively root), and the recording of a
  session contains whatever was typed or shown — including a password typed where the terminal echoes
  it. Whoever holds the management key and has the terminal enabled on a node can act as that user there.
  (A management key could already start containers and install mods, which is close to the same power;
  the switch exists so that a node you have not opted in cannot be reached this way.)
- The join code carries the shared agent token and backend key. On the command line
  (`tsm setup --join tsm1.…`) it is visible to other local users while setup runs and stays in the shell
  history; `tsm setup --join -` asks for it instead.
- Redaction in support bundles is pattern based; read a bundle before sharing it.
- The smart-plug URL is trusted configuration and may point at a private address; keep
  `controller.yaml` root-owned (it is by default).
- Wake-on-LAN packets and plug calls are unauthenticated by nature of the protocols.

- One bearer token for both agents; a compromised node could talk to the other node's agent. Per-node
  tokens and mTLS are planned.
- The weight-sync SSH key is limited to node A's QSFP address but is not yet restricted to `rrsync`.
- Container images may come from any registry the profile names; there is no registry allow-list yet.
- Public-URL checks resolve the host once before connecting, so a hostile DNS server could still
  rebind between check and use. The risk needs control of a recipe URL *and* its DNS.
- With a management key (the default) there is no `Host` header allow-list: the GUI relies on binding to
  loopback and on the key.
- A long `prepare` job can delay the watchdog's view of the active model.

Report security problems privately, as described in [SECURITY.md](../SECURITY.md), not in a public issue.
