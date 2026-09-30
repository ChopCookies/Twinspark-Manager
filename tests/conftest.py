from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from twinspark.agent.actions import AgentActions
from twinspark.agent.agent import build_agent_app
from twinspark.agent.runtime import DryRunRuntime
from twinspark.controller.agent_client import AgentClient
from twinspark.controller.controller import Controller
from twinspark.gateway.gateway import Gateway
from twinspark.schemas.config import (
    AgentConfig,
    ControllerConfig,
    NodeEndpoint,
    NodeIdentity,
    RuntimeSettings,
)
from twinspark.schemas.enums import Quantization, Topology
from twinspark.schemas.profile import ImmutableIdentity, ProfileDraft, SimpleSettings
from twinspark.security import SecretsVault

TOKEN = "test-agent-token"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def require_symlinks(tmp_path):
    """Probe permissions instead of silently replacing HF cache links with copies."""
    target = tmp_path / "symlink-probe-target"
    link = tmp_path / "symlink-probe"
    target.write_text("probe")
    try:
        link.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("HF cache symlink test requires Windows symlink privileges or Linux")
        raise
    finally:
        if link.is_symlink():
            link.unlink()
        target.unlink()


def identity(repo="org/model-a", quant=Quantization.NVFP4) -> ImmutableIdentity:
    return ImmutableIdentity(model_repo=repo, model_revision=SHA, quantization=quant,
                             image="nvcr.io/nvidia/vllm", image_digest=DIGEST, vllm_version="0.x")


def draft(name="qwen", topology=Topology.TP2, repo="org/model-a", alias="default", **simple):
    s = dict(model=repo, quantization=Quantization.NVFP4, topology=topology,
             context_length=32768, api_alias=alias)
    s.update(simple)
    return ProfileDraft(name=name, simple=SimpleSettings(**s), identity=identity(repo))


@pytest.fixture
def runtime_settings(tmp_path):
    return RuntimeSettings(hf_cache_dir=str(tmp_path / "hf"), compile_cache_dir=str(tmp_path / "cc"),
                           mods_dir=str(tmp_path / "mods"),
                           vllm_port=18100, health_timeout_s=5, drain_timeout_s=1)


@pytest.fixture
def controller_config(tmp_path, runtime_settings):
    return ControllerConfig(
        node=NodeIdentity(node_id="A", role="controller"),
        db_path=str(tmp_path / "state.db"), secrets_dir=str(tmp_path / "secrets"),
        nodes={
            "A": NodeEndpoint(agent_url="http://agent-a", qsfp_ip="10.0.0.1", qsfp_iface="enp1s0f1np1",
                              rdma_hcas=["rocep1s0f1", "roceP2p1s0f1"], ib_gid_index=3),
            "B": NodeEndpoint(agent_url="http://agent-b", qsfp_ip="10.0.0.2", qsfp_iface="enp1s0f1np1",
                              ssh_user="spark", rdma_hcas=["rocep1s0f1", "roceP2p1s0f1"],
                              ib_gid_index=3),
        },
        runtime=runtime_settings,
    )


class Cluster:
    """Controller + two dry-run agents wired over in-process ASGI transports."""

    def __init__(self, cfg: ControllerConfig, tmp_path):
        self.runtimes: dict[str, DryRunRuntime] = {}
        agents = {}
        for n in ("A", "B"):
            vault = SecretsVault(tmp_path / f"secrets-{n}")
            vault.set("backend_api_key", "backend-key")
            acfg = AgentConfig(node=NodeIdentity(node_id=n, role="agent"), runtime=cfg.runtime,
                               secrets_dir=str(tmp_path / f"secrets-{n}"))
            rt = DryRunRuntime()
            self.runtimes[n] = rt
            app = build_agent_app(acfg, AgentActions(acfg, runtime=rt, vault=vault), token=TOKEN)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://agent-{n}")
            agents[n] = AgentClient(n, f"http://agent-{n}", TOKEN, client=client)
        self.gateway = Gateway(inference_api_key="client-key", backend_api_key="backend-key")
        self.controller = Controller(cfg, self.gateway, agents, poll_interval=0.01)

    async def wait_job(self, job_id: str, timeout: float = 10):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            job = self.controller.store.load_job(job_id)
            if job and job.state.value in ("completed", "failed") and not self.controller.busy():
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job {job_id} did not finish")

    async def wait_idle(self, timeout: float = 10):
        for _ in range(int(timeout / 0.02)):
            if not self.controller.busy() and not self.controller._tasks:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("controller still busy")


@pytest.fixture
def cluster(controller_config, tmp_path):
    return Cluster(controller_config, tmp_path)
