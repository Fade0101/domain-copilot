"""Re-acquire the corpus's attributed public MMWR text snapshots from NCBI.

This is a corpus-authoring utility, not an ingestion format or adapter.
The application receives only the resulting PDF/Markdown corpus artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx

RENDERER_VERSION = "mmwr-jats-text-v1"
PUBLIC_DOMAIN_NOTICE = (
    "All material in the MMWR Series is in the public domain and may be used and "
    "reprinted without permission; citation as to source, however, is appreciated."
)
REPRESENTATION = (
    "Mechanical text snapshot of the attributed article: headings, paragraphs, lists, "
    "tables, captions and references. Table rows retain their column labels; images "
    "and image-only algorithms are not reproduced. Consult the original for graphical content."
)


def source_api_url(pmcid: str) -> str:
    if not re.fullmatch(r"PMC[0-9]+", pmcid):
        raise ValueError("A valid PMC accession is required")
    return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(
        {"db": "pmc", "id": pmcid[3:]}
    )


def _inline(element: ET.Element | None) -> str:
    if element is None:
        return ""
    result = element.text or ""
    for child in element:
        value = _inline(child)
        if child.tag in {"sup", "sub"}:
            value = ("^" if child.tag == "sup" else "_") + "(" + value + ")"
        elif child.tag == "xref" and child.get("ref-type") == "bibr":
            value = " [" + value + "] "
        elif child.tag == "break":
            value = " "
        elif child.tag == "msup" and len(child) == 2:
            value = _inline(child[0]) + "^(" + _inline(child[1]) + ")"
        elif child.tag == "msub" and len(child) == 2:
            value = _inline(child[0]) + "_(" + _inline(child[1]) + ")"
        elif child.tag == "mfrac" and len(child) == 2:
            value = "(" + _inline(child[0]) + ")/(" + _inline(child[1]) + ")"
        result += value + (child.tail or "")
    return " ".join(result.split())


def _heading(text: str, depth: int) -> str:
    return "#" * min(depth, 6) + " " + text


def _table_rows(table: ET.Element) -> list[str]:
    """Expand explicit spans, then render rows as labelled records.

    CommonMark ingestion does not require a table plugin: each row carries the
    source's column labels in ordinary text instead of relying on a distant header.
    No medical values or relationships are supplied by a model.
    """
    rows = table.findall("./thead/tr") + table.findall("./tbody/tr") + table.findall("./tr")
    if not rows:
        raise ValueError("Unsupported table structure; review the public snapshot manually")
    header_count = len(table.findall("./thead/tr"))
    if not header_count and any(cell.tag == "th" for cell in rows[0]):
        header_count = 1
    grid: dict[tuple[int, int], str] = {}
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row:
            if cell.tag not in {"td", "th"}:
                continue
            while (row_index, column) in grid:
                column += 1
            rowspan, colspan = int(cell.get("rowspan", "1")), int(cell.get("colspan", "1"))
            if not 1 <= rowspan <= 1000 or not 1 <= colspan <= 100:
                raise ValueError("Unsupported table span")
            value = _inline(cell)
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    key = row_index + row_offset, column + column_offset
                    if key in grid:
                        raise ValueError("Overlapping table cells; review source layout")
                    grid[key] = value
            column += colspan
    width = max((column for _, column in grid), default=-1) + 1
    headers: list[str] = []
    for column in range(width):
        labels = list(
            dict.fromkeys(
                grid[row, column]
                for row in range(header_count)
                if grid.get((row, column), "").strip()
            )
        )
        headers.append(" / ".join(labels) or f"Column {column + 1}")
    rendered = []
    for row_number in range(header_count, len(rows)):
        values = [grid.get((row_number, column), "") for column in range(width)]
        if any(values):
            rendered.append(
                " | ".join(
                    f"{header}: {value}" for header, value in zip(headers, values, strict=True)
                ).rstrip()
            )
    return rendered


def _blocks(element: ET.Element, depth: int = 2) -> list[str]:
    tag = element.tag
    if tag in {"p", "disp-quote", "statement", "verse-group"}:
        # A paragraph can contain a block list or table; render those separately.
        block_children = {"list", "table-wrap", "fig", "boxed-text"}
        if any(child.tag in block_children for child in element):
            result = []
            buffer = element.text or ""
            for child in element:
                if child.tag in block_children:
                    if buffer.strip():
                        result.append(" ".join(buffer.split()))
                    result.extend(_blocks(child, depth))
                    buffer = child.tail or ""
                else:
                    buffer += _inline(child) + (child.tail or "")
            if buffer.strip():
                result.append(" ".join(buffer.split()))
            return result
        return [_inline(element)] if _inline(element) else []
    if tag == "list":
        return [
            "- " + block for item in element.findall("list-item") for block in _blocks(item, depth)
        ]
    if tag in {"table-wrap", "fig"}:
        caption = " ".join(
            part
            for part in [_inline(element.find("label")), _inline(element.find("caption"))]
            if part
        )
        result = [_heading(caption, depth)] if caption else []
        if tag == "table-wrap":
            table = element.find(".//table")
            if table is None:
                raise ValueError("Image-only table cannot be represented as clinical text")
            result.extend(_table_rows(table))
            for foot in element.findall("table-wrap-foot"):
                result.extend(_blocks(foot, depth + 1))
        else:
            alt = _inline(element.find("alt-text"))
            if alt:
                result.append(alt)
            result.append(
                "[Figure image omitted from this text snapshot; see the original source.]"
            )
        return result
    if tag in {"ref", "fn"}:
        value = _inline(element)
        return [value] if value else []
    if tag in {"graphic", "inline-graphic", "media", "label", "title"}:
        return []
    result = []
    title = _inline(element.find("title"))
    if title:
        label = _inline(element.find("label"))
        result.append(_heading(" ".join(part for part in [label, title] if part), depth))
        depth += 1
    elif tag == "abstract":
        result.append(_heading("Abstract", depth))
        depth += 1
    elif tag == "ref-list":
        result.append(_heading("References", depth))
        depth += 1
    if element.text and element.text.strip():
        result.append(" ".join(element.text.split()))
    for child in element:
        result.extend(_blocks(child, depth))
        if child.tail and child.tail.strip():
            result.append(" ".join(child.tail.split()))
    return result


@dataclass(frozen=True)
class PublicSnapshot:
    markdown: str
    metadata: dict[str, object]


def render_snapshot(raw: bytes, pmcid: str, retrieved: str) -> PublicSnapshot:
    date.fromisoformat(retrieved)
    if len(raw) > 20_000_000 or b"<!ENTITY" in raw.upper():
        raise ValueError("Unsafe or oversized source XML")
    tree = ET.fromstring(raw)
    for element in tree.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    articles = tree.findall(".//article") if tree.tag != "article" else [tree]
    matches = []
    for article in articles:
        identifiers = {
            item.get("pub-id-type"): _inline(item)
            for item in article.findall("./front/article-meta/article-id")
        }
        if identifiers.get("pmcid") == pmcid:
            matches.append((article, identifiers))
    if len(matches) != 1:
        raise ValueError("The response does not contain exactly the requested public article")
    article, identifiers = matches[0]
    meta = article.find("./front/article-meta")
    assert meta is not None
    notices = [_inline(item) for item in meta.findall("./permissions/license/license-p")]
    if PUBLIC_DOMAIN_NOTICE not in notices:
        raise ValueError("An explicit MMWR public-domain reproduction notice is required")
    title = _inline(meta.find("./title-group/article-title"))
    publisher = _inline(article.find("./front/journal-meta/publisher/publisher-name"))
    pubdate = meta.find("./pub-date[@pub-type='epub']")
    if pubdate is None:
        raise ValueError("A verified publication date is required")
    published = date(
        int(_inline(pubdate.find("year"))),
        int(_inline(pubdate.find("month"))),
        int(_inline(pubdate.find("day"))),
    ).isoformat()
    if published > retrieved or not title or not publisher or not identifiers.get("pmcid-ver"):
        raise ValueError("Incomplete or inconsistent publication provenance")
    source_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    body = article.find("body")
    if body is None:
        raise ValueError("The public article has no full text")
    sections = [
        "# " + title,
        "PUBLIC SOURCE — attributed MMWR text snapshot; not synthetic.",
        f"Publisher: {publisher}\n\nSource: {source_url}\n\n"
        f"Publication date: {published}\n\nSource version: {identifiers['pmcid-ver']}\n\n"
        f"DOI: {identifiers.get('doi', '')}\n\nRetrieval date: {retrieved}",
        "Representation: " + REPRESENTATION,
        "Usage notice: " + PUBLIC_DOMAIN_NOTICE,
    ]
    for abstract in meta.findall("abstract"):
        sections.extend(_blocks(abstract))
    sections.extend(_blocks(body))
    back = article.find("back")
    if back is not None:
        sections.extend(_blocks(back))
    markdown = "\n\n".join(section for section in sections if section.strip())
    markdown = "\n".join(line.rstrip() for line in markdown.splitlines()) + "\n"
    return PublicSnapshot(
        markdown,
        {
            "title": title,
            "publisher": publisher,
            "source_url": source_url,
            "publication_date": published,
            "publication_version": identifiers["pmcid-ver"],
            "retrieval_date": retrieved,
            "doi": identifiers.get("doi"),
            "classification": "public",
            "license_name": "Public domain — MMWR reproduction notice",
            "license_url": source_url,
            "license_notice": PUBLIC_DOMAIN_NOTICE,
            "usage_notes": "Attribution retained. " + REPRESENTATION,
            "acquisition": {
                "pmcid": pmcid,
                "url": source_api_url(pmcid),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "renderer_version": RENDERER_VERSION,
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/corpus/manifest.json"))
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        args.output_directory.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for document in manifest["documents"]:
                if document["classification"] != "public":
                    continue
                acquisition = document["acquisition"]
                response = client.get(source_api_url(acquisition["pmcid"]))
                response.raise_for_status()
                raw = response.content
                if hashlib.sha256(raw).hexdigest() != acquisition["response_sha256"]:
                    raise ValueError(
                        f"Upstream response changed for {document['id']}; "
                        "the committed corpus is still reproducible offline. Review a new version."
                    )
                snapshot = render_snapshot(raw, acquisition["pmcid"], document["retrieval_date"])
                data = snapshot.markdown.encode("utf-8")
                if hashlib.sha256(data).hexdigest() != document["source_sha256"]:
                    raise ValueError(f"Source rendering changed for {document['id']}")
                # The output name comes from the validated PMC accession, not an arbitrary path.
                (args.output_directory / (acquisition["pmcid"] + ".md")).write_bytes(data)
                print(json.dumps({"source": document["id"], "verified": True}), flush=True)
                time.sleep(0.4)  # NCBI's unauthenticated rate limit is three requests/second.
    except (OSError, ValueError, KeyError, httpx.HTTPError, ET.ParseError) as exc:
        parser.exit(1, f"Source verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
