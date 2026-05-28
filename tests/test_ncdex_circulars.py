import csv
import json
from pathlib import Path

import httpx
import pytest

from scrapers.ncdex import NCDEXCheckpoint, NCDEXCircularRecord, NCDEXScraper


FIXTURE_DIR = Path("tests/fixtures/ncdex")
HTML_FIXTURE_PATH = FIXTURE_DIR / "circulars_fixture_sample.html"
RENDERED_FIXTURE_PATH = FIXTURE_DIR / "circulars_rendered_fixture_sample.html"
API_FIXTURE_PATH = FIXTURE_DIR / "circulars_api_sample.json"
NETWORK_CAPTURE_FIXTURE_PATH = FIXTURE_DIR / "circulars_network_capture_fixture_sample.json"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> NCDEXScraper:
    return NCDEXScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_ncdex_html_fixture() -> None:
    html = load_text(HTML_FIXTURE_PATH)
    assert "Circulars | National Commodity" in html
    assert "/circulars/circular_data" in html
    assert 'id="year"' in html


def test_parse_saved_ncdex_api_fixture_mapping() -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_circular_records(payload, "https://www.ncdex.com/circulars")

    assert len(records) == 3
    assert records[0].date == "2026-05-15"
    assert records[0].subject == "Submission of VAPT Report for the FY 2025-26"
    assert records[0].circular_no == "NCDEX/Member Tech Compliance-010/2026"
    assert records[0].link == "https://www.ncdex.com/public/uploads/circulars/english/2026/NCDEX-MTC-010-2026.pdf"


def test_parse_rendered_fixture_rows() -> None:
    scraper = make_scraper()
    records = scraper.parse_rendered_rows(load_text(RENDERED_FIXTURE_PATH), "https://www.ncdex.com/circulars")

    assert len(records) == 2
    assert records[0].circular_no == "NCDEX/TRADING-016/2026"
    assert records[0].department == "Trading"


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_ncdex_link("/public/uploads/circulars/example.pdf", "https://www.ncdex.com/circulars")
        == "https://www.ncdex.com/public/uploads/circulars/example.pdf"
    )


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_circular_records(payload, "https://www.ncdex.com/circulars")

    csv_path = tmp_path / "ncdex.csv"
    json_path = tmp_path / "ncdex.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 3


def test_deduplication_and_missing_circular_number_fallback() -> None:
    scraper = make_scraper()
    record = NCDEXCircularRecord(
        date="2026-05-15",
        subject="Same subject",
        circular_no="",
        link="https://www.ncdex.com/example.pdf",
        source_url="https://www.ncdex.com/circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    assert scraper.record_dedup_key(record) == ("2026-05-15", "same subject", "https://www.ncdex.com/example.pdf")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = NCDEXCheckpoint(
        source_url="https://www.ncdex.com/circulars",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-15",
        oldest_available_date="2003-11-18",
        total_records_detected=223,
        chunk_strategy="year_filters_via_browser_session",
        last_completed_chunk=2,
        records_written=3,
        unique_records_written=3,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(checkpoint_path, checkpoint)
    loaded = scraper.load_checkpoint(checkpoint_path)
    assert loaded.last_completed_chunk == 2


def test_available_years_from_page_info() -> None:
    scraper = make_scraper()
    page_info = scraper.collect_page_info(load_text(RENDERED_FIXTURE_PATH), "https://www.ncdex.com/circulars")
    assert scraper.available_years_from_page_info(page_info) == [2003, 2025, 2026]


def test_retry_handling(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.ncdex.time.sleep", lambda _: None)

    def fake_fetch_browser_payload(*, endpoint, payload, session):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("disconnect")
        return {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []}

    monkeypatch.setattr(scraper, "fetch_browser_payload", fake_fetch_browser_payload)
    payload = scraper.fetch_browser_payload_with_retry(
        endpoint="https://www.ncdex.com/circulars/circular_data",
        payload={},
        session={"page": object()},
        retries=5,
        retry_base_delay=3.0,
        retry_max_delay=60.0,
    )

    assert payload["recordsTotal"] == 0
    assert attempts["count"] == 2


def test_zero_record_payload_handling() -> None:
    scraper = make_scraper()
    assert scraper.parse_circular_records({"data": []}, "https://www.ncdex.com/circulars") == []


def test_file_lock_preflight(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    locked_path = tmp_path / "locked.csv"
    locked_path.write_text("", encoding="utf-8")

    def fake_open(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(RuntimeError, match="Output file is locked"):
        scraper.ensure_output_writable(locked_path, resume=True)


def test_validation(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = NCDEXCircularRecord(
        date="2026-05-15",
        subject="Submission of VAPT Report for the FY 2025-26",
        circular_no="NCDEX/Member Tech Compliance-010/2026",
        link="https://www.ncdex.com/public/uploads/circulars/english/2026/NCDEX-MTC-010-2026.pdf",
        source_url="https://www.ncdex.com/circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    export_path = tmp_path / "ncdex.csv"
    scraper.write_output([record], export_path)

    report = scraper.validate_export(export_path)
    assert report["total_rows"] == 1
    assert report["duplicate_key_count"] == 0
    assert (tmp_path / "ncdex_circulars_validation_report.json").exists()
    assert (tmp_path / "ncdex_circulars_year_counts.csv").exists()


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = NCDEXCircularRecord(
        date="2026-05-15",
        subject="Submission of VAPT Report for the FY 2025-26",
        circular_no="NCDEX/Member Tech Compliance-010/2026",
        link="https://www.ncdex.com/public/uploads/circulars/english/2026/NCDEX-MTC-010-2026.pdf",
        source_url="https://www.ncdex.com/circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    csv_path = tmp_path / "ncdex.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)

    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1


def test_network_capture_fixture_exists() -> None:
    payload = json.loads(NETWORK_CAPTURE_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload[0]["url"].endswith("/circulars/circular_data")
