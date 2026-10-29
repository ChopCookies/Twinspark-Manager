# Development, testing and the demo harness

Nothing here needs a Spark. The suite exercises the control path against dry-run agents,
real HTTP servers on loopback, fake `/sys` trees and canned command output.

## Set up

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,hf]"
.venv/bin/pytest                    # all Python tests, including merged recipe / setup / remote workflows
node --test tests/*.test.cjs         # all web-client tests, no browser needed
.venv/bin/ruff check .
.venv/bin/pip wheel --no-deps . --wheel-dir dist    # installable wheel incl. cookbook + web UI
```

On Windows (PowerShell):

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q -rs
```

## Continuous integration and dependency sets

`.github/workflows/ci.yml` runs on every push to `main` and `release/**` and on every pull request:

| Job | What |
|---|---|
| tests (minimum dependencies) | pytest against the oldest versions `pyproject.toml` allows, pinned in `constraints/minimum.txt` |
| tests (newest dependencies) | pytest against today's releases |
| GUI tests | `node --test tests/*.test.cjs`, and `web/dist` must equal a fresh `scripts/build_web.py` |
| lint | `ruff check .` |
| type check (mypy) | `mypy` with the settings in `pyproject.toml` (see below) |
| workstation tests (Windows) | the client, `tsm update`, release and portability tests on `windows-latest`, in the ANSI code page (not the Hermes fakes: their launchers are `#!` scripts) |
| alert rules (promtool) | `promtool check rules` and `promtool test rules` for `deploy/observability/` |
| dependency advisories | `pip-audit` on `constraints/release.txt` and `constraints/minimum.txt` |
| wheel + install smoke test | builds the wheel, installs it with `constraints/release.txt` in a clean venv and starts the demo cluster from it |

The newest-dependency test job runs under `coverage`. The report is the job's summary, the HTML is an artifact,
and the job fails below `fail_under` in `pyproject.toml` (80 %). What stays uncovered is mostly code that
only runs on hardware: Docker, RDMA, privd and systemd.

**mypy** checks every module except the ones listed under `[[tool.mypy.overrides]]` in `pyproject.toml`.
Those were written before mypy ran in CI. Their remaining findings are mostly an Optional value used without
a check, or a loose dict type. Fix one module, then delete it from the list. New modules are always checked.

CI also runs on a weekly schedule, so a new advisory or a new dependency release shows up without a push.
`.github/dependabot.yml` proposes updates for the actions and for the pins in `constraints/release.txt`.
Releases come from tags (`.github/workflows/release.yml`); the steps are in
[updating.md](updating.md#releases-maintainers).

`pyproject.toml` only sets lower bounds, so a library install takes the newest versions. A Spark install
(`install.sh`) uses `constraints/release.txt`: the exact set this release was tested with
(`TSM_UNPINNED=1` takes the newest instead). Regenerate it before a release:

```bash
python3.12 -m venv /tmp/rel && /tmp/rel/bin/pip install ".[hf]"
/tmp/rel/bin/pip freeze --exclude twinspark-manager > constraints/release.txt   # keep the two header lines
```

Raise a bound in `constraints/minimum.txt` only together with `pyproject.toml`. Tests must pass on both
sets: where a library changed behaviour between them, the test accepts both forms rather than narrowing the
range. A bound is raised when a published advisory covers the old floor (`pip-audit -r
constraints/minimum.txt --no-deps`; 0.7.1 moved Starlette, anyio and cryptography for that reason), and
`constraints/release.txt` is regenerated when `pip-audit -r constraints/release.txt` finds one.

HF-cache symlink tests skip when Windows denies link creation. Linux PTYs, Unix sockets,
root ownership, executable bits and atomic directory exchange have separate platform checks;
their remaining workflow and error tests still run on Windows. Installer tests use Bash with
fake system tools and skip if Bash is unavailable. Run the full suite on Linux before deploying.

## `tsm demo`: the whole stack on one machine

`tsm demo` provisions two sandbox "nodes" with the same code `tsm setup` uses (node B joins with a
real join code), then starts agent A on `127.0.0.1`, agent B on `127.0.0.2`, the controller, the
gateway and the GUI as real uvicorn servers. Containers are simulated by the dry-run runtime.

```python
from twinspark.demo import DemoCluster

async with DemoCluster() as d:           # d.url, d.key, d.gateway_url, d.controller
    ...                                  # drive it with httpx or a headless browser
```

On Linux, `DemoCluster(remote={"terminal": True})` also starts the terminal services (`tsm demo --with-terminal`);
that terminal is a real shell as your user, so it is off by default. Power, boot and Wake-on-LAN need the
root helper and are not available in the demo; their tests use fake `systemctl` / `efibootmgr` / `ethtool`
through the `privops.RUN` hook.

`tests/test_demo_cluster.py` uses it for process-level checks (auth, agent token, GUI served,
onboarding checklist, launch plans for every built-in recipe).

## Sandbox installs

`tsm setup --root /tmp/stage --yes --no-start` builds a complete install (configs, vault, SSH key,
systemd units) under a scratch directory. Every path inside the generated files honours the root,
so the result can be started by hand: `tsm serve agent --config /tmp/stage/etc/twinspark/agent.yaml`.
A sandbox install never touches the real `~/.ssh`, `/etc` or systemd.

`scripts/gen_units.py` regenerates `deploy/systemd/*.service` from the same generator; a test fails
if they drift.

## Web client

```bash
.venv/bin/python scripts/build_web.py          # src/ -> dist/ (vanilla assets, no bundler)
.venv/bin/python scripts/preview_web.py        # http://127.0.0.1:18744/ with an in-memory database
.venv/bin/python scripts/preview_web.py --demo-nodes   # labeled sample telemetry + a simulated maintenance run
```

The preview serves the built `web/dist` assets: rebuild and refresh after editing `web/src`.
`dist/` is committed so a plain `pip install` serves the GUI without Node.

## Test suites

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider   # no hardware; HTTP tests use loopback
ruff check .
node --test tests/*.test.cjs                                         # GUI logic, no browser
```

`tests/test_browser_live_refresh.py` drives the real GUI in Chromium against an in-process demo cluster. It
checks that the 5-second dashboard refresh patches the page in place instead of rebuilding it: no fade, and
focus and selection are kept. It needs `pip install playwright` and a Chromium (`playwright install
chromium`, or point `TSM_TEST_CHROMIUM` at one), and is skipped otherwise.

The suite must pass as root and as an ordinary user, on a machine without `efibootmgr`, `fwupd`,
`tailscale` or `netplan`. Fake every host tool a test touches; never let a test see the real one.

`tests/test_hardening_api.py`, `_controller.py` and `_agent.py` hold the regression tests for every bug
found while exercising the stack on loopback (auth, input handling, secrets, mods, watchdog,
maintenance). When you fix a bug, add its test next to those. Pinning needs Hugging Face, so the
end-to-end activation tests (`tests/test_activation.py`) stub the resolver; real pinning and real
containers are only exercised on site.

The workstation client is tested without SSH, a Spark or Hermes:

- `tests/test_client_ssh_fake.py` provides `FakeSSH`, a fake `ssh` / `ssh-keyscan` pair on `PATH` that
  checks host keys against the client's `known_hosts`, accepts or refuses logins as configured, answers
  remote commands from canned output (`controller.yaml`, `tsm init --show`, …) and **really forwards
  TCP** for `-L`, so `tests/test_client_app.py` drives client app → fake ssh → the demo cluster's API and
  gateway the way it would reach a Spark. Never let a client test reach a real `ssh`.
- `tests/test_client_hermes.py` builds fake Hermes installs (a script launcher, a fake `HOME`) and
  `tests/test_client_validate.py` a fake OpenAI-compatible gateway; `tests/test_client_routes_local.py`
  runs the client's Hermes and recipe-conversion routes on top of them.
- `tests/test_browser_client.py` opens the client GUI in Chromium (same requirements as above): the
  `#k=` address, adding a Spark, the keys dialog, node pages through the client, disconnecting.
- `tests/web_workstation.test.cjs` covers the GUI's client mode without a browser.

To run the client against the demo by hand: `tsm demo` in one terminal, then `tsm client` with
`TSM_CLIENT_HOME` set to a scratch directory and a **direct** connection to the demo's
`http://127.0.0.1:<port>` (plain http is accepted for loopback addresses only).

`scripts/measure_memory.py` starts the real services from a sandbox install and prints each one's
RSS, PSS and USS; `tests/test_lean_services.py` keeps heavy imports out of the roles that do not need
them. Check both after adding an import to the agent, the terminal or the controller's startup path.

`tests/qsfp_fakes.py` is a fake Spark for the QSFP code (`tsm qsfp`, the setup step, `node doctor`): a
`/sys` tree built from a small state table, and `ip`, `ss`, `ping` and `netplan` answered from that state —
`netplan apply` really reads the YAML under the scratch root and turns it into addresses, and can be told to
fail, so rollback is tested end to end. Use it for any new network feature; never run `netplan` or `ip addr`
for real in a test.

## On-site checklist (first contact with real hardware)

1. Install with the default dry-run mode; `tsm plan …` and compare with the command you run by
   hand today. Check every flag against the image you pin. Connect the [workstation client](client.md)
   (`tsm client`) and check the three states, then measure the services
   (`/opt/twinspark/venv/bin/python scripts/measure_memory.py` from a checkout on each node).
2. `tsm activate …` in dry-run: the preflight must pass on both nodes.
3. Maintenance window: stop the hand-started vLLM, `sudo tsm go-live` on both nodes, activate a
   small `single-a` profile first, then `tp2`.
4. Register real model specs (or pin, which resolves them) so the memory fit check runs before every switch.
5. QSFP link (never run on real Sparks yet): on each node `tsm qsfp status`, `tsm qsfp plan --node A|B`, then
   `sudo tsm qsfp apply --temporary`, `tsm qsfp verify`, and only then the permanent `sudo tsm qsfp apply`.
   Run it from the management network, not over the QSFP link. [docs/qsfp-link.md](qsfp-link.md)
6. `tsm update` (new in 0.8, not run on hardware yet). First the one manual update from 0.7 on each node
   ([updating.md](updating.md#the-first-time-07-08-by-hand)). Then `tsm update`, which should list both nodes
   at 0.8.0. With a model serving and a request loop running against the gateway, run
   `tsm update sparks --version 0.8.0`, a reinstall of the same version. Watch node B, then node A, the
   controller coming back by itself, and the model never stopping. Then try the refusals:
   `sudo tsm update policy off` on node B, and a version that does not exist.
7. Host-key pinning (new in 0.8): `tsm doctor` should report the A→B SSH check with "host key pinned from
   node B's agent". Then a weight copy to node B.
