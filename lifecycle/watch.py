"""Fetch official sources, write deterministic snapshots and report which changed.

This tool only writes under the snapshots directory. The dataset is edited by hand.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from lifecycle.extract import KINDS, SNAPSHOT_SUFFIX, extract

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = ROOT / "watch" / "sources.json"
DEFAULT_SNAPSHOTS = ROOT / "watch" / "snapshots"
USER_AGENT = "lifecycle-watch/0.1 (managed-database lifecycle dataset; polite twice-daily check)"
TIMEOUT_SECONDS = 20
RETRIES = 2
MAX_RETRY_AFTER_SECONDS = 30
MAX_PAGES = 10

Fetcher = Callable[[str], str]


def _retry_after(error: urllib.error.HTTPError) -> int | None:
    value = (error.headers.get("Retry-After") if error.headers else None) or ""
    return min(int(value), MAX_RETRY_AFTER_SECONDS) if value.strip().isdigit() else None


def http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        wait = 2 * (attempt + 1)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read().decode(response.headers.get_content_charset() or "utf-8")
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code < 500 and error.code != 429:
                break
            if error.code == 429:
                wait = _retry_after(error) or wait
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            last_error = error
        if attempt < RETRIES:
            time.sleep(wait)
    raise RuntimeError(f"GET {url} failed: {last_error}")


def source_url(source: dict) -> str:
    params = source.get("params") or {}
    if not params:
        return source["url"]
    joiner = "&" if "?" in source["url"] else "?"
    return source["url"] + joiner + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


NEXT_LINK_FIELD = {"azure-updates": "@odata.nextLink", "azure-prices": "NextPageLink"}


def next_page(kind: str, payload: str, current: str) -> str | None:
    field = NEXT_LINK_FIELD.get(kind)
    if not field:
        return None
    link = json.loads(payload).get(field)
    if not link:
        return None
    parsed, base = urllib.parse.urlsplit(link), urllib.parse.urlsplit(current)
    if parsed.scheme != "https" or (parsed.hostname, parsed.port or 443) != (base.hostname, base.port or 443):
        raise ValueError(f"next page link must be https on {base.hostname}: {link}")
    return link


def fetch_source(source: dict, fetch: Fetcher) -> list[str]:
    url: str | None = source_url(source)
    payloads = []
    while url:
        if len(payloads) == MAX_PAGES:
            raise ValueError(f"more than {MAX_PAGES} pages; stopping instead of writing a partial snapshot")
        payload = fetch(url)
        payloads.append(payload)
        url = next_page(source["kind"], payload, url)
    return payloads


def snapshot_path(snapshots_dir: Path, source: dict) -> Path:
    return snapshots_dir / f"{source['id']}{SNAPSHOT_SUFFIX[source['kind']]}"


SOURCE_ID = re.compile(r"[a-z0-9-]+")


def _check_source(n: int, source) -> None:
    if not isinstance(source, dict):
        raise ValueError(f"source #{n}: must be an object")
    for key in ("id", "kind", "url"):
        if not isinstance(source.get(key), str) or not source[key]:
            raise ValueError(f"source #{n}: missing or empty key {key!r}")
    sid = source["id"]
    if not SOURCE_ID.fullmatch(sid):
        raise ValueError(f"source #{n}: id {sid!r} must match [a-z0-9-]+")
    if source["kind"] not in KINDS:
        raise ValueError(f"{sid}: unknown kind {source['kind']!r}")
    if not source["url"].startswith("https://"):
        raise ValueError(f"{sid}: url must be https")
    if "params" in source and not isinstance(source["params"], dict):
        raise ValueError(f"{sid}: params must be an object")
    if source["kind"] in ("html", "markdown"):
        sections = source.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"{sid}: missing key 'sections' (a non-empty list)")
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("heading"), str) or not section["heading"].strip():
                raise ValueError(f"{sid}: each section needs a non-empty 'heading'")
            if section.get("keep", "tables") not in ("tables", "digest"):
                raise ValueError(f"{sid}: section keep must be 'tables' or 'digest'")


def load_sources(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError(f"{path}: missing key 'sources' (a list)")
    sources = data["sources"]
    for n, source in enumerate(sources):
        _check_source(n, source)
    ids = [s["id"] for s in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source id in sources file")
    return sources


def run(sources: list[dict], snapshots_dir: Path, fetch: Fetcher = http_get) -> tuple[list[str], list[str]]:
    """Returns (changed ids, error messages). Snapshots of failed sources are left untouched."""
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    changed, errors = [], []
    for source in sources:
        try:
            content = extract(source, fetch_source(source, fetch))
        except Exception as error:  # one broken source must not stop the others
            errors.append(f"{source['id']}: {error}")
            continue
        path = snapshot_path(snapshots_dir, source)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != content:
            path.write_text(content, encoding="utf-8")
            changed.append(source["id"])
    return changed, errors


def main(argv: list[str] | None = None, fetch: Fetcher = http_get) -> int:
    parser = argparse.ArgumentParser(prog="python -m lifecycle.watch", description=__doc__)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--snapshots-dir", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--fail-on-change", action="store_true", help="exit 2 when any snapshot changed")
    parser.add_argument("--only", action="append", metavar="ID", help="watch only this source id (repeatable)")
    args = parser.parse_args(argv)

    try:
        sources = load_sources(args.sources)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.only:
        unknown = sorted(set(args.only) - {s["id"] for s in sources})
        if unknown:
            print(f"unknown source id: {', '.join(unknown)}", file=sys.stderr)
            return 1
        sources = [s for s in sources if s["id"] in args.only]

    changed, errors = run(sources, args.snapshots_dir, fetch)
    for source_id in changed:
        print(f"changed: {source_id}")
    for message in errors:
        print(f"error: {message}", file=sys.stderr)
    print(f"{len(changed)} changed, {len(sources) - len(changed) - len(errors)} unchanged, {len(errors)} failed")
    if errors:
        return 1
    if changed and args.fail_on_change:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
