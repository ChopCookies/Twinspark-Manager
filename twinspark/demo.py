"""``tsm demo`` — the whole stack on this machine, no Spark required.

Two dry-run agents, the controller, the gateway and the web GUI, each a real HTTP server
started from config files produced by the same code ``tsm setup`` uses (node B joins with a
real join code). Containers are simulated; model downloads and registry lookups are NOT
(they go through the normal code paths and need internet), so browse, import recipes, plan
and click around freely.

It is also the harness for process-level tests: :class:`DemoCluster` starts and stops the
servers inside any asyncio loop.
"""

from __future__ import annotations

import asyncio
import getpass
import socket
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Optional

import uvicorn

from . import provision, serve
from .cookbook import build_draft, list_recipes
from .provision import Answers, Layout, Provisioner
from .security import SecretsVault


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def seed_recipes(controller) -> int:
    n = 0
    for r in list_recipes():
        try:
            controller.create_profile(build_draft(r["name"]))
            n += 1
        except Exception:  # noqa: BLE001 - a duplicate or invalid recipe must not stop the demo
            continue
    return n


class DemoCluster:
    """Controller + gateway + agents A and B on loopback addresses (127.0.0.1 / 127.0.0.2)."""

    def __init__(self, base: Optional[Path] = None, seed: bool = True, mgmt_port: Optional[int] = None,
                 gateway_port: Optional[int] = None, extra: Optional[dict] = None):
        self._tmp = None
        if base is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="twinspark-demo-")
            base = Path(self._tmp.name)
        self.base = Path(base)
        self.seed = seed
        self.mgmt_port = mgmt_port or free_port()
        self.gateway_port = gateway_port or free_port()
        self.agent_port = free_port("127.0.0.2")
        self.extra = extra or {}
        self.a_layout = Layout(self.base / "node-a")
        self.b_layout = Layout(self.base / "node-b")
        self.servers: list[uvicorn.Server] = []
        self.tasks: list[asyncio.Task] = []
        self.controller = None
        self.key = ""

    # ---- provisioning (the same code path as a real install) ------------------------------
    def provision(self) -> None:
        user = getpass.getuser()
        common = dict(service_user=user, mgmt_port=self.mgmt_port,
                      gateway_port=self.gateway_port, gateway_bind="127.0.0.1", agent_port=self.agent_port,
                      vllm_port=free_port(), runtime_mode="dry-run", peer_ssh_user=user)
        a = Answers(role="controller", node_id="A", hostname="demo-a", qsfp_iface="lo", qsfp_ip="127.0.0.1",
                    peer_ip="127.0.0.2", peer_iface="lo", hf_cache_dir=str(self.base / "node-a" / "hf"),
                    docker_group=False,
                    **common)
        a.agent_port = self.agent_port
        pa = Provisioner(a, self.a_layout, systemd=False)
        pa.apply()
        code = provision.make_join(a, pa.vault, pa.pubkey)
        info = provision.decode_join(code)
        b = Answers(role="agent", node_id="B", hostname="demo-b", qsfp_iface="lo",
                    hf_cache_dir=str(self.base / "node-b" / "hf"), docker_group=False, **common)
        provision.apply_join(b, info)
        Provisioner(b, self.b_layout, systemd=False).apply()
        self.key = SecretsVault(self.a_layout.secrets).get("management_api_key")

    # ---- lifecycle ------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.mgmt_port}"

    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"

    async def _boot(self, servers: list[uvicorn.Server]) -> None:
        for s in servers:
            self.servers.append(s)
            self.tasks.append(asyncio.create_task(s.serve()))
        for _ in range(200):
            if all(s.started for s in servers):
                return
            dead = [t for t in self.tasks if t.done()]
            if dead:
                exc = dead[0].exception()
                await self.stop()
                raise RuntimeError(f"demo server failed to start: {exc!r}")
            await asyncio.sleep(0.05)
        await self.stop()
        raise RuntimeError("demo servers did not start in 10 s")

    async def start(self) -> "DemoCluster":
        self.provision()
        q = "warning"
        # agents first: like a real boot where the controller must wait for them
        await self._boot([serve.build_agent_server(str(self.a_layout.agent_yaml), q),
                          serve.build_agent_server(str(self.b_layout.agent_yaml), q)])
        self.controller, mgmt, gw = serve.build_servers(
            str(self.a_layout.controller_yaml), seed=seed_recipes if self.seed else None, log_level=q)
        await self._boot([mgmt, gw])
        return self

    async def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        for t in self.tasks:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=10)
        self.servers, self.tasks = [], []
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None

    async def __aenter__(self) -> "DemoCluster":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()


def cmd_demo(args, api=None) -> None:
    async def main() -> None:
        base = Path(args.dir) if args.dir else None
        demo = DemoCluster(base, seed=not args.empty, mgmt_port=args.port)
        await demo.start()
        print("TwinSpark demo — two simulated Sparks on this machine (containers are NOT run).")
        print(f"  Web GUI      {demo.url}/")
        print(f"  API key      {demo.key}")
        print(f"  Gateway      {demo.gateway_url}/v1   (inference key: tsm init --show --secrets-dir "
              f"{demo.a_layout.secrets})")
        print(f"  Files        {demo.base}")
        print("  Try: import a recipe in the Cookbook, Pin & prepare it, press Plan. Ctrl-C stops and cleans up.")
        try:
            await asyncio.gather(*demo.tasks)
        finally:
            await demo.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ndemo stopped")


def add_parsers(sub, cmd) -> None:
    s = cmd("demo", cmd_demo, "try the GUI and CLI on this machine with two simulated Sparks")
    s.add_argument("--port", type=int, help="web GUI port (default: any free port)")
    s.add_argument("--dir", help="keep demo files here instead of a temp dir")
    s.add_argument("--empty", action="store_true", help="do not pre-load the built-in recipes")
    cmd_demo.local = True
