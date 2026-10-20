"""Named inference keys and their token usage (master plan §4.1).

One key per app (Hermes, OpenClaw, a friend's laptop): each limited to aliases, expirable, revocable,
shown once at creation. The controller keeps only SHA-256 hashes and a short prefix to tell keys apart.
0.6's single ``inference_api_key`` becomes the first named key ("default") on the first start, so apps
configured with it keep working. From then on only named keys open the gateway: revoking or deleting
"default" retires 0.6's key for good.

Usage: the gateway counts requests, prompt and completion tokens per key and alias (gateway.Gateway
.take_usage); the controller adds them to daily totals in its store (``usage:<day>``), kept 90 days.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import re
import secrets
import threading
import time
from typing import Any, Optional

KEYS = "inference_keys"
# set once named keys rule (0.6's key migrated, or a key created): from then on neither 0.6's single key
# nor the keyless loopback mode comes back, even when every named key was deleted
MANAGED = "inference_keys_managed"
KEEP_DAYS = 90
_NAME_RE = re.compile(r"^[\w .@()+:/-]{1,64}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,62}$")
PREFIX = "tsk_"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def public(rec: dict[str, Any], now: Optional[float] = None) -> dict[str, Any]:
    """A key as the API shows it: never the hash, with its state."""
    now = time.time() if now is None else now
    state = "revoked" if rec.get("revoked_at") else (
        "expired" if rec.get("expires_at") and rec["expires_at"] <= now else "active")
    return {k: rec.get(k) for k in ("id", "name", "app", "prefix", "aliases", "created_at", "expires_at",
                                     "revoked_at", "last_used_at", "migrated")} | {"state": state}


class KeyStore:
    """Every change is a read-modify-write of one list: one lock for all of them, so a usage flush cannot
    write back a key that was revoked a moment earlier."""

    def __init__(self, store: Any):
        self.store = store
        self._lock = threading.RLock()

    def records(self) -> list[dict[str, Any]]:
        return list(self.store.kv_get(KEYS) or [])

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.store.kv_set(KEYS, records)
        self.store.kv_set(MANAGED, True)

    def managed(self) -> bool:
        return bool(self.store.kv_get(MANAGED)) or bool(self.records())

    def migrate(self, legacy_key: Optional[str]) -> bool:
        """0.6's single key becomes the named key "default" — once. A deleted "default" never returns."""
        with self._lock:
            if self.managed():
                self.store.kv_set(MANAGED, True)
                return False
            if not legacy_key:
                return False
            self._save([{"id": "default", "name": "default", "app": None, "prefix": legacy_key[:6],
                         "sha256": digest(legacy_key), "aliases": [], "created_at": time.time(),
                         "expires_at": None, "revoked_at": None, "last_used_at": None, "migrated": True}])
            return True

    def create(self, name: str, *, aliases: Optional[list[str]] = None, expires_in_days: Optional[int] = None,
               app: Optional[str] = None) -> tuple[str, dict[str, Any]]:
        """A new key: ``(token, record)``. The token is returned only here."""
        if not _NAME_RE.match(name or ""):
            raise ValueError("key name: 1-64 letters, digits, spaces and . _ @ ( ) + : / -")
        aliases = sorted(set(aliases or []))
        if any(not _ALIAS_RE.match(a) for a in aliases):
            raise ValueError("aliases: letters, digits and . _ : - (up to 63)")
        if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
            raise ValueError("expires_in_days must be between 1 and 3650")
        with self._lock:
            records = self.records()
            if len([r for r in records if not r.get("revoked_at")]) >= 200:
                raise ValueError("200 active keys at most: revoke some first")
            token = PREFIX + secrets.token_urlsafe(32)
            now = time.time()
            rec = {"id": "key-" + secrets.token_hex(5), "name": name.strip(),
                   "app": (app or None) and str(app)[:40], "prefix": token[:10], "sha256": digest(token),
                   "aliases": aliases, "created_at": now,
                   "expires_at": now + expires_in_days * 86400 if expires_in_days else None,
                   "revoked_at": None, "last_used_at": None}
            self._save(records + [rec])
            return token, rec

    def revoke(self, key_id: str) -> dict[str, Any]:
        with self._lock:
            records = self.records()
            for r in records:
                if r["id"] == key_id:
                    if not r.get("revoked_at"):
                        r["revoked_at"] = time.time()
                        self._save(records)
                    return r
            raise KeyError(key_id)

    def delete(self, key_id: str) -> None:
        """Only a revoked or expired key can be deleted (its usage stays in the totals)."""
        with self._lock:
            records = self.records()
            rec = next((r for r in records if r["id"] == key_id), None)
            if rec is None:
                raise KeyError(key_id)
            if public(rec)["state"] == "active":
                raise ValueError("revoke the key before deleting it")
            self._save([r for r in records if r["id"] != key_id])

    def touch(self, last_used: dict[str, float]) -> bool:
        """Remember when keys were last used; written at most once a minute per key."""
        with self._lock:
            records, changed = self.records(), False
            for r in records:
                t = last_used.get(r["id"])
                if t and (not r.get("last_used_at") or t - r["last_used_at"] >= 60):
                    r["last_used_at"] = t
                    changed = True
            if changed:
                self._save(records)
            return changed


class Usage:
    """Daily totals per key and alias."""

    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def _key(day: str) -> str:
        return f"usage:{day}"

    def add(self, rows: list[dict[str, Any]]) -> None:
        by_day: dict[str, list[dict]] = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)
        for day, items in by_day.items():
            doc = self.store.kv_get(self._key(day)) or {}
            for r in items:
                cell = doc.setdefault(f"{r['key_id']}|{r['alias']}", {"requests": 0, "prompt_tokens": 0,
                                                                     "completion_tokens": 0,
                                                                     "estimated_requests": 0})
                for k in cell:
                    cell[k] += int(r.get(k) or 0)
            self.store.kv_set(self._key(day), doc)
        if by_day:
            self.prune()

    def prune(self, today: Optional[_dt.date] = None) -> None:
        cutoff = ((today or _dt.datetime.now(_dt.timezone.utc).date()) - _dt.timedelta(days=KEEP_DAYS)).isoformat()
        for key in self.store.kv_keys("usage:"):
            if key[len("usage:"):] < cutoff:
                self.store.kv_delete(key)

    def rows(self, days: int = 30, today: Optional[_dt.date] = None) -> list[dict[str, Any]]:
        today = today or _dt.datetime.now(_dt.timezone.utc).date()
        out = []
        for i in range(max(1, min(days, KEEP_DAYS)) - 1, -1, -1):
            day = (today - _dt.timedelta(days=i)).isoformat()
            for cell, v in sorted((self.store.kv_get(self._key(day)) or {}).items()):
                key_id, _, alias = cell.partition("|")
                out.append({"day": day, "key_id": key_id, "alias": alias, **v})
        return out


def to_csv(rows: list[dict[str, Any]], names: dict[str, str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["day", "key_id", "key_name", "alias", "requests", "prompt_tokens", "completion_tokens",
                "estimated_requests"])
    for r in rows:
        w.writerow([r["day"], r["key_id"], names.get(r["key_id"], ""), r["alias"], r["requests"],
                    r["prompt_tokens"], r["completion_tokens"], r["estimated_requests"]])
    return buf.getvalue()
