"""Process entry points: ``tsm serve controller|agent|privd``.

The controller process runs two HTTP servers on one event loop: the management
API (default 127.0.0.1:8443 — reach it via SSH tunnel or Tailscale) and the
stable inference gateway (default 0.0.0.0:8000). ``privd`` is the small root
helper (drop page cache, headless switch) the agent talks to over a Unix socket.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .agent.agent import build_agent_app
from .controller.agent_client import AgentClient
from .controller.app import create_app
from .controller.controller import Controller
from .gateway.app import build_gateway_app
from .gateway.gateway import Gateway
from .schemas.config import AgentConfig, ControllerConfig, Listener, load_config
from .security import SecretsVault

log = logging.getLogger("twinspark")
_LOCAL = ("127.0.0.1", "::1", "localhost")


def _server(app, listener: Listener) -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(
        app, host=listener.bind, port=listener.port, log_level="info",
        ssl_certfile=listener.tls_cert, ssl_keyfile=listener.tls_key,
        proxy_headers=False, access_log=False))


def build_controller(cfg: ControllerConfig, vault: SecretsVault) -> tuple[Controller, str]:
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


async def run_controller(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(config_path, ControllerConfig)
    vault = SecretsVault(cfg.secrets_dir)
    controller, mgmt_key = build_controller(cfg, vault)
    mgmt = _server(create_app(controller, mgmt_key), cfg.listener)
    gw = _server(build_gateway_app(controller.gateway), cfg.gateway_listener)
    await asyncio.gather(mgmt.serve(), gw.serve())


def run_privd(socket_path: str, group: str) -> None:
    from .agent.privd import privd_serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(privd_serve(socket_path, group))


def run_agent(config_path: str) -> None:
    cfg = load_config(config_path, AgentConfig)
    if cfg.runtime_mode == "dry-run":
        log.warning("tsm-agent runs in DRY-RUN mode: no containers will be started")
    app = build_agent_app(cfg)
    _server(app, cfg.listener).run()
