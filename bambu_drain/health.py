"""Status reporting.

The failure this exists to catch: the drain loop silently stops, staging fills,
and the first symptom is the printer refusing to start a print three weeks
later. A stalled drain must announce itself.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .drain import staging_usage_bytes


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

    return {
        "ts": time.time(),
        "gadget": {
            "exists": gadget.exists,
            "bound": gadget.bound if gadget.exists else False,
            "media_present": gadget.media_present if gadget.exists else False,
            "image_bytes": image_bytes,
            "idle_seconds": round(gadget.idle_seconds(), 1) if cfg.gadget.image.exists() else None,
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


def write(cfg, payload: dict) -> None:
    path = Path(cfg.health.status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
