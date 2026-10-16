"""Container specs as the agent receives them, and the exact ``docker run`` argv built from one.

Kept apart from the launch planner (``controller/launch.py``, which re-exports everything here) so the
agent on each Spark imports only this small module, not the profile/recipe schema graph the planner
needs. The agent never receives a shell string: a ``ContainerSpec`` is fully typed and validated.
"""

from __future__ import annotations

import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

OWNER_LABEL = "org.twinspark.owned"
CONTAINER_HF_HOME = "/root/.cache/huggingface"
CONTAINER_COMPILE_CACHE = "/root/.cache/twinspark-compile"
CONTAINER_MODS = "/opt/twinspark/mods"
CONTAINER_MOD_WORKDIR = "/workspace/mods"
# host sub-directory of compile_cache_dir -> path the tools use inside the container
CACHE_MOUNTS: dict[str, str] = {
    "vllm": "/root/.cache/vllm",
    "flashinfer": "/root/.cache/flashinfer",
    "triton": "/root/.triton",
    "tilelang": "/root/.tilelang",
    "deep_gemm": "/root/.deep_gemm",
    "nv": "/root/.nv",
}
ALLOWED_DEVICES = frozenset({"/dev/infiniband"})
ALLOWED_CAPS = frozenset({"IPC_LOCK", "SYS_NICE"})
_DIGEST_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_LOCAL_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

def immutable_image_ref(ref: str) -> bool:
    return bool(_DIGEST_REF_RE.fullmatch(ref) or _LOCAL_IMAGE_RE.fullmatch(ref))


_MOUNT_PATH_RE = re.compile(r"/[A-Za-z0-9._/+@-]{1,500}")
_WINDOWS_MOUNT_PATH_RE = re.compile(r"[A-Za-z]:[/\\][A-Za-z0-9._+@~-][A-Za-z0-9._/\\+@~-]{0,499}")


class Mount(BaseModel):
    host: str
    container: str
    read_only: bool = False

    @field_validator("container")
    @classmethod
    def _plain_container_path(cls, v: str) -> str:
        # `docker run -v` splits on ':' — a colon (or comma, space, newline...) inside a path
        # would make docker mount something other than the path that was validated.
        if not _MOUNT_PATH_RE.fullmatch(v):
            raise ValueError(f"mount path must be absolute and use only letters, digits and "
                             f"'._/+@-': {v!r}")
        return v

    @field_validator("host")
    @classmethod
    def _plain_host_path(cls, v: str) -> str:
        if _MOUNT_PATH_RE.fullmatch(v):
            return v
        # Local Windows plans/dry-run agents use native cache paths. Spark agents
        # validate this same schema on Linux and continue to accept POSIX hosts only.
        # The drive prefix is the only permitted colon; UNC/device paths are excluded.
        # Windows may expose a user's temp directory through an 8.3 name such as SPARKU~1.
        if os.name == "nt" and _WINDOWS_MOUNT_PATH_RE.fullmatch(v):
            parts = re.split(r"[/\\]+", v[3:])
            if ".." not in parts and any(part not in ("", ".") for part in parts):
                return v
        raise ValueError(f"host mount path must be a plain absolute path without Docker mount separators: {v!r}")


class ContainerSpec(BaseModel):
    """Everything the agent needs to start one container. Fully typed — the agent
    never receives a shell string from the controller."""

    node: Literal["A", "B"]
    role: Literal["single", "head", "worker"]
    name: str
    image_ref: str                                   # repo@sha256:... or local sha256:...
    labels: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    mounts: list[Mount] = Field(default_factory=list)
    command: list[str]                               # argv inside the container
    health_url: Optional[str] = None                 # set for nodes that serve the API
    shm_size: str = "16g"
    privileged: bool = False
    devices: list[str] = Field(default_factory=list)
    cap_add: list[str] = Field(default_factory=list)
    nofile_limit: int = 1048576
    entrypoint: Optional[str] = ""                   # "" clears the image ENTRYPOINT
    mods: list[str] = Field(default_factory=list)    # for preflight / display


class LaunchError(ValueError):
    pass


def docker_run_argv(spec: ContainerSpec, docker_bin: str = "docker", redact: bool = False,
                    env_file: Optional[str] = None) -> list[str]:
    """Build the ``docker run`` argv. Shared by the planner (display) and the agent (exec).

    ``env_file``: variables that carry secrets are passed through a private file instead of
    ``-e`` so they never appear in a process listing (``/proc/<pid>/cmdline``).
    ``--pull never``: the image must already be on the node (pulling is a separate, policy
    controlled step), so a start can never fetch and run an unreviewed image.
    """
    if not immutable_image_ref(spec.image_ref):
        raise LaunchError(f"image must be pinned by digest: {spec.image_ref}")
    argv = [
        docker_bin, "run", "-d", "--pull", "never", "--name", spec.name,
        "--gpus", "all", "--network", "host", "--ipc", "host",
        "--ulimit", "memlock=-1", "--ulimit", "stack=67108864",
        "--ulimit", f"nofile={spec.nofile_limit}:{spec.nofile_limit}",
        "--shm-size", spec.shm_size, "--restart", "no",
    ]
    if spec.privileged:
        argv.append("--privileged")
    for d in spec.devices:
        argv += ["--device", d]
    for c in spec.cap_add:
        argv += ["--cap-add", c]
    if spec.entrypoint is not None:
        argv.append(f"--entrypoint={spec.entrypoint}")
    for k, v in sorted(spec.labels.items()):
        argv += ["--label", f"{k}={v}"]
    if env_file:
        argv += ["--env-file", env_file]
    for k, v in sorted(spec.env.items()):
        shown = "***" if redact and _is_secret_env(k) else v
        argv += ["-e", f"{k}={shown}"]
    for m in spec.mounts:
        argv += ["-v", f"{m.host}:{m.container}{':ro' if m.read_only else ''}"]
    argv.append(spec.image_ref)
    argv += spec.command
    return argv


def _is_secret_env(name: str) -> bool:
    return any(s in name.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
