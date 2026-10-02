"""Controller and Agent configuration models + YAML loading.

Mirrors /etc/twinspark/controller.yaml and /etc/twinspark/agent.yaml (spec §44).
Secrets are never stored in these files — they live in the vault under
``secrets_dir`` and are referenced by slot name only (spec §40).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, TypeVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import HeadlessMode

_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class Listener(BaseModel):
    bind: str = "127.0.0.1"
    port: int
    tls_cert: Optional[str] = None
    tls_key: Optional[str] = None


class NodeEndpoint(BaseModel):
    """How the controller reaches one Spark and how vLLM on it is wired."""

    agent_url: str                          # e.g. http://192.168.100.2:9443
    qsfp_ip: Optional[str] = None           # IP on the direct 200G link (NCCL / sync)
    qsfp_iface: Optional[str] = None        # e.g. enp1s0f1np1 (NCCL_SOCKET_IFNAME)
    # RoCE devices NCCL may use (NCCL_IB_HCA). Each GB10 QSFP port is fed by TWO
    # PCIe Gen5 x4 links that show up as two RoCE devices (e.g. rocep1s0f1 and
    # roceP2p1s0f1). Listing both lets NCCL reach ~200 Gb/s; one alone caps at ~100.
    rdma_hcas: list[str] = Field(default_factory=list)
    ib_gid_index: Optional[int] = Field(default=None, ge=0, le=255)   # RoCE v2 IPv4 GID
    ssh_user: Optional[str] = None          # rsync target user for weight sync
    ssh_host: Optional[str] = None          # defaults to qsfp_ip

    @field_validator("rdma_hcas")
    @classmethod
    def _hca_names(cls, v: list[str]) -> list[str]:
        for h in v:
            if not _IFACE_RE.match(h):
                raise ValueError(f"invalid RDMA device name: {h!r}")
        return v

    @field_validator("qsfp_iface")
    @classmethod
    def _iface(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _IFACE_RE.match(v):
            raise ValueError(f"invalid interface name: {v!r}")
        return v

    @property
    def sync_host(self) -> Optional[str]:
        return self.ssh_host or self.qsfp_ip


class RuntimeSettings(BaseModel):
    """Where vLLM listens internally, where model weights live, container options."""

    vllm_port: int = 8100                   # internal; the stable gateway is on 8000
    hf_cache_dir: str = "/var/lib/twinspark/hf-cache"
    compile_cache_dir: str = "/var/cache/twinspark/compile"   # survives switches (§24)
    mods_dir: str = "/var/lib/twinspark/mods"                 # eugr-style mods (run.sh)
    master_port: int = 29501                # torch.distributed rendezvous (native)
    ray_port: int = 6379
    health_timeout_s: int = 2400            # big models + CUDA graph capture are slow
    drain_timeout_s: int = 30
    max_download_gib: Optional[float] = Field(default=None, gt=0)
    min_disk_free_gib: float = Field(default=2.0, ge=0)
    allow_image_pull: bool = True

    # ---- container wiring -------------------------------------------------
    # "rdma": --device /dev/infiniband + IPC_LOCK (enough for NCCL over RoCE).
    # "privileged": --privileged, exactly like eugr/spark-vllm-docker's default.
    container_mode: Literal["rdma", "privileged"] = "rdma"
    shm_size: str = "16g"
    nofile_limit: int = Field(default=1048576, ge=1024)
    # Persist FlashInfer / Triton / TileLang / vLLM JIT caches across switches
    # (cold start drops from ~9 min to a couple of minutes on a warm cache).
    cache_mounts: bool = True

    # ---- memory hygiene on unified memory -------------------------------------
    # vLLM's startup gate on GB10 reads *free* memory; page cache left over from
    # loading 150+ GB of weights counts as used. Dropping it before start is what
    # every dual-Spark recipe does by hand. Needs tsm-privd (root helper).
    drop_caches_before_start: bool = True
    # Refuse to start (and roll back) instead of letting vLLM fail after minutes
    # when free memory is below gpu_memory_utilization * MemTotal.
    free_memory_gate: bool = True
    privd_socket: str = "/run/twinspark/privd.sock"

    # ---- transfers -----------------------------------------------------------
    download_workers: int = Field(default=8, ge=1, le=64)
    verify_hashes: bool = True             # sha256 vs. the Hub's LFS metadata
    sync_streams: int = Field(default=4, ge=1, le=16)   # parallel rsync over QSFP
    ssh_cipher: str = "aes128-gcm@openssh.com"
    # Dedicated key + known_hosts for A -> B weight sync (written by `tsm setup`). Unset: the
    # service user's default ~/.ssh identities are used.
    ssh_key: Optional[str] = None
    ssh_known_hosts: Optional[str] = None

    @field_validator("shm_size")
    @classmethod
    def _shm(cls, v: str) -> str:
        if not re.match(r"^[0-9]+[bkmg]?$", v.lower()):
            raise ValueError("shm_size must look like '16g'")
        return v


class WatchdogSettings(BaseModel):
    """Post-activation health watch (the model can still die after 'healthy')."""

    enabled: bool = True
    interval_s: float = Field(default=15.0, gt=0)
    auto_recover: bool = True               # re-activate the same revision
    max_recoveries_per_hour: int = Field(default=3, ge=0)


class ControllerConfig(BaseModel):
    node: "NodeIdentity"
    db_path: str = "/var/lib/twinspark/state.db"
    secrets_dir: str = "/etc/twinspark/secrets"
    listener: Listener = Field(default_factory=lambda: Listener(bind="127.0.0.1", port=8443))
    gateway_listener: Listener = Field(default_factory=lambda: Listener(bind="0.0.0.0", port=8000))
    nodes: dict[str, NodeEndpoint] = Field(default_factory=dict)   # {"A": ..., "B": ...}
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    autostart: bool = True                  # re-activate the last healthy revision at boot
    # after a power cut both nodes boot together: wait this long for the other agent before the
    # autostart decides what to do (0 = do not wait)
    startup_wait_s: int = Field(default=180, ge=0, le=1800)
    auto_rollback: bool = True              # restart previous known-good on failure
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    metrics_interval_s: float = Field(default=10.0, gt=0)
    # which node downloads from the Hub when no node has the weights yet
    download_node: Literal["A", "B"] = "A"
    # community recipe folders shown in the Cookbook ("owner/repo:path[@ref]" on GitHub)
    recipe_sources: list[str] = Field(default_factory=lambda: ["eugr/spark-vllm-docker:recipes"])

    management_auth: Literal["none", "apikey"] = "apikey"
    csrf_protection: bool = True
    rate_limit_per_min: int = 600
    remote: dict[str, bool] = Field(
        default_factory=lambda: {"ssh_tunnel": True, "tailscale": False, "lan": False}
    )

    @model_validator(mode="after")
    def _check_nodes(self) -> "ControllerConfig":
        unknown = set(self.nodes) - {"A", "B"}
        if unknown:
            raise ValueError(f"node keys must be 'A' and/or 'B', got {sorted(unknown)}")
        if self.management_auth == "none" and self.listener.bind not in ("127.0.0.1", "::1"):
            raise ValueError("management_auth 'none' is only allowed on a localhost listener")
        return self


class NodeIdentity(BaseModel):
    node_id: Literal["A", "B"]
    role: Literal["controller", "agent"]
    hostname: str = ""


class AgentConfig(BaseModel):
    node: NodeIdentity
    listener: Listener = Field(default_factory=lambda: Listener(bind="127.0.0.1", port=9443))
    secrets_dir: str = "/etc/twinspark/secrets"
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    # "docker" drives real containers. "dry-run" records every command it WOULD run
    # and simulates healthy containers — safe to use next to an existing vLLM setup.
    runtime_mode: Literal["docker", "dry-run"] = "dry-run"
    docker_bin: str = "docker"
    headless_mode: HeadlessMode = HeadlessMode.HEADLESS_SAFE


ControllerConfig.model_rebuild()

T = TypeVar("T", bound=BaseModel)


def load_config(path: str | Path, model: type[T]) -> T:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return model.model_validate(data)
