"""Tests for the Canadian guidance scraper against Health Canada's real
listing-page shape (confirmed live while building this — see the module
docstring): every link inside the WET/GCWeb theme's
`property="mainContentOfPage"` region, relative or absolute, most ending
in a clean `.html` slug but at least one a query-string-only external
link (the "Device Advice: e-Learning tool")."""

from __future__ import annotations

import responses
from requests.exceptions import ConnectionError as RequestsConnectionError

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.ca.guidance import BASE_URL, LISTING_URL, GuidanceScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "ca", "guidance", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "guidance"
    )
    return storage, manifest, GuidanceScraper(http, manifest, **kwargs)


def listing_html(*links: str) -> str:
    """links: raw <a> tags, already fully formed."""
    return (
        '<html><body><nav><a href="/en/nav-noise.html">Not real content</a></nav>'
        f'<main property="mainContentOfPage">{"".join(links)}</main></body></html>'
    )


@responses.activate
def test_only_links_inside_the_main_content_region_are_captured(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html('<a href="/en/health-canada/guidance-document-one.html">Guidance document: One</a>'),
        status=200, content_type="text/html",
    )
    responses.add(
        responses.GET, f"{BASE_URL}/en/health-canada/guidance-document-one.html",
        body="<html>guidance content</html>", status=200, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.new == 1
    # document_id is the URL's raw last path segment, extension included
    # (canada.ca URLs end in .html) - see _document_id's own docstring.
    assert storage.exists("ca/guidance/documents/guidance-document-one.html/current.html")
    assert not storage.exists("ca/guidance/documents/nav-noise.html/current.html")


@responses.activate
def test_absolute_external_link_with_no_path_uses_the_domain_as_id(tmp_path):
    """A bare-domain external link (no path at all, confirmed live: the
    MDSAP program's own homepage) still gets a readable id — the domain
    itself — rather than falling straight to a hash."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html('<a href="https://www.mdsap.global/">Medical Device Single Audit Program (MDSAP)</a>'),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, "https://www.mdsap.global/", body="<html>mdsap</html>", status=200)

    summary = scraper.run()

    assert summary.new == 1
    assert storage.exists("ca/guidance/documents/www.mdsap.global/current.html")


@responses.activate
def test_a_network_failure_on_an_external_link_does_not_stop_the_run(tmp_path):
    """Regression test for a real, live failure (confirmed 2026-08-30):
    www.mdsap.global timed out/dropped the connection on every automatic
    retry for hours, repeatedly stopping the entire run before it ever
    reached the other genuinely canada.ca-hosted documents. A network
    failure on an external host must be a routine per-document miss, not
    a HardStop - unlike a failure on canada.ca itself (see the next
    test)."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html(
            '<a href="https://www.mdsap.global/">Medical Device Single Audit Program (MDSAP)</a>'
            '<a href="/en/health-canada/guidance-document-one.html">Guidance document: One</a>'
        ),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, "https://www.mdsap.global/", body=RequestsConnectionError("timed out"))
    responses.add(
        responses.GET, f"{BASE_URL}/en/health-canada/guidance-document-one.html",
        body="<html>guidance content</html>", status=200, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.stop_reason == "completed"
    assert summary.new == 1  # the canada.ca one - mdsap.global's failure didn't stop the run
    assert summary.errors == 1  # still recorded, just not fatal
    assert storage.exists("ca/guidance/documents/guidance-document-one.html/current.html")
    assert not storage.exists("ca/guidance/documents/www.mdsap.global/current.html")


@responses.activate
def test_a_network_failure_on_canada_ca_itself_still_stops_the_run(tmp_path):
    """Unlike an external link, a canada.ca fetch failing at the network
    level IS real signal about this source's own primary host - must
    still behave exactly like every other source in this tool."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html('<a href="/en/health-canada/guidance-document-one.html">Guidance document: One</a>'),
        status=200, content_type="text/html",
    )
    responses.add(
        responses.GET, f"{BASE_URL}/en/health-canada/guidance-document-one.html",
        body=RequestsConnectionError("timed out"),
    )

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.new == 0


@responses.activate
def test_query_string_link_falls_back_to_a_stable_hash(tmp_path):
    """The one real non-.html, query-string link (an external e-learning
    tool, confirmed live) has no clean path slug to key off."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    url = "https://training-formation.phac-aspc.gc.ca/course/index.php?categoryid=42&lang=en"
    responses.add(
        responses.GET, LISTING_URL,
        body=listing_html(f'<a href="{url}">Device Advice: e-Learning tool</a>'),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, url, body="<html>elearning</html>", status=200)

    summary = scraper.run()

    assert summary.new == 1
    document_id = GuidanceScraper._document_id(url)
    assert document_id != "index.php"  # not the naive path-slug - has a query string
    assert storage.exists(f"ca/guidance/documents/{document_id}/current.html")
    # stable across calls
    assert document_id == GuidanceScraper._document_id(url)


@responses.activate
def test_listing_fetch_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, LISTING_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_missing_main_content_region_is_a_hard_stop(tmp_path):
    """A page with no `property="mainContentOfPage"` region at all means
    the site's markup changed - treated as this source's own error, not a
    zero-document success."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, LISTING_URL, body="<html><body>no main region here</body></html>", status=200)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_already_known_document_is_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    html = listing_html('<a href="/en/health-canada/guidance-document-one.html">Guidance document: One</a>')
    responses.add(responses.GET, LISTING_URL, body=html, status=200, content_type="text/html")
    responses.add(
        responses.GET, f"{BASE_URL}/en/health-canada/guidance-document-one.html",
        body="<html>guidance content</html>", status=200, content_type="text/html",
    )
    scraper.run()

    # Second run: only the listing call registered — a re-fetch of the
    # detail page would raise ConnectionError from `responses`.
    responses.reset()
    responses.add(responses.GET, LISTING_URL, body=html, status=200, content_type="text/html")
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0
