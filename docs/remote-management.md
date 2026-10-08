# Remote management for headless Sparks

Your two Sparks have no monitor and no keyboard. When something breaks you still want to *look*
(logs, a shell), *recover* (restart, reboot) and, as a last resort, *power-cycle* — from your laptop.
This page covers what TwinSpark gives you for that, how to switch it on, and what to try when a
node does not answer.

> **Honest status.** All of this was built and tested without touching the real Sparks: unit and
> process-level tests with fake `systemctl`/`efibootmgr`/`ethtool`, a loopback two-node demo, and a
> browser check of the GUI. The parts that depend on the machines' firmware and network cards —
> **Wake-on-LAN, network boot, and the BootNext choice** — are written from documentation and are
> marked *try it once while you can still reach the machine* below. Nothing here has been run on a
> DGX Spark.

## At a glance

| You want to… | GUI (**Remote** page) | CLI | Needs switching on? |
|---|---|---|---|
| See why a node is unreachable | *Check reachability* | `tsm remote reach B` | no |
| Read logs (agent, kernel, previous boot, docker…) | *Logs* | `tsm remote logs B --source kernel` | no |
| Download one redacted bundle for support | *Download support bundle* | `tsm remote bundle B` | no |
| Get a shell | *Terminal* | `tsm remote terminal B` | `terminal` |
| Reboot / power off cleanly | *Power & recovery* | `tsm remote reboot B` · `poweroff B` · `cancel B` | `reboot` / `poweroff` |
| Boot once from network or USB | *Boot once from…* | `tsm remote boot B network` | `boot-next` |
| Let a node be woken by a magic packet | *Wake-on-LAN on the node* | `tsm remote wol B enp1s0f1np1 on` | `wol` |
| Wake a powered-off node | *Wake-on-LAN* | `tsm remote wake B` | `nodes.B.wake` in controller.yaml |
| Cut/restore power with a smart plug | *Smart plug* | `tsm remote plug B cycle` | `nodes.B.plug` in controller.yaml |
| Do all of the above **on this node, without the controller** | — | `sudo tsm node doctor / logs / reboot / boot / bundle` | same switches |

Read-only diagnostics (reachability, logs, the bundle) are always available to someone holding the
management key. Everything that *does* something is **off until you switch it on, on that node**.

## Switching things on

Each node has a small root-owned file, `/etc/twinspark/remote-policy.json`. It is the only thing that
decides what the controller may ask that node to do. The setup wizard asks about it (default: nothing);
afterwards:

```bash
sudo tsm remote policy                      # what is on right now (also shows why a broken file is ignored)
sudo tsm remote enable terminal             # a recorded shell on this node
sudo tsm remote enable terminal reboot poweroff boot-next wol    # or: sudo tsm remote enable all
sudo tsm remote disable poweroff
sudo tsm remote enable all --dry            # show what would change, change nothing
```

Run it on **each** node. `enable terminal` also installs and starts `twinspark-terminal.service`;
`disable terminal` stops and removes it (open shells end).

Why a file on the node and not a button in the GUI? Because the GUI talks to the controller, and the
whole point is that a stolen or misused management key cannot talk a node into powering itself off or
giving out a shell unless *you* already allowed that on the node. The file must be owned by root and
not writable by anyone else (so must the directories above it); if it is missing, unreadable, wrongly
owned or not valid JSON, **everything is off** and `tsm remote policy` / the Remote page tell you why.
Only the JSON value `true` counts — `"yes"` or `1` do not.

`sudo tsm setup --remote terminal,reboot` (or `--remote all`) does the same during installation.

## Before you close the desktop: a way back in

Going headless (`tsm headless headless-safe` for the next boot, `headless-max` to stop the desktop now)
is only safe once you can reach both nodes without their screens. `tsm headless status` and the
**Headless** tab of the Diagnostics page show, per node, the facts that decide that, each on its own:

- the desktop: running or not, and the default boot target (`graphical` or `multi-user`);
- SSH: `ssh.service` / `ssh.socket` running, and whether it starts at boot;
- Tailscale: installed, `tailscaled` running and starting at boot, the node's tailnet address, and on
  node A whether the manager port is forwarded (`unknown` when `tailscale serve status` needs root);
- the remote paths that result (`ssh`, `tailscale`, or none).

`headless-max` stops the display manager **even without `--now`**, so the CLI and the GUI ask before it
(the CLI lists each node's remote paths first and warns about a node without one; `-y` skips the
question), and the controller refuses it — like `--now` — while a model is being activated.

### Reaching the GUI over Tailscale

The controller listens on `127.0.0.1:8443`. To open it from your laptop without an SSH tunnel, forward
that port on the tailnet only, on node A:

```bash
tsm remote tailscale-serve            # state + the exact command; changes nothing
sudo tsm remote tailscale-serve --apply    # = sudo tailscale serve --bg --tcp=8443 tcp://127.0.0.1:8443
sudo tsm remote tailscale-serve --remove   # undo
```

`--bg` stores the forward in `tailscaled`'s state, so it survives reboots. Plain TCP forwarding needs no
HTTPS certificates on the tailnet. `--apply` then fetches `/api/v1/health` through the node's tailnet
address. That proves the forward works, but only from the node itself: open
`http://<node A's tailnet IP>:8443/` (`https://` with a TLS listener) from another tailnet device before
you close the desktop. The management key is still required for everything. `--apply` refuses a
controller set to `management_auth: none`, because through the forward any tailnet device could send
`Host: localhost`, and `tsm doctor` fails if that setting appears later while the forward exists.
Tailscale SSH may ask for an extra identity check depending on your tailnet policy, so do not count on
it for unattended access.

## The terminal

Open **Remote → Terminal → Open terminal** (phones get an extra key row for Esc, Tab, Ctrl-C/D/L and
the arrows), or from your laptop:

```bash
tsm remote terminal B       # raw TTY: vim, htop, sudo and Ctrl-C behave as over SSH
```

Disconnect with **Ctrl-]** then **.** (like telnet). Nothing else is intercepted.

How it works and what to know:

- The shell runs as the **TwinSpark service user** (the user you ran setup for, not root). `sudo`
  works if that user may use it, exactly as over SSH. Docker works if that user is in the `docker` group.
- It runs in its **own service** (`twinspark-terminal`), not inside the sandboxed agent, so a normal
  shell environment is available and a bug in the agent still cannot hand out a shell.
- The browser never talks to the node directly. It trades the management key for a **one-time ticket**
  (valid 30 seconds, bound to one node) and the controller relays the connection. With CSRF protection
  on, the WebSocket also checks the page's `Origin`.
- **Every session is recorded** on the node (`/var/lib/twinspark/terminal/`, mode 0600) in asciicast
  format — keystrokes and output, which means **a password typed at a prompt that echoes is recorded**.
  List them with `tsm remote recordings B`, print one with `tsm remote recordings B <name>`, replay it with
  `asciinema play`. Opening, closing (with byte counts and reason) and recording are in the audit log.
- Sessions end after 15 minutes idle or 4 hours total; at most 2 per node (limits are adjustable in the
  policy file, e.g. `"terminal_idle_s": 1800`).
- **Leaving the page ends the session.** For work that must survive a dropped connection, run `tmux`
  first and re-attach later.
- Do not paste secrets into the terminal unless you are comfortable with them being in a recording.

## Logs and the support bundle

```bash
tsm remote logs B                          # TwinSpark agent
tsm remote logs B --source kernel -n 400   # also: previous-boot, docker, privd, controller, ssh, network, nvidia…
tsm remote logs B --source previous-boot --grep Xid       # plain-text match, ignores case
tsm remote bundle B -o b.tar.gz            # one file for a bug report
```

**previous-boot** and **kernel** are the first places to look after an unexplained reboot or freeze
(out-of-memory kills, GPU `Xid` errors, link flaps). The bundle contains: versions, uptime and recent
boots, disk and memory, network and RDMA state, `nvidia-smi`, Docker state, failed units, recent logs of
all TwinSpark services, and the config files. Tokens, passwords, API keys, `Authorization` headers and
every value in the secret vault are masked, and the vault itself is listed by slot name only — but
**read it before you share it**; redaction by pattern is best effort.

## Rebooting and powering off

```bash
tsm remote reboot B            # asks you to type  REBOOT B
tsm remote poweroff B          # asks you to type  POWEROFF B
tsm remote cancel B            # changed your mind within the delay
```

- The action is **scheduled a few seconds ahead** (`--delay 2..600`, default 5) so the reply can reach
  you; `cancel` stops it. It is done by the root helper `twinspark-privd`, which must be running
  (`sudo systemctl status twinspark-privd`).
- While a model is activating or a maintenance run is active the controller refuses (HTTP 409) unless
  you add `--force` ("do it anyway" in the GUI). The node additionally refuses during `apt`/`dpkg` work
  and that is **not** overridable remotely — only `sudo tsm node reboot --force` on the node skips it. Rebooting the node that runs the **controller** takes
  the GUI, the CLI and the inference gateway down until it is back — the GUI says so before you confirm.
- A powered-off Spark stays off. You need Wake-on-LAN, the smart plug or the power button to start it.
- `-y/--yes` supplies the typed phrase for scripts; use it deliberately.

## When the controller itself is the problem: `tsm node`

If you can reach a node over SSH (or the Remote terminal, or Tailscale SSH) but the controller or agent
is down, everything above works **locally**, without the controller:

```bash
sudo tsm node doctor           # services, API, policy, disk, clock, tools — exit 1 on any FAIL
sudo tsm node status
sudo tsm node logs kernel -n 300
sudo tsm node bundle -o /tmp/b.tar.gz
sudo tsm node reboot [--force] # also: poweroff, cancel, boot, wol
```

Run with `sudo`, these act on the machine directly: root could reboot it anyway, so the switch file does
not apply and `twinspark-privd` does not need to be running. Without `sudo` they go through
`twinspark-privd` and the switches apply as usual. On the node itself, `reboot`/`poweroff` refuse while a
coordinated maintenance run or `apt`/`dpkg` is working (a reboot then could leave the OS half-upgraded);
`--force` overrides that when you know better. `sudo tsm node boot` lists the UEFI boot entries,
`sudo tsm node boot network` sets the one-time choice, `sudo tsm node wol on enp1s0f1np1` arms
Wake-on-LAN.

## Waking and power-cycling a node that does not answer

These two work from the controller (node A) and need a few lines in `/etc/twinspark/controller.yaml`.

### Wake-on-LAN

```yaml
nodes:
  B:
    agent_url: http://192.168.100.2:9443
    # …existing keys…
    wake:
      mac: "aa:bb:cc:dd:ee:ff"          # the MAC of the port that stays powered while off
      iface: enp1s0f1np1                # send from this port
      broadcast: 192.168.100.255        # the QSFP subnet's broadcast address
```

Then `tsm remote wake B` (or *Send packet* on the Remote page). The packet is sent from node A, never to
itself.

Be realistic about this:

- It works only if the node's network adapter and firmware keep the port powered while the machine is
  off and have Wake-on-LAN armed. **On a Spark that is not guaranteed** — whether the QSFP port, the
  RJ-45 port or both can wake it depends on the firmware. Find out **now, while you can still reach it**:
  1. On node B: `sudo tsm remote enable wol`, then `tsm remote wol B <iface> on`
     (equivalent to `ethtool -s <iface> wol g`; install `ethtool` if missing).
  2. `tsm remote poweroff B`, wait until it is off, `tsm remote wake B`.
  3. If it does not come back, try the other port's MAC (the RJ-45 port on the LAN usually has the best
     chance), then fall back to the smart plug.
- The `ethtool` setting **does not survive a reboot** by itself. To make it permanent put
  `wakeonlan: true` under the interface in `/etc/netplan/*.yaml` and run `sudo netplan apply`.
- Wake-on-LAN packets do not cross routers. Send them from the same segment as the node.

### Smart plug / PDU

When the OS is hung and nothing answers, cutting power is the last resort. Any plug with an HTTP
interface works (Tasmota, Shelly, Home Assistant webhooks, many PDUs). TwinSpark makes one HTTP call per
action and does not care what is behind it:

```yaml
nodes:
  B:
    # …
    plug:
      on:  {method: GET, url: "http://192.168.1.50/cm?cmnd=Power%20On&user=admin&password=${secret:plug_token}"}
      off: {method: GET, url: "http://192.168.1.50/cm?cmnd=Power%20Off&user=admin&password=${secret:plug_token}"}
      # cycle: optional single call that cuts and restores power (otherwise: off, wait settle_s, on)
      settle_s: 10
```

Shelly (Gen2) looks like `url: "http://192.168.1.51/rpc/Switch.Set?id=0&on=false"`; Home Assistant:
`method: POST`, `url: "http://ha.local:8123/api/webhook/<id>"`. Headers (for example
`Authorization: Bearer ${secret:plug_token}`) and a `body` are supported too.

Keep the credential out of the YAML: write it once into the vault on node A with

```bash
sudo tsm remote plug-token            # prompts; or:  --from-file /path
```

and reference it as `${secret:plug_token}` in the URL, body or a header. These calls come from the config
file only — an API caller cannot choose the URL — so they may point at a private address. Then:

```bash
tsm remote plug B cycle        # asks you to type  CUT POWER B
tsm remote plug B on
```

Cutting power is **not a shutdown**: files being written can be lost. Use `reboot`/`poweroff` first
whenever the node still answers. A busy cluster needs `--force`.

## Network boot as a rescue path (experimental)

For when a node's disk or OS is broken: the other Spark (or any Linux box on the same segment) answers
the target's PXE request for a short time and hands it a bootloader over TFTP. TwinSpark only prints a
plan and runs a short-lived `dnsmasq` for you; it ships no boot images.

**Not verified on DGX Spark firmware.** Whether the UEFI offers a network boot entry, and whether it can
use the QSFP port, depends on the firmware version. Check what yours offers *now*:

```bash
sudo tsm node boot             # lists the UEFI boot entries (needs efibootmgr)
```

If there is no network entry, this path is not available on your machine; use a USB stick plugged in
beforehand, or the smart plug plus the normal disk.

The walkthrough (on the node that will *serve* the boot, here node A, target node B):

```bash
sudo apt install dnsmasq-base
# 1. put an ARM64 UEFI bootloader (for example GRUB or iPXE's snp.arm64.efi) into /srv/tftp
tsm netboot plan  --mac aa:bb:cc:dd:ee:ff --iface enp1s0f1np1         # prints the exact steps and config
sudo tsm netboot serve --mac aa:bb:cc:dd:ee:ff --iface enp1s0f1np1     # runs for 30 minutes at most
# 2. in a second terminal: boot ONCE from network, then reboot the target
tsm remote boot B network
tsm remote reboot B            # or: tsm remote plug B cycle
```

Safety properties: it is **proxyDHCP** (adds boot information to your normal DHCP answer, never hands
out addresses), it answers **one MAC address only**, it stops by itself (`--minutes`, max 240), the
config is a 0600 temp file removed afterwards, and `tsm remote boot B clear` withdraws the next-boot
choice. The normal boot order is never changed, so if the network boot fails the node boots from its disk
the next time.

## When everything is down: a checklist

Start with `tsm remote reach B` (or *Check reachability*). It probes the agent, SSH and terminal ports
separately and tells you which case you are in:

| Verdict | Meaning | Do this |
|---|---|---|
| `ok` | the agent answers | the problem is in the model, not the node — `tsm logs`, `tsm doctor` |
| `agent_error` | agent port open, but it returns an error | `401` = token mismatch (`sudo tsm join-code` on A, `sudo tsm setup --join …` on B); otherwise `journalctl -u twinspark-agent` there |
| `agent_down` | the node is up (SSH answers) but the agent is not | `ssh` in, `sudo tsm node doctor`, `sudo systemctl restart twinspark-agent twinspark-privd` |
| `link_down` | nothing answers and node A's QSFP port is down | cable at both ends; is the other Spark powered? |
| `host_down` | nothing answers anywhere | LED/power; `tsm remote wake B`; then `tsm remote plug B cycle`; then the power button |

After it is back: `tsm remote logs B --source previous-boot` and `--source kernel` to see what happened,
`tsm remote bundle B` to keep the evidence, `tsm doctor` to confirm the cluster is healthy.

If the **controller** node (A) is the one that is down, none of the controller-driven features exist —
SSH or Tailscale to A and use `sudo tsm node doctor`. This is the reason `tsm node …` is separate: keep
at least one way in that does not depend on TwinSpark running (SSH with a key, or Tailscale SSH).

## Trying it without risk

```bash
tsm demo --with-terminal       # two simulated Sparks on this machine, terminal switched on
```

Open the **Remote** page: the logs, reachability and the shell work against the simulated nodes. The
demo's terminal is a real shell as *your* user on *your* machine; power, boot and Wake-on-LAN are not
available there (there is no privileged helper in the demo). Stop it with Ctrl-C.

## What this does and does not protect against

See [security.md](security.md). In short: the feature switches are fail-closed and root-owned on each
node; there is no generic "run a command" API (the terminal is a separate, opt-in, recorded service);
typed confirmations guard power actions; everything is audited (`remote.terminal_open`,
`remote.reboot`, `remote.plug_cycle`, …). It does **not** make the management key harmless — whoever
holds it can already install mods and start containers, which is close to code execution — so keep the
GUI on loopback / Tailscale / an SSH tunnel as the README says.
