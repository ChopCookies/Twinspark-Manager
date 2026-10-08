"""Container runtimes for tsm-agent.

``DockerRuntime`` drives the real Docker CLI with argv lists (never a shell).
``DryRunRuntime`` records every command it *would* run and simulates healthy
containers — it is what makes the manager testable next to a production vLLM.

Both only ever stop containers carrying the ``org.twinspark.owned=true`` label,
so an existing hand-started vLLM container is never stopped by accident. The
single exception is ``stop_foreign``, which the controller only calls after the
user confirmed the exact container name (migration from a manual launcher).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from ..controller.launch import OWNER_LABEL, ContainerSpec, docker_run_argv


class RuntimeError_(RuntimeError):
    def __init__(self, msg: str, excerpt: str = ""):
        super().__init__(msg)
        self.excerpt = excerpt


@dataclass
class ContainerStatus:
    name: str
    status: str                 # running | exited | created | missing ...
    exit_code: Optional[int] = None
    labels: dict = field(default_factory=dict)
    started_at: Optional[str] = None
    oom_killed: bool = False
    finished_at: Optional[str] = None


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _looks_like_vllm(image: str, command: str) -> bool:
    text = f"{image} {command}".lower()
    return "vllm" in text or "sglang" in text


class DockerRuntime:
    def __init__(self, docker_bin: str = "docker", timeout: int = 120):
        self.docker = docker_bin
        self.timeout = timeout

    def _run(self, argv: list[str], timeout: Optional[int] = None, check: bool = True) -> str:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError_(f"timeout: {' '.join(argv[:3])}") from exc
        except FileNotFoundError as exc:
            raise RuntimeError_(f"{argv[0]} not found — is Docker installed?") from exc
        if check and p.returncode != 0:
            raise RuntimeError_(f"{' '.join(argv[:3])} failed (rc={p.returncode})",
                                (p.stderr or p.stdout)[-2000:])
        return p.stdout

    def ping(self) -> dict[str, Any]:
        try:
            out = self._run([self.docker, "version", "--format", "{{.Server.Version}}"], timeout=10)
            return {"ok": True, "server_version": out.strip()}
        except RuntimeError_ as exc:
            return {"ok": False, "error": str(exc), "detail": exc.excerpt[-300:]}

    def image_present(self, image_ref: str) -> bool:
        out = self._run([self.docker, "image", "inspect", "--format", "{{.Id}}", image_ref],
                        check=False)
        return bool(out.strip())

    def image_inspect(self, ref: str) -> Optional[dict[str, Any]]:
        out = self._run([self.docker, "image", "inspect", ref], check=False).strip()
        if not out:
            return None
        info = json.loads(out)[0]
        cfg = info.get("Config") or {}
        env = dict(e.split("=", 1) for e in (cfg.get("Env") or []) if "=" in e)
        return {
            "id": info.get("Id"), "repo_tags": info.get("RepoTags") or [],
            "repo_digests": info.get("RepoDigests") or [],
            "architecture": info.get("Architecture"), "created": info.get("Created"),
            "labels": cfg.get("Labels") or {},
            "size_bytes": info.get("Size"),
            "versions": {k: env[k] for k in ("VLLM_VERSION", "CUDA_VERSION", "PYTORCH_VERSION",
                                              "NCCL_VERSION", "TORCH_CUDA_ARCH_LIST") if k in env},
        }

    def pull(self, image_ref: str) -> None:
        self._run([self.docker, "pull", image_ref], timeout=3600)

    def start(self, spec: ContainerSpec, env_overrides: dict[str, str]) -> str:
        if not env_overrides:
            return self._run(docker_run_argv(spec, self.docker)).strip()
        # Secret values go through a 0600 file on tmpfs, not `-e K=V` (visible in /proc/*/cmdline).
        plain = spec.model_copy(update={"env": {k: v for k, v in spec.env.items()
                                                if k not in env_overrides}})
        for k, v in env_overrides.items():
            if "\n" in v or "\r" in v or "\0" in v:
                raise RuntimeError_(f"value for {k} cannot be passed through an env file")
        tmpdir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
        fd, path = tempfile.mkstemp(prefix="tsm-env-", dir=tmpdir)       # mode 0600
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(f"{k}={v}\n" for k, v in env_overrides.items()))
            return self._run(docker_run_argv(plain, self.docker, env_file=path)).strip()
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def list_owned(self) -> list[ContainerStatus]:
        out = self._run([self.docker, "ps", "-a", "--filter", f"label={OWNER_LABEL}=true",
                         "--format", "{{.Names}}"])
        return [self.state(n) for n in out.split() if n]

    def list_foreign(self) -> list[dict[str, Any]]:
        """Running containers NOT managed by TwinSpark that look like an inference server."""
        out = self._run([self.docker, "ps", "--format", "{{json .}}"], check=False)
        found = []
        for line in out.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if f"{OWNER_LABEL}=true" in (c.get("Labels") or ""):
                continue
            if _looks_like_vllm(c.get("Image", ""), c.get("Command", "")) or \
                    self._runs_vllm(c.get("Names", "")):
                found.append({"name": c.get("Names"), "image": c.get("Image"),
                              "status": c.get("Status"), "command": (c.get("Command") or "")[:200]})
        return found

    def _runs_vllm(self, name: str) -> bool:
        if not name:
            return False
        out = self._run([self.docker, "top", name, "-eo", "args"], check=False, timeout=10)
        return "vllm serve" in out or "sglang" in out

    def state(self, name: str) -> ContainerStatus:
        out = self._run([self.docker, "inspect", "--format",
                         "{{json .}}", name], check=False).strip()
        if not out:
            return ContainerStatus(name, "missing")
        info = json.loads(out)
        state = info["State"]
        return ContainerStatus(name, state["Status"], state.get("ExitCode"),
                               info.get("Config", {}).get("Labels") or {},
                               state.get("StartedAt"), bool(state.get("OOMKilled")),
                               state.get("FinishedAt"))

    def stop(self, name: str, grace_s: int = 30) -> None:
        # label check again right before acting — defence in depth
        lbl = self._run([self.docker, "inspect", "--format",
                         f'{{{{index .Config.Labels "{OWNER_LABEL}"}}}}', name], check=False)
        if lbl.strip() != "true":
            raise RuntimeError_(f"refusing to stop container not owned by TwinSpark: {name}")
        self._run([self.docker, "stop", "-t", str(grace_s), name], timeout=grace_s + 30, check=False)
        self._run([self.docker, "rm", "-f", name], check=False)

    def remove_exited_owned(self, name: str) -> None:
        st = self.state(name)
        if st.status not in ("missing", "running") and st.labels.get(OWNER_LABEL) == "true":
            self._run([self.docker, "rm", "-f", name], check=False)

    def stop_foreign(self, name: str, grace_s: int = 60) -> None:
        """Stop a hand-started inference container after explicit user confirmation."""
        foreign = {c["name"] for c in self.list_foreign()}
        if name not in foreign:
            raise RuntimeError_(f"{name} is not a running, non-TwinSpark inference container")
        self._run([self.docker, "stop", "-t", str(grace_s), name], timeout=grace_s + 30, check=False)

    def logs(self, name: str, tail: int = 200, max_chars: int = 200_000) -> str:
        # stdout and stderr interleaved in one stream, in the order Docker recorded them
        p = subprocess.run([self.docker, "logs", "--tail", str(tail), name], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace", timeout=60)
        return p.stdout[-max_chars:]


class DryRunRuntime:
    """Records commands, simulates containers. Never touches Docker."""

    def __init__(self, docker_bin: str = "docker"):
        self.docker = docker_bin
        self.history: list[list[str]] = []
        self.containers: dict[str, ContainerStatus] = {}
        self.fail_start: set[str] = set()       # test hook: container names that "crash"
        self.foreign: list[dict[str, Any]] = []  # test hook: hand-started containers

    def ping(self) -> dict[str, Any]:
        return {"ok": True, "server_version": "dry-run"}

    def image_present(self, image_ref: str) -> bool:
        return True

    def image_inspect(self, ref: str) -> Optional[dict[str, Any]]:
        import hashlib
        digest = "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
        return {"id": ref if ref.startswith("sha256:") else digest, "repo_tags": [ref],
                "repo_digests": [], "architecture": "arm64", "labels": {}, "versions": {},
                "size_bytes": 0, "created": None, "simulated": True}

    def pull(self, image_ref: str) -> None:
        self.history.append([self.docker, "pull", image_ref])

    def start(self, spec: ContainerSpec, env_overrides: dict[str, str]) -> str:
        argv = docker_run_argv(spec, self.docker, redact=True)
        if spec.name in self.containers:
            raise RuntimeError_(f"container name already in use: {spec.name}")
        self.history.append(argv)
        crashed = spec.name in self.fail_start
        self.containers[spec.name] = ContainerStatus(
            spec.name, "exited" if crashed else "running", 1 if crashed else None, dict(spec.labels)
        )
        return f"dryrun-{spec.name}"

    def list_owned(self) -> list[ContainerStatus]:
        return [c for c in self.containers.values() if c.labels.get(OWNER_LABEL) == "true"]

    def list_foreign(self) -> list[dict[str, Any]]:
        return list(self.foreign)

    def state(self, name: str) -> ContainerStatus:
        return self.containers.get(name, ContainerStatus(name, "missing"))

    def stop(self, name: str, grace_s: int = 30) -> None:
        self.history.append([self.docker, "stop", "-t", str(grace_s), name])
        self.containers.pop(name, None)

    def remove_exited_owned(self, name: str) -> None:
        st = self.containers.get(name)
        if st and st.status != "running":
            self.containers.pop(name, None)

    def stop_foreign(self, name: str, grace_s: int = 60) -> None:
        if name not in {c["name"] for c in self.foreign}:
            raise RuntimeError_(f"{name} is not a running, non-TwinSpark inference container")
        self.history.append([self.docker, "stop", "-t", str(grace_s), name])
        self.foreign = [c for c in self.foreign if c["name"] != name]

    def logs(self, name: str, tail: int = 200, max_chars: int = 200_000) -> str:
        if name in self.fail_start:
            return "torch.OutOfMemoryError: CUDA out of memory during cuda graph capture"
        return f"[dry-run] {name}: simulated log"
