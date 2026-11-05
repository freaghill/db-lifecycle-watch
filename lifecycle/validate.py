"""Check data/lifecycle.json for structure, date order and freshness. Reports every failure."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "lifecycle.json"
SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_DAYS = 45

SERVICES = {
    "azure-database-for-postgresql-flexible": ("azure", "postgresql"),
    "azure-database-for-mysql-flexible": ("azure", "mysql"),
    "rds-postgresql": ("aws", "postgresql"),
    "rds-mysql": ("aws", "mysql"),
}
UNITS = {"azure": "vCore", "aws": "vCPU"}
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAJOR_VERSION = re.compile(r"[0-9]+(\.[0-9]+)?")
DATE_FIELDS = (
    "standard_support_end",
    "extended_support_start",
    "extended_support_billing_start",
    "extended_support_end",
)
STR = (str,)
OPT_STR = (str, type(None))
REQUIRED = {
    "id": STR,
    "cloud": STR,
    "service": STR,
    "engine": STR,
    "major_version": STR,
    "standard_support_end": OPT_STR,
    "extended_support_start": OPT_STR,
    "extended_support_billing_start": OPT_STR,
    "extended_support_price_steps": (list,),
    "extended_support_end": OPT_STR,
    "after_extended_support": STR,
    "auto_enrolled": (bool, type(None)),
    "opt_out": STR,
    "billing_notes": STR,
    "sources": (list,),
    "verified_on": STR,
}
OPTIONAL = {"notes": STR}


def parse_date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and ISO_DATE.fullmatch(value) else None
    except ValueError:
        return None


def check_record(record, index: int, today: dt.date, max_age_days: int) -> list[str]:
    if not isinstance(record, dict):
        return [f"record #{index}: not an object"]
    rid = record.get("id") if isinstance(record.get("id"), str) else f"record #{index}"
    errors: list[str] = []

    def fail(rule: str, detail: str) -> None:
        errors.append(f"{rid}: [{rule}] {detail}")

    for field, types in REQUIRED.items():
        if field not in record:
            fail("required-field", f"missing {field}")
        elif not isinstance(record[field], types):
            fail("field-type", f"{field} has type {type(record[field]).__name__}")
    for field in sorted(set(record) - set(REQUIRED) - set(OPTIONAL)):
        fail("unknown-field", field)
    for field, types in OPTIONAL.items():
        if field in record and not isinstance(record[field], types):
            fail("field-type", f"{field} has type {type(record[field]).__name__}")

    version = record.get("major_version")
    if isinstance(version, str) and not MAJOR_VERSION.fullmatch(version):
        fail("major-version", f"major_version {version!r} must look like 14 or 8.0")

    service = record.get("service")
    cloud_value = record.get("cloud") if isinstance(record.get("cloud"), str) else None
    if not isinstance(service, str):
        pass  # reported as a field-type failure
    elif service in SERVICES:
        cloud, engine = SERVICES[service]
        if record.get("cloud") != cloud:
            fail("service-cloud", f"service {service} needs cloud {cloud}")
        if record.get("engine") != engine:
            fail("service-engine", f"service {service} needs engine {engine}")
        expected_id = f"{'aws-' if cloud == 'aws' else ''}{service.replace('azure-database-for-', 'azure-')}-{record.get('major_version')}"
        if isinstance(record.get("id"), str) and record["id"] != expected_id:
            fail("id-format", f"expected {expected_id}")
    elif "service" in record:
        fail("service", f"unknown service {service!r}")

    dates = {}
    for field in DATE_FIELDS + ("verified_on",):
        value = record.get(field)
        if value is None:
            continue
        parsed = parse_date(value)
        if parsed is None:
            fail("date-format", f"{field} is not an ISO date: {value!r}")
        else:
            dates[field] = parsed

    sse, ess = dates.get("standard_support_end"), dates.get("extended_support_start")
    bill, ese = dates.get("extended_support_billing_start"), dates.get("extended_support_end")
    if sse and ess and not sse < ess:
        fail("order", "standard_support_end must be before extended_support_start")
    if sse and bill and not sse < bill:
        fail("order", "standard_support_end must be before extended_support_billing_start")
    if ess and bill and not ess <= bill:
        fail("order", "extended_support_start must be on or before extended_support_billing_start")
    if ess and ese and not ess < ese:
        fail("order", "extended_support_start must be before extended_support_end")
    if bill and ese and not bill <= ese:
        fail("order", "extended_support_billing_start must be on or before extended_support_end")
    if sse and ese and not sse < ese:
        fail("order", "standard_support_end must be before extended_support_end")
    price_from = bill or ess

    steps = record.get("extended_support_price_steps")
    if isinstance(steps, list):
        previous = None
        for n, step in enumerate(steps):
            where = f"price step {n}"
            if not isinstance(step, dict):
                fail("price-step", f"{where} is not an object")
                continue
            expected = {"from", "price_per_unit_hour", "unit", "currency", "region_reference"}
            if set(step) != expected:
                fail("price-step", f"{where} needs exactly the fields {sorted(expected)}")
            start = parse_date(step.get("from"))
            if step.get("from") is None:
                fail("price-step", f"{where} from is required")
            elif start is None:
                fail("date-format", f"{where} from is not an ISO date: {step.get('from')!r}")
            if start:
                if price_from is None:
                    fail("price-step-range", f"{where} needs extended_support_billing_start or extended_support_start")
                elif start < price_from:
                    fail("price-step-range", f"{where} from {start} is before {price_from}")
                if ese and start > ese:
                    fail("price-step-range", f"{where} from {start} is after extended_support_end {ese}")
            price = step.get("price_per_unit_hour")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
                fail("price-step", f"{where} price_per_unit_hour must be a finite non-negative number")
            unit = UNITS.get(cloud_value)
            if unit and step.get("unit") != unit:
                fail("price-step", f"{where} unit must be {unit} for {cloud_value}")
            elif not unit and step.get("unit") not in UNITS.values():
                fail("price-step", f"{where} unit must be one of {sorted(UNITS.values())}")
            if step.get("currency") != "USD":
                fail("price-step", f"{where} currency must be USD")
            if not isinstance(step.get("region_reference"), str) or not step.get("region_reference"):
                fail("price-step", f"{where} region_reference must be a non-empty string")
            if start and previous and not previous < start:
                fail("price-steps-sorted", f"{where} is not after the previous step")
            previous = start or previous

    sources = record.get("sources")
    if isinstance(sources, list):
        if not sources:
            fail("source", "at least one source is required")
        for n, source in enumerate(sources):
            if not isinstance(source, dict) or not all(isinstance(source.get(k), str) and source.get(k) for k in ("url", "title", "what")):
                fail("source", f"source {n} needs non-empty url, title and what")
            elif not source["url"].startswith("https://"):
                fail("source-https", f"source {n} is not https: {source['url']}")

    verified = dates.get("verified_on")
    if verified:
        age = (today - verified).days
        if age < 0:
            fail("freshness", f"verified_on {verified} is in the future")
        elif age > max_age_days:
            fail("freshness", f"verified_on {verified} is {age} days old (max {max_age_days})")
    return errors


SERVICE_ORDER = list(SERVICES)


def order_key(record):
    try:
        version = tuple(int(p) for p in record["major_version"].split("."))
        return (record["cloud"] != "azure", SERVICE_ORDER.index(record["service"]), version)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def validate(data, today: dt.date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> tuple[int, list[str]]:
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return 0, ["dataset: top level must be an object with a records list"]
    errors = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"dataset: [schema-version] expected {SCHEMA_VERSION}, got {data.get('schema_version')!r}")
    records = data["records"]
    seen = set()
    for index, record in enumerate(records):
        rid = record.get("id") if isinstance(record, dict) else None
        if isinstance(rid, str):
            if rid in seen:
                errors.append(f"{rid}: [unique-id] duplicate id")
            seen.add(rid)
        errors.extend(check_record(record, index, today, max_age_days))
    keys = [order_key(r) if isinstance(r, dict) else None for r in records]
    for n in range(1, len(records)):
        if keys[n - 1] and keys[n] and not keys[n - 1] < keys[n]:
            errors.append(f"{records[n].get('id')}: [record-order] must come after {records[n - 1].get('id')} (cloud, service, version)")
    return len(records), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lifecycle.validate", description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None, help="ISO date, defaults to today (UTC)")
    args = parser.parse_args(argv)
    today = args.today or dt.datetime.now(dt.timezone.utc).date()

    try:
        data = json.loads(args.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"FAIL cannot read {args.path}: {error}")
        return 1
    count, errors = validate(data, today, args.max_age_days)
    if errors:
        print(f"FAIL {len(errors)} problem(s) in {count} records:")
        for error in errors:
            print(f"  {error}")
        return 1
    print(f"OK {count} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
