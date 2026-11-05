#!/usr/bin/env python3
"""List Azure Database for PostgreSQL and MySQL flexible servers in, or close to, paid extended support.

Read-only. Uses `az graph query` for the inventory, the lifecycle dataset for dates and the
Azure Retail Prices API (or --prices-file) for prices.
"""
import argparse
import csv
import datetime as dt
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OFFER_URL = ""

PRICES_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730
LOOKAHEAD_DAYS = 365
PAGE_SIZE = 1000
MAX_INVENTORY_PAGES = 50
MAX_PRICE_PAGES = 10
DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "data" / "lifecycle.json"

ENGINES = {
    "microsoft.dbforpostgresql/flexibleservers": "postgresql",
    "microsoft.dbformysql/flexibleservers": "mysql",
}
SERVICES = {
    "postgresql": "azure-database-for-postgresql-flexible",
    "mysql": "azure-database-for-mysql-flexible",
}
PRICE_PRODUCTS = {
    "postgresql": "Azure Database for Postgres Extended Support",
    "mysql": "Azure Database for MySQL Extended Support",
}
PRICE_METER = "Extended Support vCore"
PRICE_UNIT = "1 Hour"
DATE_FIELDS = ("standard_support_end", "extended_support_start", "extended_support_billing_start", "extended_support_end")

IN_ES = "in-extended-support"
FROM_ES = "extended-support-from"
PAST_ES = "past-extended-support"
UNKNOWN = "unknown-version"
SUPPORTED = "supported"
STATUS_ORDER = {IN_ES: 0, FROM_ES: 1, PAST_ES: 2, UNKNOWN: 3, SUPPORTED: 4}
AFFECTED = (IN_ES, FROM_ES)

KQL = (
    "resources"
    " | where type in~ ('microsoft.dbforpostgresql/flexibleservers', 'microsoft.dbformysql/flexibleservers')"
    " | project id, name, type, resourceGroup, subscriptionId, tenantId, location,"
    " skuName = tostring(sku.name), skuTier = tostring(sku.tier),"
    " version = tostring(properties.version), minorVersion = tostring(properties.minorVersion),"
    " fullVersion = tostring(properties.fullVersion),"
    " haMode = tostring(properties.highAvailability.mode),"
    " replicationRole = tostring(properties.replicationRole), state = tostring(properties.state)"
    " | order by id asc"
)

COLUMNS = [
    "server", "resource_group", "subscription", "engine", "version", "region",
    "size", "status", "monthly_cost_usd", "upgrade_to", "notes",
]


class EnvError(Exception):
    pass


class PriceTruncated(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise EnvError(message)


def make_runner(az_path):
    def runner(args):
        # Turning off dynamic install keeps az from adding the resource-graph extension on its own.
        env = dict(os.environ, AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no", AZURE_CORE_ONLY_SHOW_ERRORS="true")
        try:
            proc = subprocess.run([az_path, *args[1:]], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, env=env)
        except OSError as e:
            raise EnvError(f"Cannot run az ({az_path}): {e}") from None
        return proc.returncode, proc.stdout, proc.stderr
    return runner


def run_az(runner, args):
    if args[:2] not in (["graph", "query"], ["account", "show"]):
        raise ValueError(f"az command not allowed: {args[:2]}")
    return runner(["az", *args, "--output", "json"])


def check_login(runner):
    code, _, _ = run_az(runner, ["account", "show"])
    if code != 0:
        raise EnvError("You are not logged in to Azure. Run 'az login' first.")


def query_inventory(runner, subscriptions=None):
    rows, token, seen = [], None, set()
    for _ in range(MAX_INVENTORY_PAGES):
        args = ["graph", "query", "-q", KQL, "--first", str(PAGE_SIZE)]
        if subscriptions:
            args += ["--subscriptions", *subscriptions]
        if token:
            args += ["--skip-token", token]
        code, out, err = run_az(runner, args)
        if code != 0:
            text = (err or out or "").strip()
            low = text.lower()
            if "resource-graph" in low or ("graph" in low and ("misspelled" in low or "not recognized" in low)):
                raise EnvError("The az resource-graph extension is missing. Run 'az extension add --name resource-graph', then try again.")
            if "az login" in low or "not logged in" in low:
                raise EnvError("You are not logged in to Azure. Run 'az login' first.")
            first = text.splitlines()[0] if text else f"exit code {code}"
            raise EnvError(f"az graph query failed: {first}")
        try:
            page = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            raise EnvError("az graph query returned output that is not JSON.") from None
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise EnvError("az graph query returned an unexpected shape (no 'data' list).")
        rows.extend(data)
        token = page.get("skip_token") or page.get("skipToken")
        if not token:
            return rows
        if not isinstance(token, str) or token in seen:
            raise EnvError("az graph query returned a repeated skip token. Stopped paging.")
        seen.add(token)
    raise EnvError(f"az graph query returned more than {MAX_INVENTORY_PAGES} pages. Use --subscriptions to narrow it.")


def load_dataset(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EnvError(f"Dataset not found: {path}. Pass --dataset PATH to lifecycle.json.") from None
    except (OSError, ValueError) as e:
        raise EnvError(f"Cannot read dataset {path}: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise EnvError(f"Dataset {path} has no 'records' list.")
    records = {}
    for rec in data["records"]:
        if not isinstance(rec, dict) or rec.get("service") not in SERVICES.values():
            continue
        version = rec.get("major_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+)?", version):
            raise EnvError(f"Dataset {path} has a bad major_version: {version!r}.")
        for field in DATE_FIELDS:
            value = rec.get(field)
            if value is None:
                continue
            try:
                if not isinstance(value, str):
                    raise ValueError
                dt.date.fromisoformat(value)
            except ValueError:
                raise EnvError(f"Dataset {path} has a bad date in {rec.get('id') or version} {field}: {value!r}.") from None
        records[(rec["service"], version)] = rec
    if not records:
        raise EnvError(f"Dataset {path} has no Azure flexible server records.")
    return records


def parse_date(value):
    return dt.date.fromisoformat(value) if value else None


def status_for(record, today):
    """Return (status, date or None)."""
    if record is None:
        return UNKNOWN, None
    sse, start, billing, end = (parse_date(record.get(f)) for f in DATE_FIELDS)
    if not any((sse, start, billing, end)):
        return SUPPORTED, None
    if end and today > end:
        return PAST_ES, end
    effective = billing or start
    if effective is None:
        return (SUPPORTED, None) if sse and today <= sse else (UNKNOWN, None)
    if today >= effective:
        return IN_ES, effective
    if (effective - today).days <= LOOKAHEAD_DAYS:
        return FROM_ES, effective
    if sse and today > sse:
        return UNKNOWN, None
    return SUPPORTED, None


def major_version(engine, row):
    raw = (row.get("version") or row.get("fullVersion") or "").strip()
    parts = raw.split(".")
    if not parts[0].isdigit():
        return raw or None
    if engine == "mysql":
        return ".".join(parts[:2]) if len(parts) >= 2 and parts[1].isdigit() else raw
    return parts[0]


def parse_vcores(sku_name):
    m = re.match(r"^Standard_[A-Za-z]+(\d+)", sku_name or "")
    return int(m.group(1)) if m else None


def version_key(version):
    return tuple(int(p) if p.isdigit() else -1 for p in version.split("."))


def upgrade_target(records, engine, today, current=None):
    service = SERVICES[engine]
    floor = version_key(current) if current else None
    best = None
    for (svc, version), rec in records.items():
        if svc != service or status_for(rec, today)[0] != SUPPORTED:
            continue
        if floor is not None and version_key(version) <= floor:
            continue
        if best is None or version_key(version) > version_key(best[0]):
            best = (version, rec)
    if best is None:
        return None
    version, rec = best
    if not any(rec.get(k) for k in DATE_FIELDS):
        return f"{version} (dates not published)"
    return version


def http_fetch(url):
    if not url.startswith(PRICES_URL):
        raise ValueError(f"URL not allowed: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "azure-es-check"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def page_items(page):
    items = page.get("Items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise ValueError("price page has no 'Items' list")
    return [i for i in items if isinstance(i, dict)]


def price_items_from_api(fetcher, engine, region):
    flt = f"productName eq '{PRICE_PRODUCTS[engine]}' and armRegionName eq '{region}'"
    url = PRICES_URL + "?" + urllib.parse.urlencode({"$filter": flt}, quote_via=urllib.parse.quote)
    items = []
    for _ in range(MAX_PRICE_PAGES):
        page = fetcher(url)
        items.extend(page_items(page))
        url = page.get("NextPageLink")
        if not url:
            return items
    raise PriceTruncated("price list truncated")


def pick_price(items, engine, region, today):
    best = None
    for item in items:
        if item.get("productName") != PRICE_PRODUCTS[engine] or item.get("armRegionName") != region:
            continue
        meter = item.get("meterName") or ""
        if meter != PRICE_METER or "free tier" in f"{item.get('skuName') or ''} {meter}".lower():
            continue
        if item.get("type") != "Consumption" or item.get("currencyCode") != "USD":
            continue
        if item.get("unitOfMeasure") != PRICE_UNIT:
            continue
        price = item.get("retailPrice")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            continue
        start = item.get("effectiveStartDate") or ""
        if start:
            try:
                if dt.date.fromisoformat(str(start)[:10]) > today:
                    continue
            except ValueError:
                continue
        key = (start, price)
        if best is None or key > best:
            best = key
    return best[1] if best else None


def fetch_price(engine, region, today, *, cache, fetcher=None, price_items=None):
    """USD per vCore-hour, or None. Raises the cached lookup error when the API cannot be read."""
    key = (engine, region)
    if key not in cache:
        try:
            items = price_items if price_items is not None else price_items_from_api(fetcher, engine, region)
            cache[key] = pick_price(items, engine, region, today)
        except (OSError, ValueError, http.client.HTTPException) as e:
            cache[key] = e
    if isinstance(cache[key], Exception):
        raise cache[key]
    return cache[key]


def load_prices_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EnvError(f"Cannot read prices file {path}: {e}") from None
    pages = data if isinstance(data, list) else [data]
    try:
        return [item for page in pages for item in page_items(page)]
    except ValueError:
        raise EnvError(f"Prices file {path} is not in the Retail Prices API shape (no 'Items' list).") from None


def money(value):
    return f"{value:.2f}"


def cost_text(low, high):
    return money(low) if low == high else f"{money(low)}-{money(high)}"


def build_rows(inventory, records, today, *, fetcher=None, price_items=None):
    cache, rows = {}, []
    for srv in inventory:
        engine = ENGINES.get((srv.get("type") or "").lower())
        if engine is None:
            continue
        version = major_version(engine, srv)
        record = records.get((SERVICES[engine], version)) if version else None
        status, when = status_for(record, today)
        vcores = parse_vcores(srv.get("skuName"))
        ha = (srv.get("haMode") or "").strip().lower() not in ("", "disabled")
        stopped = (srv.get("state") or "").strip().lower() == "stopped"
        role = (srv.get("replicationRole") or "").strip().lower()
        region = (srv.get("location") or "").lower()
        notes, low, high = [], None, None

        if "replica" in role:
            notes.append("read replica, priced on its own")
        if status in AFFECTED and not record.get("extended_support_billing_start"):
            notes.append("billing start not published; using extended support start")
        if status in AFFECTED:
            if stopped:
                low = high = 0.0
                notes.append("stopped servers are not billed; Azure restarts a stopped server after 7 days")
            elif vcores is None:
                notes.append("cost unknown: vCores not found in SKU name")
            else:
                try:
                    price = fetch_price(engine, region, today, cache=cache, fetcher=fetcher, price_items=price_items)
                except PriceTruncated:
                    price = None
                    notes.append("cost unknown: price list truncated")
                except (OSError, ValueError, http.client.HTTPException) as e:
                    price = None
                    notes.append(f"cost unknown: price lookup failed ({e.__class__.__name__})")
                else:
                    if price is None:
                        notes.append("cost unknown: no price for region")
                if price is not None:
                    low = high = vcores * price * HOURS_PER_MONTH
                    if ha and engine == "mysql":
                        low = high = low * 2
                    elif ha:
                        high = low * 2
                        notes.append("HA standby billing not documented")
        if status == UNKNOWN:
            notes.append("no dataset record for this version" if record is None else "extended support dates not published")

        if low is None:
            cost = "unknown" if status in AFFECTED else "-"
        else:
            cost = cost_text(low, high)
        current = version if version and re.fullmatch(r"\d+(\.\d+)?", version) else None
        target = upgrade_target(records, engine, today, current) if status != SUPPORTED else None
        rows.append({
            "server": srv.get("name") or "",
            "resource_group": srv.get("resourceGroup") or "",
            "subscription": srv.get("subscriptionId") or "",
            "engine": engine,
            "version": version or "unknown",
            "region": region,
            "size": (f"{vcores} vCore" if vcores is not None else "? vCore") + (" HA" if ha else ""),
            "status": f"{status} {when.isoformat()}" if status == FROM_ES else status,
            "monthly_cost_usd": cost,
            "upgrade_to": target or "-",
            "notes": "; ".join(notes),
            "_status": status,
            "_date": when,
            "_low": low,
            "_high": high,
            "_verified": record.get("verified_on") if record else None,
        })
    rows.sort(key=lambda r: (STATUS_ORDER[r["_status"]], r["_date"] or dt.date.max, r["engine"], r["server"], r["subscription"]))
    return rows


def public(row):
    return {c: row[c] for c in COLUMNS}


def render_table(rows):
    if not rows:
        return ""
    headers = [c.replace("_", " ") for c in COLUMNS]
    cells = [[r[c] for c in COLUMNS] for r in rows]
    widths = [max(len(h), *(len(line[i]) for line in cells)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines += ["  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in cells]
    return "\n".join(lines)


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def total_text(rows):
    priced = [r for r in rows if r["_low"] is not None]
    if not priced:
        return "unknown"
    return cost_text(sum(r["_low"] for r in priced), sum(r["_high"] for r in priced))


def summary(rows):
    subs = {r["subscription"] for r in rows if r["subscription"]}
    lines = [f"Scanned {plural(len(rows), 'server')} in {plural(len(subs), 'subscription')}."]
    affected = [r for r in rows if r["_status"] in AFFECTED]
    past = sum(1 for r in rows if r["_status"] == PAST_ES)
    unknown_version = sum(1 for r in rows if r["_status"] == UNKNOWN)
    if affected:
        now = [r for r in affected if r["_status"] == IN_ES]
        upcoming = [r for r in affected if r["_status"] == FROM_ES]
        parts = []
        if now:
            parts.append(f"now: {total_text(now)}")
        if upcoming:
            parts.append(f"upcoming: {total_text(upcoming)}")
        total = total_text(affected)
        total = total if total == "unknown" else f"{total} USD per month"
        lines.append(f"{plural(len(affected), 'affected server')}. Estimated extended support cost: "
                     f"{total} ({', '.join(parts)}).")
        unpriced = sum(1 for r in affected if r["_low"] is None)
        if unpriced:
            lines.append(f"Cost is unknown for {plural(unpriced, 'affected server')} and is not in the total.")
    elif not (past or unknown_version):
        lines.append("No affected servers: none is in extended support or starts it within 365 days.")
    else:
        lines.append("No server is in or entering paid extended support.")
    if past or unknown_version:
        lines.append(f"{plural(past + unknown_version, 'server')} need attention: {past} past the end of extended support, "
                     f"{unknown_version} on versions without published dates.")
    return lines


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(public(r) for r in rows)


def write_json(path, rows, today):
    payload = {"today": today.isoformat(), "rows": [public(r) for r in rows]}
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check_output_path(path):
    parent = Path(path).resolve().parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise EnvError(f"Cannot write {path}: folder {parent} does not exist or is not writable.")


def parse_args(argv):
    p = Parser(
        prog="azure_es_check.py",
        description="Read-only check of Azure Database for PostgreSQL and MySQL flexible servers for extended support status and cost.",
    )
    p.add_argument("--today", type=dt.date.fromisoformat, help="run date, YYYY-MM-DD (default: today in UTC)")
    p.add_argument("--subscriptions", nargs="+", metavar="ID", help="only these subscription IDs")
    p.add_argument("--dataset", default=str(DEFAULT_DATASET), help="path to lifecycle.json (default: %(default)s)")
    p.add_argument("--prices-file", metavar="PATH", help="offline prices in the Retail Prices API JSON shape")
    p.add_argument("--csv", metavar="PATH", help="also write the rows as CSV")
    p.add_argument("--json", metavar="PATH", help="also write the rows as JSON")
    return p.parse_args(argv)


def main(argv=None, *, runner=None, fetcher=None):
    try:
        args = parse_args(argv)
        today = args.today or dt.datetime.now(dt.timezone.utc).date()
        for out_path in (args.csv, args.json):
            if out_path:
                check_output_path(out_path)
        if runner is None:
            az_path = shutil.which("az")
            if az_path is None:
                raise EnvError("az not found. Install the Azure CLI, or run this in Azure Cloud Shell.")
            runner = make_runner(az_path)
        records = load_dataset(args.dataset)
        price_items = load_prices_file(args.prices_file) if args.prices_file else None
        check_login(runner)
        inventory = query_inventory(runner, args.subscriptions)
    except EnvError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    rows = build_rows(inventory, records, today, fetcher=fetcher or http_fetch, price_items=price_items)
    table = render_table(rows)
    if table:
        print(table)
        print()
    for line in summary(rows):
        print(line)
    verified = sorted({r["_verified"] for r in rows if r["_verified"]}) or \
        sorted({r["verified_on"] for r in records.values() if r.get("verified_on")})
    when = f", verified on {verified[0]}" if verified else ""
    price_src = args.prices_file if args.prices_file else "Azure Retail Prices API (list price)"
    print(f"Dates: {args.dataset}{when}. Prices: {price_src}, USD, {HOURS_PER_MONTH} hours per month.")
    if OFFER_URL:
        print(f"Help with the upgrade: {OFFER_URL}")

    try:
        if args.csv:
            write_csv(args.csv, rows)
        if args.json:
            write_json(args.json, rows, today)
    except OSError as e:
        print(f"error: cannot write output file: {e}", file=sys.stderr)
        return 3
    return 1 if any(r["_status"] in AFFECTED for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
