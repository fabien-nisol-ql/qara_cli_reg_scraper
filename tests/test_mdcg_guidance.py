"""Tests for the MDCG guidance scraper against the real listing page shape
(https://health.ec.europa.eu/medical-devices-sector/new-regulations/
guidance-mdcg-endorsed-documents-and-other-guidance_en, confirmed live
while building this — see the module docstring): 21 `<table
class="ecl-table">` elements, one per topic category, most rows linking a
`/document/download/<uuid>_en?filename=...` PDF from the Reference cell,
but a real minority linking from the Title cell instead, or to an entirely
different host with no filename extension at all."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.eu.mdcg_guidance import (
    BASE_URL,
    LISTING_URL,
    MdcgGuidanceScraper,
)
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "eu", "mdcg_guidance", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "mdcg_guidance"
    )
    return storage, manifest, MdcgGuidanceScraper(http, manifest, **kwargs)


def listing_html(*category_tables: tuple[str, str]) -> str:
    """category_tables: list of (category_heading, table_inner_html) —
    table_inner_html is everything after the header row."""
    body = "".join(
        f"""<h2>{heading}</h2>
        <table class="ecl-table">
          <tr class="ecl-table__row">
            <th class="ecl-table__header">Reference</th>
            <th class="ecl-table__header">Title</th>
            <th class="ecl-table__header">Publication</th>
          </tr>
          {rows}
        </table>"""
        for heading, rows in category_tables
    )
    return f"<html><body>{body}</body></html>"


def reference_linked_row(uuid: str, reference: str, title: str, filename: str, publication: str) -> str:
    """The common shape: the link sits in the Reference cell."""
    return f"""<tr class="ecl-table__row">
        <td class="ecl-table__cell" data-ecl-table-header="Reference">
          <a href="/document/download/{uuid}_en?filename={filename}">{reference}</a>
        </td>
        <td class="ecl-table__cell" data-ecl-table-header="Title">{title}</td>
        <td class="ecl-table__cell" data-ecl-table-header="Publication">{publication}</td>
      </tr>"""


def title_linked_row(uuid: str, reference: str, title: str, filename: str, publication: str) -> str:
    """The real minority shape (7 of 156 rows, confirmed live): Reference
    is plain text (e.g. "Q&A"), the link sits in the Title cell instead."""
    return f"""<tr class="ecl-table__row">
        <td class="ecl-table__cell" data-ecl-table-header="Reference">{reference}</td>
        <td class="ecl-table__cell" data-ecl-table-header="Title">
          <a href="/document/download/{uuid}_en?filename={filename}">{title}</a>
        </td>
        <td class="ecl-table__cell" data-ecl-table-header="Publication">{publication}</td>
      </tr>"""


def external_row(url: str, reference: str, title: str, publication: str) -> str:
    """The rarest shape (3 of 156 rows, confirmed live): links to a page
    with no `/document/download/` UUID and no filename extension at all —
    a EUR-Lex notice, an ec.europa.eu overview page, an ema.europa.eu page."""
    return f"""<tr class="ecl-table__row">
        <td class="ecl-table__cell" data-ecl-table-header="Reference"><a href="{url}">{reference}</a></td>
        <td class="ecl-table__cell" data-ecl-table-header="Title">{title}</td>
        <td class="ecl-table__cell" data-ecl-table-header="Publication">{publication}</td>
      </tr>"""


@responses.activate
def test_real_row_shape_is_parsed_and_saved(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    uuid = "15a33521-87f1-4939-92a1-ef23f2b09c6c"
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html((
            "Annex XVI products",
            reference_linked_row(uuid, "MDCG 2023-6", "Guidance on equivalence", "mdcg_2023-6_en.pdf", "December 2023"),
        )),
        status=200, content_type="text/html",
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/document/download/{uuid}_en",
        body=b"%PDF-1.7 fake pdf bytes", status=200, content_type="application/pdf",
    )

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "budget_reached" or summary.stop_reason == "completed"
    assert storage.exists(f"eu/mdcg_guidance/documents/{uuid}/current.pdf")
    meta = json.loads(storage.read_text(f"eu/mdcg_guidance/documents/{uuid}/current.meta.json"))
    assert meta["source_metadata"]["reference"] == "MDCG 2023-6"
    assert meta["source_metadata"]["category"] == "Annex XVI products"
    assert meta["source_metadata"]["publication"] == "December 2023"
    assert meta["content_type"] == "application/pdf"


@responses.activate
def test_link_in_title_cell_is_still_found(tmp_path):
    """A row whose Reference cell is plain text ("Q&A") must still be
    picked up via its Title cell's link — confirmed live shape, 7/156 rows."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    uuid = "a396270b-926d-46b5-b15b-a930171f2f86"
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html((
            "Annex XVI products",
            title_linked_row(uuid, "Q&A", "Q&A on transitional provisions", "qna.pdf", "September 2023"),
        )),
        status=200, content_type="text/html",
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/document/download/{uuid}_en",
        body=b"%PDF-1.7 fake pdf bytes", status=200, content_type="application/pdf",
    )

    summary = scraper.run()

    assert summary.new == 1
    assert storage.exists(f"eu/mdcg_guidance/documents/{uuid}/current.pdf")
    meta = json.loads(storage.read_text(f"eu/mdcg_guidance/documents/{uuid}/current.meta.json"))
    assert meta["source_metadata"]["reference"] == "Q&A"
    assert meta["title"] == "Q&A on transitional provisions"


@responses.activate
def test_external_link_with_no_extension_falls_back_to_html(tmp_path):
    """A row linking somewhere with no /document/download/ uuid and no
    filename extension (EUR-Lex, an overview page, ...) must still get a
    stable document_id and a sane ext/content_type instead of erroring."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    external_url = "https://www.ema.europa.eu/en/human-regulatory/overview/medical-devices"
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html((
            "Other topics",
            external_row(external_url, "EMA Guidance", "Questions & Answers for applicants", "n/a"),
        )),
        status=200, content_type="text/html",
    )
    responses.add(
        responses.GET, external_url,
        body="<html>ema overview page</html>", status=200, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.new == 1
    saved = list(storage.list("eu/mdcg_guidance/documents/"))
    assert any(path.endswith("current.html") for path in saved)


@responses.activate
def test_two_uuids_never_collide_regardless_of_link_cell(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    uuid_a, uuid_b = (
        "15a33521-87f1-4939-92a1-ef23f2b09c6c",
        "ea4acf26-979a-4dbb-92ff-8d1d804da51a",
    )
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html((
            "Annex XVI products",
            reference_linked_row(uuid_a, "MDCG 2023-6", "Equivalence", "a.pdf", "December 2023")
            + reference_linked_row(uuid_b, "MDCG 2023-5", "Classification", "b.pdf", "December 2023"),
        )),
        status=200, content_type="text/html",
    )
    for uuid in (uuid_a, uuid_b):
        responses.add(
            responses.GET, f"{BASE_URL}/document/download/{uuid}_en",
            body=b"%PDF-1.7 fake pdf bytes", status=200, content_type="application/pdf",
        )

    summary = scraper.run()

    assert summary.new == 2
    assert storage.exists(f"eu/mdcg_guidance/documents/{uuid_a}/current.pdf")
    assert storage.exists(f"eu/mdcg_guidance/documents/{uuid_b}/current.pdf")


@responses.activate
def test_listing_fetch_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, LISTING_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_already_known_document_is_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    uuid = "15a33521-87f1-4939-92a1-ef23f2b09c6c"
    html = listing_html((
        "Annex XVI products",
        reference_linked_row(uuid, "MDCG 2023-6", "Equivalence", "a.pdf", "December 2023"),
    ))
    responses.add(responses.GET, LISTING_URL, body=html, status=200, content_type="text/html")
    responses.add(
        responses.GET, f"{BASE_URL}/document/download/{uuid}_en",
        body=b"%PDF-1.7 fake pdf bytes", status=200, content_type="application/pdf",
    )
    scraper.run()

    # Second run: only the listing-page call registered — a re-fetch of
    # the PDF would raise ConnectionError from `responses`.
    responses.reset()
    responses.add(responses.GET, LISTING_URL, body=html, status=200, content_type="text/html")
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0


def test_ext_and_content_type_recognizes_every_observed_extension():
    cases = {
        "https://health.ec.europa.eu/document/download/x_en?filename=a.pdf": ("pdf", "application/pdf"),
        "https://health.ec.europa.eu/document/download/x_en?filename=a.docx": (
            "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "https://health.ec.europa.eu/document/download/x_en?filename=a.doc": ("doc", "application/msword"),
        "https://health.ec.europa.eu/document/download/x_en?filename=a.xlsx": (
            "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        "https://www.ema.europa.eu/en/human-regulatory/overview/medical-devices": ("html", "text/html"),
        "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52023XC0508(01)": ("html", "text/html"),
    }
    for url, expected in cases.items():
        assert MdcgGuidanceScraper._ext_and_content_type(url) == expected


def test_document_id_prefers_the_download_uuid_over_a_hash():
    uuid = "15a33521-87f1-4939-92a1-ef23f2b09c6c"
    url = f"https://health.ec.europa.eu/document/download/{uuid}_en?filename=a.pdf"
    assert MdcgGuidanceScraper._document_id(url) == uuid


def test_document_id_falls_back_to_a_stable_hash_for_non_standard_links():
    url = "https://www.ema.europa.eu/en/human-regulatory/overview/medical-devices"
    document_id = MdcgGuidanceScraper._document_id(url)
    assert document_id == MdcgGuidanceScraper._document_id(url)  # stable
    assert document_id != MdcgGuidanceScraper._document_id(url + "?x=1")  # url-specific
