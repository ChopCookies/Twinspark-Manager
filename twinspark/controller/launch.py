"""Launch planner: revision -> exact per-node container specs (spec §2.4, §12-§14).

This is the piece that turns a saved profile into the concrete ``docker run`` +
``vllm serve`` invocations on each Spark. It is a pure function of
(revision, cluster config) so it can be inspected safely with ``tsm plan``
before anything is started.

Container wiring mirrors the setup that is proven on GB10 pairs
(eugr/spark-vllm-docker): host network + host IPC, RoCE devices for NCCL
(``/dev/infiniband`` + IPC_LOCK, or ``--privileged``), a huge nofile limit, the
NCCL/Gloo/UCX interface variables, ``NCCL_IB_HCA`` + GID index, persistent JIT
caches, and optional *mods* (patch directories with a ``run.sh``) applied inside
the container right before ``exec vllm serve``.

Multi-node wiring
-----------------
* ``native`` (default for ``auto``/``mp``): vLLM's own multi-node launcher —
  ``--nnodes 2 --node-rank {0,1} --master-addr <A qsfp ip>``, worker runs with
  ``--headless``. The worker (rank 1) is started first, then the head, like the
  community launchers do.
* ``ray``: Ray head on A, Ray worker on B, ``vllm serve`` on A with
  ``--distributed-executor-backend ray`` once both Ray nodes are alive.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..schemas.config import ControllerConfig
from ..schemas.enums import DistributedBackend, Topology
from ..schemas.profile import ProfileRevision, flag_base

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
_MULTI = (Topology.TP2, Topology.PP2, Topology.TP_EP)


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
        # Windows may expose a user's temp directory through an 8.3 name such as DANNYG~1.
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


class LaunchPlan(BaseModel):
    revision_id: str
    backend: DistributedBackend
    served_model_name: str
    containers: list[ContainerSpec]
    routes: dict[str, list[str]]                     # alias -> backend base URLs
    route_models: dict[str, str] = Field(default_factory=dict)
    node_utilization: dict[str, float] = Field(default_factory=dict)
    model_requirements: list[dict[str, Any]] = Field(default_factory=list)
    start_order: list[list[str]]                     # waves of container names
    wave_delays_s: list[float] = Field(default_factory=list)   # pause AFTER each wave
    gpu_memory_utilization: float
    mods: list[str] = Field(default_factory=list)
    model_repo: str = ""
    model_revision: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def nodes(self) -> list[str]:
        return sorted({c.node for c in self.containers})

    def rendered_commands(self, docker_bin: str = "docker") -> dict[str, str]:
        """Human-readable commands for the transparency view (secrets redacted)."""
        return {
            c.name: shlex.join(docker_run_argv(c, docker_bin, redact=True)) for c in self.containers
        }


class LaunchError(ValueError):
    pass


# ----------------------------------------------------------------------------
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


def _flag(argv: list[str], name: str, value: Any) -> None:
    """Append one CLI flag in vLLM's conventions."""
    if value is None:
        return
    if value is True:
        argv.append(f"--{name}")
    elif value is False:
        argv.append(f"--no-{name}")
    elif isinstance(value, (dict, list)):
        argv += [f"--{name}", json.dumps(value, separators=(",", ":"))]
    else:
        argv += [f"--{name}", str(value)]


def mods_wrapper(mods: list[str], vllm_argv: list[str]) -> list[str]:
    """``bash -c`` script: apply each mod like eugr's launcher does, then exec vLLM.

    Mods are copied from the read-only mount to a writable work dir, ``run.sh``
    runs with ``WORKSPACE_DIR`` = the image's working directory, and any failure
    aborts the container (non-zero exit -> activation fails with the log tail).
    """
    lines = ["set -euo pipefail", 'export WORKSPACE_DIR="$PWD"']
    for m in mods:
        q = shlex.quote(m)
        lines += [
            f"echo '[twinspark] applying mod {m}'",
            f"mkdir -p {CONTAINER_MOD_WORKDIR}/{q}",
            f"cp -a {CONTAINER_MODS}/{q}/. {CONTAINER_MOD_WORKDIR}/{q}/",
            f"( cd {CONTAINER_MOD_WORKDIR}/{q} && chmod +x run.sh && ./run.sh )",
        ]
    lines.append("echo '[twinspark] mods applied, starting vLLM'")
    lines.append("exec " + shlex.join(vllm_argv))
    return ["bash", "-c", "\n".join(lines)]


# ----------------------------------------------------------------------------
class LaunchPlanner:
    """Pure planner. ``gpu_memory_utilization`` comes from the memory planner."""

    def __init__(self, config: ControllerConfig):
        self.config = config

    def resolve_backend(self, rev: ProfileRevision) -> DistributedBackend:
        b = rev.draft.distributed_backend
        if b in (DistributedBackend.AUTO, DistributedBackend.MP):
            return DistributedBackend.NATIVE   # "mp" is the native process-group path
        return b

    def plan(self, rev: ProfileRevision, gpu_memory_utilization: float,
             backend_api_key_set: bool = True,
             node_utilization: Optional[dict[str, float]] = None) -> LaunchPlan:
        if rev.draft.secondary:
            plans = [self.plan(part, (node_utilization or {}).get(node) or
                               part.draft.advanced.gpu_memory_utilization or gpu_memory_utilization,
                               backend_api_key_set)
                     for node, part in zip(("A", "B"), rev.parts(), strict=True)]
            containers = [c for p in plans for c in p.containers]
            for c in containers:
                c.name = f"tsm-{rev.profile_name}-{c.node.lower()}-{rev.revision_id.rsplit('-', 1)[-1]}"
                c.labels["org.twinspark.profile"] = rev.profile_name
            return LaunchPlan(
                revision_id=rev.revision_id, backend=DistributedBackend.NATIVE,
                served_model_name=plans[0].served_model_name,
                containers=containers, routes={a: u for p in plans for a, u in p.routes.items()},
                route_models={a: m for p in plans for a, m in p.route_models.items()},
                start_order=[[c.name for c in containers]], wave_delays_s=[0.0],
                gpu_memory_utilization=plans[0].gpu_memory_utilization,
                node_utilization={n: u for p in plans for n, u in p.node_utilization.items()},
                model_requirements=[m for p in plans for m in p.model_requirements],
                mods=list(dict.fromkeys(m for p in plans for m in p.mods)),
                model_repo=rev.identity.model_repo, model_revision=rev.identity.model_revision,
                notes=["split: independent models on A and B, routed by separate aliases"] +
                      [f"node {p.nodes[0]}: {note}" for p in plans for note in p.notes],
            )
        cfg, rt = self.config, self.config.runtime
        simple, topo = rev.draft.simple, rev.draft.simple.topology
        adv = rev.draft.advanced
        backend = self.resolve_backend(rev)
        notes: list[str] = [
            "Verify every flag against the pinned image once before the first activation: vLLM CLI flags "
            "change between releases.",
        ]

        if gpu_memory_utilization < 0.5:
            notes.append(f"WARNING: gpu_memory_utilization {gpu_memory_utilization:.2f} is very low — "
                         f"the node reports little total memory, or the reserve is too large")
        for n in rev.required_nodes():
            if n not in cfg.nodes:
                raise LaunchError(f"topology {topo.value} needs node {n}, which is not configured")
        multi = topo in _MULTI
        if multi:
            if not (cfg.nodes["A"].qsfp_ip and cfg.nodes["B"].qsfp_ip):
                raise LaunchError("multi-node topologies need qsfp_ip for node A and B")
            for n in ("A", "B"):
                if not cfg.nodes[n].rdma_hcas:
                    notes.append(f"WARNING: nodes.{n}.rdma_hcas is empty — NCCL will fall back to "
                                 f"TCP sockets over the QSFP link (much slower). Run `tsm rdma`.")

        if simple.tool_calling and not rev.draft.behaviour.tool_call_parser:
            raise LaunchError("tool calling is enabled but no tool_call_parser is set")

        served = rev.profile_name
        base_args = self._vllm_args(rev, served, gpu_memory_utilization, notes)
        short = rev.revision_id.rsplit("-", 1)[-1]
        labels = {
            OWNER_LABEL: "true",
            "org.twinspark.profile": rev.profile_name,
            "org.twinspark.revision": rev.revision_id,
            "org.twinspark.model": rev.identity.model_repo,
            "org.twinspark.model_revision": rev.identity.model_revision,
        }
        mounts = self._mounts(adv.mods)
        if adv.mods:
            notes.append(f"mods applied in order before vLLM starts: {', '.join(adv.mods)}")

        containers: list[ContainerSpec] = []
        routes: dict[str, list[str]] = {}
        order: list[list[str]] = []
        delays: list[float] = []

        def api_host(node: str) -> str:
            # vLLM on A only needs to be reachable by the local gateway. On B it must be
            # reachable from A — over the direct link, never the LAN.
            if node == "A":
                return "127.0.0.1"
            ip = cfg.nodes["B"].qsfp_ip
            if not ip:
                raise LaunchError("serving from node B needs nodes.B.qsfp_ip")
            return ip

        def serve_args(node: str) -> list[str]:
            return base_args + ["--host", api_host(node), "--port", str(rt.vllm_port)]

        def make(node: str, role: str, name: str, argv: list[str], health: Optional[str],
                 rdma: bool) -> ContainerSpec:
            command = mods_wrapper(adv.mods, argv) if adv.mods else argv
            return ContainerSpec(
                node=node, role=role, name=name, image_ref=rev.identity.image_ref,
                labels={**labels, "org.twinspark.role": role},
                env=self._env(node, adv.env, multi=rdma), mounts=mounts,
                command=command, health_url=health, shm_size=rt.shm_size,
                privileged=rdma and rt.container_mode == "privileged",
                devices=["/dev/infiniband"] if rdma and rt.container_mode == "rdma" else [],
                cap_add=["IPC_LOCK"] if rdma and rt.container_mode == "rdma" else [],
                nofile_limit=rt.nofile_limit, mods=list(adv.mods),
            )

        if topo in (Topology.SINGLE_A, Topology.SINGLE_B, Topology.REPLICATED):
            nodes = ["A", "B"] if topo == Topology.REPLICATED else rev.required_nodes()
            for n in nodes:
                name = f"tsm-{rev.profile_name}-{n.lower()}-{short}"
                containers.append(make(n, "single", name,
                                       serve_args(n) + ["--tensor-parallel-size", "1"],
                                       f"http://{api_host(n)}:{rt.vllm_port}", rdma=False))
            order.append([c.name for c in containers])
            delays.append(0.0)
            for alias in simple.aliases:
                routes[alias] = [c.health_url for c in containers if c.health_url]
            if topo == Topology.REPLICATED:
                notes.append("replicated: two independent copies, gateway round-robins requests")
        else:
            tp, pp = (1, 2) if topo == Topology.PP2 else (2, 1)
            par = ["--tensor-parallel-size", str(tp), "--pipeline-parallel-size", str(pp)]
            if topo == Topology.TP_EP and not self._has_flag(rev, "enable-expert-parallel"):
                par.append("--enable-expert-parallel")
            a_ip = cfg.nodes["A"].qsfp_ip
            head_name = f"tsm-{rev.profile_name}-a-{short}"
            worker_name = f"tsm-{rev.profile_name}-b-{short}"
            head_url = f"http://{api_host('A')}:{rt.vllm_port}"

            if backend == DistributedBackend.NATIVE:
                dist = ["--distributed-executor-backend", "mp", "--nnodes", "2",
                        "--master-addr", a_ip, "--master-port", str(rt.master_port)]
                head_cmd = serve_args("A") + par + dist + ["--node-rank", "0"]
                # the worker must use the same model/parallel args, no API server
                worker_cmd = base_args + par + dist + ["--node-rank", "1", "--headless"]
                # worker first (it waits for the rendezvous), then the head — the
                # launch order every published dual-Spark recipe uses
                order += [[worker_name], [head_name]]
                delays += [max(2.0, adv.head_start_delay_s), 0.0]
            else:  # RAY
                rp = rt.ray_port
                wait_py = (
                    "import ray,time; ray.init(address='auto')\n"
                    "while sum(1 for n in ray.nodes() if n['Alive']) < 2: time.sleep(2)"
                )
                head_serve = serve_args("A") + par + ["--distributed-executor-backend", "ray"]
                head_script = (
                    f"ray start --head --node-ip-address={shlex.quote(a_ip)} --port={rp} "
                    f"&& python3 -c {shlex.quote(wait_py)} && exec {shlex.join(head_serve)}"
                )
                b_ip = cfg.nodes["B"].qsfp_ip
                worker_script = (
                    f"ray start --address={shlex.quote(a_ip)}:{rp} "
                    f"--node-ip-address={shlex.quote(b_ip)} --block"
                )
                head_cmd = ["bash", "-c", head_script]
                worker_cmd = ["bash", "-c", worker_script]
                order += [[head_name], [worker_name]]    # Ray head must exist first
                delays += [5.0, 0.0]
                notes.append("ray backend: needs an image that ships Ray (NGC >= 26.04 does not)")
                if adv.mods:
                    raise LaunchError("mods are only supported with the native (mp) backend")

            containers.append(make("A", "head", head_name, head_cmd, head_url, rdma=True))
            containers.append(make("B", "worker", worker_name, worker_cmd, None, rdma=True))
            for alias in simple.aliases:
                routes[alias] = [head_url]

        if backend_api_key_set:
            for c in containers:
                if c.health_url:
                    # the agent injects the real value from its vault; never stored in plans
                    c.env["VLLM_API_KEY"] = "${secret:backend_api_key}"

        return LaunchPlan(
            revision_id=rev.revision_id, backend=backend, served_model_name=served,
            containers=containers, routes=routes, start_order=order, wave_delays_s=delays,
            route_models={alias: served for alias in routes},
            node_utilization={n: gpu_memory_utilization for n in rev.required_nodes()},
            model_requirements=[{"repo": rev.identity.model_repo,
                                 "revision": rev.identity.model_revision,
                                 "include": list(adv.download_include), "nodes": rev.required_nodes()}] +
                               [{"repo": ref.split("@", 1)[0], "revision": ref.partition("@")[2],
                                 "include": [], "nodes": rev.required_nodes()} for ref in adv.extra_models],
            gpu_memory_utilization=gpu_memory_utilization, mods=list(adv.mods),
            model_repo=rev.identity.model_repo, model_revision=rev.identity.model_revision,
            notes=notes,
        )

    # ------------------------------------------------------------------
    def _mounts(self, mods: list[str]) -> list[Mount]:
        rt = self.config.runtime
        try:
            mounts = [Mount(host=rt.hf_cache_dir, container=CONTAINER_HF_HOME)]
            if rt.cache_mounts:
                base = rt.compile_cache_dir.rstrip("/")
                mounts += [Mount(host=f"{base}/{sub}", container=path)
                           for sub, path in CACHE_MOUNTS.items()]
            else:
                mounts.append(Mount(host=rt.compile_cache_dir, container=CONTAINER_COMPILE_CACHE))
            if mods:
                mounts.append(Mount(host=rt.mods_dir, container=CONTAINER_MODS, read_only=True))
            return mounts
        except ValidationError as exc:
            raise LaunchError("invalid runtime mount paths: cache and mods directories must be plain absolute paths") \
                from exc

    def _env(self, node: str, profile_env: dict[str, str], multi: bool) -> dict[str, str]:
        """defaults < node wiring < profile env (explicit recipe values win)."""
        rt = self.config.runtime
        env: dict[str, str] = {
            "HF_HOME": CONTAINER_HF_HOME,
            "HF_HUB_OFFLINE": "1",                 # weights are pre-staged; never download at start
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "NCCL_IGNORE_CPU_AFFINITY": "1",
        }
        if not rt.cache_mounts:
            env["VLLM_CACHE_ROOT"] = CONTAINER_COMPILE_CACHE
            env["TRITON_CACHE_DIR"] = f"{CONTAINER_COMPILE_CACHE}/triton"
        env.update(self._node_env(node, multi))
        env.update(profile_env)
        return env

    def _node_env(self, node: str, multi: bool) -> dict[str, str]:
        ep = self.config.nodes.get(node)
        env: dict[str, str] = {}
        if not ep:
            return env
        if ep.qsfp_iface:
            for var in ("NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME", "TP_SOCKET_IFNAME",
                        "UCX_NET_DEVICES", "OMPI_MCA_btl_tcp_if_include", "MN_IF_NAME"):
                env[var] = ep.qsfp_iface
        if ep.qsfp_ip:
            env["VLLM_HOST_IP"] = ep.qsfp_ip
        if multi and ep.rdma_hcas:
            env["NCCL_IB_HCA"] = ",".join(ep.rdma_hcas)
            env["NCCL_IB_DISABLE"] = "0"
            if ep.ib_gid_index is not None:
                env["NCCL_IB_GID_INDEX"] = str(ep.ib_gid_index)
        return env

    @staticmethod
    def _has_flag(rev: ProfileRevision, name: str) -> bool:
        a = rev.draft.advanced
        if name in a.extra_vllm_flags:
            return True
        return any(t.startswith("-") and flag_base(t) == name for t in a.extra_vllm_args)

    def _vllm_args(self, rev: ProfileRevision, served: str, util: float,
                   notes: list[str]) -> list[str]:
        s, b, a, ident = rev.draft.simple, rev.draft.behaviour, rev.draft.advanced, rev.identity
        raw_bases = {flag_base(t) for t in a.extra_vllm_args if t.startswith("-")}
        argv = ["vllm", "serve", ident.model_repo, "--revision", ident.model_revision,
                "--served-model-name", served]

        def put(name: str, value: Any) -> None:
            if value is None:
                return
            if name in raw_bases or name in a.extra_vllm_flags:
                notes.append(f"--{name} comes from the raw recipe arguments; structured value ignored")
                return
            _flag(argv, name, value)

        put("max-model-len", s.context_length)
        _flag(argv, "gpu-memory-utilization", str(util))
        if a.max_num_seqs or not a.max_num_seqs_vllm_default:
            put("max-num-seqs", a.max_num_seqs or s.concurrency)
        put("max-num-batched-tokens", a.max_num_batched_tokens)
        put("dtype", a.dtype)
        put("kv-cache-dtype", a.kv_dtype)
        put("block-size", a.block_size)
        put("attention-backend", a.attention_backend)
        put("load-format", a.weight_loader)
        put("tokenizer-mode", a.tokenizer_mode)
        put("enable-prefix-caching", a.prefix_cache)
        if a.chunked_prefill is not None:
            put("enable-chunked-prefill", a.chunked_prefill)
        if a.eager_mode:
            put("enforce-eager", True)
        if a.trust_remote_code:
            put("trust-remote-code", True)
        put("speculative-config", a.speculative_config)
        put("compilation-config", a.compilation_config)

        # --- behaviour -----------------------------------------------------
        put("reasoning-parser", b.reasoning_parser)
        if s.tool_calling:
            put("enable-auto-tool-choice", True)
            put("tool-call-parser", b.tool_call_parser)
        put("chat-template", b.chat_template)
        kwargs = dict(b.chat_template_kwargs)
        if b.manage_thinking_kwarg and b.reasoning_parser and "enable_thinking" not in kwargs:
            kwargs["enable_thinking"] = s.thinking
        if kwargs:
            put("default-chat-template-kwargs", kwargs)
        overrides = b.generation_overrides()
        if overrides:
            put("override-generation-config", overrides)

        for name, value in a.extra_vllm_flags.items():
            _flag(argv, name, value)
        argv += list(a.extra_vllm_args)
        return argv
