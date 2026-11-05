import hashlib
import json
from pathlib import Path

import pytest

from lifecycle.extract import (
    ExtractError,
    extract_azure_prices,
    extract_azure_updates,
    extract_html,
    extract_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures"
HTML_SECTIONS = [{"heading": "Support policy"}, {"heading": "minor VERSIONS"}]
MD_SECTIONS = [{"heading": "Major versions"}]


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def prices(name="prices.json"):
    return json.loads(extract_azure_prices([json.loads(read(name))]))


# --- HTML ------------------------------------------------------------------

def test_html_keeps_only_tables_of_configured_sections():
    assert extract_html(read("page.html"), HTML_SECTIONS) == (
        "## Support policy\n"
        "\n"
        "Version | Standard end\n"
        "14 | January 31, 2027\n"
        "15 | November 11, 2027\n"
        "\n"
        "Year 3 | 2x\n"
        "\n"
        "## Minor versions\n"
        "\n"
        "14.20 | March 2027\n"
    )


def test_html_drops_prose_hidden_elements_and_page_furniture():
    text = extract_html(read("page.html"), HTML_SECTIONS)
    for dropped in ("prose", "made-up", "consent", "hidden row", "99", "icon", "Home", "On this page", "Ask the bot", "session-1234", "trackView", "Footer"):
        assert dropped not in text


def test_html_extraction_is_deterministic_and_ignores_whitespace_layout():
    html = read("page.html")
    reflowed = html.replace("\n", "\r\n").replace("<td>", "\t<td>  ")
    assert extract_html(html, HTML_SECTIONS) == extract_html(html, HTML_SECTIONS) == extract_html(reflowed, HTML_SECTIONS)


def test_html_implied_end_tags_keep_every_cell_and_row():
    html = "<main><h2>T</h2><table><tr><td>a<td>b<tr><td>c<td>d</table><p>after</main>"
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\na | b\nc | d\n"


def test_html_unclosed_cell_inside_skipped_tag_does_not_hide_the_rest():
    html = "<main><nav><ul><li>menu</nav><h2>T</h2><table><tr><td>kept</td></tr></table></main>"
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\nkept\n"


def test_html_stray_end_tags_are_ignored():
    html = "<main><h2>T</h2></td></span><table><tr><td>x</td></tr></table></nav></main>"
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\nx\n"


def test_html_stray_end_tag_does_not_close_main_through_a_wrapping_div():
    html = "<div class=wrap><main><h2>T</h2></div><table><tr><td>x</td></tr></table></main></div>"
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\nx\n"


def test_html_hidden_nested_table_is_excluded():
    html = ("<main><h2>T</h2><table><tr><td>a</td><td><div hidden><table><tr><td>secret</td></tr></table></div>"
            "<script><table><tr><td>js</td></tr></table></script></td></tr></table></main>")
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\na | \n"


def test_html_nested_table_does_not_reset_outer_rows():
    html = "<main><h2>T</h2><table><tr><td>a</td><td><table><tr><td>in</td></tr></table></td></tr><tr><td>b</td><td>c</td></tr></table></main>"
    assert extract_html(html, [{"heading": "T"}]) == "## T\n\na | in\nb | c\n\nin\n"


def test_html_digest_section_stores_a_hash_not_the_text():
    text = extract_html(read("page.html"), [{"heading": "Notes on extended support", "keep": "digest"}])
    expected = hashlib.sha256("Extended support costs extra & is billed hourly. Year 3 | 2x".encode()).hexdigest()
    assert text == f"## Notes on extended support\ntext-sha256: {expected}\n"
    assert "billed" not in text


@pytest.mark.parametrize(
    "html, message",
    [
        ("<html><body><h2>T</h2><table><tr><td>x</td></tr></table></body></html>", "found 0"),
        ("<main><h2>T</h2><table><tr><td>x</td></tr></table>", "not closed"),
        ("<main><h2>T</h2></main><main></main>", "found 2"),
        ("<main><h2>Other</h2><table><tr><td>x</td></tr></table></main>", "found 0 matching headings"),
        ("<main><h2>T</h2><table><tr><td>x</td></tr></table><h3>T</h3></main>", "found 2 matching headings"),
        ("<main><h2>T</h2><p>only prose</p><h2>U</h2><table><tr><td>x</td></tr></table></main>", "no table rows"),
        ("<main><h2>T</h2><table><tr><td> </td></tr></table></main>", "no table rows"),
    ],
)
def test_html_missing_main_missing_section_or_empty_result_fails(html, message):
    with pytest.raises(ExtractError, match=message):
        extract_html(html, [{"heading": "T"}])


def test_html_empty_digest_section_fails():
    with pytest.raises(ExtractError, match="no text"):
        extract_html("<main><h2>T</h2><h2>U</h2></main>", [{"heading": "T", "keep": "digest"}])


# --- Markdown --------------------------------------------------------------

def test_markdown_keeps_only_tables_of_configured_sections():
    text = extract_markdown(read("calendar.md"), MD_SECTIONS)
    assert text == "## Major versions\n\nVersion | End\n14 | 28 February 2027\n10 Deprecated | N/A \\| none\n"
    assert extract_markdown(read("calendar.md").replace("\n", "\r\n"), MD_SECTIONS) == text


def test_markdown_ignores_code_fences_and_fails_on_missing_section():
    with pytest.raises(ExtractError, match="found 0"):
        extract_markdown(read("calendar.md"), [{"heading": "Not a heading"}])
    with pytest.raises(ExtractError, match="no table rows"):
        extract_markdown(read("calendar.md"), [{"heading": "Footnotes"}])


def test_markdown_digest_changes_when_section_text_changes():
    page = read("calendar.md")
    sections = [{"heading": "Footnotes", "keep": "digest"}]
    before = extract_markdown(page, sections)
    assert extract_markdown(page.replace("Made-up", "Edited"), sections) != before
    assert extract_markdown(page.replace("Intro prose", "Other intro"), sections) == before


# --- Azure Updates ---------------------------------------------------------

def test_updates_filter_matches_lifecycle_topics_whatever_the_tag():
    items = json.loads(extract_azure_updates([json.loads(read("updates.json"))]))
    assert [i["id"] for i in items] == ["900001", "900003", "900005", "900006", "900007", "900008"]
    assert items[0] == {
        "id": "900001",
        "title": "Announcing: Extended Support for Azure Database for PostgreSQL Flexible Server",
        "date": "2026-08-24",
        "link": "https://azure.microsoft.com/updates?id=900001",
    }
    by_id = {i["id"]: i for i in items}
    assert by_id["900003"]["date"] == "2026-05-03"  # modified wins over created
    assert by_id["900008"]["date"] == "2026-01-10"
    assert all(set(i) == {"id", "title", "date", "link"} for i in items)


def test_updates_output_is_stable_across_input_order_and_pages():
    page = json.loads(read("updates.json"))
    reversed_page = dict(page, value=list(reversed(page["value"])))
    split = [dict(page, value=page["value"][:2]), dict(page, value=page["value"][2:])]
    assert extract_azure_updates([page]) == extract_azure_updates([reversed_page]) == extract_azure_updates(split)


# --- Azure Retail Prices ---------------------------------------------------

def eastus(rows, meter="Extended Support vCore"):
    return [r for r in rows if r["region"] == "eastus" and r["meter"] == meter]


def test_prices_exclude_free_tier_and_keep_eastus_per_engine():
    result = prices()
    assert set(result) == {"mysql", "postgresql"}
    for engine in result:
        assert [r["price"] for r in eastus(result[engine])] == [0.07]
        assert not any("free tier" in r["meter"].lower() for r in result[engine])
        assert all(r["price"] > 0 for r in result[engine])


def test_prices_filter_consumption_usd_and_positive_only():
    rows = prices()["postgresql"]
    regions = {r["region"] for r in rows}
    assert "reservationregion" not in regions and "eurregion" not in regions and "zeroregion" not in regions
    assert "Some Other Product" not in json.dumps(rows)


def test_prices_keep_meter_and_effective_date_so_new_meters_and_duplicates_show():
    rows = prices()["mysql"]
    assert eastus(rows, "Extended Support Year 3 vCore") == [
        {"region": "eastus", "meter": "Extended Support Year 3 vCore", "price": 0.14, "effective_start": "2028-10-01T00:00:00Z"}
    ]
    brazil = [r for r in rows if r["region"] == "brazilsoutheast"]
    assert [(r["effective_start"], r["price"]) for r in brazil] == [("2025-11-01T00:00:00Z", 0.18), ("2026-06-01T00:00:00Z", 0.18)]


def test_prices_output_is_sorted_and_stable_across_input_order():
    page = json.loads(read("prices.json"))
    reversed_page = dict(page, Items=list(reversed(page["Items"])))
    assert extract_azure_prices([page]) == extract_azure_prices([reversed_page])
    rows = prices()["mysql"]
    keys = [(r["region"], r["meter"], r["effective_start"], r["price"]) for r in rows]
    assert keys == sorted(keys)


def test_updates_with_no_matching_items_fail():
    with pytest.raises(ExtractError, match="no matching Azure Updates items"):
        extract_azure_updates([{"value": [{"id": "1", "title": "New feature for PostgreSQL"}]}])
    with pytest.raises(ExtractError):
        extract_azure_updates([{"value": []}])


def test_prices_with_no_paid_row_for_any_engine_fail():
    with pytest.raises(ExtractError, match="no paid extended support price rows"):
        extract_azure_prices([{"Items": []}])


def test_prices_null_names_do_not_crash():
    page = {"Items": [
        {"productName": "Azure Database for Postgres Extended Support", "meterName": None, "skuName": None, "armRegionName": None,
         "retailPrice": 0.07, "currencyCode": "USD", "type": "Consumption", "effectiveStartDate": None},
    ]}
    assert json.loads(extract_azure_prices([page]))["postgresql"] == [{"region": "", "meter": "", "price": 0.07, "effective_start": ""}]


def test_prices_missing_paid_meter_for_one_engine_gives_empty_engine_without_crash():
    page = json.loads(read("prices_free_tier_only.json"))
    with pytest.raises(ExtractError):
        extract_azure_prices([page])
    paid_pg = json.loads(read("prices.json"))["Items"][2]
    result = json.loads(extract_azure_prices([dict(page, Items=page["Items"] + [paid_pg])]))
    assert result["mysql"] == [] and [r["price"] for r in eastus(result["postgresql"])] == [0.07]
