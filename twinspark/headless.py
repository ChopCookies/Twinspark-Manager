"""Headless appliance modes (spec §6, §7), applied through tsm-privd.

* ``desktop``        — graphical.target as default; optionally start the desktop now.
* ``headless-safe``  — multi-user.target as default (takes effect at next boot;
                       the running desktop is left alone so nothing disappears
                       under a logged-in user).
* ``headless-max``   — multi-user.target as default **and** stop the display
                       manager now; the reclaimed memory is measured, not guessed.

On a GB10 a GNOME session typically holds 1.5-3 GiB of unified memory. Going
headless is also a prerequisite for recipes that use the firmware display
reservation as extra KV cache (``nvidia_drm modeset=1 fbdev=0``); that part is
deliberately not automated here — see README "Squeezing memory".
"""

from __future__ import annotations

from typing import Any

from .agent.privd import PrivClient
from .agent.sysinfo import desktop_facts, memory_snapshot
from .schemas.enums import HeadlessMode


def mode_steps(mode: HeadlessMode, now: bool) -> list[tuple[str, dict[str, Any]]]:
    if mode == HeadlessMode.DESKTOP:
        steps = [("boot_target", {"target": "graphical"})]
        if now:
            steps.append(("display_manager", {"action": "start"}))
        return steps
    steps = [("boot_target", {"target": "multi-user"})]
    if mode == HeadlessMode.HEADLESS_MAX or now:
        steps.append(("display_manager", {"action": "stop"}))
    return steps


def acts_now(mode: HeadlessMode | str, now: bool) -> bool:
    """Does applying ``mode`` start or stop the display manager right away (not only at next boot)?

    True for ``headless-max`` even without ``--now`` (it always stops the desktop), and for every mode
    with ``--now`` (``desktop --now`` starts it). ``headless-safe`` alone only changes the boot target.
    """
    return any(op == "display_manager" for op, _ in mode_steps(HeadlessMode(mode), now))


def apply_mode(mode: HeadlessMode, client: PrivClient, now: bool = False,
               dry_run: bool = False) -> dict[str, Any]:
    before = memory_snapshot()
    desk = desktop_facts()
    steps = mode_steps(mode, now)
    if dry_run:
        return {"mode": mode.value, "applied": False, "dry_run": True,
                "steps": [{"op": op, "params": p} for op, p in steps],
                "desktop_before": desk}
    results = []
    for op, params in steps:
        results.append({"op": op, "params": params, "result": client.call(op, params)})
    after = memory_snapshot()
    return {
        "mode": mode.value, "applied": True, "dry_run": False, "steps": results,
        "desktop_before": desk,
        "reclaimed_gib": round(after["mem_available_gib"] - before["mem_available_gib"], 2),
        "effective": "now" if any(s["op"] == "display_manager" for s in results) else "next boot",
    }
