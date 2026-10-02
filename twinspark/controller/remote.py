"""Controller side of remote management: terminal bridge, diagnostics, power, Wake-on-LAN, smart plug.

The controller never runs a command on a node itself. It asks the node's agent for a typed
action, or relays the terminal stream to that node's ``tsm-termd``. What a node allows is decided
by the root-owned policy on that node; this module only reports it and gives good error messages.
Everything that changes a machine is audited, and the dangerous ones need a typed confirmation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import socket
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from ..security import SecretsVault
from .agent_client import AgentActionError

if TYPE_CHECKING:
    from .controller import Controller

TICKET_TTL_S = 30
MAX_TICKETS = 50
MAX_BRIDGES = 8
_SECRET_REF = "${secret:plug_token}"


class RemoteError(Exception):
    """Something the operator can act on; ``status`` is the HTTP status the API returns."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Ticket:
    node: str
    actor: str
    expires: float
    cols: int = 80
    rows: int = 24


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError("a MAC address has six bytes")
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac: str, broadcast: str, port: int, bind_ip: Optional[str], repeat: int = 3) -> None:
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind_ip:
            s.bind((bind_ip, 0))
        for i in range(repeat):
            s.sendto(pkt, (broadcast, port))
            if i + 1 < repeat:
                time.sleep(0.1)


async def tcp_open(host: str, port: int, timeout: float = 2.5) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    with contextlib.suppress(Exception):
        await w.wait_closed()
    return True


class RemoteService:
    def __init__(self, ctrl: "Controller"):
        self.ctrl = ctrl
        self.tickets: dict[str, Ticket] = {}
        self.bridges = 0
        # replaced in tests
        # trust_env=False: nodes and smart plugs are on the local network, never behind a proxy
        self.http_factory = lambda: httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4),
                                                      follow_redirects=False, trust_env=False)
        self.send_wol = send_magic_packet
        self.sleep = asyncio.sleep

    # ---- helpers ----------------------------------------------------------------------------
    def node(self, name: str):
        if name not in self.ctrl.agents or name not in self.ctrl.config.nodes:
            raise RemoteError(f"unknown node '{name}'", 404)
        return self.ctrl.agents[name]

    async def _call(self, node: str, action: str, /, timeout: float = 20, **params: Any) -> Any:
        try:
            return await self.node(node).call(action, timeout=timeout, **params)
        except AgentActionError as exc:
            raise RemoteError(exc.detail, 502 if "unreachable" in exc.detail else 409) from exc

    def _host(self, node: str) -> str:
        return urlsplit(self.ctrl.config.nodes[node].agent_url).hostname or "127.0.0.1"

    def _audit(self, action: str, node: str, detail: Optional[dict] = None) -> None:
        self.ctrl._audit("user", action, f"node/{node}", detail or {})

    # ---- overview / status -------------------------------------------------------------------
    async def status(self, node: str) -> dict[str, Any]:
        ep = self.ctrl.config.nodes[node]
        plug_actions = sorted(k for k in ("on", "off", "cycle") if ep.plug and getattr(ep.plug, k))
        out: dict[str, Any] = {"node": node, "reachable": False, "wake_configured": bool(ep.wake),
                               "plug_configured": bool(ep.plug), "plug_actions": plug_actions,
                               "mac": ep.wake.mac if ep.wake else None}
        try:
            st = await self.node(node).call("remote_status", timeout=12)
        except AgentActionError as exc:
            out["error"] = exc.detail
            return out
        out.update(st)
        out["reachable"] = True
        return out

    async def overview(self) -> dict[str, Any]:
        nodes = sorted(self.ctrl.agents)
        results = await asyncio.gather(*(self.status(n) for n in nodes))
        act = self.ctrl.active()
        return {"nodes": dict(zip(nodes, results, strict=True)), "controller_node": self.ctrl.config.node.node_id,
                "active": act["profile"] if act else None, "busy": self.ctrl.busy()}

    async def reach(self, node: str) -> dict[str, Any]:
        """Why can't I reach this node? Probe the agent, SSH and the terminal service separately."""
        self.node(node)
        ep = self.ctrl.config.nodes[node]
        agent = urlsplit(ep.agent_url)
        host = agent.hostname or "127.0.0.1"
        agent_port = agent.port or (443 if agent.scheme == "https" else 80)
        ssh_host = ep.ssh_host or ep.qsfp_ip or host
        agent_ok, ssh_ok, term_ok = await asyncio.gather(
            tcp_open(host, agent_port), tcp_open(ssh_host, ep.ssh_port), tcp_open(host, ep.terminal_port))
        answers = False
        detail = ""
        if agent_ok:
            try:
                await self.node(node).call("remote_status", timeout=8)
                answers = True
            except AgentActionError as exc:
                detail = exc.detail
        link = None
        peer = "A" if node != "A" else "B"
        if not agent_ok and not ssh_ok and node != self.ctrl.config.node.node_id and peer in self.ctrl.agents:
            iface = self.ctrl.config.nodes[self.ctrl.config.node.node_id].qsfp_iface
            with contextlib.suppress(Exception):
                mine = await self.ctrl.agents[self.ctrl.config.node.node_id].call("remote_status", timeout=8)
                link = next((i for i in mine.get("interfaces", []) if i["name"] == iface), None)
        verdict, summary, steps = _triage(node, agent_ok, answers, ssh_ok, link, ep, detail,
                                          self.ctrl.config.node.node_id)
        return {"node": node, "agent_port_open": agent_ok, "agent_answers": answers, "ssh_port_open": ssh_ok,
                "terminal_port_open": term_ok, "host": host, "qsfp_link": link, "verdict": verdict,
                "summary": summary, "steps": steps, "checked_at": time.time()}

    # ---- diagnostics -----------------------------------------------------------------------
    async def logs(self, node: str, source: str, lines: int, since_s: Optional[int], grep: Optional[str]) -> dict:
        params: dict[str, Any] = {"source": source, "lines": lines}
        if since_s:
            params["since_s"] = since_s
        if grep:
            params["grep"] = grep
        try:
            return await self.node(node).call("remote_logs", timeout=30, **params)
        except AgentActionError as exc:
            raise RemoteError(exc.detail, 502 if "unreachable" in exc.detail else 422) from exc

    async def bundle(self, node: str) -> tuple[str, bytes, list[str]]:
        r = await self._call(node, "remote_bundle", timeout=120)
        self._audit("remote.bundle", node, {"size": r.get("size"), "problems": len(r.get("problems", []))})
        return r["name"], base64.b64decode(r["b64"]), r.get("problems", [])

    # ---- terminal --------------------------------------------------------------------------
    def _purge(self) -> None:
        now = time.monotonic()
        for k in [k for k, t in self.tickets.items() if t.expires < now]:
            del self.tickets[k]

    async def issue_ticket(self, node: str, cols: int = 80, rows: int = 24,
                           actor: str = "user") -> dict[str, Any]:
        self.node(node)
        try:
            st = await self.node(node).call("remote_status", timeout=10)
        except AgentActionError as exc:
            raise RemoteError(f"node {node} is not reachable ({exc.detail}) — see the reachability check",
                              502) from exc
        if not st["policy"]["terminal"]:
            why = f" ({st['policy']['error']})" if st["policy"].get("error") else ""
            raise RemoteError(f"the terminal is switched off on node {node}{why}. On that node run: "
                              f"sudo tsm remote enable terminal", 409)
        if not st.get("terminal_service"):
            raise RemoteError(f"the terminal service is not running on node {node}. On that node run: "
                              f"sudo systemctl enable --now twinspark-terminal", 409)
        self._purge()
        if len(self.tickets) >= MAX_TICKETS:
            raise RemoteError("too many pending terminal tickets", 429)
        token = secrets.token_urlsafe(24)
        self.tickets[token] = Ticket(node, actor, time.monotonic() + TICKET_TTL_S,
                                     cols if isinstance(cols, int) else 80, rows if isinstance(rows, int) else 24)
        return {"ticket": token, "expires_in_s": TICKET_TTL_S, "ws_path": "/api/v1/remote/terminal/ws"}

    def redeem(self, token: str) -> Optional[Ticket]:
        """Single use: the ticket is gone after the first attempt, valid or not."""
        self._purge()
        return self.tickets.pop(token, None) if isinstance(token, str) else None

    async def open_upstream(self, node: str, cols: int, rows: int, actor: str):
        """Connect to the node's terminal service. Returns an open websockets connection."""
        from websockets.asyncio.client import connect

        ep = self.ctrl.config.nodes[node]
        agent = self.node(node)
        scheme = "wss" if urlsplit(ep.agent_url).scheme == "https" else "ws"
        url = f"{scheme}://{self._host(node)}:{ep.terminal_port}/v1/terminal?cols={int(cols)}&rows={int(rows)}"
        url += "&actor=" + quote(actor[:40], safe="")
        # proxy=None: the node is on the local link; an HTTP(S)_PROXY in the environment must not apply
        kwargs: dict[str, Any] = {"additional_headers": dict(agent._headers), "open_timeout": 6,
                                  "max_size": 2 * 1024 ** 2, "ping_interval": 20, "ping_timeout": 20,
                                  "proxy": None}
        if scheme == "wss":
            kwargs["ssl"] = ssl.create_default_context()
        try:
            return await connect(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RemoteError(f"could not reach the terminal service on node {node} "
                              f"({type(exc).__name__}) — is twinspark-terminal running?", 502) from exc

    async def _termd_get(self, node: str, path: str) -> Any:
        ep = self.ctrl.config.nodes[node]
        scheme = "https" if urlsplit(ep.agent_url).scheme == "https" else "http"
        url = f"{scheme}://{self._host(node)}:{ep.terminal_port}{path}"
        async with self.http_factory() as c:
            try:
                r = await c.get(url, headers=dict(self.node(node)._headers))
            except httpx.HTTPError as exc:
                raise RemoteError(f"terminal service on node {node} not reachable "
                                  f"({type(exc).__name__})", 502) from exc
        if r.status_code != 200:
            raise RemoteError(f"terminal service answered HTTP {r.status_code}", 404 if r.status_code == 404 else 502)
        return r.json()

    async def recordings(self, node: str) -> list[dict]:
        return (await self._termd_get(node, "/v1/terminal/recordings"))["recordings"]

    async def recording(self, node: str, name: str) -> dict:
        return await self._termd_get(node, "/v1/terminal/recordings/" + quote(name, safe=""))

    # ---- power -----------------------------------------------------------------------------
    def _guard_busy(self, force: bool) -> None:
        if self.ctrl.busy() and not force:
            raise RemoteError("the cluster is busy (an activation, preparation or maintenance run is in "
                              "progress); wait for it or repeat with force", 409)

    async def power(self, node: str, action: str, confirm: str, delay_s: int = 5, force: bool = False) -> dict:
        if action not in ("reboot", "poweroff"):
            raise RemoteError("action must be 'reboot' or 'poweroff'")
        want = f"{action.upper()} {node}"
        if confirm != want:
            raise RemoteError(f"type '{want}' to confirm", 422)
        self._guard_busy(force)
        act = self.ctrl.active()
        r = await self._call(node, "remote_power", timeout=30, action=action, delay_s=delay_s)
        own = node == self.ctrl.config.node.node_id
        self._audit(f"remote.{action}", node, {"delay_s": delay_s, "active_model": act["profile"] if act else None,
                                              "controller_goes_down": own})
        return {**r, "controller_goes_down": own, "active_model": act["profile"] if act else None,
                "autostart": self.ctrl.config.autostart}

    async def power_cancel(self, node: str) -> dict:
        r = await self._call(node, "remote_power_cancel")
        self._audit("remote.power_cancel", node, r)
        return r

    async def boot_status(self, node: str) -> dict:
        return await self._call(node, "remote_boot_status")

    async def boot_next(self, node: str, target: str) -> dict:
        r = await self._call(node, "remote_boot_next", target=target)
        self._audit("remote.boot_next", node, r)
        return r

    async def boot_clear(self, node: str) -> dict:
        r = await self._call(node, "remote_boot_next_clear")
        self._audit("remote.boot_next_clear", node, {})
        return r

    async def wol_set(self, node: str, iface: str, mode: str) -> dict:
        r = await self._call(node, "remote_wol_set", iface=iface, mode=mode)
        self._audit("remote.wol_set", node, {"iface": iface, "mode": mode})
        return r

    # ---- out-of-band power ------------------------------------------------------------------
    async def wake(self, node: str) -> dict:
        self.node(node)
        ep = self.ctrl.config.nodes[node]
        if node == self.ctrl.config.node.node_id:
            raise RemoteError(f"node {node} runs this controller, so it cannot wake itself. Wake it from the "
                              f"other node (`tsm wake <mac>`) or from your laptop.", 409)
        if not ep.wake:
            raise RemoteError(f"no Wake-on-LAN settings for node {node}: add nodes.{node}.wake (mac, iface, "
                              f"broadcast) to controller.yaml — `tsm node info` on that node prints the MAC", 409)
        bind_ip = None
        if ep.wake.iface:
            from .. import hostprobe
            ifc = next((i for i in hostprobe.list_interfaces() if i.name == ep.wake.iface), None)
            if ifc is None or not ifc.addr:
                raise RemoteError(f"interface {ep.wake.iface} has no IPv4 address on this machine", 409)
            bind_ip = ifc.addr
        try:
            await asyncio.to_thread(self.send_wol, ep.wake.mac, ep.wake.broadcast, ep.wake.port, bind_ip)
        except OSError as exc:
            raise RemoteError(f"could not send the magic packet: {exc.strerror or exc}", 502) from exc
        self._audit("remote.wake", node, {"mac": ep.wake.mac, "broadcast": ep.wake.broadcast})
        return {"sent": True, "mac": ep.wake.mac, "broadcast": ep.wake.broadcast,
                "note": "A magic packet cannot be acknowledged. If the node does not appear within a minute or "
                        "two, Wake-on-LAN is not working on that port — use the smart plug or a power button."}

    def _expand(self, text: str) -> str:
        if _SECRET_REF not in text:
            return text
        token = SecretsVault(self.ctrl.config.secrets_dir).get("plug_token")
        if not token:
            raise RemoteError("the plug request uses ${secret:plug_token} but no token is stored — run "
                              "`sudo tsm remote plug-token`", 409)
        return text.replace(_SECRET_REF, token)

    async def _plug_call(self, req) -> int:
        url = self._expand(req.url)
        headers = {k: self._expand(v) for k, v in req.headers.items()}
        body = self._expand(req.body) if req.body is not None else None
        async with self.http_factory() as c:
            try:
                r = await c.request(req.method, url, headers=headers, content=body)
            except httpx.HTTPError as exc:
                raise RemoteError(f"the plug did not answer ({type(exc).__name__})", 502) from exc
        if not 200 <= r.status_code < 300:
            raise RemoteError(f"the plug answered HTTP {r.status_code}", 502)
        return r.status_code

    async def plug(self, node: str, action: str, confirm: str, force: bool = False) -> dict:
        self.node(node)
        ep = self.ctrl.config.nodes[node]
        if action not in ("on", "off", "cycle"):
            raise RemoteError("action must be on, off or cycle")
        if not ep.plug:
            raise RemoteError(f"no smart plug configured for node {node} "
                              f"(nodes.{node}.plug in controller.yaml)", 409)
        if action != "on":
            want = f"CUT POWER {node}"
            if confirm != want:
                raise RemoteError(f"this switches the machine off abruptly (no shutdown). "
                                  f"Type '{want}' to confirm", 422)
            self._guard_busy(force)
        plug = ep.plug
        steps: list[int] = []
        if action == "on":
            if not plug.on:
                raise RemoteError("this plug has no 'on' request configured", 409)
            steps.append(await self._plug_call(plug.on))
        elif action == "off":
            if not plug.off:
                raise RemoteError("this plug has no 'off' request configured", 409)
            steps.append(await self._plug_call(plug.off))
        elif plug.cycle:
            steps.append(await self._plug_call(plug.cycle))
        elif plug.off and plug.on:
            steps.append(await self._plug_call(plug.off))
            await self.sleep(plug.settle_s)
            steps.append(await self._plug_call(plug.on))
        else:
            raise RemoteError("cycling needs either a 'cycle' request or both 'off' and 'on'", 409)
        self._audit(f"remote.plug_{action}", node, {"http": steps})
        return {"ok": True, "action": action, "http": steps}


def _triage(node: str, agent_ok: bool, answers: bool, ssh_ok: bool, link: Optional[dict], ep, detail: str,
            controller_node: str) -> tuple[str, str, list[str]]:
    wake = " or use Wake-on-LAN" if ep.wake else ""
    plug = " or the smart plug (Cycle)" if ep.plug else ""
    if answers:
        return "ok", f"Node {node}'s agent answers.", []
    if agent_ok and not answers:
        return ("agent_error", f"Node {node}'s agent port is open but the agent returned an error: {detail[:200]}",
                [f"ssh to node {node} and run: sudo journalctl -u twinspark-agent -n 80 --no-pager",
                 "401 means the agent token differs between nodes: run `sudo tsm join-code` on node A and "
                 "`sudo tsm setup --join …` on node B"])
    if ssh_ok:
        return ("agent_down",
                f"Node {node} is up (its SSH port answers) but the TwinSpark agent does not.",
                [f"ssh to node {node}", "sudo systemctl status twinspark-agent twinspark-privd",
                 "sudo journalctl -u twinspark-agent -n 80 --no-pager", "sudo systemctl restart twinspark-agent",
                 "`tsm node doctor` there checks the whole node without needing the controller"])
    if link is not None and link.get("state") not in (None, "up", "unknown"):
        return ("link_down",
                f"Nothing answers on node {node} and this machine's QSFP port {link['name']} is {link['state']}.",
                ["Check the QSFP cable at both ends and that the other Spark is powered on",
                 f"On this machine: ip -br link show {link['name']}",
                 f"If the cable is fine, node {node} is off or hung: try power on{wake}{plug}"])
    steps = [f"Nothing on node {node} answers (agent, SSH). It is powered off, hung, or the network path is down.",
             "Check the QSFP cable and the node's power LED"]
    if ep.wake:
        steps.append("Press Wake-on-LAN, then wait one to two minutes")
    if ep.plug:
        steps.append("If it stays silent, use the smart plug: Cycle (cuts power — the OS does not shut down cleanly)")
    if not ep.wake and not ep.plug:
        steps.append(f"No remote power is configured for node {node}. Add nodes.{node}.wake or nodes.{node}.plug in "
                     f"controller.yaml (docs/remote-management.md), or press the power button")
    if node == controller_node:
        steps.insert(0, "This node runs the controller you are talking to, so it is up; this check probes it "
                        "over the network.")
    return "host_down", f"Node {node} is not reachable (powered off, hung, or cut off).", steps
