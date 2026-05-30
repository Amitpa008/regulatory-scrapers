import json
from pathlib import Path

from bs4 import BeautifulSoup

from scrapers.nsekra import NSEKRAScraper


SHELL_FIXTURE = Path("tests/fixtures/nsekra/circulars_fixture_sample.html")
RENDERED_FIXTURE = Path("tests/fixtures/nsekra/circulars_rendered_fixture_sample.html")
API_FIXTURE = Path("tests/fixtures/nsekra/circulars_api_fixture_sample.json")


def load_shell_fixture() -> str:
    return SHELL_FIXTURE.read_text(encoding="utf-8")


def load_rendered_fixture() -> str:
    return RENDERED_FIXTURE.read_text(encoding="utf-8")


def load_api_fixture() -> dict:
    return json.loads(API_FIXTURE.read_text(encoding="utf-8"))


def make_scraper() -> NSEKRAScraper:
    return NSEKRAScraper(config={"source": "nsekra-circulars"}, rate_limit_seconds=0)


def test_detects_js_shell_raw_html() -> None:
    scraper = make_scraper()
    html = load_shell_fixture()
    assert "enable JavaScript to run this app" in html
    soup = BeautifulSoup(html, "html.parser")
    assets = scraper.extract_script_assets(soup, "https://www.nsekra.com/circulars")
    assert "https://www.nsekra.com/static/js/main.2dc18c1f.js" in assets


def test_parse_rendered_circular_rows() -> None:
    scraper = make_scraper()
    records = scraper.parse_rendered_circular_rows(load_rendered_fixture(), "https://www.nsekra.com/circulars")
    assert len(records) == 2
    assert records[0].date == "2025-06-16"
    assert records[0].circular_no == "NSE/KRA/2025/06"
    assert records[0].subject == "Registration of clients with KRA"
    assert records[0].link == "https://www.nsekra.com/downloads/circulars/nsekra_2025_06.pdf"


def test_parse_api_payload_mapping() -> None:
    scraper = make_scraper()
    records, total_records = scraper.parse_api_payload(load_api_fixture(), "https://www.nsekra.com/circulars")
    assert total_records == 2
    assert len(records) == 2
    assert records[1].date == "2025-04-24"
    assert records[1].circular_no == "NSE/KRA/2025/05"
    assert records[1].subject == "Clarification on KYC records for Non-Resident Indians"


def test_absolute_url_conversion_and_link_types() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_link("/downloads/circulars/nsekra_2025_06.pdf", "https://www.nsekra.com/circulars")
        == "https://www.nsekra.com/downloads/circulars/nsekra_2025_06.pdf"
    )
    assert scraper.detect_link_type("https://www.nsekra.com/downloads/circulars/nsekra_2025_06.pdf") == "pdf"
    assert scraper.detect_link_type("https://external.example.com/file.pdf") == "external"


def test_circular_number_extraction() -> None:
    scraper = make_scraper()
    assert scraper.extract_reference("Clarification under NSE/KRA/2025/06 for KYC records") == "NSE/KRA/2025/06"
    assert scraper.extract_reference("General KYC clarification") == ""


def test_json_and_csv_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_api_payload(load_api_fixture(), "https://www.nsekra.com/circulars")
    csv_path = tmp_path / "nsekra.csv"
    json_path = tmp_path / "nsekra.json"
    scraper.write_output(records, csv_path)
    scraper.write_output(records, json_path)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe_falls_back_when_link_missing() -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_api_payload(load_api_fixture(), "https://www.nsekra.com/circulars")
    records[0].link = ""
    duplicate = scraper.parse_api_payload(load_api_fixture(), "https://www.nsekra.com/circulars")[0][0]
    duplicate.link = ""
    deduped = scraper.deduplicate_records([records[0], duplicate])
    assert len(deduped) == 1


def test_date_normalization() -> None:
    scraper = make_scraper()
    assert scraper.normalize_date("16-Jun-2025") == "2025-06-16"
    assert scraper.normalize_date("2025-04-24") == "2025-04-24"
    assert scraper.normalize_date("2025-04-24T00:00:00") == "2025-04-24"
    assert scraper.normalize_date("2026-01-05 00:00:00.0") == "2026-01-05"


def test_text_cleanup_repairs_replacement_character_quotes() -> None:
    scraper = make_scraper()
    assert scraper.clean_text("Standard Operating Procedure (�SOP�) for KRA Business") == 'Standard Operating Procedure ("SOP") for KRA Business'


def test_validation_flags_missing_circular_no(tmp_path: Path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_api_payload(load_api_fixture(), "https://www.nsekra.com/circulars")
    records[0].circular_no = ""
    out_path = tmp_path / "nsekra_circulars_archive.csv"
    scraper.write_output(records, out_path)
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["missing_circular_no_count"] == 1
    assert (tmp_path / "nsekra_circulars_validation_report.json").exists()


def test_zero_record_page_parsing() -> None:
    scraper = make_scraper()
    records, total_records = scraper.parse_api_payload({"data": {"CircularView": [{"TotalRecords": 0}]}}, "https://www.nsekra.com/circulars")
    assert records == []
    assert total_records == 0


def test_checkpoint_resume_window_uses_per_run_chunk_count() -> None:
    scraper = make_scraper()
    window = scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=100, max_chunks_this_run=50)
    assert window["resume_from_chunk"] == 101
    assert window["expected_end_chunk"] == 150
    assert window["chunks_this_run"] == 50


def test_invalid_chunk_window_raises() -> None:
    scraper = make_scraper()
    try:
        scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=100, max_chunks_this_run=0)
    except RuntimeError as exc:
        assert "Invalid chunk window" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected RuntimeError for invalid chunk window")
