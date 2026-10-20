"""``tsm client`` — the workstation client (macOS, Windows, Linux; install with pipx).

    tsm client                      # open the GUI in your browser (http://127.0.0.1:<port>)
    tsm client add home 192.168.1.20 --user spark
    tsm client connect home         # same flow in the terminal; keeps the tunnel up until Ctrl-C
    tsm client list | remove NAME | env NAME | ports NAME [--gateway 18000]

The GUI is served on loopback only; the address it prints carries a session key in its fragment.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
import threading
import webbrowser
from typing import Optional

DEFAULT_PORT = 18765


def _session():
    from .client.session import ClientSession
    return ClientSession()


def _find(session, name: str):
    c = session.connections.get(name) or next((x for x in session.connections.list() if x.name == name), None)
    if c is None:
        names = ", ".join(x.name for x in session.connections.list()) or "none yet"
        sys.exit(f"no connection called {name!r} (saved: {names}) — add one: tsm client add NAME HOST --user USER")
    return c


# ---- the GUI ------------------------------------------------------------------------------------
async def _serve(port: Optional[int], open_browser: bool, autoconnect: bool) -> None:
    import uvicorn

    from .client.app import create_client_app, new_session_key
    from .client.ssh import free_local_port
    session = _session()
    key, launch = new_session_key(), new_session_key()
    app = create_client_app(session, key, autoconnect=autoconnect, launch_code=launch)
    port = port or free_local_port(DEFAULT_PORT)
    # loop="none": keep the default event loop (on Windows the Proactor loop, which can start ssh)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="none",
                                           access_log=False, ws_max_size=2 * 1024 ** 2))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if task.done():
        await task
        return
    url = f"http://127.0.0.1:{port}/#k={key}"
    print(f"TwinSpark client {__import__('twinspark').__version__} — open {url}")
    print("Keep this running while you use the GUI or apps connected through it (Ctrl-C ends it; the Sparks "
          "keep serving).")
    if open_browser:
        # the browser gets a one-time code, not the key: its command line is visible to other users
        threading.Thread(target=webbrowser.open, args=(f"http://127.0.0.1:{port}/launch/{launch}",),
                         daemon=True).start()
    await task


def cmd_client(args, api=None) -> None:
    action = getattr(args, "action", None) or "open"
    if action == "open":
        try:
            asyncio.run(_serve(args.port, not args.no_browser, not args.no_autoconnect))
        except KeyboardInterrupt:
            pass
        return
    if action == "list":
        s = _session()
        rows = s.connections.list()
        if not rows:
            print("no connections yet — tsm client add NAME HOST --user USER")
        for c in rows:
            where = c.direct_url or f"{c.ssh_user}@{c.host}:{c.ssh_port}"
            ports = f"  local api :{c.local_api_port} gateway :{c.local_gateway_port}" if c.local_api_port else ""
            print(f"{c.name:16} {c.mode:10} {where}{ports}")
        return
    if action == "add":
        from .client.connections import Connection, suggest_mode
        s = _session()
        mode = args.mode or ("direct" if args.url else suggest_mode(args.host))
        try:
            c = s.connections.add(Connection(id="", name=args.name, mode=mode, host=args.host,
                                             ssh_user=args.user or "", ssh_port=args.ssh_port,
                                             identity_file=args.identity, direct_url=args.url))
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"added {c.name} ({c.mode}, {c.host}) — connect with: tsm client connect {c.name}  or open the GUI: "
              f"tsm client")
        return
    if action == "remove":
        s = _session()
        c = _find(s, args.name)
        s.connections.remove(c.id, credentials=s.creds)
        print(f"removed {c.name} and its stored keys")
        return
    if action == "env":
        s = _session()
        c = _find(s, args.name)
        if not c.local_api_port:
            sys.exit(f"{c.name} has no local ports yet — connect once first: tsm client connect {c.name}")
        key = s.creds.get(c.id, "management_key")
        print(f"export TSM_API=http://127.0.0.1:{c.local_api_port}")
        if key:
            print(f"export TSM_KEY={key}")
        print(f"# OpenAI-compatible endpoint for local apps: http://127.0.0.1:{c.local_gateway_port}/v1 "
              f"(while `tsm client` or `tsm client connect {c.name}` runs)")
        return
    if action == "ports":
        s = _session()
        c = _find(s, args.name)
        changes = {}
        for flag, field in (("api", "local_api_port"), ("gateway", "local_gateway_port")):
            value = getattr(args, flag)
            if value is not None:
                changes[field] = None if value == "auto" else _port(value)
        if changes:
            try:
                c = s.connections.update(c.id, **changes)
            except ValueError as exc:
                sys.exit(str(exc))
        auto = "picked at the next connect"
        print(f"{c.name}: local API port {c.local_api_port or auto}, local gateway port "
              f"{c.local_gateway_port or auto}" + (" — reconnect to use them" if changes else ""))
        return
    if action == "connect":
        try:
            asyncio.run(_connect_interactive(args.name))
        except KeyboardInterrupt:
            print("\ndisconnected (the Sparks keep serving)")
        return


def _port(value: str) -> int:
    if not value.isdigit() or not 1024 <= int(value) <= 65535:
        sys.exit(f"{value!r} is not a port from 1024 to 65535 (or `auto`)")
    return int(value)


def _yes(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


async def _connect_interactive(name: str) -> None:
    from .client import ssh
    s = _session()
    c = _find(s, name)
    try:
        if c.uses_ssh:
            keys = await asyncio.to_thread(ssh.scan_host_keys, c.host, c.ssh_port)
            state = ssh.host_key_status(s.home, c.host, c.ssh_port, keys)
            if state != "trusted":
                why = "CHANGED since you trusted it" if state == "changed" else "not seen before"
                print(f"{c.host} presents these host keys ({why}):")
                for k in keys:
                    print(f"  {k.type:20} {k.fingerprint}")
                print("Compare with what node A prints for `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.")
                if not _yes("Trust them? [y/N] "):
                    sys.exit("not trusted — nothing was changed")
                ssh.trust_host_keys(s.home, c.host, c.ssh_port, keys, replace=state == "changed")
        while True:
            st = await s.connect(c.id)
            if st["phase"] == "connected":
                break
            need, msg = st["need"], st["message"]
            if need == "auth":
                print(msg or f"SSH login as {c.ssh_user}@{c.host} was refused.")
                ask = "Install this client's own SSH key with your password once? [y/N] "
                if not sys.stdin.isatty() or not _yes(ask):
                    sys.exit("not connected")
                _, public = ssh.ensure_client_key(s.home)
                pw = getpass.getpass(f"password for {c.ssh_user}@{c.host} (used once, not stored): ")
                await ssh.install_key_with_password(c, pw, public, home=s.home)
                del pw
                continue
            if need == "keys":
                print(msg or "The TwinSpark keys could not be read over SSH.")
                mk = getpass.getpass("management_api_key (from `sudo tsm init --show` on node A, empty to stop): ")
                if not mk.strip():
                    sys.exit("not connected")
                s.creds.set(c.id, "management_key", mk.strip())
                ik = getpass.getpass("inference_api_key (empty to skip): ")
                if ik.strip():
                    s.creds.set(c.id, "inference_key", ik.strip())
                continue
            sys.exit(msg or f"not connected ({st['phase']})")
        st = await s.status()
        node = st["node"]
        print(f"connected to {c.name}: node A runs TwinSpark {node.get('version')} — "
              f"{(node.get('compat') or {}).get('note', '')}")
        for n, a in sorted((node.get("agents") or {}).items()):
            print(f"  node {n}: agent {a.get('version')} ({a.get('runtime_mode')})"
                  + ("" if a.get("ok") else "  ! version differs"))
        model = node.get("model") or {}
        act = model.get("active")
        print(f"  model: {act['profile'] + ' ' + act.get('label', '') if act else 'none active'}"
              f"{' (serving: ' + ', '.join(model.get('aliases') or []) + ')' if model.get('ready') else ''}")
        print(f"  OpenAI-compatible endpoint: {st['endpoint']['base_url']}   (key: tsm client env {c.name})")
        print(f"  management API: {st['endpoint']['manager_url']}   (tsm CLI: eval \"$(tsm client env {c.name})\")")
        print("Keeping the tunnel up; Ctrl-C to end.")
        last = None
        while True:
            await asyncio.sleep(5)
            cur = s.tunnel.state if s.tunnel else "stopped"
            if cur != last and last is not None:
                why = f" — {s.tunnel.last_error}" if s.tunnel and s.tunnel.last_error else ""
                print(f"  connection: {cur}{why}")
            last = cur
    except ssh.SSHError as exc:
        sys.exit(str(exc))
    finally:
        await s.aclose()


def add_parsers(sub, cmd) -> None:
    s = cmd("client", cmd_client, "the workstation client: connect to your Sparks by address, GUI on localhost")
    cs = s.add_subparsers(dest="action", metavar="<action>")
    o = cs.add_parser("open", help="open the GUI (the default)")
    for p in (s, o):
        p.add_argument("--port", type=int, help=f"local GUI port (default {DEFAULT_PORT} or a free one)")
        p.add_argument("--no-browser", action="store_true", help="only print the address")
        p.add_argument("--no-autoconnect", action="store_true", help="do not reconnect to the last Spark")
    a = cs.add_parser("add", help="save a connection to node A")
    a.add_argument("name")
    a.add_argument("host", help="node A's LAN address or hostname, or its Tailscale address")
    a.add_argument("--user", help="SSH user on node A")
    a.add_argument("--ssh-port", type=int, default=22)
    a.add_argument("--mode", choices=["ssh", "tailscale", "direct"], help="default: tailscale for tailnet "
                                                                          "addresses, else ssh")
    a.add_argument("--identity", help="SSH private key to use (default: your agent and this client's own key)")
    a.add_argument("--url", help="direct mode: the https URL of a deliberately configured listener")
    cs.add_parser("list", help="saved connections")
    r = cs.add_parser("remove", help="forget a connection and its stored keys")
    r.add_argument("name")
    c = cs.add_parser("connect", help="connect in this terminal and keep the tunnel up (Ctrl-C ends)")
    c.add_argument("name")
    e = cs.add_parser("env", help="print TSM_API/TSM_KEY for the tsm CLI through a connection")
    e.add_argument("name")
    pp = cs.add_parser("ports", help="show or change the local ends of the forwards (applies at the next connect)")
    pp.add_argument("name")
    pp.add_argument("--api", metavar="PORT|auto", help="local port of node A's management API")
    pp.add_argument("--gateway", metavar="PORT|auto", help="local port of the model gateway (what apps use)")
    cmd_client.local = True
