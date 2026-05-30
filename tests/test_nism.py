import json
from pathlib import Path

from scrapers.nism import NISMScraper


CIRCULARS_FIXTURE = Path("tests/fixtures/nism/circulars_fixture_sample.html")
CIRCULARS_3_FIXTURE = Path("tests/fixtures/nism/circulars_3_fixture_sample.html")
ARCHIVE_FIXTURE = Path("tests/fixtures/nism/circular_archive_fixture_sample.html")
DETAIL_FIXTURE = Path("tests/fixtures/nism/circular_detail_fixture_sample.html")


def load_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> NISMScraper:
    return NISMScraper(config={"source": "nism-circulars"}, rate_limit_seconds=0)


def test_parse_recent_listing_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_recent_listing(load_fixture(CIRCULARS_FIXTURE), "https://www.nism.ac.in/circulars/")
    assert len(records) == 1
    assert records[0].date == "2026-05-18"
    assert records[0].circular_no == "NISM/Certification/ NISM-Series-XXV-B: Persons Associated with Investment Advice (Sales and Other Non-Core Services) Certification Examination/2026/01"


def test_parse_circulars_3_fixture_and_sebi_reference() -> None:
    scraper = make_scraper()
    records = scraper.parse_recent_listing(load_fixture(CIRCULARS_3_FIXTURE), "https://www.nism.ac.in/circulars-3/")
    assert len(records) == 1
    assert records[0].date == "2026-03-11"
    assert records[0].circular_no.startswith("SEBI Circular for NISM Series XXV-A")


def test_detect_view_all_archive_link() -> None:
    scraper = make_scraper()
    probe = scraper.inspect_page(
        "https://www.nism.ac.in/circulars/",
        load_fixture(CIRCULARS_FIXTURE),
        200,
        "https://www.nism.ac.in/circulars/",
    )
    assert probe["archive_url"] == "https://www.nism.ac.in/circular-archive-list/?type=circular"


def test_parse_archive_rows_and_pagination() -> None:
    scraper = make_scraper()
    records = scraper.parse_archive_listing(load_fixture(ARCHIVE_FIXTURE), "https://www.nism.ac.in/circular-archive-list/?type=circular")
    assert len(records) == 2
    assert records[0].date == "2009-05-12"
    assert records[1].date == "2007-10-03"
    probe = scraper.inspect_page(
        "https://www.nism.ac.in/circular-archive-list/?type=circular",
        load_fixture(ARCHIVE_FIXTURE),
        200,
        "https://www.nism.ac.in/circular-archive-list/?type=circular",
    )
    assert probe["archive_pagination_exists"] is True


def test_parse_detail_title_date_last_updated_and_downloads() -> None:
    scraper = make_scraper()
    detail = scraper.parse_detail_page(
        load_fixture(DETAIL_FIXTURE),
        "https://www.nism.ac.in/circular/sebi-circular-for-nism-series-xxv-a-persons-associated-with-research-services-sales-and-other-non-core-services-certification-examination-dated-march-11-2026/",
    )
    assert detail["date"] == "2026-03-11"
    assert detail["last_updated"] == "March 12, 2026"
    assert detail["download_links"] == [
        "https://www.sebi.gov.in/legal/circulars/mar-2026/ease-of-doing-business-relaxation-in-certification-requirement-for-persons-associated-with-research-services-pars-sales-and-other-non-core-services_100249.html"
    ]


def test_extract_click_here_download_links_and_safe_references() -> None:
    scraper = make_scraper()
    assert scraper.extract_reference("NISM/Certification/AIF Managers CPE/2026/01 dated January 30, 2026") == "NISM/Certification/AIF Managers CPE/2026/01"
    assert scraper.extract_reference("SEBI Notification for NISM Series-XIX-D dated June 25, 2025") == "SEBI Notification for NISM Series-XIX-D"
    assert scraper.extract_reference("General update without formal reference") == ""


def test_date_normalization_supports_ordinals_and_short_months() -> None:
    scraper = make_scraper()
    assert scraper.normalize_date("18th May 2026") == "2026-05-18"
    assert scraper.normalize_date("02nd Mar 2026") == "2026-03-02"
    assert scraper.normalize_date("30th Jan 2026") == "2026-01-30"
    assert scraper.normalize_date("24 Jun 2010") == "2010-06-24"


def test_absolute_url_and_link_type_detection() -> None:
    scraper = make_scraper()
    assert scraper.normalize_link("/circular/example/", "https://www.nism.ac.in/circulars/") == "https://www.nism.ac.in/circular/example/"
    assert scraper.detect_link_type("https://www.nism.ac.in/circular/example/") == "html/detail"
    assert scraper.detect_link_type("https://www.nism.ac.in/wp-content/uploads/file.pdf") == "pdf"
    assert scraper.detect_link_type("https://www.sebi.gov.in/legal/circulars/test.html") == "html/detail"


def test_dedupe_and_blank_circular_number_behavior() -> None:
    scraper = make_scraper()
    records = scraper.parse_archive_listing(load_fixture(ARCHIVE_FIXTURE), "https://www.nism.ac.in/circular-archive-list/?type=circular")
    records[1].circular_no = ""
    deduped = scraper.deduplicate_records(records + [records[0]])
    assert len(deduped) == 2


def test_json_and_csv_writing_support_include_downloads(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_archive_listing(load_fixture(ARCHIVE_FIXTURE), "https://www.nism.ac.in/circular-archive-list/?type=circular")
    records[0].download_links = ["https://www.sebi.gov.in/example.html"]
    csv_path = tmp_path / "nism.csv"
    json_path = tmp_path / "nism.json"
    scraper.write_output(records, csv_path, include_downloads=True)
    scraper.write_output(records, json_path, include_downloads=True)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,download_links,source_url,scraped_at"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert payload[0]["download_links"] == '["https://www.sebi.gov.in/example.html"]'


def test_validation(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_archive_listing(load_fixture(ARCHIVE_FIXTURE), "https://www.nism.ac.in/circular-archive-list/?type=circular")
    out_path = tmp_path / "nism_circulars_archive.csv"
    scraper.write_output(records, out_path)
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["missing_subject_count"] == 0
    assert (tmp_path / "nism_circulars_validation_report.json").exists()
