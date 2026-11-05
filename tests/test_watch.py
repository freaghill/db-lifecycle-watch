import hashlib
import http.client
import io
import json
import urllib.error
from pathlib import Path

import pytest

from lifecycle import watch

FIXTURES = Path(__file__).parent / "fixtures"
DATASET = Path(__file__).resolve().parent.parent / "data" / "lifecycle.json"

SOURCES = [
    {"id": "page", "kind": "html", "url": "https://example.com/page", "sections": [{"heading": "Support policy"}]},
    {"id": "calendar", "kind": "markdown", "url": "https://example.com/calendar.md", "sections": [{"heading": "Major versions"}]},
    {"id": "updates", "kind": "azure-updates", "url": "https://example.com/updates", "params": {"$filter": "contains(title,'MySQL')"}},
    {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices", "params": {"$filter": "productName eq 'X'"}},
]
RESPONSES = {
    "https://example.com/page": "page.html",
    "https://example.com/calendar.md": "calendar.md",
    "https://example.com/updates?%24filter=contains%28title%2C%27MySQL%27%29": "updates.json",
    "https://example.com/prices?%24filter=productName%20eq%20%27X%27": "prices.json",
}


class FakeFetch:
    def __init__(self, responses=None, failing=()):
        self.responses = dict(RESPONSES if responses is None else responses)
        self.failing = set(failing)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.failing:
            raise RuntimeError(f"GET {url} failed: timed out")
        return (FIXTURES / self.responses[url]).read_text(encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    return ["--sources", str(sources), "--snapshots-dir", str(snapshots)], snapshots


def test_first_run_writes_one_snapshot_per_source_and_lists_all(setup, capsys):
    args, snapshots = setup
    fetch = FakeFetch()
    assert watch.main(args, fetch) == 0
    out = capsys.readouterr().out
    for source in SOURCES:
        assert f"changed: {source['id']}" in out
    assert sorted(p.name for p in snapshots.iterdir()) == ["calendar.txt", "page.txt", "prices.json", "updates.json"]
    assert len(fetch.calls) == len(SOURCES)


def test_repeat_run_reports_zero_changed(setup, capsys):
    args, _ = setup
    watch.main(args, FakeFetch())
    capsys.readouterr()
    assert watch.main(args + ["--fail-on-change"], FakeFetch()) == 0
    assert "0 changed, 4 unchanged, 0 failed" in capsys.readouterr().out


def test_altered_snapshot_with_fail_on_change_exits_2_and_names_source(setup, capsys):
    args, snapshots = setup
    watch.main(args, FakeFetch())
    capsys.readouterr()
    path = snapshots / "calendar.txt"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[2] = "Version | Changed by hand\n"
    path.write_text("".join(lines), encoding="utf-8")
    assert watch.main(args + ["--fail-on-change"], FakeFetch()) == 2
    out = capsys.readouterr().out
    assert "changed: calendar" in out and "1 changed, 3 unchanged" in out
    assert "Version | End\n" in path.read_text(encoding="utf-8")


def test_change_without_flag_exits_0(setup):
    args, snapshots = setup
    watch.main(args, FakeFetch())
    (snapshots / "prices.json").write_text("{}\n", encoding="utf-8")
    assert watch.main(args, FakeFetch()) == 0


def test_network_error_reports_exits_1_and_keeps_other_snapshots(setup, capsys):
    args, snapshots = setup
    assert watch.main(args, FakeFetch(failing={"https://example.com/page"})) == 1
    captured = capsys.readouterr()
    assert "error: page: GET https://example.com/page failed" in captured.err
    assert not (snapshots / "page.txt").exists()
    assert (snapshots / "calendar.txt").exists() and (snapshots / "prices.json").exists()


def test_failed_source_leaves_existing_snapshot_untouched(setup):
    args, snapshots = setup
    watch.main(args, FakeFetch())
    before = (snapshots / "page.txt").read_text(encoding="utf-8")
    assert watch.main(args + ["--fail-on-change"], FakeFetch(failing={"https://example.com/page"})) == 1
    assert (snapshots / "page.txt").read_text(encoding="utf-8") == before


def test_only_limits_the_run(setup, capsys):
    args, snapshots = setup
    fetch = FakeFetch()
    assert watch.main(args + ["--only", "prices"], fetch) == 0
    assert [p.name for p in snapshots.iterdir()] == ["prices.json"]
    assert len(fetch.calls) == 1
    assert watch.main(args + ["--only", "nope"], fetch) == 1


def write_pages(tmp_path, pages):
    files = {}
    for n, (url, body) in enumerate(pages.items()):
        path = tmp_path / f"page{n}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        files[url] = path
    return lambda url: files[url].read_text(encoding="utf-8")


def test_prices_pagination_follows_next_page_link(tmp_path):
    first = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
    second = {"NextPageLink": None, "Items": [dict(first["Items"][2], armRegionName="japaneast", retailPrice=0.12)]}
    first["NextPageLink"] = "https://example.com/prices?page=2"
    fetch = write_pages(tmp_path, {"https://example.com/prices": first, "https://example.com/prices?page=2": second})
    source = {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices"}
    assert watch.run([source], tmp_path / "snaps", fetch) == (["prices"], [])
    rows = json.loads((tmp_path / "snaps" / "prices.json").read_text(encoding="utf-8"))["postgresql"]
    assert "japaneast" in {r["region"] for r in rows}


def test_updates_pagination_follows_odata_next_link(tmp_path):
    page = json.loads((FIXTURES / "updates.json").read_text(encoding="utf-8"))
    first = dict(page, value=page["value"][:2], **{"@odata.nextLink": "https://example.com/updates?$skip=2"})
    second = dict(page, value=page["value"][2:])
    fetch = write_pages(tmp_path, {"https://example.com/updates": first, "https://example.com/updates?$skip=2": second})
    source = {"id": "updates", "kind": "azure-updates", "url": "https://example.com/updates"}
    assert watch.run([source], tmp_path / "snaps", fetch) == (["updates"], [])
    items = json.loads((tmp_path / "snaps" / "updates.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in items] == ["900001", "900003", "900005", "900006", "900007", "900008"]


def test_hitting_max_pages_with_a_next_link_left_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "MAX_PAGES", 3)
    calls = []

    def endless(url):
        calls.append(url)
        return json.dumps({"Items": [], "NextPageLink": f"https://example.com/prices?page={len(calls) + 1}"})

    source = {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices"}
    changed, errors = watch.run([source], tmp_path / "snaps", endless)
    assert changed == [] and len(errors) == 1 and "more than 3 pages" in errors[0]
    assert len(calls) == 3 and not (tmp_path / "snaps" / "prices.json").exists()


def test_exactly_max_pages_without_a_next_link_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "MAX_PAGES", 3)
    item = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))["Items"][2]
    calls = []

    def three_pages(url):
        calls.append(url)
        link = f"https://example.com/prices?page={len(calls) + 1}" if len(calls) < 3 else None
        return json.dumps({"Items": [item], "NextPageLink": link})

    source = {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices"}
    assert watch.run([source], tmp_path / "snaps", three_pages) == (["prices"], [])
    assert len(calls) == 3


def test_next_link_host_compare_ignores_case_and_default_port(tmp_path):
    item = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))["Items"][2]
    fetch = write_pages(tmp_path, {
        "https://example.com/prices": {"Items": [item], "NextPageLink": "https://EXAMPLE.com:443/prices?page=2"},
        "https://EXAMPLE.com:443/prices?page=2": {"Items": [], "NextPageLink": None},
    })
    source = {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices"}
    assert watch.run([source], tmp_path / "snaps", fetch) == (["prices"], [])


@pytest.mark.parametrize("link", ["http://example.com/prices?page=2", "https://evil.example/prices?page=2", "https://example.com:8443/prices?page=2"])
def test_next_link_must_be_https_on_the_same_host(tmp_path, link):
    fetch = write_pages(tmp_path, {"https://example.com/prices": {"Items": [], "NextPageLink": link}})
    source = {"id": "prices", "kind": "azure-prices", "url": "https://example.com/prices"}
    changed, errors = watch.run([source], tmp_path / "snaps", fetch)
    assert changed == [] and "next page link must be https on example.com" in errors[0]


def test_params_join_with_ampersand_when_url_has_a_query():
    source = {"id": "x", "kind": "azure-prices", "url": "https://example.com/p?api-version=1", "params": {"$filter": "a eq 'b'"}}
    assert watch.source_url(source) == "https://example.com/p?api-version=1&%24filter=a%20eq%20%27b%27"
    assert watch.source_url(dict(source, url="https://example.com/p")).startswith("https://example.com/p?%24filter=")


def test_missing_section_fails_and_keeps_the_old_snapshot(setup, tmp_path, capsys):
    args, snapshots = setup
    watch.main(args, FakeFetch())
    before = (snapshots / "page.txt").read_text(encoding="utf-8")
    moved = tmp_path / "moved.html"
    moved.write_text("<main><h2>Renamed section</h2><table><tr><td>14</td></tr></table></main>", encoding="utf-8")
    capsys.readouterr()
    assert watch.main(args, FakeFetch(dict(RESPONSES, **{"https://example.com/page": str(moved)}))) == 1
    assert "error: page: section 'Support policy': found 0 matching headings" in capsys.readouterr().err
    assert (snapshots / "page.txt").read_text(encoding="utf-8") == before


def test_page_without_main_fails_instead_of_using_the_whole_document(setup, capsys):
    args, snapshots = setup
    assert watch.main(args, FakeFetch(dict(RESPONSES, **{"https://example.com/page": "calendar.md"}))) == 1
    assert "error: page: expected one <main> element, found 0" in capsys.readouterr().err
    assert not (snapshots / "page.txt").exists()


def test_empty_result_fails_and_keeps_the_old_snapshot(setup, tmp_path, capsys):
    args, snapshots = setup
    watch.main(args, FakeFetch())
    before = (snapshots / "calendar.txt").read_text(encoding="utf-8")
    empty = tmp_path / "empty.md"
    empty.write_text("# Example calendar\n\n## Major versions\n\nNo table today.\n", encoding="utf-8")
    capsys.readouterr()
    fetch = FakeFetch(dict(RESPONSES, **{"https://example.com/calendar.md": str(empty)}))
    assert watch.main(args, fetch) == 1
    assert "error: calendar: section 'Major versions': no table rows" in capsys.readouterr().err
    assert (snapshots / "calendar.txt").read_text(encoding="utf-8") == before


class FakeResponse(io.BytesIO):
    class headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(watch.time, "sleep", sleeps.append)

    def install(outcomes):
        def urlopen(request, timeout):
            calls.append((request.full_url, request.get_header("User-agent"), timeout))
            outcome = outcomes.pop(0)
            if isinstance(outcome, tuple):
                raise urllib.error.HTTPError(request.full_url, outcome[0], "error", outcome[1], None)
            if isinstance(outcome, int):
                raise urllib.error.HTTPError(request.full_url, outcome, "error", {}, None)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome.encode("utf-8"))

        monkeypatch.setattr(watch.urllib.request, "urlopen", urlopen)
        return calls, sleeps

    return install


def test_http_get_does_not_retry_a_404(fake_urlopen):
    calls, sleeps = fake_urlopen([404])
    with pytest.raises(RuntimeError, match="404"):
        watch.http_get("https://example.com/x")
    assert len(calls) == 1 and sleeps == []


def test_http_get_tries_a_503_three_times(fake_urlopen):
    calls, sleeps = fake_urlopen([503, 503, 503])
    with pytest.raises(RuntimeError, match="503"):
        watch.http_get("https://example.com/x")
    assert len(calls) == 3 and len(sleeps) == 2


def test_http_get_returns_body_after_a_503(fake_urlopen):
    calls, _ = fake_urlopen([503, "hello"])
    assert watch.http_get("https://example.com/x") == "hello"
    assert len(calls) == 2
    assert all(ua == watch.USER_AGENT and timeout == 20 for _, ua, timeout in calls)


def test_http_get_retries_network_errors(fake_urlopen):
    calls, _ = fake_urlopen([urllib.error.URLError("timed out"), TimeoutError(), "ok"])
    assert watch.http_get("https://example.com/x") == "ok" and len(calls) == 3


def test_watch_never_touches_the_dataset(setup):
    args, _ = setup
    digest = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    watch.main(args, FakeFetch())
    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == digest


def test_committed_sources_file_is_valid():
    sources = watch.load_sources(watch.DEFAULT_SOURCES)
    assert {s["kind"] for s in sources} == {"html", "markdown", "azure-updates", "azure-prices"}


def test_user_agent_has_no_personal_data():
    assert "@" not in watch.USER_AGENT and "http" not in watch.USER_AGENT


GOOD = {"id": "a", "kind": "html", "url": "https://example.com", "sections": [{"heading": "T"}]}


@pytest.mark.parametrize(
    "sources, message",
    [
        ([dict(GOOD, url="http://example.com")], "url must be https"),
        ([dict(GOOD, id="../evil")], "must match"),
        ([dict(GOOD, id="Upper")], "must match"),
        ([{k: v for k, v in GOOD.items() if k != "url"}], "missing or empty key 'url'"),
        ([{k: v for k, v in GOOD.items() if k != "sections"}], "missing key 'sections'"),
        ([dict(GOOD, sections=[{"heading": "T", "keep": "all"}])], "keep must be"),
        ([dict(GOOD, kind="rss")], "unknown kind"),
        ([GOOD, GOOD], "duplicate source id"),
    ],
)
def test_bad_sources_file_is_rejected_with_a_clear_error(tmp_path, sources, message):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        watch.load_sources(path)


def test_sources_file_without_sources_key_gives_a_clear_error(tmp_path, capsys):
    path = tmp_path / "sources.json"
    path.write_text("{}", encoding="utf-8")
    assert watch.main(["--sources", str(path)]) == 1
    assert "missing key 'sources'" in capsys.readouterr().err


def test_http_get_retries_a_429_honouring_retry_after_capped(fake_urlopen):
    calls, sleeps = fake_urlopen([(429, {"Retry-After": "5"}), (429, {"Retry-After": "600"}), "ok"])
    assert watch.http_get("https://example.com/x") == "ok"
    assert len(calls) == 3 and sleeps == [5, 30]


def test_http_get_429_without_retry_after_uses_default_backoff(fake_urlopen):
    calls, sleeps = fake_urlopen([429, "ok"])
    assert watch.http_get("https://example.com/x") == "ok" and sleeps == [2]


def test_http_get_retries_http_exceptions(fake_urlopen):
    calls, _ = fake_urlopen([http.client.IncompleteRead(b"part"), "ok"])
    assert watch.http_get("https://example.com/x") == "ok" and len(calls) == 2
