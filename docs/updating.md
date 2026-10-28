# Updating TwinSpark (`tsm update`)

From 0.8.0, `tsm update` installs new versions from the project's
[GitHub releases](https://github.com/ChopCookies/Twinspark-Manager/releases). It updates both Sparks in one
run and the workstation client on its own. The model keeps serving throughout.

```bash
tsm update                    # what runs where, and the newest release
tsm update sparks             # node B, then node A
tsm update here               # this computer (pipx or venv install of the client)
tsm update status             # progress of the Sparks' update
tsm update sparks --version 0.8.0     # a published version, also an older one: that is how to go back
```

The GUI does the same under **Updates → TwinSpark → Check for a new version**. The workstation client's
**Connection** page says when a newer client is available.

## The first time: 0.7 → 0.8 by hand

Nodes on 0.7.x do not know `tsm update` yet, so update to 0.8 once the old way, on each node:

```bash
cd Twinspark-Manager && git pull && sudo ./install.sh --no-setup
sudo systemctl restart twinspark-privd twinspark-agent twinspark-controller   # node A; on B: privd + agent
sudo systemctl restart twinspark-gateway      # node A, only with gateway_mode: standalone
```

This also writes `/etc/twinspark/update-policy.json` (see below). Every later version comes through
`tsm update`. Until then, `tsm update` and the GUI name each node that still runs an older version.

## What happens on the Sparks

`tsm update sparks` asks node A's controller, which works through the nodes one at a time: **node B first,
node A last**. For each node:

1. **Download.** The node fetches the release's wheel and `constraints-release.txt` from GitHub. Each file
   must have the SHA-256 listed in the release's `SHA256SUMS`, and the digest GitHub reports must agree.
   The URLs are built on the node from the fixed repository name and the release tag. A request cannot
   point the update at another server.
2. **Backup.** The program environment `/opt/twinspark/venv` is copied to `venv.rollback`.
3. **Install.** `pip install --upgrade -c constraints-release.txt <wheel>[hf]` installs the dependency
   versions that release was tested with.
4. **Verify.** The new environment must import and report the target version.
5. **Restart.** The TwinSpark services that were running are restarted: privd, the agent, and on node A the
   gateway (standalone mode) and the controller last. Each must stay up for 20 seconds.

If step 3, 4 or 5 fails, the node puts `venv.rollback` back and, if it had already restarted, restarts again
on the old version. The run then stops: node A is not touched when node B rolled back. After a success the
old environment stays as `venv.previous` until the next update.

The work on a node runs in its own transient systemd unit, `twinspark-update-<run id>`, which tsm-privd
starts. It runs as root, with the system Python and from a copy of the worker script. It never imports
anything from the environment it replaces, and it carries on while the agent and the controller restart.
On node A the controller is restarted in the middle of its own run. The new controller finds the run in
its database and finishes it.

**The model keeps serving.** Restarting the TwinSpark services does not touch the inference containers, and
a restarted controller re-attaches the running deployment. Before node A's gateway restarts, the run waits
up to `runtime.drain_timeout_s` for requests in flight to finish. A request still running after that is
cut, and the run records it.

While an update runs, model switches, maintenance and other management writes are refused (409). The
playground and remote management stay available. A failed update holds nothing.

## Switching it off on a node

`install.sh` writes `/etc/twinspark/update-policy.json` once:

```json
{"enabled": true, "allow_prerelease": false}
```

It is root's file. The agent, the GUI and the management key cannot change it:

```bash
sudo tsm update policy off            # only manual updates on this node
sudo tsm update policy on             # back on
sudo tsm update policy on --prerelease  # also release candidates (tsm update sparks --prerelease)
tsm update policy                     # show
```

A node with updates switched off makes `tsm update sparks` refuse before anything starts.

## This computer: `tsm update here`

| Installed with | `tsm update here` |
|---|---|
| `pipx` | installs the release's wheel into the pipx environment, with the release's constraints |
| a virtual environment (`pip install …`) | the same, into that environment |
| `pip install -e .` (a checkout) | refuses: `git pull` updates it |
| install.sh (a Spark) | refuses: use `tsm update sparks` |
| the system Python | refuses: reinstall with pipx |

Installed extras (`client`, `hf`) are kept. On Windows, `tsm update here` continues in a separate Python
process so that the running `tsm.exe` can be replaced. Close `tsm client` windows first. Afterwards, start
`tsm client` again.

## Troubleshooting

| You see | Meaning | Fix |
|---|---|---|
| "a TwinSpark older than 0.8.0, which cannot update itself" | that node still runs 0.7 | the first update by hand (above) |
| "updates are switched off by /etc/twinspark/update-policy.json" | the node's policy | `sudo tsm update policy on` on that node, if you want it |
| "… missing — run sudo ./install.sh --no-setup once" | no policy file yet | the first update by hand writes it |
| "was not installed by install.sh" | a node run from a checkout or a hand-made venv | update it by hand |
| "GitHub is not reachable from node A" | node A has no internet, or GitHub's API limit is used up | try later; the Sparks need GitHub and PyPI for an update |
| "the update failed and was rolled back" | the new version did not install, import or stay up | `journalctl -u 'twinspark-update-*'` and `journalctl -u twinspark-agent` on that node |
| "lost its update record" / "the update worker stopped without finishing" | the worker was killed (reboot, OOM) | check `/opt/twinspark/venv` on that node: `venv.rollback` holds the previous environment |

## Releases (maintainers)

A release is a tag. `.github/workflows/release.yml` does the rest:

1. Set the version in `pyproject.toml` and `twinspark/__init__.py`, and add a `## X.Y.Z — date — title`
   section to `CHANGELOG.md`. Regenerate `catalog/index.json` (`python scripts/build_catalog.py`).
2. Merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The workflow first checks that the tag matches `pyproject.toml`. It runs the tests, builds the wheel and
   the sdist, and installs the wheel in a clean venv. It then writes `SHA256SUMS`
   (`scripts/release_assets.py`) and publishes the GitHub release, with that version's CHANGELOG section as
   the release notes.

Versions such as `0.9.0rc1` become pre-releases, which `tsm update` installs only with `--prerelease`.
Publishing to PyPI is optional. Add this repository as a trusted publisher of `twinspark-manager` on pypi.org
(workflow `release.yml`, environment `pypi`). Then set the repository variable `PUBLISH_PYPI` to `true`.
