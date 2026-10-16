# The workstation client

`tsm client` runs on your own computer (macOS, Windows or Linux). You give it node A's address and SSH
user once; it logs in, keeps the forwards up and serves the whole GUI on `http://127.0.0.1`. The work
that does not need the Sparks — recipe browsing and conversion, configuring and validating agent apps
such as Hermes — runs here. The Sparks keep only the lean services that run the model.

```
 your computer                                    node A                         node B
 ┌──────────────────────────────┐   SSH -L   ┌──────────────────────┐  QSFP  ┌──────────────┐
 │ browser ─► tsm client :18765 │═══════════►│ controller :8443     │◄══════►│ agent        │
 │   GUI, recipes, Hermes setup │            │ gateway    :8000     │        │ docker/vLLM  │
 │ apps ─► 127.0.0.1:<port>/v1 ─┼───────────►│ agent, docker/vLLM   │        └──────────────┘
 └──────────────────────────────┘            └──────────────────────┘
```

Nothing on the Sparks depends on the client. Closing the laptop ends the forwards; the model keeps
serving, jobs keep running, and the next connect picks up where node A is.

## Install

Python 3.12 or newer and an OpenSSH client (built into macOS, Linux and Windows 10/11). With
[pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/ChopCookies/Twinspark-Manager
pipx inject twinspark-manager keyring        # optional: keys in the system keychain
# or, from a checkout:  pipx install ".[client]"
```

`keyring` (also the `[client]` extra) puts the API keys go into the macOS Keychain, the Windows
Credential Manager or the Secret Service. Without it (or without a usable keyring) they go into a
private file (mode 0600) in the client's folder:

| OS | Client folder |
|---|---|
| Linux | `~/.config/twinspark` (or `$XDG_CONFIG_HOME/twinspark`) |
| macOS | `~/Library/Application Support/TwinSpark` |
| Windows | `%APPDATA%\TwinSpark` |

`TSM_CLIENT_HOME` overrides it. The folder holds the saved connections, the client's own `known_hosts`
and its own SSH key (`ssh/twinspark_ed25519`); nothing outside it is written. Your own
`~/.ssh/known_hosts` and keys are never changed.

## Connect

```bash
tsm client          # opens http://127.0.0.1:18765/#k=… in your browser
```

The part after `#k=` is a key for this launch only. It stays in that browser tab and leaves the address
bar at once; another tab or program on this computer cannot use the client without it. Keep
`tsm client` running while you use the GUI or apps connected through it (Ctrl-C ends it).

On **Connection → Add a Spark** enter node A's address (LAN IP, hostname or Tailscale address) and the
SSH user you log in with. The first connect walks you through what node A needs:

1. **Host key.** The client shows the SSH host key fingerprints node A presents. Compare them with the
   Spark itself (`for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done` on its console or
   in an SSH session you already trust). Only what you confirm is stored, and only if node A still
   presents it at that moment. A key that later changes stops the connection until you look at it again.
2. **SSH login.** Your SSH agent and keys are tried first. If node A refuses them, you can enter the
   password once: the client logs in with it, adds its own public key to `~/.ssh/authorized_keys` of
   that user and forgets the password. It is never stored, never on a command line and only ever sent
   to the host key you confirmed. (Or add the key yourself with `ssh-copy-id` and connect again.)
3. **API keys.** The client runs `tsm init --show` on node A as that user. If the user cannot read the
   vault, paste the two keys (`sudo tsm init --show` on node A prints them) or, with passwordless sudo,
   let the client read them with `sudo -n`.

Then the page shows three separate facts, each with its own fix:

- **Client connected** — the SSH forwards (or the direct connection) are up.
- **Agents compatible** — node A and both agents run a TwinSpark version this client works with.
- **Model ready** — a model is active and the gateway routes to it.

A client and Sparks whose minor versions differ still connect; the page says which side to update.

The same works in a terminal:

```bash
tsm client add home 192.168.1.20 --user spark
tsm client connect home     # host key, key install and keys interactively; keeps the tunnel up until Ctrl-C
tsm client list | remove home
eval "$(tsm client env home)" && tsm status     # the tsm CLI through the client's forward
```

## Connection types

| Type | When | What it needs on the Sparks |
|---|---|---|
| `ssh` (default) | node A on your LAN | sshd — nothing else; the management API stays on `127.0.0.1` |
| `tailscale` | node A on your tailnet | sshd reachable over Tailscale; chosen automatically for Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`, `*.ts.net`). **Find on my tailnet** lists your peers |
| `direct` | a management listener you exposed on purpose | `listener.bind` on a LAN address **with TLS** (`tls_cert`/`tls_key`); its certificate must verify on this computer |

SSH is the recommended path: it needs no listener changes and authenticates the host. The forwards
use node A's port numbers locally when they are free (`8443`, `8000`), otherwise free ports; the
chosen ports are kept, so apps configured once keep working after a restart. The SSH session sends
keep-alives (a dead link is noticed within about 45 seconds) and reconnects with a back-off of 1 to 30
seconds when the network drops.

## The endpoint for apps on this computer

**Connection** shows the OpenAI-compatible base URL for apps on this computer, for example
`http://127.0.0.1:8000/v1`, and copies the inference key. That address is the client's forward, so it
works while `tsm client` runs. Apps on other machines keep using node A's gateway directly
(`http://node-a:8000/v1`), as before. The management key never leaves the client: apps only ever get
the inference key.

**Integrations → Test from this computer** sends a chat, a streamed reply and a tool call through
exactly that address, so a failure says whether the forward, the key, the model or the tool parser is
at fault.

## Hermes and other agent apps

The **Hermes Agent** card on Integrations finds Hermes on this computer (the default) or on node A,
shows its state (installed, configured, reachable, compatible, running), previews the exact change to
its `config.yaml` and `.env`, applies it with a backup, validates it, starts or stops its gateway and
restores any backup. Running Hermes here keeps its memory and tools off the Sparks. Details:
[agent-integrations.md](agent-integrations.md#hermes-agent-lifecycle).

## What runs where

| On the workstation client | On node A |
|---|---|
| the GUI, connections, host keys, the key install | profiles, revisions, pins, jobs, the deployment |
| recipe index, community recipe fetching, recipe → profile conversion | storing the converted profile (`POST /api/v1/profiles`) |
| Hermes detect / preview / apply / validate / launch / restore | the gateway the apps talk to |
| "test from this computer" | compatibility jobs, evaluation records |
| the browser terminal's WebSocket relay | the terminal itself (socket-activated, idle when unused) |

Everything else in the GUI is node A's API, passed through with the stored management key.

## Keeping the Sparks up to date

The client and the Sparks are separate installs. Update the Sparks as before (`git pull && sudo
./install.sh --no-setup` on each node, then restart the services) and the client with
`pipx upgrade twinspark-manager` (or `pipx install --force …` for a git install). **Agents
compatible** says when they drifted apart.

## Existing setups

Nothing changes for a deployment that is reached by an SSH tunnel or Tailscale and the GUI on
`:8443`: node A still serves the GUI, the API and the CLI. The client is an additional way in.

## Windows notes

- Use the OpenSSH client that ships with Windows (`ssh.exe` in `C:\Windows\System32\OpenSSH`), or set
  `TSM_SSH` / `TSM_SSH_KEYSCAN` to another `ssh.exe` / `ssh-keyscan.exe`.
- The one-time password key install uses `SSH_ASKPASS_REQUIRE`, which needs OpenSSH 8.4 or newer
  (`ssh -V`; Windows 10 ships 8.1) and has not been tried on Windows yet. If it fails, add the client's
  key yourself:
  `type %APPDATA%\TwinSpark\ssh\twinspark_ed25519.pub | ssh user@node-a "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"`.
- Hermes on Windows: the card looks in `%HERMES_HOME%`, `%LOCALAPPDATA%\hermes` and `%USERPROFILE%\.hermes`.
  A Hermes inside WSL is not managed from the Windows side.

## Troubleshooting

| You see | Meaning | Fix |
|---|---|---|
| "This tab's client session ended" | the client was restarted; the tab has the old key | open the new address `tsm client` printed |
| "Not connected to the Sparks" on a node page | the forwards are down or not made yet | **Connection**: connect, or read the message there |
| "Node A refused the stored key" | the management key was rotated on node A | **Connection → Keys**: paste the current one |
| Host key changed | node A was reinstalled — or something sits in between | compare the fingerprints on the Spark before replacing |
| "No SSH client found" | `ssh` is not on `PATH` | install OpenSSH, or set `TSM_SSH` |
