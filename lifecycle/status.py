"""Write status/ JSON and shields.io badges for data freshness and source checks.

Read-only for the dataset, sources and snapshots. Always exits 0: the badges carry the state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from lifecycle.extract import extract
from lifecycle.validate import DEFAULT_PATH as DEFAULT_DATASET
from lifecycle.watch import (
    DEFAULT_SNAPSHOTS,
    DEFAULT_SOURCES,
    Fetcher,
    fetch_source,
    http_get,
    load_sources,
    snapshot_path,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "status"
GREEN_MAX_DAYS = 30
YELLOW_MAX_DAYS = 45


def data_color(oldest: dt.date | None, today: dt.date) -> str:
    if oldest is None:
        return "red"
    age = (today - oldest).days
    if age < 0:
        return "red"
    if age <= GREEN_MAX_DAYS:
        return "green"
    if age <= YELLOW_MAX_DAYS:
        return "yellow"
    return "red"


def sources_color(total: int, changed: list[str], failed: list[str]) -> str:
    if failed or total == 0:
        return "red"
    if changed:
        return "yellow"
    return "green"


def check_sources(sources: list[dict], snapshots_dir: Path, fetch: Fetcher) -> tuple[list[str], list[str]]:
    """Returns (changed ids, failed ids). Compares in memory and never writes snapshots."""
    changed, failed = [], []
    for source in sources:
        try:
            content = extract(source, fetch_source(source, fetch))
            path = snapshot_path(snapshots_dir, source)
            old = path.read_text(encoding="utf-8") if path.exists() else None
        except Exception as error:  # one broken source must not stop the others
            print(f"error: {source['id']}: {error}", file=sys.stderr)
            failed.append(source["id"])
            continue
        if old != content:
            changed.append(source["id"])
    return sorted(changed), sorted(failed)


def verified_dates(dataset: Path) -> tuple[int, list[dt.date]]:
    records = json.loads(dataset.read_text(encoding="utf-8"))["records"]
    return len(records), sorted(dt.date.fromisoformat(r["verified_on"]) for r in records)


def build_status(dataset: Path, sources_path: Path, snapshots_dir: Path, today: dt.date, fetch: Fetcher) -> dict:
    try:
        records, dates = verified_dates(dataset)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: dataset: {error}", file=sys.stderr)
        records, dates = 0, []
    try:
        sources = load_sources(sources_path)
    except (OSError, ValueError) as error:
        print(f"error: sources: {error}", file=sys.stderr)
        sources = []
    changed, failed = check_sources(sources, snapshots_dir, fetch)
    oldest = dates[0] if dates else None
    return {
        "records": records,
        "data_verified_oldest": oldest.isoformat() if oldest else None,
        "data_verified_newest": dates[-1].isoformat() if dates else None,
        "sources_total": len(sources),
        "sources_checked_on": today.isoformat() if len(failed) < len(sources) else "none",
        "sources_ok": len(sources) - len(failed),
        "sources_failed": failed,
        "sources_changed": changed,
    }


def badges(status: dict, today: dt.date) -> tuple[dict, dict]:
    oldest = status["data_verified_oldest"]
    color = data_color(dt.date.fromisoformat(oldest) if oldest else None, today)
    data = badge("data verified", oldest or "unknown", color)
    color = sources_color(status["sources_total"], status["sources_changed"], status["sources_failed"])
    return data, badge("sources checked", status["sources_checked_on"], color)


def badge(label: str, message: str, color: str) -> dict:
    return {"schemaVersion": 1, "label": label, "message": message, "color": color}


def dump(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_outputs(status: dict, out_dir: Path, today: dt.date) -> None:
    data_badge, sources_badge = badges(status, today)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.json").write_text(dump(status), encoding="utf-8")
    (out_dir / "badge-data.json").write_text(dump(data_badge), encoding="utf-8")
    (out_dir / "badge-sources.json").write_text(dump(sources_badge), encoding="utf-8")


def main(argv: list[str] | None = None, fetch: Fetcher = http_get) -> int:
    parser = argparse.ArgumentParser(prog="python -m lifecycle.status", description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--snapshots-dir", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD, default today (UTC)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    today = args.today or dt.datetime.now(dt.timezone.utc).date()

    status = build_status(args.dataset, args.sources, args.snapshots_dir, today, fetch)
    write_outputs(status, args.out_dir, today)
    data_badge, sources_badge = badges(status, today)
    print(
        f"data verified {data_badge['message']} ({data_badge['color']}), "
        f"sources {status['sources_ok']}/{status['sources_total']} ok, "
        f"{len(status['sources_changed'])} changed ({sources_badge['color']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
