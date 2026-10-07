"""Mechanical public-source rendering must preserve attribution and table relationships."""

from __future__ import annotations

import pytest

from scripts.corpus_sources import PUBLIC_DOMAIN_NOTICE, render_snapshot, source_api_url


def source_xml(body: str, *, notice: str = PUBLIC_DOMAIN_NOTICE) -> bytes:
    """Clearly synthetic parser fixture; never a corpus source or real provenance."""
    return f"""<article><front>
      <journal-meta><publisher><publisher-name>Synthetic parser fixture</publisher-name>
      </publisher></journal-meta><article-meta>
      <article-id pub-id-type="pmcid">PMC123</article-id>
      <article-id pub-id-type="pmcid-ver">PMC123.1</article-id>
      <title-group><article-title>SYNTHETIC parser fixture</article-title></title-group>
      <pub-date pub-type="epub"><day>1</day><month>1</month><year>2024</year></pub-date>
      <permissions><license><license-p>{notice}</license-p></license></permissions>
      </article-meta></front><body>{body}</body></article>""".encode()


def test_public_snapshot_requires_an_explicit_reproduction_notice() -> None:
    with pytest.raises(ValueError, match="public-domain reproduction notice"):
        render_snapshot(
            source_xml("<p>Example.</p>", notice="No usage grant"), "PMC123", "2024-02-01"
        )


def test_snapshot_rejects_the_wrong_article() -> None:
    with pytest.raises(ValueError, match="exactly the requested"):
        render_snapshot(source_xml("<p>Example.</p>"), "PMC124", "2024-02-01")


def test_snapshot_keeps_provenance_and_marks_omitted_figures() -> None:
    result = render_snapshot(
        source_xml("<fig><label>Figure 1</label><caption><p>Fixture diagram</p></caption></fig>"),
        "PMC123",
        "2024-02-01",
    )
    assert PUBLIC_DOMAIN_NOTICE in result.markdown
    assert "Figure image omitted" in result.markdown
    assert "Figure 1 Fixture diagram" in result.markdown
    assert result.metadata["publication_date"] == "2024-01-01"
    assert result.metadata["publication_version"] == "PMC123.1"
    assert result.metadata["retrieval_date"] == "2024-02-01"


def test_tables_repeat_spanning_headers_and_row_labels_without_inventing_values() -> None:
    xml = source_xml("""<table-wrap><label>Table 1</label><table>
      <thead><tr><th rowspan="2">Process</th><th colspan="2">Review</th></tr>
      <tr><th>Allowed</th><th>Restricted</th></tr></thead>
      <tbody><tr><td rowspan="2">Alpha</td><td>Recorded</td><td>Unrecorded</td></tr>
      <tr><td>Attributed</td><td>Unattributed</td></tr></tbody>
      </table><table-wrap-foot><p>Fixture footnote.</p></table-wrap-foot></table-wrap>""")
    result = render_snapshot(xml, "PMC123", "2024-02-01").markdown
    assert "Process: Alpha | Review / Allowed: Recorded | Review / Restricted: Unrecorded" in result
    assert (
        "Process: Alpha | Review / Allowed: Attributed | Review / Restricted: Unattributed"
        in result
    )
    assert "Fixture footnote." in result


def test_superscripts_and_references_remain_distinguishable() -> None:
    xml = source_xml(
        '<p>Value 10<sup>9</sup>; marker X<sub>2</sub> <xref ref-type="bibr">4</xref>.</p>'
    )
    assert "10^(9); marker X_(2) [4] ." in render_snapshot(xml, "PMC123", "2024-02-01").markdown


def test_empty_table_cells_remain_empty_without_trailing_whitespace() -> None:
    xml = source_xml(
        "<table-wrap><table><thead><tr><th>Process</th><th>Notes</th></tr></thead>"
        "<tbody><tr><td>Alpha</td><td/></tr></tbody></table></table-wrap>"
    )
    result = render_snapshot(xml, "PMC123", "2024-02-01").markdown
    assert "Process: Alpha | Notes:\n" in result
    assert all(line == line.rstrip() for line in result.splitlines())


def test_image_only_table_requires_manual_review() -> None:
    with pytest.raises(ValueError, match="Image-only table"):
        render_snapshot(source_xml("<table-wrap><graphic/></table-wrap>"), "PMC123", "2024-02-01")


def test_entity_declarations_are_rejected() -> None:
    raw = b'<!DOCTYPE article [<!ENTITY bad SYSTEM "file:///unread">]>' + source_xml(
        "<p>Example</p>"
    )
    with pytest.raises(ValueError, match="Unsafe"):
        render_snapshot(raw, "PMC123", "2024-02-01")


def test_source_acquisition_is_confined_to_ncbi_accessions() -> None:
    assert (
        source_api_url("PMC123")
        == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=123"
    )
    with pytest.raises(ValueError, match="valid PMC accession"):
        source_api_url("../outside")
