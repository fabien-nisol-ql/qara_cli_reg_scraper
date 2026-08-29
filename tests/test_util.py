from qara_reg_scraper.util import derive_original_filename, slugify


def test_slugify_basic():
    assert slugify("21 CFR Part 800!!") == "21-cfr-part-800"
    assert slugify("") == "untitled"


def test_derive_original_filename_prefers_content_disposition_plain():
    headers = {"Content-Disposition": 'attachment; filename="K252474 Summary.pdf"'}
    assert derive_original_filename("https://example.gov/download?id=1", headers) == "K252474 Summary.pdf"


def test_derive_original_filename_prefers_rfc5987_extended_form():
    # filename* takes priority over filename= when both are present.
    headers = {
        "Content-Disposition": (
            "attachment; filename=\"fallback.pdf\"; "
            "filename*=UTF-8''K252474%20Summary.pdf"
        )
    }
    assert derive_original_filename("https://example.gov/download?id=1", headers) == "K252474 Summary.pdf"


def test_derive_original_filename_falls_back_to_url_when_no_header():
    headers: dict[str, str] = {}
    assert derive_original_filename("https://example.gov/docs/K252474.pdf", headers) == "K252474.pdf"


def test_derive_original_filename_none_when_neither_source_has_one():
    headers: dict[str, str] = {}
    assert derive_original_filename("https://example.gov/api/v1/records?id=1", headers) is None


def test_derive_original_filename_ignores_unparseable_content_disposition():
    headers = {"Content-Disposition": "attachment"}  # no filename at all
    assert derive_original_filename("https://example.gov/docs/K252474.pdf", headers) == "K252474.pdf"
