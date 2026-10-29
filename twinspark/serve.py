"""Process entry points: ``tsm serve controller|agent|privd|termd``.

The controller process runs two HTTP servers on one event loop: the management
API (default 127.0.0.1:8443 — reach it via SSH tunnel or Tailscale) and the
stable inference gateway (default 0.0.0.0:8000). ``privd`` is the small root
helper (drop page cache, headless switch) the agent talks to over a Unix socket.

Imports are per role. Every Spark runs the agent and the root helper next to the model, so they must
not load the controller, the cookbook or FastAPI just because they share this module (measured with
``scripts/measure_memory.py``; docs/memory-planning.md has the numbers).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - type names only
    import uvicorn

    from .controller.controller import Controller
    from .schemas.config import ControllerConfig, Listener
    from .security import SecretsVault

log = logging.getLogger("twinspark")
_LOCAL = ("127.0.0.1", "::1", "localhost")
# systemd socket activation (sd_listen_fds): the first passed descriptor is always 3
SD_LISTEN_FDS_START = 3


def _server(app, listener: "Listener", log_level: str = "info", fd: Optional[int] = None) -> "uvicorn.Server":
    import uvicorn

    common: dict[str, Any] = dict(log_level=log_level, ssl_certfile=listener.tls_cert, ssl_keyfile=listener.tls_key,
                                  proxy_headers=False, access_log=False, ws_max_size=2 * 1024 ** 2)
    if fd is not None:
        return uvicorn.Server(uvicorn.Config(app, fd=fd, **common))
    return uvicorn.Server(uvicorn.Config(app, host=listener.bind, port=listener.port, **common))


def build_controller(cfg: "ControllerConfig", vault: "SecretsVault") -> tuple["Controller", str]:
    from .controller.agent_client import AgentClient
    from .controller.controller import Controller
    from .gateway.gateway import Gateway

    inference_key = vault.get("inference_api_key")
    if not inference_key and cfg.gateway_listener.bind not in _LOCAL:
        raise SystemExit("gateway listens on a non-local address but inference_api_key is empty "
                         "— run `tsm init` first")
    token = vault.get("agent_token")
    if not token:
        raise SystemExit("agent_token missing — run `tsm init`")
    gateway = Gateway(inference_key or None, vault.get("backend_api_key") or None)
    agents = {n: AgentClient(n, ep.agent_url, token) for n, ep in cfg.nodes.items()}
    controller = Controller(cfg, gateway, agents, hf_token=vault.get("hf_token") or None)
    return controller, vault.get("management_api_key")


def build_servers(config_path: str, seed=None,
                  log_level: str = "info") -> tuple["Controller", "uvicorn.Server", Optional["uvicorn.Server"]]:
    """Controller + the two uvicorn servers (management API, gateway), not yet running."""
    from .controller.app import create_app
    from .gateway.app import build_gateway_app
    from .schemas.config import ControllerConfig, load_config
    from .security import SecretsVault

    cfg = load_config(config_path, ControllerConfig)
    vault = SecretsVault(cfg.secrets_dir)
    controller, mgmt_key = build_controller(cfg, vault)
    if seed:
        seed(controller)
    mgmt = _server(create_app(controller, mgmt_key), cfg.listener, log_level)
    if cfg.gateway_mode == "standalone":
        # the gateway is its own service: publish to it instead of serving it (gateway/state.py)
        from .gateway.state import GatewayLink
        controller.gateway_link = GatewayLink(controller.gateway, cfg.gateway_state_path, cfg.gateway_control_socket)
        controller.gateway_link.attach()
        return controller, mgmt, None
    gw = _server(build_gateway_app(controller.gateway), cfg.gateway_listener, log_level)
    return controller, mgmt, gw


async def run_controller(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    _, mgmt, gw = build_servers(config_path)
    await asyncio.gather(mgmt.serve(), *([gw.serve()] if gw is not None else []))


def run_gateway(config_path: str) -> None:
    """The standalone gateway (``gateway_mode: standalone``): no controller, FastAPI or pydantic in here."""
    from .gateway.standalone import run
    asyncio.run(run(config_path))


def build_agent_server(config_path: str, log_level: str = "info") -> "uvicorn.Server":
    from .agent.agent import build_agent_app
    from .schemas.config import AgentConfig, load_config

    cfg = load_config(config_path, AgentConfig)
    if cfg.runtime_mode == "dry-run":
        log.warning("tsm-agent runs in DRY-RUN mode: no containers will be started")
    return _server(build_agent_app(cfg), cfg.listener, log_level)


def socket_activated_fd(environ: Optional[dict] = None, pid: Optional[int] = None) -> Optional[int]:
    """The listening socket systemd passed us (``LISTEN_FDS``/``LISTEN_PID``), or None."""
    env = os.environ if environ is None else environ
    try:
        fds, owner = int(env.get("LISTEN_FDS", "0")), int(env.get("LISTEN_PID", "0"))
    except ValueError:
        return None
    if fds < 1 or owner != (os.getpid() if pid is None else pid):
        return None
    return SD_LISTEN_FDS_START


def build_termd_server(config_path: str, log_level: str = "info", idle_exit_s: float = 0.0,
                       fd: Optional[int] = None) -> "uvicorn.Server":
    """The terminal service: same address as the agent, its own port (``remote_mgmt.terminal_port``).

    Under socket activation (``twinspark-terminal.socket``) systemd owns the port and starts this
    process on the first connection; with ``idle_exit_s`` it ends again once no shell has been open for
    that long, so an enabled-but-unused terminal costs no memory next to the model.
    """
    from .remote.termd import build_termd_app
    from .schemas.config import AgentConfig, load_config

    cfg = load_config(config_path, AgentConfig)
    listener = cfg.listener.model_copy(update={"port": cfg.remote_mgmt.terminal_port})
    app = build_termd_app(cfg, idle_exit_s=idle_exit_s)
    server = _server(app, listener, log_level, fd=fd if fd is not None else socket_activated_fd())
    app.state.server = server            # the idle watcher asks this server to finish
    return server


def run_termd(config_path: str, idle_exit_s: float = 0.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_termd_server(config_path, idle_exit_s=idle_exit_s).run()


def run_privd(socket_path: str, group: str, remote_policy: str | None = None,
              require_root_policy: bool = True) -> None:
    from .agent.privd import privd_serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(privd_serve(socket_path, group, remote_policy, require_root_policy))


def run_agent(config_path: str) -> None:
    build_agent_server(config_path).run()
