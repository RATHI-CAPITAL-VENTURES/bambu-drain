#!/usr/bin/env python3
"""Move closed minor series out of CHANGELOG.md into docs/changelog/<X.Y>.md.

The `changelog-archive` guard fails when the root changelog holds more than one
minor series and tells you to run `make changelog-archive`. This is that.
"""

from __future__ import annotations

import re
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
ARCHIVE = ROOT / "docs" / "changelog"
HEADER = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\b.*$", re.M)


def split_sections(text: str) -> tuple[str, "OrderedDict[str, list[tuple[str, str]]]"]:
    """Return (preamble, {minor_series: [(header_line, body), ...]})."""
    matches = list(HEADER.finditer(text))
    if not matches:
        return text, OrderedDict()

    preamble = text[: matches[0].start()]
    series: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        key = f"{m.group(1)}.{m.group(2)}"
        series.setdefault(key, []).append((m.group(0), text[m.end():end].rstrip()))
    return preamble, series


def main() -> int:
    if not CHANGELOG.exists():
        print("no CHANGELOG.md", file=sys.stderr)
        return 1

    preamble, series = split_sections(CHANGELOG.read_text())
    if len(series) <= 1:
        print("nothing to archive — CHANGELOG.md holds a single minor series")
        return 0

    keep = next(iter(series))
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    for key in list(series)[1:]:
        target = ARCHIVE / f"{key}.md"
        body = [f"# Changelog — {key} series", "",
                "Archived from `CHANGELOG.md`. See the root changelog for the "
                "current series.", ""]
        for header, text in series[key]:
            body += [header, text, ""]
        target.write_text("\n".join(body).rstrip() + "\n")
        print(f"· archived {key} -> {target.relative_to(ROOT)}")

    kept = [preamble.rstrip(), ""]
    for header, text in series[keep]:
        kept += [header, text, ""]
    kept += ["Older series are archived under `docs/changelog/`.", ""]
    CHANGELOG.write_text("\n".join(kept).lstrip("\n"))
    print(f"· CHANGELOG.md now holds {keep} only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
