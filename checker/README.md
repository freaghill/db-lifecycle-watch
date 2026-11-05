# Azure extended support checker

`azure_es_check.py` lists your Azure Database for PostgreSQL and MySQL flexible servers. For each one it shows if the server is in paid extended support or starts it within 365 days, an estimated monthly cost, and the newest supported version to upgrade to.

It is one Python file and uses only the standard library. It is tested on Python 3.11 and newer.

## What it reads, and what it never does

It reads:

- the server list, with `az graph query` (Azure Resource Graph). The query reads name, resource group, subscription, region, SKU, version, HA mode, replication role and state;
- your login state, with `az account show`;
- lifecycle dates from `data/lifecycle.json` in this repository;
- extended support prices from the public [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) at `https://prices.azure.com`, or from a file you pass with `--prices-file`.

It never changes anything in Azure. It runs no other `az` command, never installs an `az` extension, and sends no tracking data. It writes files only when you pass `--csv` or `--json`.

## Access

You need the **Reader** role on the subscriptions you want to check. Subscriptions delegated to you through Azure Lighthouse should be included when `az graph query` can see them. Use `--subscriptions` to limit the check. Subscriptions where you have no Reader access are left out without a warning. Check the "Scanned N servers in M subscriptions" line to see what was covered.

You also need the `resource-graph` extension for `az`. Cloud Shell has it. Locally, add it once with `az extension add --name resource-graph`. The script does not add it for you.

## Run it

In Azure Cloud Shell (Bash):

```sh
git clone https://github.com/freaghill/db-lifecycle-watch.git
cd db-lifecycle-watch
python3 checker/azure_es_check.py
```

Locally, log in first with `az login`, then run the same command. The script looks for the dataset at `../data/lifecycle.json` next to itself. If you copy only the script, also copy `data/lifecycle.json` and pass `--dataset PATH`.

Options:

```sh
python3 checker/azure_es_check.py --subscriptions <id> <id>    # only these subscriptions
python3 checker/azure_es_check.py --csv servers.csv --json servers.json
python3 checker/azure_es_check.py --today 2027-01-15           # status as of another day
python3 checker/azure_es_check.py --prices-file prices.json    # offline prices, same JSON shape as the API
```

Exit codes:

- `0`: it ran, and no server is in extended support or starts it within 365 days.
- `1`: it ran, and at least one server is in extended support or starts it within 365 days. You can use this to fail a CI job as a reminder.
- `3`: a problem with the environment, for example `az` is missing, you are not logged in, the `resource-graph` extension is missing, or the dataset is not found.

Servers past the end of extended support, and servers on versions without published dates, are shown and counted in a "need attention" summary line, but they do not change the exit code.

## Status values

- `in-extended-support`: billing has started. If billing start is not published, extended support has started.
- `extended-support-from <date>`: billing (or extended support) starts within 365 days.
- `past-extended-support`: extended support has ended.
- `supported`: standard support has not ended, or the version has no published dates yet.
- `unknown-version`: the dataset has no record for this version, or standard support has ended and no extended support date is published.

Cost is shown only for the first two.

`upgrade to` is the newest supported version of the same engine that is newer than the server's version. Getting there may need more than one major version step. Supported servers show `-`.

## Without the script

This one-line query lists servers on the versions that are in extended support or start it soon, as of the dataset check on 2026-11-04 (PostgreSQL 11 to 14, MySQL 5.7 and 8.0). It shows no cost and no dates. It does not page, so it shows at most 1000 servers. Update its version list by hand when the dataset changes.

```sh
az graph query --first 1000 -o table -q "resources | where type in~ ('microsoft.dbforpostgresql/flexibleservers','microsoft.dbformysql/flexibleservers') | extend v = tostring(properties.version) | where (type =~ 'microsoft.dbforpostgresql/flexibleservers' and v in ('11','12','13','14')) or (type =~ 'microsoft.dbformysql/flexibleservers' and (v startswith '5.7' or v startswith '8.0')) | project name, resourceGroup, subscriptionId, location, type, version = v, sku = tostring(sku.name)"
```

## Assumptions

- A month is 730 hours.
- Prices are list prices in USD per vCore-hour for the server's region. Prices differ between regions. If the API has no price for a region, the cost shows as unknown. The script never uses another region's price.
- Cost is vCores x price x 730. A server in `extended-support-from` shows the cost it will have once billing starts, at today's price.
- MySQL with HA: the cost is doubled, because Microsoft documents that the standby vCores are billed.
- PostgreSQL with HA: the cost is a range from x1 to x2, with the note "HA standby billing not documented".
- Each read replica is its own server and is priced on its own.
- Stopped servers cost 0. Microsoft does not bill stopped servers for extended support. Azure starts a stopped server again after 7 days, and billing starts again with it.
- vCores come from the SKU name: `Standard_D4ds_v5` is 4, `Standard_E16ds_v5` is 16, and Burstable `Standard_B1ms` is 1 and `Standard_B2s` is 2. If the name does not match this pattern, the cost shows as unknown.
- The year 3 price rule (twice the year 1 and 2 rate) is not applied, because no date or meter is published for it.

## Where the dates come from

All dates come from `data/lifecycle.json`. Each record lists its sources and a `verified_on` date, and the script prints the oldest `verified_on` it used. See `data/README.md` for the fields and how the data is checked. The script contains no dates or prices of its own.
