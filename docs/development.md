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

`tests/qsfp_fakes.py` is a fake Spark for the QSFP code (`tsm qsfp`, the setup step, `node doctor`): a
`/sys` tree built from a small state table, and `ip`, `ss`, `ping` and `netplan` answered from that state —
`netplan apply` really reads the YAML under the scratch root and turns it into addresses, and can be told to
fail, so rollback is tested end to end. Use it for any new network feature; never run `netplan` or `ip addr`
for real in a test.

## On-site checklist (first contact with real hardware)

1. Install with the default dry-run mode; `tsm plan …` and compare with the command you run by
   hand today. Check every flag against the image you pin.
2. `tsm activate …` in dry-run: the preflight must pass on both nodes.
3. Maintenance window: stop the hand-started vLLM, `sudo tsm go-live` on both nodes, activate a
   small `single-a` profile first, then `tp2`.
4. Register real model specs (or pin, which resolves them) so the memory fit check runs before every switch.
5. QSFP link (never run on real Sparks yet): on each node `tsm qsfp status`, `tsm qsfp plan --node A|B`, then
   `sudo tsm qsfp apply --temporary`, `tsm qsfp verify`, and only then the permanent `sudo tsm qsfp apply`.
   Run it from the management network, not over the QSFP link. [docs/qsfp-link.md](qsfp-link.md)
