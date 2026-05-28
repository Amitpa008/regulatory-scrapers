from pathlib import Path

import httpx

from scrapers.sebi import SEBIScraper
from storage.database import DocumentDatabase


FIXTURE_DIR = Path("tests/fixtures/sebi")


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def make_response(url: str, html: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(200, text=html, request=request)


def make_scraper(tmp_path) -> SEBIScraper:
    return SEBIScraper(
        config={},
        database=DocumentDatabase(tmp_path / "sebi.db"),
        rate_limit_seconds=0,
    )


def test_parse_circular_listing_fixture_reads_at_least_five_records(tmp_path) -> None:
    scraper = make_scraper(tmp_path)
    response = make_response(
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&smid=0&ssid=7",
        load_fixture("circulars_listing.html"),
    )
    page = scraper._parse_listing_page(response, "circulars")

    assert len(page.rows) >= 5
    assert page.rows[0]["title"] == "Status of SPVs post conclusion or termination of Concession Agreement"
    assert page.rows[0]["published_date"].isoformat() == "2026-05-15"


def test_detail_url_normalization(tmp_path) -> None:
    scraper = make_scraper(tmp_path)
    normalized = scraper._normalize_detail_url(
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&smid=0&ssid=7",
        "/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html",
    )
    assert (
        normalized
        == "https://www.sebi.gov.in/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html"
    )


def test_fetch_document_uses_detail_fixture_and_extracts_pdf_url(tmp_path) -> None:
    scraper = make_scraper(tmp_path)
    record = {
        "source": "SEBI",
        "regulator": "SEBI",
        "document_type": "Circulars",
        "published_date": None,
        "title": "Status of SPVs post conclusion or termination of Concession Agreement",
        "detail_url": "https://www.sebi.gov.in/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html",
        "url": "https://www.sebi.gov.in/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html",
    }
    response = make_response(record["detail_url"], load_fixture("circulars_detail.html"))
    document = scraper._build_document_from_detail_response(record, response)

    assert document.reference_no == "SEBI/HO/DDHS/DDHS-PoD-2/I/11698/2026"
    assert str(document.pdf_url) == "https://www.sebi.gov.in/sebi_data/attachdocs/may-2026/1778844282193.pdf"
    assert document.published_date.isoformat() == "2026-05-15"


def test_missing_pdf_does_not_crash_full_scrape(tmp_path) -> None:
    scraper = make_scraper(tmp_path)
    detail_html = load_fixture("circulars_detail.html").replace(
        '<iframe src="../../../web/?file=https://www.sebi.gov.in/sebi_data/attachdocs/may-2026/1778844282193.pdf" width="100%" style="max-height:90%; height:600px;" title="Status of SPVs post conclusion or termination of Concession Agreement" allowfullscreen webkitallowfullscreen>Status of SPVs post conclusion or termination of Concession Agreement</iframe>',
        "",
    )
    record = {
        "source": "SEBI",
        "regulator": "SEBI",
        "document_type": "Circulars",
        "published_date": None,
        "title": "Status of SPVs post conclusion or termination of Concession Agreement",
        "detail_url": "https://www.sebi.gov.in/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html",
        "url": "https://www.sebi.gov.in/legal/circulars/may-2026/status-of-spvs-post-conclusion-or-termination-of-concession-agreement_101467.html",
    }

    scraper.fetch_http_or_browser = lambda url: make_response(url, detail_html)  # type: ignore[assignment]
    stats = scraper.process_records([record])

    assert stats["failed"] == 0
    assert stats["inserted"] == 1
    assert stats["pdf_downloaded"] == 0
