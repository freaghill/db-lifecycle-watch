import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from lifecycle import validate as v

DATASET = Path(__file__).resolve().parent.parent / "data" / "lifecycle.json"
IN_SCOPE = {
    "azure-database-for-postgresql-flexible": ["11", "12", "13", "14", "15", "16", "17"],
    "azure-database-for-mysql-flexible": ["5.7", "8.0", "8.4"],
    "rds-postgresql": ["11", "12", "13", "14", "15", "16", "17"],
    "rds-mysql": ["5.7", "8.0", "8.4"],
}
TODAY = dt.date(2026, 11, 4)


def step(start, price, unit, region):
    return {"from": start, "price_per_unit_hour": price, "unit": unit, "currency": "USD", "region_reference": region}


def record(rid, cloud, service, engine, version, **overrides):
    unit, region = ("vCore", "eastus") if cloud == "azure" else ("vCPU", "us-east-1")
    base = {
        "id": rid,
        "cloud": cloud,
        "service": service,
        "engine": engine,
        "major_version": version,
        "standard_support_end": "2026-07-31",
        "extended_support_start": "2026-08-01",
        "extended_support_billing_start": "2026-09-01",
        "extended_support_price_steps": [step("2026-09-01", 0.07, unit, region), step("2028-09-01", 0.14, unit, region)],
        "extended_support_end": "2029-07-31",
        "after_extended_support": "forced upgrade",
        "auto_enrolled": True,
        "opt_out": "upgrade",
        "billing_notes": "per core-hour",
        "sources": [{"url": "https://example.com/policy", "title": "Policy", "what": "dates"}],
        "verified_on": TODAY.isoformat(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def data():
    """A synthetic dataset, independent of the committed values."""
    return {
        "schema_version": 1,
        "records": [
            record("azure-postgresql-flexible-13", "azure", "azure-database-for-postgresql-flexible", "postgresql", "13"),
            record("azure-postgresql-flexible-14", "azure", "azure-database-for-postgresql-flexible", "postgresql", "14"),
            record("aws-rds-mysql-8.0", "aws", "rds-mysql", "mysql", "8.0", notes="synthetic"),
        ],
    }


def find(data, rid):
    return next(r for r in data["records"] if r["id"] == rid)


def errors_of(data, today=TODAY):
    return v.validate(data, today)[1]


def run_cli(tmp_path, data, *args):
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return v.main(["--path", str(path), *args])


# --- committed dataset -----------------------------------------------------

@pytest.fixture
def committed():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_committed_dataset_is_valid_on_its_latest_verification_day(committed, capsys):
    latest = max(r["verified_on"] for r in committed["records"])
    assert v.main(["--path", str(DATASET), "--today", latest]) == 0
    assert capsys.readouterr().out.strip() == f"OK {len(committed['records'])} records"


def test_committed_dataset_covers_scope_with_https_sources(committed):
    present = {(r["service"], r["major_version"]) for r in committed["records"]}
    assert {(s, ver) for s, versions in IN_SCOPE.items() for ver in versions} <= present
    for r in committed["records"]:
        assert any(s["url"].startswith("https://") for s in r["sources"])


# --- synthetic dataset -----------------------------------------------------

def test_synthetic_dataset_is_valid(data, tmp_path, capsys):
    assert run_cli(tmp_path, data, "--today", TODAY.isoformat()) == 0
    assert capsys.readouterr().out.strip() == "OK 3 records"


def test_stale_record_fails_and_names_id_and_age(tmp_path, data, capsys):
    find(data, "azure-postgresql-flexible-14")["verified_on"] = (TODAY - dt.timedelta(days=60)).isoformat()
    assert run_cli(tmp_path, data, "--today", TODAY.isoformat()) == 1
    out = capsys.readouterr().out
    assert "azure-postgresql-flexible-14: [freshness]" in out and "60 days old (max 45)" in out
    assert out.count("[freshness]") == 1


def test_freshness_boundary_and_max_age_option(data):
    assert errors_of(data, TODAY + dt.timedelta(days=45)) == []
    late = errors_of(data, TODAY + dt.timedelta(days=46))
    assert len(late) == 3 and all("[freshness]" in e for e in late)
    assert v.validate(data, TODAY + dt.timedelta(days=46), max_age_days=60)[1] == []


def test_future_verified_on_fails(data):
    errors = errors_of(data, TODAY - dt.timedelta(days=1))
    assert len(errors) == 3 and all("in the future" in e for e in errors)


def test_extended_start_before_standard_end_fails(tmp_path, data, capsys):
    find(data, "aws-rds-mysql-8.0")["extended_support_start"] = "2026-07-01"
    assert run_cli(tmp_path, data, "--today", TODAY.isoformat()) == 1
    assert "aws-rds-mysql-8.0: [order] standard_support_end must be before extended_support_start" in capsys.readouterr().out


R0 = "azure-postgresql-flexible-13"


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["records"].insert(1, copy.deepcopy(d["records"][0])), f"{R0}: [unique-id] duplicate id"),
        (lambda d: d["records"][0].pop("opt_out"), f"{R0}: [required-field] missing opt_out"),
        (lambda d: d["records"][0].update(auto_enrolled="yes"), f"{R0}: [field-type] auto_enrolled has type str"),
        (lambda d: d["records"][0].update(notes=3), f"{R0}: [field-type] notes has type int"),
        (lambda d: d["records"][0].update(extra=1), f"{R0}: [unknown-field] extra"),
        (lambda d: d["records"][0].update(extended_support_end="31 March 2027"), f"{R0}: [date-format] extended_support_end"),
        (lambda d: d["records"][0].update(standard_support_end="2026-02-30"), f"{R0}: [date-format] standard_support_end"),
        (lambda d: d["records"][0].update(extended_support_end="2029-W05-1"), f"{R0}: [date-format] extended_support_end"),
        (lambda d: d["records"][0].update(extended_support_end="20290731"), f"{R0}: [date-format] extended_support_end"),
        (lambda d: d["records"][0].update(verified_on="2026-11-4"), f"{R0}: [date-format] verified_on"),
        (lambda d: d["records"][0]["sources"][0].update(url="http://example.com/page"), f"{R0}: [source-https]"),
        (lambda d: d["records"][0].update(sources=[]), f"{R0}: [source] at least one source"),
        (lambda d: d["records"][0]["sources"][0].pop("what"), f"{R0}: [source] source 0 needs"),
        (lambda d: d["records"][0].update(service="cloud-sql"), f"{R0}: [service] unknown service"),
        (lambda d: d["records"][0].update(engine="mysql"), f"{R0}: [service-engine]"),
        (lambda d: d["records"][0].update(cloud="aws"), f"{R0}: [service-cloud]"),
        (lambda d: d["records"][0].update(major_version="13.x", id="azure-postgresql-flexible-13.x"), f"azure-postgresql-flexible-13.x: [major-version] major_version '13.x'"),
        (lambda d: d["records"][0].update(major_version="v13"), f"{R0}: [major-version]"),
        (lambda d: d["records"][0].update(service=["rds-mysql"]), f"{R0}: [field-type] service has type list"),
        (lambda d: d["records"][0].update(cloud={"a": 1}), f"{R0}: [field-type] cloud has type dict"),
        (lambda d: d["records"].append("not a record"), "record #3: not an object"),
        (lambda d: d["records"][0].update(cloud="gcp", service="cloud-sql", extended_support_price_steps=[step("2026-09-01", 0.07, "hour", "x")]), f"{R0}: [price-step] price step 0 unit must be one of ['vCPU', 'vCore']"),
        (lambda d: d["records"][0].update(extended_support_start=None, extended_support_billing_start="2026-07-01", extended_support_price_steps=[]), f"{R0}: [order] standard_support_end must be before extended_support_billing_start"),
        (lambda d: d["records"][0].update(extended_support_billing_start=None, extended_support_price_steps=[step("2026-07-15", 0.07, "vCore", "eastus")]), f"{R0}: [price-step-range] price step 0 from 2026-07-15 is before 2026-08-01"),
        (lambda d: d["records"][0].update(id="pg-13"), "pg-13: [id-format] expected azure-postgresql-flexible-13"),
        (lambda d: d.update(schema_version=2), "dataset: [schema-version]"),
        (lambda d: d["records"].reverse(), "[record-order]"),
        # date order
        (lambda d: d["records"][0].update(extended_support_billing_start="2026-07-15"), f"{R0}: [order] extended_support_start must be on or before extended_support_billing_start"),
        (lambda d: d["records"][0].update(extended_support_end="2026-08-01"), f"{R0}: [order] extended_support_start must be before extended_support_end"),
        (lambda d: d["records"][0].update(extended_support_start=None, extended_support_billing_start="2029-08-01"), f"{R0}: [order] extended_support_billing_start must be on or before extended_support_end"),
        (lambda d: d["records"][0].update(extended_support_start=None, extended_support_billing_start=None, extended_support_price_steps=[], extended_support_end="2026-07-01"), f"{R0}: [order] standard_support_end must be before extended_support_end"),
        # price steps
        (lambda d: d["records"][0]["extended_support_price_steps"].reverse(), f"{R0}: [price-steps-sorted]"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update({"from": None}), f"{R0}: [price-step] price step 0 from is required"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update({"from": "Sept 2026"}), f"{R0}: [date-format] price step 0 from"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update({"from": "2026-08-15"}), f"{R0}: [price-step-range] price step 0 from 2026-08-15 is before 2026-09-01"),
        (lambda d: d["records"][0]["extended_support_price_steps"][1].update({"from": "2029-08-01"}), f"{R0}: [price-step-range] price step 1 from 2029-08-01 is after extended_support_end"),
        (lambda d: d["records"][0].update(extended_support_start=None, extended_support_billing_start=None), f"{R0}: [price-step-range] price step 0 needs extended_support_billing_start or extended_support_start"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(price_per_unit_hour=-1), f"{R0}: [price-step] price step 0 price_per_unit_hour"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(price_per_unit_hour="0.07"), f"{R0}: [price-step] price step 0 price_per_unit_hour"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(price_per_unit_hour=True), f"{R0}: [price-step] price step 0 price_per_unit_hour"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(price_per_unit_hour=float("nan")), f"{R0}: [price-step] price step 0 price_per_unit_hour"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(price_per_unit_hour=float("inf")), f"{R0}: [price-step] price step 0 price_per_unit_hour"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(unit="vCPU"), f"{R0}: [price-step] price step 0 unit must be vCore for azure"),
        (lambda d: find(d, "aws-rds-mysql-8.0")["extended_support_price_steps"][0].update(unit="vCore"), "aws-rds-mysql-8.0: [price-step] price step 0 unit must be vCPU for aws"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(currency="EUR"), f"{R0}: [price-step] price step 0 currency must be USD"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].update(region_reference=""), f"{R0}: [price-step] price step 0 region_reference"),
        (lambda d: d["records"][0]["extended_support_price_steps"][0].pop("currency"), f"{R0}: [price-step] price step 0 needs exactly the fields"),
        (lambda d: d["records"][0]["extended_support_price_steps"].append("0.07"), f"{R0}: [price-step] price step 2 is not an object"),
    ],
)
def test_rule_failures(tmp_path, data, capsys, mutate, expected):
    mutate(data)
    assert run_cli(tmp_path, data, "--today", TODAY.isoformat()) == 1
    assert expected in capsys.readouterr().out


def test_price_step_range_falls_back_to_extended_support_start(data):
    find(data, R0).update(extended_support_billing_start=None, extended_support_price_steps=[step("2026-08-01", 0.07, "vCore", "eastus")])
    assert errors_of(data) == []


def test_nan_in_json_file_is_rejected(tmp_path, data, capsys):
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps(data).replace("0.07", "NaN", 1), encoding="utf-8")
    assert v.main(["--path", str(path), "--today", TODAY.isoformat()]) == 1
    assert "[price-step]" in capsys.readouterr().out


def test_all_failures_are_reported_not_just_the_first(data):
    data["records"][0]["sources"][0]["url"] = "http://example.com"
    data["records"][1].pop("billing_notes")
    find(data, "aws-rds-mysql-8.0")["extended_support_start"] = "2020-01-01"
    joined = "\n".join(errors_of(data, TODAY + dt.timedelta(days=100)))
    assert "[source-https]" in joined and "[required-field]" in joined and "[order]" in joined
    assert joined.count("[freshness]") == 3


def test_top_level_must_hold_a_records_list():
    assert v.validate({"schema_version": 1}, TODAY)[1] == ["dataset: top level must be an object with a records list"]


def test_unreadable_file_fails(tmp_path, capsys):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert v.main(["--path", str(bad)]) == 1
    assert "FAIL cannot read" in capsys.readouterr().out
