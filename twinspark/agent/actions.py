"""tsm-agent typed actions (spec §4.2).

Fixed set of typed actions — there is no generic ``run_command`` anywhere.
Every action validates its own parameters. Long operations (download, verify,
sync, link tests) run as background tasks and are polled with ``task_status``.
Root-only operations (drop page cache, headless mode) go through tsm-privd.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from .. import __version__
from ..controller.launch import (
    ALLOWED_CAPS,
    ALLOWED_DEVICES,
    CONTAINER_MODS,
    OWNER_LABEL,
    ContainerSpec,
    immutable_image_ref,
)
from ..schemas.config import AgentConfig
from ..schemas.enums import HeadlessMode
from ..security import SecretsVault
from ..weights import execute_delete, hf_repo_dir, plan_delete, scan_cache, snapshot_complete
from . import linktest, mods, sysinfo, transfers
from .privd import PrivClient, PrivdUnavailable
from .runtime import DockerRuntime, DryRunRuntime, RuntimeError_, port_in_use
from .tasks import TaskRegistry

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]+$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_NAME_RE = re.compile(r"^tsm-[a-z0-9._-]+$")
_FOREIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")
_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
_GLOB_RE = re.compile(r"^[A-Za-z0-9._*?/\[\]-]{1,128}$")
_SECRET_REF = re.compile(r"^\$\{secret:([a-z_]+)\}$")


class ActionError(Exception):
    def __init__(self, msg: str, excerpt: str = "", status: int = 422):
        super().__init__(msg)
        self.excerpt = excerpt
        self.status = status


class AgentActions:
    def __init__(self, config: AgentConfig, runtime=None, vault: Optional[SecretsVault] = None,
                 privd: Optional[PrivClient] = None):
        self.config = config
        self.vault = vault or SecretsVault(config.secrets_dir)
        if runtime is not None:
            self.runtime = runtime
        elif config.runtime_mode == "docker":
            self.runtime = DockerRuntime(config.docker_bin)
        else:
            self.runtime = DryRunRuntime(config.docker_bin)
        self.privd = privd or PrivClient(config.runtime.privd_socket)
        self.tasks = TaskRegistry()
        from .monitoring import HostSampler
        from .node_maintenance import NodeMaintenance
        self.host_sampler = HostSampler()
        self.maintenance = NodeMaintenance(self)
        self.downloader: Optional[Callable[[dict], None]] = None   # test hook
        self.registry: dict[str, Callable[[dict], Any]] = {
            "system_telemetry": lambda params: self.host_sampler.sample(),
            "maintenance_probe": self.maintenance.probe,
            "maintenance_quiet": lambda params: self.maintenance.quiet() or {"quiet": True},
            "maintenance_start": self.maintenance.start,
            "maintenance_status": self.maintenance.status,
            "maintenance_reboot": self.maintenance.reboot,
            "maintenance_verify": self.maintenance.verify,
            "hardware_facts": self.hardware_facts,
            "memory_telemetry": self.memory_telemetry,
            "preflight": self.preflight,
            "image_ensure": self.image_ensure,
            "image_inspect": self.image_inspect,
            "container_start": self.container_start,
            "containers_stop_owned": self.containers_stop_owned,
            "container_state": self.container_state,
            "container_logs": self.container_logs,
            "containers_list": self.containers_list,
            "health_probe": self.health_probe,
            "foreign_list": self.foreign_list,
            "foreign_stop": self.foreign_stop,
            "weights_present": self.weights_present,
            "weights_inventory": self.weights_inventory,
            "weights_delete": self.weights_delete,
            "download": self.download,
            "verify": self.verify,
            "sync": self.sync,
            "ssh_check": self.ssh_check,
            "task_status": self.task_status,
            "task_cancel": self.task_cancel,
            "tasks_list": self.tasks_list,
            "link_test": self.link_test,
            "rdma_facts": self.rdma_facts,
            "reclaim_memory": self.reclaim_memory,
            "headless_status": self.headless_status,
            "headless_apply": self.headless_apply,
            "mods_list": self.mods_list,
            "mods_status": self.mods_status,
            "mods_install": self.mods_install,
            "mods_remove": self.mods_remove,
        }

    @property
    def dry_run(self) -> bool:
        return isinstance(self.runtime, DryRunRuntime)

    @property
    def rt(self):
        return self.config.runtime

    async def dispatch(self, action: str, params: dict) -> Any:
        fn = self.registry.get(action)
        if fn is None:
            raise ActionError(f"unknown action: {action}", status=400)
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(params)
            # docker / filesystem calls block — keep them off the event loop
            return await asyncio.to_thread(fn, params)
        except RuntimeError_ as exc:
            raise ActionError(str(exc), exc.excerpt, status=500) from exc
        except (mods.ModError, FileNotFoundError) as exc:
            raise ActionError(str(exc)) from exc
        except PrivdUnavailable as exc:
            raise ActionError(str(exc), status=503) from exc
        except (RuntimeError, OSError) as exc:        # privd op failures, filesystem errors
            raise ActionError(str(exc)[:2000], status=500) from exc

    # ---- facts ------------------------------------------------------------
    def hardware_facts(self, params: dict) -> dict:
        mem = sysinfo.memory_snapshot()
        host = platform.uname()
        facts: dict[str, Any] = {
            "node_id": self.config.node.node_id,
            "hostname": host.node,
            "arch": host.machine,
            "kernel": host.release,
            "cpu_count": os.cpu_count(),
            "runtime_mode": self.config.runtime_mode,
            "agent_version": __version__,
            **mem,
            **sysinfo.nvidia_facts(),
            **sysinfo.desktop_facts(),
            "swappiness": sysinfo.swappiness(),
            "hf_cache_dir": self.rt.hf_cache_dir,
            "mods_dir": self.rt.mods_dir,
            "disk_free_gib": sysinfo.disk_free_gib(self.rt.hf_cache_dir),
            "privd_available": self.privd.available(),
            "rdma_active": [d["hca"] for d in sysinfo.rdma_devices() if d["active"]],
            "infiniband_dev": os.path.isdir("/dev/infiniband"),
        }
        if not self.dry_run:
            facts["docker"] = self.runtime.ping()
        try:
            facts["foreign_inference"] = self.runtime.list_foreign()
        except RuntimeError_:
            facts["foreign_inference"] = []
        return facts

    def memory_telemetry(self, params: dict) -> dict:
        return sysinfo.memory_snapshot()

    def preflight(self, params: dict) -> dict:
        """Read-only checks before a switch. Port check is real even in dry-run."""
        rt = self.rt
        problems: list[str] = []
        warnings: list[str] = []
        owned_ok = params.get("allow_owned_port", False)
        if port_in_use(rt.vllm_port) and not (owned_ok and any(
                c.status == "running" for c in self.runtime.list_owned())):
            problems.append(f"port {rt.vllm_port} is already in use by a process TwinSpark "
                            f"does not manage (existing vLLM?)")
        for d in (rt.hf_cache_dir, rt.compile_cache_dir):
            p = Path(d)
            if p.exists() and not os.access(p, os.W_OK):
                problems.append(f"{d} is not writable by the agent")
        free_gib = sysinfo.disk_free_gib(rt.hf_cache_dir)
        need = float(params.get("need_disk_gib", 0))
        if need and free_gib < need:
            problems.append(f"only {free_gib:.0f} GiB free on disk, need ~{need:.0f} GiB")
        names = [str(m) for m in params.get("mods") or []]
        if names:
            st = mods.mods_status(rt.mods_dir, names)
            missing = [n for n, s in st.items() if not s["present"]]
            if missing:
                problems.append(f"mod(s) not installed on node {self.config.node.node_id}: "
                                f"{', '.join(missing)} — install via `tsm mods install`")
        if params.get("need_rdma") and not self.dry_run:
            if not os.path.isdir("/dev/infiniband"):
                problems.append("/dev/infiniband is missing — RoCE is not available for NCCL")
            active = {d["hca"] for d in sysinfo.rdma_devices() if d["active"]}
            for h in params.get("hcas") or []:
                if h not in active:
                    problems.append(f"RDMA device {h} is not ACTIVE on node {self.config.node.node_id}")
        if not self.dry_run:
            ping = self.runtime.ping()
            if not ping.get("ok"):
                problems.append(f"docker is not usable by the agent: {ping.get('error')}")
        desk = sysinfo.desktop_facts()
        if desk["desktop_running"]:
            warnings.append(f"graphical desktop is running ({desk['desktop_rss_gib']:.1f} GiB) — "
                            f"headless mode frees that for the KV cache")
        return {"ok": not problems, "problems": problems, "warnings": warnings,
                "disk_free_gib": free_gib}

    # ---- images ------------------------------------------------------------
    def image_ensure(self, params: dict) -> dict:
        ref = str(params.get("image_ref", ""))
        if not immutable_image_ref(ref):
            raise ActionError("image_ref must be pinned by digest")
        if self.runtime.image_present(ref):
            return {"present": True, "pulled": False}
        if ref.startswith("sha256:") or not self.rt.allow_image_pull:
            raise ActionError(f"image is not cached and pulling is disabled: {ref}")
        self.runtime.pull(ref)
        return {"present": True, "pulled": True}

    def image_inspect(self, params: dict) -> dict:
        ref = str(params.get("ref", ""))
        if not _IMAGE_REF_RE.match(ref):
            raise ActionError("invalid image reference")
        info = self.runtime.image_inspect(ref)
        if info is None:
            return {"present": False, "ref": ref}
        return {"present": True, "ref": ref, **info}

    # ---- containers --------------------------------------------------------
    def container_start(self, params: dict) -> dict:
        spec = ContainerSpec.model_validate(params.get("spec") or {})
        node = self.config.node.node_id
        if spec.node != node:
            raise ActionError(f"spec is for node {spec.node}, this is {node}")
        if not _NAME_RE.match(spec.name) or spec.labels.get(OWNER_LABEL) != "true":
            raise ActionError("container must be tsm-* and carry the ownership label")
        rt = self.rt
        rw_roots = [Path(rt.hf_cache_dir).resolve(), Path(rt.compile_cache_dir).resolve()]
        mods_root = Path(rt.mods_dir).resolve()
        for m in spec.mounts:
            host = Path(m.host).resolve()

            def inside(r: Path, host: Path = host) -> bool:
                return host == r or r in host.parents

            if inside(mods_root):
                if not m.read_only:
                    raise ActionError(f"mods must be mounted read-only: {m.host}")
            elif not any(inside(r) for r in rw_roots):
                raise ActionError(f"mount outside allowed roots: {m.host}")
            elif not self.dry_run:
                host.mkdir(parents=True, exist_ok=True)
        # node-local policy: the controller cannot escalate beyond what this node allows
        if spec.privileged and rt.container_mode != "privileged":
            raise ActionError("privileged containers are not allowed by this node's "
                              "runtime.container_mode")
        if set(spec.devices) - ALLOWED_DEVICES or set(spec.cap_add) - ALLOWED_CAPS:
            raise ActionError("device or capability not allowed")
        if spec.devices and not self.dry_run:
            missing = [d for d in spec.devices if not os.path.exists(d)]
            if missing:
                raise ActionError(f"device(s) missing on node {node}: {missing}")
        if spec.mods:
            st = mods.mods_status(rt.mods_dir, spec.mods)
            missing = [n for n, s in st.items() if not s["present"]]
            if missing:
                raise ActionError(f"mod(s) not installed on node {node}: {', '.join(missing)}")
            if not any(m.container == CONTAINER_MODS for m in spec.mounts):
                raise ActionError("mods requested but the mods directory is not mounted")
        # resolve ${secret:...} placeholders locally — secrets never travel in plans
        overrides: dict[str, str] = {}
        for k, v in spec.env.items():
            m = _SECRET_REF.match(v)
            if m:
                val = self.vault.get(m.group(1))
                if not val:
                    raise ActionError(f"secret slot '{m.group(1)}' is empty on node {node}")
                overrides[k] = val
        # a crashed earlier attempt of the same revision leaves an exited container
        self.runtime.remove_exited_owned(spec.name)
        cid = self.runtime.start(spec, overrides)
        return {"container": spec.name, "id": cid, "dry_run": self.dry_run}

    def containers_stop_owned(self, params: dict) -> dict:
        keep = params.get("keep_revision")
        stopped = []
        for c in self.runtime.list_owned():
            if keep and c.labels.get("org.twinspark.revision") == keep:
                continue
            self.runtime.stop(c.name, int(params.get("grace_s", 30)))
            stopped.append(c.name)
        return {"stopped": stopped}

    def containers_list(self, params: dict) -> dict:
        owned = [{"name": c.name, "status": c.status, "exit_code": c.exit_code,
                  "revision": c.labels.get("org.twinspark.revision"),
                  "model": c.labels.get("org.twinspark.model"),
                  "role": c.labels.get("org.twinspark.role"), "started_at": c.started_at,
                  "oom_killed": c.oom_killed}
                 for c in self.runtime.list_owned()]
        return {"owned": owned, "foreign": self.runtime.list_foreign()}

    def container_state(self, params: dict) -> dict:
        st = self.runtime.state(self._name(params))
        return {"name": st.name, "status": st.status, "exit_code": st.exit_code,
                "oom_killed": st.oom_killed, "started_at": st.started_at}

    def container_logs(self, params: dict) -> dict:
        tail = max(1, min(int(params.get("tail", 200)), 20000))
        return {"log": self.runtime.logs(self._name(params), tail)}

    async def health_probe(self, params: dict) -> dict:
        """One non-blocking probe. The controller loops with its own deadline."""
        name = self._name(params)
        st = await asyncio.to_thread(self.runtime.state, name)
        if st.status not in ("running", "created", "restarting"):
            log = await asyncio.to_thread(self.runtime.logs, name, 120)
            return {"status": st.status, "healthy": False, "exited": True,
                    "exit_code": st.exit_code, "oom_killed": st.oom_killed,
                    "log_tail": log[-4000:]}
        url = params.get("url")
        if not url:                                   # worker containers have no API
            return {"status": st.status, "healthy": True, "exited": False}
        if self.dry_run:
            return {"status": st.status, "healthy": True, "exited": False}
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{url}/health")
            return {"status": st.status, "healthy": r.status_code == 200, "exited": False}
        except httpx.HTTPError:
            return {"status": st.status, "healthy": False, "exited": False}

    def foreign_list(self, params: dict) -> dict:
        return {"foreign": self.runtime.list_foreign()}

    def foreign_stop(self, params: dict) -> dict:
        name = str(params.get("name", ""))
        if not _FOREIGN_RE.match(name) or params.get("confirm") != name:
            raise ActionError("foreign_stop needs the exact container name twice (name + confirm)")
        self.runtime.stop_foreign(name, int(params.get("grace_s", 60)))
        return {"stopped": name}

    # ---- weights ------------------------------------------------------------
    def weights_present(self, params: dict) -> dict:
        repo, rev = self._repo_rev(params)
        include = self._globs(params.get("include"))
        snap = hf_repo_dir(self.rt.hf_cache_dir, repo) / "snapshots" / rev
        if self.dry_run:
            return {"present": True, "path": str(snap), "dry_run": True}
        return {"present": snapshot_complete(snap, include), "path": str(snap)}

    def weights_inventory(self, params: dict) -> dict:
        inv = scan_cache(self.rt.hf_cache_dir)
        in_use = []
        for c in self.runtime.list_owned():
            if c.status == "running" and c.labels.get("org.twinspark.model"):
                in_use.append({"repo": c.labels["org.twinspark.model"],
                               "revision": c.labels.get("org.twinspark.model_revision"),
                               "container": c.name})
        inv["in_use"] = in_use
        inv["node_id"] = self.config.node.node_id
        inv["downloads"] = self.tasks.running("download")
        return inv

    def weights_delete(self, params: dict) -> dict:
        repo = str(params.get("repo", ""))
        rev = params.get("revision")
        if not _REPO_RE.match(repo) or (rev is not None and not _SHA_RE.match(str(rev))):
            raise ActionError("need repo 'org/name' and optionally a 40-char revision sha")
        for c in self.runtime.list_owned():
            if c.status == "running" and c.labels.get("org.twinspark.model") == repo and \
                    (rev is None or c.labels.get("org.twinspark.model_revision") == rev):
                raise ActionError(f"{repo} is in use by running container {c.name}", status=409)
        for t in self.tasks.running():
            if t.get("repo") == repo:
                raise ActionError(f"{repo} has a running {t['kind']} task", status=409)
        plan = plan_delete(self.rt.hf_cache_dir, repo, rev)
        if params.get("preview") or self.dry_run:
            return {"preview": True, "dry_run": self.dry_run, **plan}
        return execute_delete(plan, self.rt.hf_cache_dir)

    async def download(self, params: dict) -> dict:
        repo, rev = self._repo_rev(params)
        include = self._globs(params.get("include"))
        verify = bool(params.get("verify", self.rt.verify_hashes))
        if self.dry_run:
            async def fake(tid):
                self.tasks.update(tid, detail=f"[dry-run] would download {repo}@{rev}")
                return {"dry_run": True}
            return self.tasks.spawn("download", fake, repo=repo, revision=rev)
        existing = [t for t in self.tasks.running("download")
                    if t.get("repo") == repo and t.get("revision") == rev]
        if existing:
            return {"task_id": existing[0]["task_id"], "joined": True}
        token = self.vault.get("hf_token") or None

        async def run(tid):
            return await transfers.run_download(self.tasks, tid, self.rt, repo, rev, include,
                                                token, verify, downloader=self.downloader)
        return self.tasks.spawn("download", run, repo=repo, revision=rev)

    async def verify(self, params: dict) -> dict:
        repo, rev = self._repo_rev(params)
        if self.dry_run:
            async def fake(tid):
                return {"dry_run": True}
            return self.tasks.spawn("verify", fake, repo=repo, revision=rev)

        async def run(tid):
            return await transfers.run_verify(self.tasks, tid, self.rt, repo, rev)
        return self.tasks.spawn("verify", run, repo=repo, revision=rev)

    async def sync(self, params: dict) -> dict:
        repo, rev = self._repo_rev(params)
        host, user = str(params.get("target_host", "")), str(params.get("ssh_user", ""))
        dst = str(params.get("target_hf_home") or self.rt.hf_cache_dir)
        if not _HOST_RE.match(host) or not _USER_RE.match(user):
            raise ActionError("sync needs a valid target_host and ssh_user")
        if not _PATH_RE.match(dst) or ".." in dst:
            raise ActionError("target_hf_home must be an absolute path")
        include = self._globs(params.get("include"))
        streams = int(params.get("streams") or self.rt.sync_streams)
        streams = max(1, min(streams, 16))

        async def run(tid):
            return await transfers.run_sync(self.tasks, tid, self.rt, repo, rev, include,
                                            host, user, dst, streams, self.dry_run)
        return self.tasks.spawn("sync", run, repo=repo, revision=rev, target=host)

    def ssh_check(self, params: dict) -> dict:
        host, user = str(params.get("target_host", "")), str(params.get("ssh_user", ""))
        if not _HOST_RE.match(host) or not _USER_RE.match(user):
            raise ActionError("ssh_check needs a valid target_host and ssh_user")
        argv = transfers.ssh_command(self.rt) + [f"{user}@{host}", "true"]
        if self.dry_run:
            return {"ok": True, "dry_run": True, "argv": argv}
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": p.returncode == 0, "error": p.stderr.strip()[-400:] if p.returncode else None}

    def task_status(self, params: dict) -> dict:
        t = self.tasks.status(str(params.get("task_id")))
        if t is None:
            raise ActionError("unknown task (agent restarted?)", status=404)
        return t

    def task_cancel(self, params: dict) -> dict:
        return {"cancelled": self.tasks.cancel(str(params.get("task_id")))}

    def tasks_list(self, params: dict) -> dict:
        return {"tasks": [self.tasks.status(t) for t in list(self.tasks.tasks)][-50:]}

    # ---- link test / RDMA ----------------------------------------------------------
    async def link_test(self, params: dict) -> dict:
        role = str(params.get("role", "initiator"))
        mode = str(params.get("mode", "tcp"))
        peer = str(params.get("peer_ip", ""))
        port = int(params.get("port", 29511))
        duration = float(params.get("duration_s", 5))
        streams = int(params.get("streams", 4))
        hcas = [str(h) for h in params.get("hcas") or []]
        gid = params.get("gid_index")
        if role not in ("initiator", "responder") or mode not in ("tcp", "rdma"):
            raise ActionError("role must be initiator|responder and mode tcp|rdma")
        if not (1024 < port < 65000) or not (0 < duration <= 60) or not (1 <= streams <= 16):
            raise ActionError("link_test needs port 1025-64999, duration_s <= 60, streams 1-16")
        if role == "initiator" and not _HOST_RE.match(peer):
            raise ActionError("initiator link_test needs a valid peer_ip")
        if any(not re.match(r"^[A-Za-z0-9._-]{1,32}$", h) for h in hcas):
            raise ActionError("invalid RDMA device name")
        if self.dry_run:
            async def fake(tid):
                return linktest.simulated(mode, role, streams)
            return self.tasks.spawn("link", fake)

        async def run(tid):
            if mode == "tcp":
                if role == "responder":
                    return await linktest.tcp_responder(params.get("bind") or "0.0.0.0", port,
                                                        duration + 20)
                return await linktest.tcp_initiator(peer, port, duration, streams)
            return await linktest.rdma_run(hcas, None if gid is None else int(gid), port, duration,
                                           peer if role == "initiator" else None,
                                           register=lambda p: self.tasks.register_proc(tid, p))
        return self.tasks.spawn("link", run)

    def rdma_facts(self, params: dict) -> dict:
        devices = sysinfo.rdma_devices()
        return {"devices": devices,
                "suggestion": sysinfo.suggest_rdma(devices, params.get("qsfp_ip")),
                "perftest": linktest.perftest_available()}

    # ---- memory hygiene / headless (privd) -----------------------------------------------
    def reclaim_memory(self, params: dict) -> dict:
        before = sysinfo.memory_snapshot()
        if self.dry_run:
            return {"dropped": False, "dry_run": True, "before": before, "after": before}
        result: dict[str, Any] = {"before": before}
        if params.get("drop_caches", True):
            if self.privd.available():
                result["privd"] = self.privd.call("drop_caches")
                result["dropped"] = True
            else:
                result["dropped"] = False
                result["note"] = ("tsm-privd not available — page cache was not dropped; "
                                  "install twinspark-privd.service")
        result["after"] = sysinfo.memory_snapshot()
        return result

    def headless_status(self, params: dict) -> dict:
        out = {"desktop": sysinfo.desktop_facts(), "privd_available": self.privd.available()}
        if out["privd_available"] and not self.dry_run:
            try:
                out["privd"] = self.privd.call("status")
            except (PrivdUnavailable, RuntimeError) as exc:
                out["privd_error"] = str(exc)
        return out

    def headless_apply(self, params: dict) -> dict:
        from ..headless import apply_mode
        try:
            mode = HeadlessMode(str(params.get("mode", "")))
        except ValueError:
            raise ActionError("mode must be desktop, headless-safe or headless-max") from None
        now = bool(params.get("now", False))
        if not self.dry_run and not self.privd.available():
            raise PrivdUnavailable("tsm-privd is not running on node "
                                   f"{self.config.node.node_id}; install twinspark-privd.service")
        return apply_mode(mode, self.privd, now=now, dry_run=self.dry_run)

    # ---- mods --------------------------------------------------------------------------
    def mods_list(self, params: dict) -> dict:
        return {"mods": mods.list_mods(self.rt.mods_dir), "mods_dir": self.rt.mods_dir}

    def mods_status(self, params: dict) -> dict:
        names = [str(n) for n in params.get("names") or []]
        return {"status": mods.mods_status(self.rt.mods_dir, names)}

    def mods_install(self, params: dict) -> dict:
        return mods.install_mod(self.rt.mods_dir, str(params.get("name", "")),
                                str(params.get("archive_b64", "")))

    def mods_remove(self, params: dict) -> dict:
        return {"removed": mods.remove_mod(self.rt.mods_dir, str(params.get("name", "")))}

    # ---- internals ----------------------------------------------------------
    def _name(self, params: dict) -> str:
        name = str(params.get("name", ""))
        if not _NAME_RE.match(name):
            raise ActionError("invalid container name")
        return name

    def _repo_rev(self, params: dict) -> tuple[str, str]:
        repo, rev = str(params.get("repo", "")), str(params.get("revision", ""))
        if not _REPO_RE.match(repo) or not _SHA_RE.match(rev):
            raise ActionError("need repo 'org/name' and a 40-char revision sha")
        return repo, rev

    @staticmethod
    def _globs(value) -> list[str]:
        out = [str(v) for v in (value or [])]
        for g in out:
            if not _GLOB_RE.match(g) or ".." in g or g.startswith("/"):
                raise ActionError(f"invalid include pattern {g!r}")
        return out

    # kept for API compatibility with 0.3 callers
    def privd_path(self) -> str:
        return self.rt.privd_socket
