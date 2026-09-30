"""Agent adapter; dry-run is isolated from every privileged operation."""

from __future__ import annotations

from .maintenance import run_id


class NodeMaintenance:
    def __init__(self, actions):
        self.actions = actions
        self.runs = {}
        self.boot = "dry-run-boot-0"

    def quiet(self):
        a = self.actions
        if any(c.status not in ("exited", "missing", "created") for c in a.runtime.list_owned()):
            raise RuntimeError("managed containers are still running")
        if a.runtime.list_foreign():
            raise RuntimeError("unmanaged inference is running; stop it before maintenance")
        if any(t["state"] == "running" for t in a.tasks.tasks.values()):
            raise RuntimeError("agent transfers or diagnostics are still running")
        if not a.dry_run:
            # Reboots affect every container, not just recognized inference servers.
            if a.runtime._run([a.runtime.docker, "ps", "-q"], timeout=10).strip():
                raise RuntimeError("other Docker containers are running; stop them before maintenance")
            import subprocess

            p = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if p.returncode or p.stdout.strip():
                raise RuntimeError("GPU workloads are active or their status cannot be verified")

    def probe(self, params):
        a = self.actions
        if a.dry_run:
            return {
                "enabled": True,
                "allow_firmware": True,
                "dry_run": True,
                "boot_id": self.boot,
                "package_preview": "Simulation: no packages will change.",
            }
        return {**a.privd.call("maintenance_probe"), "dry_run": False}

    def start(self, params):
        rid = run_id(params)
        self.quiet()
        if self.actions.dry_run:
            return self.runs.setdefault(
                rid, {"run_id": rid, "state": "awaiting_reboot", "boot_id": self.boot, "dry_run": True}
            )
        return self.actions.privd.call("maintenance_start", params)

    def status(self, params):
        rid = run_id(params)
        if self.actions.dry_run:
            return self.runs.get(rid, {"state": "missing", "boot_id": self.boot})
        return self.actions.privd.call("maintenance_status", params)

    def reboot(self, params):
        rid = run_id(params)
        self.quiet()
        if self.actions.dry_run:
            state = self.runs[rid]
            self.boot = "dry-run-boot-" + rid
            state.update(state="completed", new_boot_id=self.boot)
            return state
        return self.actions.privd.call("maintenance_reboot", params)

    def verify(self, params):
        a = self.actions
        if a.dry_run:
            return {"healthy": True, "dry_run": True}
        a.privd.call("maintenance_verify", {"run_id": run_id(params)})
        from . import sysinfo

        facts = sysinfo.nvidia_facts(ttl=0)
        if not a.runtime.ping().get("ok"):
            raise RuntimeError("Docker is not healthy after reboot")
        if not facts.get("gpu_name"):
            raise RuntimeError("GPU is unavailable after reboot")
        active = {d["hca"] for d in sysinfo.rdma_devices() if d["active"]}
        missing = set(params.get("rdma_hcas", [])) - active
        if missing:
            raise RuntimeError("RDMA links did not recover: " + ", ".join(sorted(missing)))
        return {"healthy": True, "gpu": facts, "rdma_active": sorted(active)}
