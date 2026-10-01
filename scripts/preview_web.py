"""Local GUI preview with built-in draft profiles and an in-memory database.

Run: python scripts/preview_web.py --port 18744
No agents are connected; no models or containers are started.
"""

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from twinspark.controller.app import create_app  # noqa: E402
from twinspark.controller.controller import Controller  # noqa: E402
from twinspark.cookbook import build_draft, list_recipes  # noqa: E402
from twinspark.gateway.gateway import Gateway  # noqa: E402
from twinspark.schemas.config import (  # noqa: E402
    AgentConfig,
    ControllerConfig,
    NodeEndpoint,
    NodeIdentity,
    RuntimeSettings,
)


def demo_agents(config, folder):
    """In-process simulated agents. No SSH, Docker, privilege helper, or network calls."""
    from twinspark.agent.actions import AgentActions
    from twinspark.agent.agent import build_agent_app
    from twinspark.controller.agent_client import AgentClient

    agents = {}
    for n, offset in (("A", 0), ("B", 12)):
        cfg = AgentConfig(
            node=NodeIdentity(node_id=n, role="agent"), secrets_dir=str(folder / n), runtime=config.runtime
        )
        actions = AgentActions(cfg)

        def sample(offset=offset):
            wave = math.sin(time.time() / 10 + offset)
            return {
                "demo": True,
                "at": time.time(),
                "cpu_pct": round(22 + offset + wave * 4, 1),
                "mem_total_gib": 119.6,
                "mem_available_gib": 31.8 + wave,
                "page_cache_gib": 6.4,
                "swap_used_gib": 0,
                "gpu": {
                    "utilization_pct": round(76 + wave * 8, 1),
                    "power_w": round(84 + wave * 10 + offset, 1),
                    "temperature_c": 62 + round(wave * 3),
                },
                "network": {
                    "qsfp0": {
                        "rx_bytes_s": 1.4e9 + wave * 1e8,
                        "tx_bytes_s": 1.1e9,
                        "speed_mbps": 200000,
                        "state": "up",
                    },
                    "eth0": {"rx_bytes_s": 450000, "tx_bytes_s": 92000, "speed_mbps": 10000, "state": "up"},
                },
            }

        actions.host_sampler = SimpleNamespace(sample=sample)
        facts = {
            "hostname": f"spark-{n.lower()} · demo",
            "gpu_name": "NVIDIA GB10 (sample)",
            "runtime_mode": "dry-run",
            "mem_total_gib": 119.6,
            "rdma_active": ["qsfp0"],
        }
        actions.registry["hardware_facts"] = lambda params, facts=facts: facts
        config.nodes[n] = NodeEndpoint(agent_url=f"http://preview-{n}",
                                       qsfp_ip="10.0.0.1" if n == "A" else "10.0.0.2",
                                       qsfp_iface="qsfp0", ssh_user="preview")
        app = build_agent_app(cfg, actions, token="preview")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://preview-{n}")
        agents[n] = AgentClient(n, f"http://preview-{n}", "preview", client=client)
    return agents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18744)
    parser.add_argument(
        "--demo-nodes", action="store_true", help="show labeled sample metrics and simulated maintenance"
    )
    args = parser.parse_args()
    config = ControllerConfig(
        node=NodeIdentity(node_id="A", role="controller"), db_path=":memory:", management_auth="none", autostart=False
    )
    temp = tempfile.TemporaryDirectory(prefix="twinspark-preview-")
    folder = Path(temp.name)
    config.runtime = RuntimeSettings(
        hf_cache_dir=str(folder / "hf"), mods_dir=str(folder / "mods"), compile_cache_dir=str(folder / "compile")
    )
    config.watchdog.enabled = False
    config.metrics_interval_s = 2
    agents = demo_agents(config, folder) if args.demo_nodes else {}
    controller = Controller(config, Gateway("preview", "preview"), agents, poll_interval=1)
    for recipe in list_recipes():
        controller.create_profile(build_draft(recipe["name"]))
    print("Local GUI preview: changes last until this process stops; no Spark agents connected.")
    try:
        uvicorn.run(
            create_app(controller, "", run_startup=bool(agents), background=bool(agents)),
            host="127.0.0.1",
            port=args.port,
        )
    finally:
        temp.cleanup()


if __name__ == "__main__":
    main()
