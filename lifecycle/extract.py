"""Deterministic extractors that turn a fetched source into snapshot text.

HTML and markdown snapshots keep only the configured sections: their tables as normalised rows,
or a digest of the section text. Provider prose is never stored.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from html.parser import HTMLParser


class ExtractError(ValueError):
    pass


# --- HTML ------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "nav", "noscript", "template", "svg", "button", "form", "iframe", "head"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
HEADINGS = {f"h{n}": n for n in range(1, 7)}
# Start tags that implicitly close an open element, and the elements that stop the search.
IMPLIED_CLOSE = {
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot"}),
    "thead": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tbody": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tfoot": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "li": ({"li"}, {"ul", "ol"}),
    "p": ({"p"}, {"div", "section", "article", "main", "td", "th", "li", "table", "body"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
}


class Node:
    __slots__ = ("tag", "attrs", "children", "closed")

    def __init__(self, tag: str, attrs: dict):
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.closed = False


class _Tree(HTMLParser):
    """Builds an element tree with a tag stack; end tags close up to the matching start tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag in IMPLIED_CLOSE:
            closes, stops = IMPLIED_CLOSE[tag]
            cut = None
            for i in range(len(self.stack) - 1, 0, -1):
                open_tag = self.stack[i].tag
                if open_tag in stops:
                    break
                if open_tag in closes:
                    cut = i
            if cut is not None:
                del self.stack[cut:]
        node = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack[i].closed = True
                del self.stack[i:]
                return
            if self.stack[i].tag == "main":
                break  # a stray end tag must not close <main> through an ancestor
        # A stray end tag with no open match is ignored.

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _hidden(node: Node) -> bool:
    return "hidden" in node.attrs or node.attrs.get("aria-hidden", "").lower() == "true"


def _find_all(node: Node, tag: str, visible_only: bool = False):
    for child in node.children:
        if isinstance(child, Node):
            if visible_only and (child.tag in SKIP_TAGS or _hidden(child)):
                continue
            if child.tag == tag:
                yield child
            yield from _find_all(child, tag, visible_only)


def _text(node: Node) -> str:
    parts: list[str] = []

    def walk(n: Node) -> None:
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in SKIP_TAGS or _hidden(child):
                continue
            else:
                parts.append(" ")
                walk(child)
                parts.append(" ")

    walk(node)
    return " ".join("".join(parts).split())


def _table_rows(table: Node) -> list[str]:
    rows: list[str] = []

    def walk(n: Node) -> None:
        for child in n.children:
            if not isinstance(child, Node) or child.tag in SKIP_TAGS or _hidden(child):
                continue
            if child.tag == "tr":
                cells = [_text(c) for c in child.children if isinstance(c, Node) and c.tag in ("td", "th") and not _hidden(c)]
                if any(cells):
                    rows.append(" | ".join(cells))
            elif child.tag != "table":  # nested tables are separate blocks
                walk(child)

    walk(table)
    return rows


def html_blocks(html: str) -> list[tuple]:
    """Headings, tables and text blocks of the single, explicitly closed <main> element."""
    parser = _Tree()
    parser.feed(html)
    parser.close()
    mains = [m for m in _find_all(parser.root, "main")]
    if len(mains) != 1:
        raise ExtractError(f"expected one <main> element, found {len(mains)}")
    if not mains[0].closed:
        raise ExtractError("<main> element is not closed")

    blocks: list[tuple] = []

    def walk(n: Node) -> None:
        for child in n.children:
            if isinstance(child, str):
                if child.strip():
                    blocks.append(("text", child))
            elif child.tag in SKIP_TAGS or _hidden(child):
                continue
            elif child.tag in HEADINGS:
                blocks.append(("heading", HEADINGS[child.tag], _text(child)))
            elif child.tag == "table":
                blocks.append(("table", _table_rows(child)))
                for nested in _find_all(child, "table", visible_only=True):
                    blocks.append(("table", _table_rows(nested)))
            else:
                walk(child)

    walk(mains[0])
    return blocks


# --- Markdown --------------------------------------------------------------

MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
MD_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


def _md_cells(line: str) -> str:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return " | ".join(" ".join(re.sub(r"<br\s*/?>", " ", c, flags=re.I).split()) for c in cells)


def markdown_blocks(text: str) -> list[tuple]:
    blocks: list[tuple] = []
    table: list[str] | None = None
    fence = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if fence:
            if line.startswith(fence):
                fence = None
            continue
        if line.startswith("```") or line.startswith("~~~"):
            fence = line[:3]
            table = None
            continue
        if line.startswith("|"):
            if table is None:
                table = []
                blocks.append(("table", table))
            if not MD_TABLE_SEPARATOR.match(line):
                row = _md_cells(line)
                if row.replace("|", "").strip():
                    table.append(row)
            continue
        table = None
        heading = MD_HEADING.match(line)
        if heading:
            blocks.append(("heading", len(heading.group(1)), " ".join(heading.group(2).split())))
        elif line:
            blocks.append(("text", line))
    return blocks


# --- Section selection -----------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def select_sections(blocks: list[tuple], sections: list[dict]) -> str:
    """Each section runs from its heading to the next heading of the same or a higher level."""
    out: list[str] = []
    for section in sections:
        wanted = _norm(section["heading"])
        starts = [i for i, b in enumerate(blocks) if b[0] == "heading" and _norm(b[2]) == wanted]
        if len(starts) != 1:
            raise ExtractError(f"section {section['heading']!r}: found {len(starts)} matching headings, need 1")
        start = starts[0]
        level = blocks[start][1]
        end = next((i for i in range(start + 1, len(blocks)) if blocks[i][0] == "heading" and blocks[i][1] <= level), len(blocks))
        body = blocks[start + 1:end]
        out.append(f"## {blocks[start][2]}")
        if section.get("keep", "tables") == "digest":
            text = " ".join(" ".join(b[2] if b[0] == "heading" else "\n".join(b[1]) if b[0] == "table" else b[1] for b in body).split())
            if not text:
                raise ExtractError(f"section {section['heading']!r}: no text")
            out.append("text-sha256: " + hashlib.sha256(text.encode("utf-8")).hexdigest())
        else:
            tables = [b[1] for b in body if b[0] == "table" and b[1]]
            if not tables:
                raise ExtractError(f"section {section['heading']!r}: no table rows")
            for rows in tables:
                out.append("")
                out.extend(rows)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def extract_html(html: str, sections: list[dict]) -> str:
    return select_sections(html_blocks(html), sections)


def extract_markdown(text: str, sections: list[dict]) -> str:
    return select_sections(markdown_blocks(text), sections)


# --- Azure Updates ---------------------------------------------------------

UPDATE_ENGINE = re.compile(r"postgres|mysql", re.I)
UPDATE_TOPIC = re.compile(r"end of standard support|end of support|extended support|retire|deprecat|end of life", re.I)


def extract_azure_updates(pages: list[dict]) -> str:
    """Items whose title names PostgreSQL or MySQL and a lifecycle topic, whatever the tag."""
    items = {}
    for page in pages:
        for item in page.get("value") or []:
            title = " ".join((item.get("title") or "").split())
            if not (UPDATE_ENGINE.search(title) and UPDATE_TOPIC.search(title)):
                continue
            item_id = str(item["id"])
            items[item_id] = {
                "id": item_id,
                "title": title,
                "date": (item.get("modified") or item.get("created") or "")[:10],
                "link": f"https://azure.microsoft.com/updates?id={item_id}",
            }
    if not items:
        raise ExtractError("no matching Azure Updates items")
    return dump_json(sorted(items.values(), key=lambda i: i["id"]))


# --- Azure Retail Prices ---------------------------------------------------

PRICE_PRODUCTS = {
    "Azure Database for Postgres Extended Support": "postgresql",
    "Azure Database for MySQL Extended Support": "mysql",
}


def extract_azure_prices(pages: list[dict]) -> str:
    """engine -> sorted paid rows. Every row is kept, so a new meter or a duplicate shows up as a change."""
    prices: dict[str, list[dict]] = {engine: [] for engine in sorted(set(PRICE_PRODUCTS.values()))}
    for page in pages:
        for item in page.get("Items") or []:
            engine = PRICE_PRODUCTS.get(item.get("productName"))
            if engine is None:
                continue
            meter = item.get("meterName") or ""
            if "free tier" in f"{item.get('skuName') or ''} {meter}".lower():
                continue
            if item.get("type") != "Consumption" or item.get("currencyCode") != "USD":
                continue
            price = item.get("retailPrice")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                continue
            prices[engine].append({
                "region": item.get("armRegionName") or "",
                "meter": meter,
                "price": price,
                "effective_start": item.get("effectiveStartDate") or "",
            })
    if not any(prices.values()):
        raise ExtractError("no paid extended support price rows")
    for rows in prices.values():
        rows.sort(key=lambda r: (r["region"], r["meter"], r["effective_start"], r["price"]))
    return dump_json(prices)


def dump_json(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def extract(source: dict, payloads: list[str]) -> str:
    kind = source["kind"]
    if kind == "html":
        return extract_html(payloads[0], source["sections"])
    if kind == "markdown":
        return extract_markdown(payloads[0], source["sections"])
    if kind == "azure-updates":
        return extract_azure_updates([json.loads(p) for p in payloads])
    if kind == "azure-prices":
        return extract_azure_prices([json.loads(p) for p in payloads])
    raise ExtractError(f"unknown kind {kind!r}")


KINDS = ("html", "markdown", "azure-updates", "azure-prices")
SNAPSHOT_SUFFIX = {"html": ".txt", "markdown": ".txt", "azure-updates": ".json", "azure-prices": ".json"}
