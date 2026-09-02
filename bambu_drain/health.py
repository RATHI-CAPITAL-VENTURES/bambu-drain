"""Status reporting.

The failure this exists to catch: the drain loop silently stops, staging fills,
and the first symptom is the printer refusing to start a print three weeks
later. A stalled drain must announce itself.

`snapshot()` computes `problems`, `ok` and a one-line `verdict` here, on the
Pi, so that anything reading the pushed status file — RIA on the Mac, a
dashboard, a human — agrees about what "healthy" means without reimplementing
it. The verdict is deliberately STABLE during healthy operation so it can drive
a change-detecting watch.

**One check the Pi cannot make about itself: staleness.** The file's `ts` is the
drain loop's heartbeat, written every pass. If the loop stalls, or the Pi dies,
or the network drops, the file simply stops being updated — and a dead process
cannot report that it is dead. So the READER compares `ts` to now and injects
`drain_loop_age_seconds` before calling `problems()`. That single check covers
every way the Pi can go quiet, which is why it is the reader's job and not ours.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .drain import staging_usage_bytes


# How long the drain loop may go without a pass before that is itself a fault.
# It polls every 30 s by default, so ten minutes is many missed cycles, not a
# slow one.
STALE_AFTER_SECONDS = 600
STAGING_WARN_PCT = 75.0
# An integrity event is only news for this long. Without a bound, a single
# incident alarms forever — a `ship_mismatch` from a power cut hours earlier was
# still the reported verdict long after the cause was fixed, which trains you to
# ignore the alert.
EVENT_WINDOW_SECONDS = 3600


def usb_state() -> str | None:
    """What the USB controller thinks. `configured` means a host enumerated us."""
    p = Path("/sys/class/udc/fe980000.usb/state")
    try:
        return p.read_text().strip()
    except OSError:
        return None


def problems(payload: dict) -> list[str]:
    """What is wrong, in plain language. Empty means healthy.

    One definition of healthy, here, so the Pi's CLI and whatever reads the
    pushed status file cannot drift apart about what counts as a problem.
    """
    out = []
    g, d, a = payload["gadget"], payload["drain"], payload["archive"]

    if not g.get("exists"):
        out.append("USB gadget does not exist")
    elif not g.get("bound"):
        out.append("USB gadget is not bound to the UDC")
    elif not g.get("media_present"):
        out.append("no medium inserted — the printer sees an empty card reader")

    st = g.get("usb_state")
    if st == "not attached":
        out.append("printer not attached (usually a charge-only cable)")

    if d.get("staging_pct", 0) >= STAGING_WARN_PCT:
        out.append(
            f"staging {d['staging_pct']:.0f}% full — the Mac has not been "
            f"reachable and the drain loop stops at 100%"
        )

    age = payload.get("drain_loop_age_seconds")
    if age is not None and age > STALE_AFTER_SECONDS:
        out.append(f"drain loop has not run for {age / 60:.0f} minutes")

    now = payload.get("ts") or time.time()
    for ev in payload.get("recent_events", []):
        if ev["kind"] not in ("local_corrupt", "copy_mismatch", "ship_mismatch"):
            continue
        age = now - ev.get("ts", 0)
        if age > EVENT_WINDOW_SECONDS:
            continue
        out.append(
            f"data integrity event {int(age / 60)}m ago: {ev['kind']} — {ev['detail']}"
        )
        break

    if a.get("files_pending_ship", 0) and not payload.get("ship_reachable", True):
        out.append(f"{a['files_pending_ship']} file(s) waiting — Mac unreachable")

    return out


def verdict(payload: dict) -> str:
    """One line, stable when healthy. Suitable as a change-detector."""
    probs = problems(payload)
    return "ok" if not probs else "PROBLEM: " + "; ".join(probs)


def snapshot(cfg, ledger, gadget, drainer) -> dict:
    try:
        image_bytes = cfg.gadget.image.stat().st_size
    except OSError:
        image_bytes = 0

    used = staging_usage_bytes(cfg.drain.staging)
    stats = ledger.stats()
    recent = [
        {"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]}
        for r in ledger.recent_events(10)
    ]
    last_drain = next((e for e in recent if e["kind"] == "drain"), None)

    payload = {
        "ts": time.time(),
        "gadget": {
            "exists": gadget.exists,
            "bound": gadget.bound if gadget.exists else False,
            "media_present": gadget.media_present if gadget.exists else False,
            "image_bytes": image_bytes,
            "idle_seconds": round(gadget.idle_seconds(), 1) if cfg.gadget.image.exists() else None,
            "usb_state": usb_state(),
        },
        "drain": {
            "blocked_reason": drainer.blocked_reason(),
            "last_drain_ts": last_drain["ts"] if last_drain else None,
            "staging_bytes": used,
            "staging_budget_bytes": int(cfg.drain.staging_max_gb * 1024**3),
            "staging_pct": round(100 * used / (cfg.drain.staging_max_gb * 1024**3), 1),
        },
        "archive": stats,
        "recent_events": recent,
    }
    payload["problems"] = problems(payload)
    payload["ok"] = not payload["problems"]
    payload["verdict"] = verdict(payload)
    return payload


def write(cfg, payload: dict) -> None:
    path = Path(cfg.health.status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
