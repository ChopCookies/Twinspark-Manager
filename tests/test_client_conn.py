"""Workstation client: paths, saved connections, the credential store, tailnet helpers, local ports."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import types
from pathlib import Path

import pytest

from twinspark.client import connections as cx
from twinspark.client.connections import Connection, ConnectionStore
from twinspark.client.paths import client_home, read_json
from twinspark.client.secrets import CredentialStore
from twinspark.client.ssh import free_local_port, port_is_free

POSIX = os.name != "nt"


def conn(**kw) -> Connection:
    base = dict(id="spark-a", name="Spark A", host="spark-a.local", ssh_user="spark")
    base.update(kw)
    return Connection(**base)


# ---- paths --------------------------------------------------------------------------------------
def test_client_home_per_platform(tmp_path):
    home = tmp_path / "u"
    assert client_home(create=False, platform="linux", environ={}, user_home=home) == home / ".config" / "twinspark"
    assert client_home(create=False, platform="linux", environ={"XDG_CONFIG_HOME": str(tmp_path / "x")},
                       user_home=home) == tmp_path / "x" / "twinspark"
    # a relative XDG_CONFIG_HOME is invalid by the spec and ignored
    assert client_home(create=False, platform="linux", environ={"XDG_CONFIG_HOME": "rel"},
                       user_home=home) == home / ".config" / "twinspark"
    assert client_home(create=False, platform="darwin", environ={}, user_home=home) == \
        home / "Library" / "Application Support" / "TwinSpark"
    assert client_home(create=False, platform="win32", environ={"APPDATA": str(tmp_path / "Roaming")},
                       user_home=home) == tmp_path / "Roaming" / "TwinSpark"
    assert client_home(create=False, platform="win32", environ={}, user_home=home) == \
        home / "AppData" / "Roaming" / "TwinSpark"
    assert client_home(create=False, platform="darwin", environ={"TSM_CLIENT_HOME": str(tmp_path / "o")},
                       user_home=home) == tmp_path / "o"


def test_client_home_is_created_private(tmp_path, monkeypatch):
    monkeypatch.setenv("TSM_CLIENT_HOME", str(tmp_path / "ch"))
    p = client_home()
    assert p.is_dir()
    if POSIX:
        assert stat.S_IMODE(p.stat().st_mode) == 0o700


# ---- validation -----------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["spark-a", "spark-a.local", "192.168.1.20", "fd7a:115c:a1e0::1", "::1",
                                  "fe80::1%en0", "spark-a.tail1234.ts.net", "100.101.102.103", "a" * 63])
def test_valid_hosts(host):
    assert conn(host=host).validate().host == host


@pytest.mark.parametrize("host", ["-oProxyCommand=x", "-p", "spark a", "spark;id", "spark\nb", "user@spark",
                                  "spark:22", "[::1]", "", "$(id)", "spark`id`", "a..b", "-spark", "spark-",
                                  "999.1.1.1", "1.2.3", "fe80::1%en0;id", "1.2.3.4%eth0", "x" * 64])
def test_invalid_hosts_are_refused(host):
    with pytest.raises(ValueError) as exc:
        conn(host=host).validate()
    assert str(exc.value)                       # a plain sentence for the UI


@pytest.mark.parametrize("user", ["spark", "Spark.Admin", "_svc", "user-1", "a" * 32])
def test_valid_users(user):
    conn(ssh_user=user).validate()


@pytest.mark.parametrize("user", ["", "spark admin", "spark;rm -rf /", "-oProxyCommand=x", "a" * 33, "spark$",
                                  "us\ner", "../x", ".hidden", "user@host"])
def test_invalid_users_are_refused(user):
    with pytest.raises(ValueError, match="user name|SSH user"):
        conn(ssh_user=user).validate()


@pytest.mark.parametrize("field,value", [("ssh_port", 0), ("ssh_port", 65536), ("manager_port", -1),
                                         ("gateway_port", "8000"), ("local_api_port", 70000), ("ssh_port", True)])
def test_ports_are_checked(field, value):
    with pytest.raises(ValueError, match="1 to 65535"):
        conn(**{field: value}).validate()


def test_other_field_checks():
    with pytest.raises(ValueError, match="different"):
        conn(local_api_port=9000, local_gateway_port=9000).validate()
    with pytest.raises(ValueError, match="mode"):
        conn(mode="telnet").validate()
    with pytest.raises(ValueError, match="id"):
        conn(id="Bad Id").validate()
    with pytest.raises(ValueError, match="name"):
        conn(name="  ").validate()
    with pytest.raises(ValueError, match="'%'"):
        conn(identity_file="/keys/%h").validate()
    conn(identity_file="~/.ssh/id_ed25519").validate()


def test_direct_url_rules():
    ok = conn(mode="direct", host="", ssh_user="", direct_url="https://spark-a.tail1.ts.net:8443")
    assert ok.validate() is ok
    conn(mode="direct", host="", ssh_user="", direct_url="http://127.0.0.1:8443").validate()
    for bad, msg in [("http://spark-a:8443", "https"), ("https://u:p@spark-a:8443", "password"),
                     ("ftp://spark-a", "http"), ("https://spark-a:8443/api?x=1", "only scheme"),
                     ("https://-oProxyCommand=x:8443", "not an address"), (None, "URL")]:
        with pytest.raises(ValueError, match=msg):
            conn(mode="direct", host="", ssh_user="", direct_url=bad).validate()


def test_tailscale_mode_wants_a_tailnet_address():
    for host in ("100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0:ab12::1", "spark-a.tail1234.ts.net", "spark-a"):
        conn(mode="tailscale", host=host).validate()
    for host in ("192.168.1.20", "100.128.0.1", "spark-a.local", "fe80::1"):
        with pytest.raises(ValueError, match="not a Tailscale address"):
            conn(mode="tailscale", host=host).validate()
    assert cx.suggest_mode("100.100.1.2") == "tailscale"
    assert cx.suggest_mode("spark-a.tailabc.ts.net.") == "tailscale"
    assert cx.suggest_mode("192.168.1.2") == "ssh"


def test_round_trip_and_unknown_keys_ignored():
    c = conn(identity_file="/k", local_api_port=18443, last_connected_at=5.0)
    d = c.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert Connection.from_dict({**d, "future_field": 1}) == c
    with pytest.raises(ValueError):
        Connection.from_dict({"name": "x"})


# ---- connection store -----------------------------------------------------------------------------
def test_store_crud_and_file(tmp_path):
    store = ConnectionStore(tmp_path)
    assert store.list() == []
    a = store.add(conn(id="", name="Spark A!"))
    assert a.id == "spark-a"
    b = store.add(conn(id="", name="Spark A"))
    assert b.id == "spark-a-2"
    with pytest.raises(ValueError, match="already exists"):
        store.add(conn(id="spark-a"))
    with pytest.raises(ValueError):
        store.add(conn(id="", name="x", host="-oProxyCommand=evil"))
    assert [c.id for c in ConnectionStore(tmp_path).list()] == ["spark-a", "spark-a-2"]
    upd = store.update("spark-a", host="10.0.0.5", local_api_port=18443)
    assert store.get("spark-a").host == "10.0.0.5" and upd.local_api_port == 18443
    with pytest.raises(ValueError):
        store.update("spark-a", ssh_user="bad user")
    assert store.get("spark-a").ssh_user == "spark"           # a refused update changes nothing
    with pytest.raises(ValueError, match="cannot be changed"):
        store.update("spark-a", id="other")
    with pytest.raises(KeyError):
        store.update("missing", host="x")
    assert store.mark_connected("spark-a", when=123.0).last_connected_at == 123.0
    doc = json.loads((tmp_path / "connections.json").read_text())
    assert doc["version"] == 1 and len(doc["connections"]) == 2
    if POSIX:
        assert stat.S_IMODE((tmp_path / "connections.json").stat().st_mode) == 0o600
    assert store.remove("spark-a-2") is True and store.remove("spark-a-2") is False
    assert [c.id for c in store.list()] == ["spark-a"]
    assert not list(tmp_path.glob(".connections.json.*.tmp"))


def test_store_remove_also_deletes_credentials(tmp_path):
    store = ConnectionStore(tmp_path)
    creds = CredentialStore(tmp_path, use_keyring=False)
    store.add(conn())
    creds.set("spark-a", "management_key", "m" * 43)
    store.remove("spark-a", credentials=creds)
    assert creds.get("spark-a", "management_key") is None


def test_corrupt_store_is_moved_aside_not_overwritten(tmp_path, caplog):
    (tmp_path / "connections.json").write_text("{not json")
    store = ConnectionStore(tmp_path)
    assert store.list() == []
    assert list(tmp_path.glob("connections.json.bad-*"))
    store.add(conn())
    assert [c.id for c in store.list()] == ["spark-a"]


def test_store_skips_unreadable_rows(tmp_path):
    (tmp_path / "connections.json").write_text(json.dumps({"version": 1, "connections": [
        {"id": "ok", "name": "OK", "host": "h", "ssh_user": "u"}, {"oops": 1}, "junk"]}))
    assert [c.id for c in ConnectionStore(tmp_path).list()] == ["ok"]
    assert read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


# ---- credential store -----------------------------------------------------------------------------
def test_credentials_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)            # "not installed", even where it is
    store = CredentialStore(tmp_path)
    assert store.backend == "file"
    assert store.get("spark-a", "management_key") is None
    store.set("spark-a", "management_key", "mgmt-" + "x" * 38)
    store.set("spark-a", "inference_key", "inf-" + "y" * 39)
    store.set("spark-b", "inference_key", "other-" + "z" * 30)
    path = tmp_path / "credentials.json"
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = CredentialStore(tmp_path)
    assert again.get("spark-a", "management_key") == "mgmt-" + "x" * 38
    again.delete("spark-a", "management_key")
    assert again.get("spark-a", "management_key") is None
    again.delete_all("spark-a")
    assert again.get("spark-a", "inference_key") is None
    assert again.get("spark-b", "inference_key") == "other-" + "z" * 30
    assert "spark-a" not in json.loads(path.read_text())["connections"]


def test_credentials_refuse_passwords_and_bad_values(tmp_path):
    store = CredentialStore(tmp_path, use_keyring=False)
    for name in ("password", "ssh_password", "anything"):
        with pytest.raises(ValueError, match="not a credential"):
            store.set("spark-a", name, "hunter2")
    with pytest.raises(ValueError):
        store.set("spark-a", "management_key", "two\nlines")
    with pytest.raises(ValueError):
        store.set("../etc", "management_key", "x" * 40)
    assert not (tmp_path / "credentials.json").exists()


def _fake_keyring(module_name: str = "fakekeyring.backend", priority: float = 5) -> types.ModuleType:
    vault: dict[tuple[str, str], str] = {}
    mod = types.ModuleType("keyring")
    backend_cls = type("Keyring", (), {"priority": priority, "__module__": module_name})

    class PasswordDeleteError(Exception):
        pass

    def get_password(service, user):
        return vault.get((service, user))

    def set_password(service, user, value):
        vault[(service, user)] = value

    def delete_password(service, user):
        if (service, user) not in vault:
            raise PasswordDeleteError(user)
        del vault[(service, user)]

    mod.get_keyring = lambda: backend_cls()
    mod.get_password, mod.set_password, mod.delete_password = get_password, set_password, delete_password
    mod.vault = vault
    return mod


def test_credentials_use_a_usable_keyring(tmp_path, monkeypatch):
    kr = _fake_keyring()
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.delenv("TSM_CLIENT_KEYRING", raising=False)
    store = CredentialStore(tmp_path)
    assert store.backend == "keyring"
    store.set("spark-a", "management_key", "k" * 43)
    assert kr.vault == {("TwinSpark Manager", "spark-a:management_key"): "k" * 43}
    assert store.get("spark-a", "management_key") == "k" * 43
    assert not (tmp_path / "credentials.json").exists()           # nothing on disk
    store.delete_all("spark-a")
    store.delete_all("spark-a")                                     # deleting what is gone is fine
    assert kr.vault == {}
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    assert CredentialStore(tmp_path).backend == "file"


@pytest.mark.parametrize("module_name,priority", [("keyring.backends.fail", 0), ("keyring.backends.null", -1),
                                                  ("some.backend", 0)])
def test_unusable_keyring_backends_fall_back_to_the_file(tmp_path, monkeypatch, module_name, priority):
    monkeypatch.setitem(sys.modules, "keyring", _fake_keyring(module_name, priority))
    monkeypatch.delenv("TSM_CLIENT_KEYRING", raising=False)
    assert CredentialStore(tmp_path).backend == "file"


def test_keyring_failure_at_runtime_switches_to_the_file(tmp_path, monkeypatch, caplog):
    kr = _fake_keyring()

    def broken(*_a):
        raise RuntimeError("keyring locked")

    kr.set_password = broken
    monkeypatch.setitem(sys.modules, "keyring", kr)
    monkeypatch.delenv("TSM_CLIENT_KEYRING", raising=False)
    store = CredentialStore(tmp_path)
    secret = "s" * 43
    store.set("spark-a", "inference_key", secret)
    assert store.backend == "file"
    assert store.get("spark-a", "inference_key") == secret
    assert secret not in caplog.text


# ---- tailscale --------------------------------------------------------------------------------------
STATUS = {
    "BackendState": "Running",
    "Self": {"HostName": "laptop", "TailscaleIPs": ["100.64.0.9"]},
    "Peer": {
        "k1": {"HostName": "spark-b", "DNSName": "spark-b.tail1.ts.net.", "OS": "linux", "Online": False,
               "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"]},
        "k2": {"HostName": "spark-a", "DNSName": "spark-a.tail1.ts.net.", "OS": "linux", "Online": True,
               "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.1"]},
        "k3": {"HostName": "phone", "OS": "iOS", "Online": True, "TailscaleIPs": []},
        "k4": "junk",
    },
}


def test_tailscale_peers_parses_status():
    seen = []

    def run(argv, timeout):
        seen.append(argv)
        return 0, json.dumps(STATUS)

    peers = cx.tailscale_peers(run)
    assert seen == [["tailscale", "status", "--json"]]
    assert [p["name"] for p in peers] == ["spark-a", "spark-b"]       # online first, no IP-less device
    assert peers[0]["address"] == "100.64.0.1" and peers[0]["dns_name"] == "spark-a.tail1.ts.net"
    assert peers[1]["online"] is False


@pytest.mark.parametrize("result", [(127, "tailscale is not installed"), (1, "logged out"), (0, "not json"),
                                    (0, "[]"), (0, json.dumps({"Peer": None}))])
def test_tailscale_peers_never_fails(result):
    assert cx.tailscale_peers(lambda argv, timeout: result) == []


def test_tailscale_peers_runner_exception_and_missing_binary(monkeypatch):
    def boom(argv, timeout):
        raise OSError("nope")

    assert cx.tailscale_peers(boom) == []
    monkeypatch.setattr(cx, "_tailscale_binary", lambda: None)       # default runner, nothing installed
    assert cx.tailscale_peers() == []


# ---- local ports --------------------------------------------------------------------------------------
def test_free_local_port_prefers_and_avoids():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        assert not port_is_free(taken)
        other = free_local_port(taken)
        assert other != taken and port_is_free(other)
    free = free_local_port()
    assert free_local_port(free) == free
    assert free_local_port(free, avoid={free}) != free
    with pytest.raises(ValueError):
        port_is_free(0)


def test_paths_written_atomically_leave_no_temp_files(tmp_path):
    store = CredentialStore(tmp_path, use_keyring=False)
    for i in range(5):
        store.set("spark-a", "management_key", f"key-{i}-" + "x" * 30)
    assert sorted(p.name for p in Path(tmp_path).iterdir()) == ["credentials.json"]
