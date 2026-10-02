# The QSFP link between the two Sparks

`tsm qsfp` sets up, checks and repairs the direct cable between the Sparks. It works on the machine
you run it on and does not need the controller, so you can use it from an SSH session on the
management network while the rest of TwinSpark is still being installed — or broken.

> **Status.** Written against a fake Spark (fake `/sys`, `ip`, `ss`, `ping` and a `netplan` that really
> turns YAML into addresses), including the failure and rollback paths. It has **not** been run on real
> hardware yet. The first time, use `tsm qsfp plan` and `sudo tsm qsfp apply --temporary` (below): they
> are the cautious route.

## Why two addresses

Each QSFP port of a Spark is fed by two PCIe x4 halves, so one cable shows up as **two** Ethernet
interfaces and **two** RoCE devices — the *twins*:

| | first half (primary) | second half (secondary) |
|---|---|---|
| Ethernet | `enp1s0f1np1` | `enP2p1s0f1np1` |
| RoCE | `rocep1s0f1` | `roceP2p1s0f1` |

Each twin carries up to about 100 Gb/s. NCCL only reaches roughly 200 Gb/s when **both** have an
address and both RoCE devices are listed in `rdma_hcas`. With one address you get about half.

The layout used here is the one the Spark community settled on — NVIDIA's *Connect two Sparks*
playbook and, in more detail, [eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker)
(`docs/NETWORKING.md`, `autodiscover.sh`; MIT — ideas and layout only, no code copied):

- a **different /24 for each twin** (`192.168.100.x` and `192.168.101.x`). Putting both twins of one
  port on one subnet confuses routing and ARP, which is why eugr's guide says not to;
- MTU 9000, static addresses, `dhcp4: false`, `dhcp6: false`, `link-local: []`;
- the **same physical port** on both Sparks (the right-hand one, `f1np1`);
- node A is host `.1`, node B host `.2` on both subnets.

| | node A | node B |
|---|---|---|
| `enp1s0f1np1` | `192.168.100.1/24` | `192.168.100.2/24` |
| `enP2p1s0f1np1` | `192.168.101.1/24` | `192.168.101.2/24` |

## The safe sequence

On **each** Spark (node A first — then repeat with `--node B` on node B):

```bash
tsm qsfp status                       # what the machine has: ports, link, addresses, MTU, RoCE
tsm qsfp plan --node A                # what would change; nothing is written
sudo tsm qsfp apply --node A --temporary   # addresses until the next reboot; no netplan file is written
tsm qsfp verify                       # ping the other Spark through each twin with jumbo frames
sudo tsm qsfp apply --node A          # make it permanent (writes /etc/netplan/60-twinspark-qsfp.yaml)
```

When both Sparks are done, on node A:

```bash
tsm rdma --apply                      # lists both RoCE devices in controller.yaml
sudo systemctl restart twinspark-controller
```

If node B is not set up yet, `tsm qsfp scan` on node A finds it (hosts on the link answering on SSH;
`--identify` asks for the GPU name over key-based SSH, like eugr's `autodiscover.sh`).

`--node` can be left out on a machine where `tsm setup` ran (it is in `agent.yaml`) or where one twin
already has an address.

## What apply writes

`/etc/netplan/60-twinspark-qsfp.yaml` (mode 0600), for node A:

```yaml
# Managed by TwinSpark (tsm qsfp) — change it with `tsm qsfp`, undo it with `sudo tsm qsfp revert`.
# Two subnets on purpose: both twins of one port on one subnet confuses routing
# (eugr/spark-vllm-docker docs/NETWORKING.md; NVIDIA's two-Spark playbook does the same).
# optional: boot does not wait for these links (the other Spark may be off or unplugged).
network:
  version: 2
  ethernets:
    enp1s0f1np1:
      dhcp4: false
      dhcp6: false
      link-local: []
      optional: true
      mtu: 9000
      addresses: [192.168.100.1/24]
    enP2p1s0f1np1:
      dhcp4: false
      dhcp6: false
      link-local: []
      optional: true
      mtu: 9000
      addresses: [192.168.101.1/24]
```

`optional: true` keeps boot from waiting for these links when the other Spark is off or unplugged. The
renderer is whatever the machine already uses; the file does not set one.

If a file called `60-twinspark-qsfp.yaml` already exists and was **not** written by TwinSpark — the older
setup snippet told people to create exactly that name — it is left alone and TwinSpark uses
`61-twinspark-qsfp.yaml` for its part.

## What it will not do

The machines are headless, so a wrong network change cannot be fixed with a keyboard. `apply`:

1. **refuses an interface that carries the default route**, or one that an SSH session is coming in
   through (it reads `SSH_CONNECTION` and the live sshd connection table, because `sudo` usually drops
   the former; IPv6 link-local sessions are matched by their interface). If the routing table cannot be
   read it stops rather than guess;
2. **never edits a netplan file it did not write.** Its own file starts with the marker line
   `# Managed by TwinSpark (tsm qsfp)`; anything else is foreign. If a foreign file already configures
   one of the interfaces, the plan says so and stops — nothing is touched;
3. runs **`netplan generate`** first, so a file netplan rejects is never applied;
4. **verifies the result** — addresses and MTU live on every planned interface, and the management
   interface still has its default route and addresses. If not, it **puts the previous file back,
   re-applies it, and removes the addresses and MTU the failed attempt left behind** (networkd keeps
   them when a file disappears). Ctrl-C and hang-up are ignored during those few seconds so the rollback
   always runs;
5. keeps the previous file as a backup in `/var/backups/twinspark-qsfp` — root-owned, mode 0700, and
   `revert` only copies a backup back if it carries the marker and neither it nor the directory is
   writable by anyone else;
6. refuses a range that overlaps another interface (use `--subnet 10.77.0.0/24`), a public range, or
   an MTU outside 576–9216;
7. asks before it does anything (`--yes` skips the question), and needs `sudo` for the real thing.
   Applying a layout that is already in place changes nothing, so it is safe to repeat.

`sudo tsm qsfp revert` puts the previous file back (or removes ours), runs `netplan apply` and removes
the addresses that file had set; it has the same default-route and SSH guards as `apply`, and refuses a
file without the marker. `sudo tsm qsfp revert --temporary` removes the addresses set with
`--temporary` (they are recorded in `/run`, which a reboot clears together with the addresses) and puts
the MTU back.

## You already have one twin set up

The usual situation after following an older guide: `enp1s0f1np1` has `192.168.100.1/24`, set in some
other netplan file, and the second twin has nothing. `tsm qsfp plan` then **keeps the existing twin
as it is** and adds only the missing one on the next subnet with the same host number
(`192.168.101.1/24`). It tells you if the kept twin still has MTU 1500 — jumbo frames (and full RoCE
speed) need 9000 on both, which you change where that twin is configured.

If both twins already share one subnet from a foreign file, the plan warns and leaves it to you:
`tsm qsfp status` marks it as a failure and says what to change.

## `tsm setup`

The wizard shows the same plan when the cabled port is missing addresses and **asks** whether to apply it
(default: no). `--yes` alone never changes the network; add `--configure-qsfp` to do it unattended. On
node B the host number and subnet come from the join code. Anything unsafe is reported and skipped —
setup carries on without it.

`tsm node doctor` lists the same findings as warnings, with the command that fixes each. It stays
quiet on a Spark that has no second node configured.

## Commands

| | |
|---|---|
| `tsm qsfp status` (or just `tsm qsfp`) | ports, twins, link, addresses, MTU, RoCE devices and GIDs, with a fix for each finding |
| `tsm qsfp plan` | the file that `apply` would write and every blocker; changes nothing |
| `sudo tsm qsfp apply` | write and apply; `--temporary` for addresses only until reboot; `--yes` |
| `sudo tsm qsfp revert` | undo the last apply |
| `tsm qsfp verify` | local checks, then ping the other Spark through each twin (jumbo first) and check SSH |
| `tsm qsfp scan` | hosts on the link subnets answering on SSH; `--identify --user NAME` names the GPU |

Options for `plan` / `apply`: `--node A|B`, `--host-number N`, `--subnet 192.168.100.0/24` (the first
twin's /24; the second twin gets the next one), `--mtu 9000`, `--iface NAME` (pick a port when more than
one has a cable). All commands accept `--json` before the sub-command (`tsm --json qsfp status`).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `no QSFP link is up` | the cable must be in the **same** port on both Sparks and the other Spark must be on. `ibdev2netdev` shows `(Up)` for a cabled port. |
| Peer answers small pings but not jumbo | the other Spark still has MTU 1500. Run `sudo tsm qsfp apply --node B` there. |
| No answer at all | the other Spark has no addresses yet, or is cabled to the other port. `tsm qsfp scan` shows what answers. |
| `already configures …` | a foreign netplan file mentions the interface. Move it away or add the second twin there by hand; TwinSpark will not edit it. |
| `carries this machine's default route` | you picked the management NIC. The QSFP twins are `enp1s0f1np1` / `enP2p1s0f1np1` (or the `f0np0` pair). |
| Two cables | one cable gives the full bandwidth; `status` warns and uses `f1np1` unless `--iface` says otherwise. |

## RoCE GID index

`link-local: []` (as in both guides) removes the IPv6 link-local addresses, and with them the GIDs at
the start of each RoCE device's table, so the index of the IPv4 RoCE v2 GID can change (for example
from 3 to 1). `tsm qsfp status` warns (`qsfp gid`) when `ib_gid_index` in `controller.yaml` no longer
matches; `tsm rdma --apply` rewrites it.

## Limits and first-run risks

- One cabled port is managed, in one file. A second port needs its own cable and `--iface`, and
  applying it replaces the first layout; use one cable.
- Addresses from DHCP or another tool count as "already configured" and are kept; `169.254.x.x`
  addresses do not.
- A `match:` stanza in another netplan file is compared by literal interface name only. If it matches
  by pattern, driver or MAC the pre-flight cannot see it; the post-apply check still notices that the
  addresses are not there and rolls back.
- `netplan apply` reconfigures the whole machine's network, not only these two interfaces. The checks
  above are meant to catch a bad result, but this has not been exercised on a real Spark: do the first
  run from the management network with another way in (a second SSH session, the monitor you normally
  do not have), after `tsm qsfp plan` and `--temporary`.
- Changing the address of a twin that is in use interrupts NCCL traffic on it: stop the model first
  (`tsm stop`) and update `qsfp_ip` in `controller.yaml` and the agent's `listener.bind` on node B.

## Not covered

A three-node mesh or a switched fabric (eugr's repo documents those), bonding, VLANs, IPv6 on the link,
and anything that is not a ConnectX port.

## Credits

The two-twin layout, the "one subnet per twin" rule, the MTU, `link-local: []` and the idea of
discovering the other Spark by scanning the link subnet come from NVIDIA's playbook and from
[eugr/spark-vllm-docker](https://github.com/eugr/spark-vllm-docker) (MIT).
