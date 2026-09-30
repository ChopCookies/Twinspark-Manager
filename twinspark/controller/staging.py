"""Weight staging shared by activations and stand-alone stage jobs.

Make ``repo@revision`` complete on a set of nodes with the least traffic:
reuse any node that already has it, download from the Hub only when no node
has it, then copy over the QSFP link (either direction) and optionally re-hash
on the receiving node.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, Optional

from .agent_client import AgentClient

if TYPE_CHECKING:
    from ..schemas.config import ControllerConfig
    from ..schemas.job import Job, JobStep


class StageError(RuntimeError):
    pass


class WeightsStager:
    def __init__(self, config: "ControllerConfig", agents: dict[str, AgentClient],
                 persist: Callable[["Job"], None], poll: float = 2.0,
                 dry_run: bool = False):
        self.config = config
        self.agents = agents
        self.persist = persist
        self.poll = poll
        self.dry_run = dry_run
        self._hf_home: dict[str, str] = {}

    def agent(self, node: str) -> AgentClient:
        if node not in self.agents:
            raise StageError(f"node {node} is not configured / agent unreachable")
        return self.agents[node]

    async def present(self, node: str, repo: str, rev: str) -> bool:
        return bool((await self.agent(node).call("weights_present", repo=repo, revision=rev))["present"])

    async def hf_home(self, node: str) -> str:
        if node not in self._hf_home:
            facts = await self.agent(node).call("hardware_facts", timeout=30)
            self._hf_home[node] = facts.get("hf_cache_dir") or self.config.runtime.hf_cache_dir
        return self._hf_home[node]

    def _progress(self, job: "Job", step: "JobStep", prefix: str) -> Callable[[dict], None]:
        last = {"t": 0.0}

        def cb(t: dict) -> None:
            detail = t.get("detail") or f"{t.get('kind')} running"
            step.message = f"{prefix}{detail}"
            if t.get("total_bytes"):
                step.progress = min(0.99, (t.get("done_bytes") or 0) / t["total_bytes"])
            if time.monotonic() - last["t"] > 1.0:       # don't hammer SQLite
                last["t"] = time.monotonic()
                self.persist(job)
        return cb

    async def ensure(self, job: "Job", step: "JobStep", repo: str, rev: str, nodes: list[str],
                     include: Optional[list[str]] = None, download_node: Optional[str] = None,
                     verify: Optional[bool] = None,
                     cancel_check: Optional[Callable[[], bool]] = None) -> str:
        include = list(include or [])
        verify = self.config.runtime.verify_hashes if verify is None else verify
        have = {n: await self.present(n, repo, rev) for n in nodes}
        if all(have.values()):
            return f"{repo}@{rev[:8]} already on {', '.join(nodes)}"
        sources = [n for n in nodes if have[n]]
        if not sources:
            # another configured node may hold it even if it does not serve this plan
            for n in self.agents:
                if n not in nodes and await self.present(n, repo, rev):
                    sources.append(n)
        notes = []
        if not sources:
            dn = download_node if download_node in nodes else nodes[0]
            step.message = f"downloading {repo}@{rev[:8]} on node {dn}"
            self.persist(job)
            task = await self.agent(dn).call("download", repo=repo, revision=rev, include=include,
                                             verify=verify)
            await self.agent(dn).wait_task(task["task_id"], timeout=12 * 3600, poll=self.poll,
                                           on_progress=self._progress(job, step, f"node {dn}: "),
                                           cancel_check=cancel_check)
            if not await self.present(dn, repo, rev):
                raise StageError(f"download finished but the snapshot is incomplete on node {dn}")
            sources = [dn]
            have[dn] = True
            notes.append(f"downloaded on {dn}" + (" + verified" if verify else ""))
        for target in [n for n in nodes if not have[n]]:
            src = sources[0]
            ep = self.config.nodes.get(target)
            if ep is None or not (ep.sync_host and ep.ssh_user):
                raise StageError(f"node {target} lacks {repo}@{rev[:8]} and nodes.{target}.qsfp_ip/"
                                 f"ssh_user are not set, so it cannot receive a copy")
            dst = await self.hf_home(target)
            step.message = f"copying {repo}@{rev[:8]} {src} → {target} over QSFP"
            self.persist(job)
            task = await self.agent(src).call(
                "sync", repo=repo, revision=rev, include=include, target_host=ep.sync_host,
                ssh_user=ep.ssh_user, target_hf_home=dst, streams=self.config.runtime.sync_streams)
            await self.agent(src).wait_task(task["task_id"], timeout=6 * 3600, poll=self.poll,
                                            on_progress=self._progress(job, step, f"{src}→{target}: "),
                                            cancel_check=cancel_check)
            if not await self.present(target, repo, rev):
                raise StageError(f"copy to node {target} finished but the snapshot is not complete there")
            if verify and not self.dry_run:
                task = await self.agent(target).call("verify", repo=repo, revision=rev)
                res = await self.agent(target).wait_task(
                    task["task_id"], timeout=3 * 3600, poll=self.poll,
                    on_progress=self._progress(job, step, f"node {target}: "),
                    cancel_check=cancel_check)
                if (res.get("result") or {}).get("verified"):
                    notes.append(f"verified on {target}")
            notes.append(f"copied {src}→{target}")
        return f"{repo}@{rev[:8]} on {', '.join(nodes)} ({'; '.join(notes)})"
