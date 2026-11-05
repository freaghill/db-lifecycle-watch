import ast
import csv
import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = ROOT / "checker" / "azure_es_check.py"
DATASET = FIXTURES / "checker_dataset.json"
PRICES = FIXTURES / "checker_prices.json"

spec = importlib.util.spec_from_file_location("azure_es_check", SCRIPT)
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)

TODAY = dt.date(2026, 11, 4)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def server(name="pg-demo-9", engine="pg", version="13", sku="Standard_D2ds_v5", location="eastus",
           ha="Disabled", state="Ready", role="None"):
    typ = {"pg": "microsoft.dbforpostgresql/flexibleservers", "my": "microsoft.dbformysql/flexibleservers"}[engine]
    return {"name": name, "type": typ, "resourceGroup": "rg-demo", "subscriptionId": "sub-demo",
            "location": location, "skuName": sku, "version": version, "haMode": ha,
            "replicationRole": role, "state": state}


class FakeAz:
    def __init__(self, pages, login=True):
        self.pages = list(pages)
        self.login = login
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[1:3] == ["account", "show"]:
            return (0, "{}", "") if self.login else (1, "", "ERROR: Please run 'az login' to setup account.")
        if args[1:3] == ["graph", "query"]:
            return 0, json.dumps(self.pages.pop(0)), ""
        raise AssertionError(f"unexpected az call {args}")


def no_fetch(url):
    raise AssertionError(f"unexpected fetch {url}")


def records():
    return chk.load_dataset(DATASET)


def rows_for(servers, prices=PRICES, fetcher=no_fetch, today=TODAY):
    items = chk.load_prices_file(prices) if prices else None
    return chk.build_rows(servers, records(), today, fetcher=fetcher, price_items=items)


def run(capsys, pages, *extra, runner=None, fetcher=no_fetch):
    runner = runner or FakeAz(pages)
    code = chk.main(["--today", "2026-11-04", "--dataset", str(DATASET), "--prices-file", str(PRICES), *extra],
                    runner=runner, fetcher=fetcher)
    return code, capsys.readouterr()


def by_name(rows):
    return {r["server"]: r for r in rows}


# --- acceptance ---

def test_ac1_mixed_servers(capsys):
    code, out = run(capsys, [fixture("checker_inventory_ac1.json")])
    assert code == 1
    lines = out.out.splitlines()
    pg13 = next(line for line in lines if line.startswith("pg-demo-1 "))
    assert "in-extended-support" in pg13 and "204.40-408.80" in pg13 and "4 vCore HA" in pg13
    assert "HA standby billing not documented" in pg13 and "17" in pg13
    mysql = next(line for line in lines if line.startswith("mysql-demo-1 "))
    assert "extended-support-from 2027-02-01" in mysql and "102.20" in mysql
    assert "8.4 (dates not published)" in mysql
    pg16 = next(line for line in lines if line.startswith("pg-demo-2 "))
    assert re.search(r"\bsupported\b", pg16) and "extended-support" not in pg16
    assert ("2 affected servers. Estimated extended support cost: 306.60-511.00 USD per month "
            "(now: 204.40-408.80, upcoming: 102.20).") in out.out
    assert "Scanned 3 servers in 1 subscription." in out.out
    assert [line.split()[0] for line in lines[1:4]] == ["pg-demo-1", "mysql-demo-1", "pg-demo-2"]


def test_ac2_no_servers_exit_0(capsys):
    code, out = run(capsys, [{"count": 0, "data": []}])
    assert code == 0
    assert "No affected servers" in out.out


def test_ac2_only_supported_servers_exit_0(capsys):
    code, out = run(capsys, [{"data": [server(version="17")]}])
    assert code == 0
    assert "No affected servers" in out.out


def test_ac3_az_missing_exit_3(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    code = chk.main(["--dataset", str(DATASET)])
    out = capsys.readouterr()
    assert code == 3
    assert out.err.strip().count("\n") == 0 and "az not found" in out.err
    assert "Traceback" not in out.err and out.out == ""


def test_not_logged_in_exit_3(capsys):
    code, out = run(capsys, [], runner=FakeAz([], login=False))
    assert code == 3 and "az login" in out.err and out.err.count("\n") == 1


def test_missing_resource_graph_extension_exit_3(capsys):
    def runner(args):
        if args[1:3] == ["account", "show"]:
            return 0, "{}", ""
        return 2, "", "ERROR: 'graph' is misspelled or not recognized by the system."
    code, out = run(capsys, [], runner=runner)
    assert code == 3 and "az extension add --name resource-graph" in out.err and out.err.count("\n") == 1


def test_missing_dataset_exit_3(capsys, tmp_path):
    code = chk.main(["--dataset", str(tmp_path / "nope.json")], runner=FakeAz([]))
    assert code == 3 and "Dataset not found" in capsys.readouterr().err


def test_ac4_unknown_version():
    row = rows_for([server(version="10")])[0]
    assert row["status"] == "unknown-version"
    assert row["monthly_cost_usd"] == "-"
    assert not re.search(r"\d{4}-\d{2}-\d{2}", " ".join(chk.public(row).values()))
    assert row["_date"] is None and row["_low"] is None


def test_ac5_csv_and_json_match_stdout(capsys, tmp_path):
    csv_path, json_path = tmp_path / "out.csv", tmp_path / "out.json"
    code, out = run(capsys, [fixture("checker_inventory_ac1.json")], "--csv", str(csv_path), "--json", str(json_path))
    assert code == 1
    with open(csv_path, newline="") as f:
        csv_rows = list(csv.DictReader(f))
    json_rows = json.loads(json_path.read_text())["rows"]
    assert csv_rows == json_rows
    assert len(csv_rows) == 3
    table = out.out.split("\n\n")[0].splitlines()
    assert len(table) == 1 + len(csv_rows)
    header = table[0]
    starts = [header.index(c.replace("_", " ")) for c in chk.COLUMNS]
    assert header.split() == " ".join(c.replace("_", " ") for c in chk.COLUMNS).split()
    for line, row in zip(table[1:], csv_rows):
        bounds = starts[1:] + [None]
        values = {c: line[a:b].strip() for c, a, b in zip(chk.COLUMNS, starts, bounds)}
        assert values == row


def test_no_files_written_without_flags(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(capsys, [fixture("checker_inventory_ac1.json")])
    assert list(tmp_path.iterdir()) == []


def test_ac7_stdlib_imports_only():
    tree = ast.parse(SCRIPT.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names)
    assert "lifecycle" not in modules


def test_ac7_network_limited_to_az_and_prices_host():
    src = SCRIPT.read_text()
    assert set(re.findall(r"https?://[^\s\"'/]+", src)) == {"https://prices.azure.com"}
    with pytest.raises(ValueError):
        chk.http_fetch("https://example.com/x")
    with pytest.raises(ValueError):
        chk.run_az(lambda a: (0, "", ""), ["group", "delete"])
    assert src.count("subprocess.run(") == 1


def test_az_calls_are_read_only(capsys):
    runner = FakeAz([fixture("checker_inventory_ac1.json")])
    run(capsys, [], runner=runner)
    assert [c[1:3] for c in runner.calls] == [["account", "show"], ["graph", "query"]]


# --- status ---

REC = {"standard_support_end": "2026-07-31", "extended_support_start": "2026-08-01",
       "extended_support_billing_start": "2026-09-01", "extended_support_end": "2027-03-31"}


@pytest.mark.parametrize("record,today,expected", [
    (REC, dt.date(2026, 9, 1), ("in-extended-support", dt.date(2026, 9, 1))),
    (REC, dt.date(2026, 8, 15), ("extended-support-from", dt.date(2026, 9, 1))),
    (dict(REC, extended_support_billing_start=None), dt.date(2026, 8, 1), ("in-extended-support", dt.date(2026, 8, 1))),
    (REC, dt.date(2025, 9, 1), ("extended-support-from", dt.date(2026, 9, 1))),
    (REC, dt.date(2025, 8, 31), ("supported", None)),
    (REC, dt.date(2027, 3, 31), ("in-extended-support", dt.date(2026, 9, 1))),
    (REC, dt.date(2027, 4, 1), ("past-extended-support", dt.date(2027, 3, 31))),
    (None, TODAY, ("unknown-version", None)),
    (dict(REC, extended_support_start=None, extended_support_billing_start=None, extended_support_end=None),
     dt.date(2026, 8, 1), ("unknown-version", None)),
    (dict(REC, extended_support_start=None, extended_support_billing_start=None, extended_support_end=None),
     dt.date(2026, 7, 31), ("supported", None)),
    ({k: None for k in REC}, TODAY, ("supported", None)),
])
def test_status_boundaries(record, today, expected):
    assert chk.status_for(record, today) == expected


def test_dates_not_published_is_supported_and_labelled_as_target():
    recs = records()
    assert chk.status_for(recs[("azure-database-for-mysql-flexible", "8.4")], TODAY) == ("supported", None)
    assert chk.upgrade_target(recs, "mysql", TODAY) == "8.4 (dates not published)"
    assert chk.upgrade_target(recs, "postgresql", TODAY) == "17"


def test_mysql_full_version_maps_to_major():
    assert chk.major_version("mysql", {"version": "8.0.21"}) == "8.0"
    assert chk.major_version("mysql", {"version": "5.7"}) == "5.7"
    assert chk.major_version("postgresql", {"version": "13"}) == "13"
    row = rows_for([server(engine="my", version="8.0.21", sku="Standard_B2s", location="westeurope")])[0]
    assert row["version"] == "8.0" and row["status"] == "extended-support-from 2027-02-01"


@pytest.mark.parametrize("sku,expected", [
    ("Standard_E16ds_v5", 16), ("Standard_B1ms", 1), ("Standard_D4ds_v5", 4), ("Standard_B2s", 2),
    ("GP_Gen5_x", None), ("", None), (None, None),
])
def test_parse_vcores(sku, expected):
    assert chk.parse_vcores(sku) == expected


def test_unparseable_sku_gives_unknown_cost():
    row = rows_for([server(sku="GP_Gen5_x")])[0]
    assert row["monthly_cost_usd"] == "unknown" and row["_low"] is None
    assert "vCores not found" in row["notes"]


# --- cost ---

def test_mysql_ha_doubles_cost():
    row = rows_for([server(engine="my", version="8.0", sku="Standard_B2s", location="westeurope", ha="ZoneRedundant")])[0]
    assert row["monthly_cost_usd"] == "204.40"


def test_postgresql_ha_gives_range_and_flag():
    row = rows_for([server(sku="Standard_D4ds_v5", ha="SameZone")])[0]
    assert row["monthly_cost_usd"] == "204.40-408.80"
    assert "HA standby billing not documented" in row["notes"]


def test_stopped_server_costs_zero():
    row = rows_for([server(state="Stopped")])[0]
    assert row["monthly_cost_usd"] == "0.00"
    assert "stopped servers are not billed; Azure restarts a stopped server after 7 days" in row["notes"]


def test_replica_is_its_own_row_priced_alone():
    rows = rows_for([server(name="pg-demo-1"), server(name="pg-demo-1-replica", role="AsyncReplica")])
    assert len(rows) == 2
    assert {r["monthly_cost_usd"] for r in rows} == {"102.20"}
    assert "read replica" in by_name(rows)["pg-demo-1-replica"]["notes"]


def test_region_without_price_is_unknown_with_no_fallback():
    row = rows_for([server(location="westeurope")])[0]
    assert row["monthly_cost_usd"] == "unknown"
    assert "no price for region" in row["notes"]


def test_free_tier_and_zero_meters_are_ignored():
    row = rows_for([server()], prices=FIXTURES / "checker_prices_free_only.json")[0]
    assert row["monthly_cost_usd"] == "unknown"
    assert chk.pick_price(fixture("checker_prices.json")["Items"], "postgresql", "eastus", TODAY) == 0.07
    assert chk.pick_price(fixture("checker_prices.json")["Items"], "mysql", "westeurope", TODAY) == 0.07


def test_cost_only_for_affected_statuses():
    rows = rows_for([server(version="16"), server(name="pg-old", version="10")])
    assert {r["monthly_cost_usd"] for r in rows} == {"-"}


def test_summary_counts_unknown_costs_outside_total():
    rows = rows_for([server(), server(name="pg-x", sku="GP_Gen5_x")])
    text = "\n".join(chk.summary(rows))
    assert "2 affected servers" in text and "102.20 USD per month" in text
    assert "Cost is unknown for 1 affected server" in text


# --- prices API ---

def test_api_fetch_is_cached_per_region_and_engine():
    urls = []

    def fetcher(url):
        urls.append(url)
        return fixture("checker_prices.json")

    rows = rows_for([server(name="a"), server(name="b"), server(name="c", engine="my", version="8.0")],
                    prices=None, fetcher=fetcher)
    assert len(urls) == 2
    assert all(u.startswith("https://prices.azure.com/api/retail/prices?") for u in urls)
    assert "armRegionName%20eq%20%27eastus%27" in urls[0]
    assert {r["monthly_cost_usd"] for r in rows} == {"102.20"}


def test_api_follows_next_page_link():
    pages = [{"Items": [], "NextPageLink": "https://prices.azure.com/api/retail/prices?page=2"},
             {"Items": fixture("checker_prices.json")["Items"], "NextPageLink": None}]
    got = []

    def fetcher(url):
        got.append(url)
        return pages[len(got) - 1]

    assert rows_for([server()], prices=None, fetcher=fetcher)[0]["monthly_cost_usd"] == "102.20"
    assert len(got) == 2


def test_api_failure_gives_unknown_cost_and_no_retry():
    calls = []

    def fetcher(url):
        calls.append(url)
        raise OSError("down")

    rows = rows_for([server(name="a"), server(name="b")], prices=None, fetcher=fetcher)
    assert len(calls) == 1
    assert all("price lookup failed" in r["notes"] and r["monthly_cost_usd"] == "unknown" for r in rows)


# --- inventory ---

def test_arg_paging_uses_skip_token():
    runner = FakeAz(fixture("checker_inventory_paged.json"))
    rows = chk.query_inventory(runner, ["sub-a", "sub-b"])
    assert [r["name"] for r in rows] == ["pg-demo-1", "pg-demo-3"]
    first, second = runner.calls
    assert "--skip-token" not in first
    assert second[second.index("--skip-token") + 1] == "token-page-2"
    assert first[first.index("--subscriptions") + 1:first.index("--subscriptions") + 3] == ["sub-a", "sub-b"]
    assert first[first.index("--first") + 1] == "1000"


def test_real_dataset_loads():
    recs = chk.load_dataset(ROOT / "data" / "lifecycle.json")
    assert ("azure-database-for-postgresql-flexible", "13") in recs
    assert ("azure-database-for-mysql-flexible", "8.0") in recs


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as e:
        chk.main(["--help"])
    assert e.value.code == 0
    assert "--prices-file" in capsys.readouterr().out


def test_offer_line_hidden_by_default(capsys):
    assert chk.OFFER_URL == ""
    _, out = run(capsys, [fixture("checker_inventory_ac1.json")])
    assert "upgrade:" not in out.out.lower()
    assert out.out.rstrip().splitlines()[-1].startswith("Dates: ")


# --- attention rows, targets, edge statuses ---

def test_past_and_unknown_rows_exit_0_with_attention_line(capsys):
    servers = [server(name="pg-old", version="10"),
               server(name="my-past", engine="my", version="5.7", sku="Standard_B2s")]
    code, out = run(capsys, [{"data": servers}], "--today", "2029-04-01")
    assert code == 0
    assert "No server is in or entering paid extended support." in out.out
    assert "2 servers need attention: 1 past the end of extended support, 1 on versions without published dates." in out.out
    assert "No affected servers" not in out.out
    rows = rows_for(servers, today=dt.date(2029, 4, 1))
    assert all(r["monthly_cost_usd"] == "-" for r in rows)
    assert by_name(rows)["my-past"]["status"] == "past-extended-support"
    assert by_name(rows)["my-past"]["upgrade_to"] == "8.4 (dates not published)"
    assert by_name(rows)["pg-old"]["upgrade_to"] == "17"


def test_supported_row_has_no_target():
    assert rows_for([server(version="16")])[0]["upgrade_to"] == "-"


def test_no_downgrade_target():
    row = rows_for([server(version="18")])[0]
    assert row["status"] == "unknown-version" and row["upgrade_to"] == "-"
    assert chk.upgrade_target(records(), "postgresql", TODAY, "17") is None


def test_end_passed_with_null_start_is_past():
    rec = {"standard_support_end": "2026-01-31", "extended_support_start": None,
           "extended_support_billing_start": None, "extended_support_end": "2026-06-30"}
    assert chk.status_for(rec, TODAY) == ("past-extended-support", dt.date(2026, 6, 30))


def test_standard_end_passed_and_es_far_away_is_unknown():
    rec = {"standard_support_end": "2026-01-31", "extended_support_start": "2028-01-01",
           "extended_support_billing_start": None, "extended_support_end": None}
    assert chk.status_for(rec, TODAY) == ("unknown-version", None)


def test_billing_start_not_published_note():
    row = rows_for([server(version="14")])[0]
    assert row["status"] == "extended-support-from 2027-02-01"
    assert "billing start not published; using extended support start" in row["notes"]
    assert "billing start not published" not in rows_for([server()])[0]["notes"]


# --- price selection ---

def price(value, start="2026-09-01T00:00:00Z", unit="1 Hour", currency="USD"):
    return {"armRegionName": "eastus", "productName": "Azure Database for Postgres Extended Support",
            "meterName": "Extended Support vCore", "skuName": "Extended Support", "retailPrice": value,
            "currencyCode": currency, "type": "Consumption", "unitOfMeasure": unit, "effectiveStartDate": start}


def test_pick_price_newer_date_wins_over_older_higher_price():
    items = [price(0.09, "2025-01-01T00:00:00Z"), price(0.07, "2026-09-01T00:00:00Z")]
    assert chk.pick_price(items, "postgresql", "eastus", TODAY) == 0.07


@pytest.mark.parametrize("item", [price(0.07, currency="EUR"), price(0.07, unit="1/Month"),
                                  price(0.07, start="2026-12-01T00:00:00Z")])
def test_pick_price_ignores_wrong_currency_unit_and_future_rows(item):
    assert chk.pick_price([item], "postgresql", "eastus", TODAY) is None


def test_price_page_not_a_dict_gives_unknown_cost():
    row = rows_for([server()], prices=None, fetcher=lambda url: ["not", "a", "page"])[0]
    assert row["monthly_cost_usd"] == "unknown" and "price lookup failed" in row["notes"]


def test_price_http_exception_gives_unknown_cost():
    import http.client

    def fetcher(url):
        raise http.client.IncompleteRead(b"")

    row = rows_for([server()], prices=None, fetcher=fetcher)[0]
    assert row["monthly_cost_usd"] == "unknown" and "price lookup failed" in row["notes"]


def test_price_list_truncated():
    page = {"Items": [], "NextPageLink": "https://prices.azure.com/api/retail/prices?next"}
    row = rows_for([server()], prices=None, fetcher=lambda url: page)[0]
    assert row["monthly_cost_usd"] == "unknown" and "price list truncated" in row["notes"]


# --- environment and input errors ---

def exit3(capsys, argv, runner=None):
    code = chk.main(argv, runner=runner or FakeAz([{"data": []}]), fetcher=no_fetch)
    err = capsys.readouterr().err
    assert code == 3 and err.startswith("error: ") and err.count("\n") == 1
    return err


def test_empty_records_exit_3(capsys, tmp_path):
    path = tmp_path / "d.json"
    path.write_text('{"records": []}')
    assert "no Azure flexible server records" in exit3(capsys, ["--dataset", str(path)])


@pytest.mark.parametrize("content", ["{not json", "[]", '{"records": {}}',
                                     '{"records": [{"service": "azure-database-for-mysql-flexible", "major_version": 8}]}',
                                     '{"records": [{"service": "azure-database-for-mysql-flexible", "major_version": "8.0", '
                                     '"standard_support_end": "2027-02-30"}]}'])
def test_bad_dataset_exit_3(capsys, tmp_path, content):
    path = tmp_path / "d.json"
    path.write_text(content)
    exit3(capsys, ["--dataset", str(path)])


def test_bad_prices_file_exit_3(capsys, tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"items": []}')
    exit3(capsys, ["--dataset", str(DATASET), "--prices-file", str(path)])


def test_unwritable_csv_exit_3_before_query(capsys, tmp_path):
    runner = FakeAz([])
    err = exit3(capsys, ["--dataset", str(DATASET), "--csv", str(tmp_path / "missing" / "out.csv")], runner=runner)
    assert "Cannot write" in err and runner.calls == []


def test_write_failure_exit_3(capsys, tmp_path, monkeypatch):
    def broken(path, rows):
        raise PermissionError("denied")

    monkeypatch.setattr(chk, "write_csv", broken)
    code, out = run(capsys, [{"data": []}], "--csv", str(tmp_path / "out.csv"))
    assert code == 3 and "cannot write output file" in out.err


def test_bad_today_exit_3(capsys):
    assert "--today" in exit3(capsys, ["--today", "2026-13-01", "--dataset", str(DATASET)])


def test_az_output_list_exit_3(capsys):
    exit3(capsys, ["--dataset", str(DATASET)], runner=FakeAz([[{"name": "x"}]]))


def test_repeated_skip_token_exit_3(capsys):
    page = {"data": [], "skip_token": "same"}
    assert "repeated skip token" in exit3(capsys, ["--dataset", str(DATASET)], runner=FakeAz([page, page, page]))


def test_skip_token_page_limit(monkeypatch):
    monkeypatch.setattr(chk, "MAX_INVENTORY_PAGES", 3)
    pages = [{"data": [], "skip_token": f"t{i}"} for i in range(5)]
    with pytest.raises(chk.EnvError, match="more than 3 pages"):
        chk.query_inventory(FakeAz(pages))


def test_az_runs_by_resolved_path(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["env"] = kwargs["env"]
        return type("P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    monkeypatch.setattr(chk.subprocess, "run", fake_run)
    chk.make_runner(r"C:\az\az.cmd")(["az", "account", "show"])
    assert seen["args"] == [r"C:\az\az.cmd", "account", "show"]
    assert seen["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"


def test_az_not_runnable_is_env_error(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(chk.subprocess, "run", fake_run)
    with pytest.raises(chk.EnvError):
        chk.make_runner("az")(["az", "account", "show"])


# --- summary and footer ---

def test_footer_shows_oldest_verified_on_of_matched_records(capsys, tmp_path):
    data = json.loads(DATASET.read_text())
    for rec in data["records"]:
        if rec["id"] == "azure-mysql-flexible-8.0":
            rec["verified_on"] = "2026-10-01"
        if rec["id"] == "azure-postgresql-flexible-11":
            rec["verified_on"] = "2026-09-01"
    path = tmp_path / "d.json"
    path.write_text(json.dumps(data))
    code = chk.main(["--today", "2026-11-04", "--dataset", str(path), "--prices-file", str(PRICES)],
                    runner=FakeAz([fixture("checker_inventory_ac1.json")]), fetcher=no_fetch)
    out = capsys.readouterr().out
    assert code == 1 and "verified on 2026-10-01." in out


def test_all_unknown_cost_summary_says_unknown():
    rows = rows_for([server(sku="GP_Gen5_x")])
    text = "\n".join(chk.summary(rows))
    assert "Estimated extended support cost: unknown (now: unknown)." in text
    assert "0.00" not in text


def test_scanned_count_line():
    rows = rows_for([server(name="a"), dict(server(name="b"), subscriptionId="sub-other"), server(name="c", version="16")])
    assert chk.summary(rows)[0] == "Scanned 3 servers in 2 subscriptions."
