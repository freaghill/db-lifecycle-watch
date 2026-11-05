# Lifecycle dataset

`lifecycle.json` lists lifecycle dates and extended support prices for managed PostgreSQL and MySQL on Azure and AWS. It has one record per cloud, service and major version.

The top level is `{"schema_version": 1, "records": [...]}`. Keep records ordered by cloud (Azure first), service, then version, so diffs stay easy to read.

It covers PostgreSQL 11 to 17 and MySQL 5.7, 8.0 and 8.4 on both clouds. PostgreSQL 18 is not included yet, on purpose.

The watcher does not snapshot the Azure pricing pages, the Amazon RDS pricing pages or the AWS Price List. Values from them rely on the 45-day re-check, so open them on every re-check.

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | `<service short name>-<major_version>`, for example `azure-postgresql-flexible-14` or `aws-rds-mysql-8.0`. The validator checks the format. |
| `cloud` | `azure` or `aws` | |
| `service` | string | `azure-database-for-postgresql-flexible`, `azure-database-for-mysql-flexible`, `rds-postgresql` or `rds-mysql`. |
| `engine` | `postgresql` or `mysql` | Must match the service. |
| `major_version` | string | `"14"`, `"8.0"`, `"5.7"`. |
| `standard_support_end` | ISO date or null | Last day of standard support. |
| `extended_support_start` | ISO date or null | First day in extended support. |
| `extended_support_billing_start` | ISO date or null | First billed day. Can be later than the start when there is a grace period. |
| `extended_support_price_steps` | list | Price changes in date order. Each step is `{from, price_per_unit_hour, unit, currency, region_reference}`. `unit` is `vCore` for Azure and `vCPU` for AWS, `currency` is `USD`, `region_reference` is the region the price is for (`eastus`, `us-east-1`). An empty list means no price with a stated date is published for this version yet. |
| `extended_support_end` | ISO date or null | Last day of extended support. |
| `after_extended_support` | short text | What happens next, for example `forced upgrade` or `not documented`. |
| `auto_enrolled` | bool or null | Whether servers enter extended support without any action. |
| `opt_out` | short text | How to avoid extended support, if at all. |
| `billing_notes` | short text | What is billed, for example HA standbys, replicas or stopped servers. |
| `sources` | list | At least one `{url, title, what}`. Every `url` must be https. `what` says which values come from that page. |
| `verified_on` | ISO date | Day a person last checked every value of the record against its live sources. |
| `notes` | string, optional | Caveats, rules without a stated date, and unknowns. |

When a source does not state a value, use null and say so in `notes`. Never guess, and never derive a date or price from a rule. Add a price step only when the source states both its date and its price. If the source gives only a rule (for example "year 3 costs twice the rate"), write the rule and its source in `notes` and leave the step out.

## Rules the validator checks

- Every field above is present with the right type, and there are no other fields. Ids are unique and follow the format.
- Dates are `YYYY-MM-DD` and real calendar days.
- `standard_support_end` < `extended_support_start` <= `extended_support_billing_start` <= `extended_support_end`, and `standard_support_end` < `extended_support_end`, where the values are present.
- Price steps are sorted by `from`, each `from` is set, and it falls between the billing start (or the extended support start) and `extended_support_end`. Prices are finite, non-negative numbers.
- Every record has at least one source, and every source URL is https.
- Records are in order: cloud (Azure first), service, then version.
- No `verified_on` is in the future or older than 45 days.

The validator lists every problem it finds, not only the first.

## Verification and the 45-day rule

1. Open every source of the record and check each date and price.
2. Fix what changed, then set `verified_on` to today.
3. Run `python -m lifecycle.validate`.

The validator fails when any `verified_on` is more than 45 days old. CI then turns red, which forces a fresh check at least every 45 days. This is on purpose: the dates matter only if they are current, and the website shows `verified_on` next to them.

## Data and licence

Dates and prices are extracted from official provider sources. The text is our own. Always link the source when you use or republish a value.
