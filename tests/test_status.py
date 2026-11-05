import datetime as dt
import json
from pathlib import Path

import pytest

from lifecycle import status, watch
from test_watch import RESPONSES, SOURCES, FakeFetch

DATASET = Path(__file__).resolve().parent.parent / "data" / "lifecycle.json"
PAGE_URL = "https://example.com/page"
CALENDAR_URL = "https://example.com/calendar.md"


def tree(path: Path) -> dict:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.fixture
def repo(tmp_path):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    watch.run(SOURCES, snapshots, FakeFetch())
    dataset = tmp_path / "lifecycle.json"
    dataset.write_text(json.dumps({"schema_version": 1, "records": [
        {"id": "b", "verified_on": "2026-11-04"},
        {"id": "a", "verified_on": "2026-10-20"},
    ]}), encoding="utf-8")
    out = tmp_path / "status"
    args = ["--dataset", str(dataset), "--sources", str(sources), "--snapshots-dir", str(snapshots), "--out-dir", str(out)]
    return args, snapshots, out


def read(out: Path, name: str) -> dict:
    return json.loads((out / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("age, color", [(0, "green"), (30, "green"), (31, "yellow"), (45, "yellow"), (46, "red")])
def test_data_color_thresholds(age, color):
    oldest = dt.date(2026, 11, 4)
    assert status.data_color(oldest, oldest + dt.timedelta(days=age)) == color


def test_all_sources_ok_and_unchanged_is_green(repo):
    args, _, out = repo
    assert status.main(args + ["--today", "2026-11-05"], FakeFetch()) == 0
    result = read(out, "status.json")
    assert result == {
        "records": 2,
        "data_verified_oldest": "2026-10-20",
        "data_verified_newest": "2026-11-04",
        "sources_total": 4,
        "sources_checked_on": "2026-11-05",
        "sources_ok": 4,
        "sources_failed": [],
        "sources_changed": [],
    }
    assert read(out, "badge-sources.json") == {
        "schemaVersion": 1, "label": "sources checked", "message": "2026-11-05", "color": "green",
    }
    assert read(out, "badge-data.json") == {
        "schemaVersion": 1, "label": "data verified", "message": "2026-10-20", "color": "green",
    }


def test_one_changed_source_is_yellow(repo):
    args, _, out = repo
    original = FakeFetch()

    def altered(url):
        text = original(url)
        return text.replace("28 February 2027", "31 March 2027") if url == CALENDAR_URL else text

    assert status.main(args + ["--today", "2026-11-05"], altered) == 0
    assert read(out, "status.json")["sources_changed"] == ["calendar"]
    assert read(out, "badge-sources.json")["color"] == "yellow"


def test_failed_and_changed_sources_are_red_exit_0_and_snapshots_untouched(repo):
    args, snapshots, out = repo
    before = tree(snapshots)
    fetch = FakeFetch(failing={PAGE_URL})

    def mixed(url):
        text = fetch(url)
        return text.replace("28 February 2027", "31 March 2027") if url == CALENDAR_URL else text

    assert status.main(args + ["--today", "2026-11-05"], mixed) == 0
    result = read(out, "status.json")
    assert result["sources_failed"] == ["page"]
    assert result["sources_changed"] == ["calendar"]
    assert result["sources_ok"] == 3
    assert read(out, "badge-sources.json")["color"] == "red"
    assert tree(snapshots) == before


def test_missing_snapshot_counts_as_changed_and_is_not_written(repo):
    args, snapshots, out = repo
    (snapshots / "prices.json").unlink()
    before = tree(snapshots)
    assert status.main(args + ["--today", "2026-11-05"], FakeFetch()) == 0
    assert read(out, "status.json")["sources_changed"] == ["prices"]
    assert tree(snapshots) == before


def test_output_is_deterministic_with_sorted_keys(repo):
    args, _, out = repo
    fetch = FakeFetch(failing={PAGE_URL, CALENDAR_URL})
    status.main(args + ["--today", "2026-11-05"], fetch)
    first = tree(out)
    status.main(args + ["--today", "2026-11-05"], FakeFetch(failing={CALENDAR_URL, PAGE_URL}))
    assert tree(out) == first
    assert read(out, "status.json")["sources_failed"] == ["calendar", "page"]
    for name, content in first.items():
        text = content.decode("utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n", name


def test_unreadable_sources_and_dataset_still_exit_0_with_red_badges(tmp_path):
    out = tmp_path / "status"
    missing = tmp_path / "missing.json"
    args = ["--dataset", str(missing), "--sources", str(missing), "--snapshots-dir", str(tmp_path / "s"),
            "--out-dir", str(out), "--today", "2026-11-05"]
    assert status.main(args, FakeFetch()) == 0
    assert read(out, "badge-data.json")["color"] == "red"
    assert read(out, "badge-data.json")["message"] == "unknown"
    assert read(out, "badge-sources.json")["color"] == "red"


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"schema_version": 1, "records": [{"id": "a"}]}),
    json.dumps({"schema_version": 1, "records": [{"id": "a", "verified_on": "2026-13-01"}]}),
])
def test_broken_dataset_gives_red_data_badge_and_exit_0(repo, tmp_path, content):
    args, _, out = repo
    dataset = tmp_path / "broken.json"
    dataset.write_text(content, encoding="utf-8")
    assert status.main(args + ["--dataset", str(dataset), "--today", "2026-11-05"], FakeFetch()) == 0
    assert read(out, "badge-data.json") == {
        "schemaVersion": 1, "label": "data verified", "message": "unknown", "color": "red",
    }


def test_invalid_sources_file_gives_red_sources_badge_and_exit_0(repo, tmp_path):
    args, _, out = repo
    sources = tmp_path / "bad-sources.json"
    sources.write_text(json.dumps({"sources": [{"id": "x", "kind": "html", "url": "http://example.com"}]}), encoding="utf-8")
    assert status.main(args + ["--sources", str(sources), "--today", "2026-11-05"], FakeFetch()) == 0
    assert read(out, "status.json")["sources_total"] == 0
    assert read(out, "badge-sources.json") == {
        "schemaVersion": 1, "label": "sources checked", "message": "none", "color": "red",
    }


def test_unreadable_snapshot_counts_as_failed_and_exit_0(repo):
    args, snapshots, out = repo
    (snapshots / "page.txt").write_bytes(b"\xff\xfe not utf-8")
    assert status.main(args + ["--today", "2026-11-05"], FakeFetch()) == 0
    result = read(out, "status.json")
    assert result["sources_failed"] == ["page"]
    assert result["sources_checked_on"] == "2026-11-05"
    assert read(out, "badge-sources.json")["color"] == "red"


def test_future_verified_on_is_red(repo):
    args, _, out = repo
    assert status.main(args + ["--today", "2026-10-19"], FakeFetch()) == 0
    assert read(out, "badge-data.json")["color"] == "red"


def test_all_sources_failed_writes_none_as_checked_date(repo):
    args, _, out = repo
    fetch = FakeFetch(failing=set(RESPONSES))
    assert status.main(args + ["--today", "2026-11-05"], fetch) == 0
    assert read(out, "status.json")["sources_checked_on"] == "none"
    assert read(out, "badge-sources.json")["message"] == "none"
    assert read(out, "badge-sources.json")["color"] == "red"


@pytest.mark.parametrize("age, color", [(30, "green"), (31, "yellow"), (46, "red")])
def test_committed_dataset_badge(tmp_path, age, color):
    oldest = min(r["verified_on"] for r in json.loads(DATASET.read_text(encoding="utf-8"))["records"])
    today = dt.date.fromisoformat(oldest) + dt.timedelta(days=age)
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": []}), encoding="utf-8")
    out = tmp_path / "status"
    args = ["--dataset", str(DATASET), "--sources", str(sources), "--snapshots-dir", str(tmp_path / "s"),
            "--out-dir", str(out), "--today", today.isoformat()]
    assert status.main(args, FakeFetch()) == 0
    assert read(out, "badge-data.json")["message"] == oldest
    assert read(out, "badge-data.json")["color"] == color
