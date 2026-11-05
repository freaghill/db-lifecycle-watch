# Managed-database lifecycle data

[![ci](https://github.com/freaghill/db-lifecycle-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/freaghill/db-lifecycle-watch/actions/workflows/ci.yml)
[![watch sources](https://github.com/freaghill/db-lifecycle-watch/actions/workflows/watch.yml/badge.svg)](https://github.com/freaghill/db-lifecycle-watch/actions/workflows/watch.yml)
[![data verified](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/freaghill/db-lifecycle-watch/main/status/badge-data.json)](status/status.json)
[![sources checked](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/freaghill/db-lifecycle-watch/main/status/badge-sources.json)](status/status.json)

Repository: https://github.com/freaghill/db-lifecycle-watch

A small, hand-checked dataset of end-of-support dates and extended support prices for Azure Database for PostgreSQL and MySQL (flexible server) and Amazon RDS for PostgreSQL and MySQL. A watcher checks the official sources twice a day and opens a pull request when one of them changes.

- `data/lifecycle.json`: the dataset. Field definitions and rules are in `data/README.md`.
- `lifecycle/validate.py`: checks structure, date order, sources and freshness.
- `lifecycle/watch.py`: fetches the sources and writes snapshots to `watch/snapshots/`.
- `watch/sources.json`: the list of sources.
- `lifecycle/status.py`: writes `status/status.json` and the two status badges. It never edits the dataset or the snapshots.

Python 3.11 or newer. The tools use only the standard library. Tests need pytest.

## Is this maintained?

Check the badges at the top.

- **data verified** shows the oldest `verified_on` date in the dataset. It is green up to 30 days old, yellow from 31 to 45 days and red after that. A person re-checks every source at least every 45 days. The validator, and so CI, fails once any record is older than that.
- **sources checked** shows the day of the last weekly status check, not every watch run. That check fetches the official sources and compares them with the snapshots on the default branch. It is green when all sources were fetched and none differ, yellow when a snapshot differs from the live page (a review PR is waiting, or a change was dismissed), and red when a source could not be fetched. It shows `none` when no source could be checked.
- A weekly `heartbeat` workflow runs `python -m lifecycle.status` and commits `status/` to the default branch. Both badges are recomputed only then, so the data badge can lag a re-check by up to a week. Start `heartbeat.yml` by hand to update it at once. The heartbeat run fails when a source failed or the data badge is red.

The full numbers are in [status/status.json](status/status.json).

## Coverage

- Azure Database for PostgreSQL flexible server and Amazon RDS for PostgreSQL: major versions 11 to 17.
- Azure Database for MySQL flexible server and Amazon RDS for MySQL: 5.7, 8.0 and 8.4.

PostgreSQL 18 is left out on purpose for now. Add it as a separate change once its extended support dates matter.

## What the watcher does not watch

Some cited sources have no snapshot: the Azure pricing pages, the Amazon RDS pricing pages and the AWS Price List. Prices from them are checked only by the 45-day re-check, so re-read them on every re-check. The Azure Retail Prices API is watched.

## Run it

```sh
python -m lifecycle.validate                      # OK or a list of every problem, exit 1 on failure
python -m lifecycle.validate --today 2027-01-15   # check freshness as of another day
python -m lifecycle.watch                         # refresh snapshots, print what changed
python -m lifecycle.watch --fail-on-change        # exit 2 if any snapshot changed
python -m lifecycle.watch --only aws-rds-mysql-version-management
python -m lifecycle.status                        # write status/ and the badges, always exit 0
python -m lifecycle.status --today 2026-12-20     # badge colours as of another day
uv run --with pytest pytest -q                    # tests, no network needed
```

The watcher exits 1 if a source cannot be fetched. It keeps the old snapshot of that source and still updates the others.

## How updates work

The watcher never edits `data/lifecycle.json`. A person does.

1. The `watch sources` workflow runs twice a day. It runs the validator and the watcher.
2. When a snapshot changed, it pushes the new snapshots to the branch `bot/source-changes` and opens one pull request, or updates the open one. The PR lists the changed sources and shows a short diff.
3. Read the diff. If a date or price moved, fix `data/lifecycle.json` in your own branch and PR, update `verified_on` and the notes, and run the validator. Do not push to `bot/source-changes`: the next run force-pushes that branch.
4. Merge the bot PR. The merged snapshots become the new baseline.

Many changes are only wording. Merge those without a data change.

To dismiss a change, close the bot PR without merging. The PR body holds a `snapshot-tree` line. While the sources give the same snapshots, the bot does not open a new PR. It opens one again as soon as a source changes further. To bring a dismissed change back, run the workflow after deleting that line from the closed PR, or merge a manual snapshot update.

The PR step runs only on the default branch. A manual run on another branch still validates and watches, but opens no PR.

The validator fails once any `verified_on` is older than 45 days, so CI goes red until someone checks the sources again. The watch workflow also fails in that case, but it still opens the PR first.

## Repository settings

- Turn on **Settings > Actions > General > Workflow permissions > Allow GitHub Actions to create and approve pull requests**. Without it, the watch workflow cannot open the PR.
- The workflows use only the built-in `GITHUB_TOKEN`. No other secrets.
- GitHub sends failure emails for scheduled runs only to the user who last edited the cron line. Check the Actions tab now and then, so a failing watch does not go unseen.
- A PR opened with `GITHUB_TOKEN` does not start the `ci` workflow by itself. Close and reopen the PR, or push a commit to it, to run CI.
- In a public repository, GitHub turns off scheduled workflows after 60 days without repository activity ([GitHub docs: disabling and enabling a workflow](https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/disabling-and-enabling-a-workflow)). The bot's own pushes to `bot/source-changes` may not count, so do not rely on them. The weekly `heartbeat` workflow commits `status/` to the default branch, and the date in it changes every week, so there is a commit at least once a week. This commit is expected to count as repository activity, but GitHub does not document whether pushes made with `GITHUB_TOKEN` reset the 60-day timer. So the 45-day human re-check stays the safety net. About two months after going public, check that the schedules are still enabled. If a workflow was turned off anyway, turn it back on in the Actions tab or with `gh workflow enable watch.yml` and `gh workflow enable heartbeat.yml`, then start each once with `gh workflow run watch.yml` and `gh workflow run heartbeat.yml`.
- If you protect the default branch, allow `github-actions` to push to it. Otherwise the heartbeat cannot commit `status/`.

## Add a source

Add an entry to `watch/sources.json`:

```json
{
  "id": "short-unique-id",
  "kind": "html",
  "url": "https://...",
  "sections": [
    {"heading": "Exact heading text of the lifecycle table section"},
    {"heading": "A prose section to watch", "keep": "digest"}
  ]
}
```

`id` uses only `a-z`, `0-9` and `-`. `kind` is one of:

- `html`: reads the page's single `<main>` element. Scripts, styles, navigation, buttons and hidden elements are dropped.
- `markdown`: the same for a `.md` page. Use it when the provider offers one (AWS does).
- `azure-updates`: the Azure Updates API. Keeps id, title, date (last modified, else created) and link of items about PostgreSQL or MySQL and end of support, extended support, retirement, deprecation or end of life.
- `azure-prices`: the Azure Retail Prices API. Keeps paid consumption rows in USD per engine, with region, meter name, price and effective date. Free Tier meters are dropped.

`html` and `markdown` sources need `sections`. A section runs from its heading to the next heading of the same or a higher level. The heading must match exactly one heading on the page, ignoring case and spacing. With the default `"keep": "tables"`, the snapshot holds only the rows of that section's tables. With `"keep": "digest"`, it holds a SHA-256 of the section text: use it for sections without a table, so a change shows up without copying the provider's text. Then open the page to see what changed.

The source counts as failed, and its old snapshot stays, when the page has no single closed `<main>`, a section is missing, or a section has no table rows or no text. Fix the heading in `sources.json` when a provider renames a section. An `azure-updates` source with no matching item, or an `azure-prices` source with no paid row for any engine, also fails.

JSON sources can take `params` (query parameters, for example an OData `$filter`). Then run `python -m lifecycle.watch --only <id>` and commit the new snapshot.

Be polite to the sources: one request per source per run (plus pagination, at most 10 pages), a 20 second timeout, at most 2 retries.

## Checker

`checker/azure_es_check.py` is a read-only script that lists your Azure Database for PostgreSQL and MySQL flexible servers, their extended support status, an estimated monthly cost and the version to upgrade to. It takes its dates from `data/lifecycle.json`. See [checker/README.md](checker/README.md).

## Licence

- Code: MIT, see [LICENSE](LICENSE).
- Data in `data/lifecycle.json`: CC BY 4.0, see [data/LICENSE.md](data/LICENSE.md). Please credit "db-lifecycle-watch by freaghill, https://github.com/freaghill/db-lifecycle-watch".
