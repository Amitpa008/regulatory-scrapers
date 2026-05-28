import csv
import json
from pathlib import Path

import httpx
import pytest

from scrapers.cdsl import CDSLCheckpoint, CDSLCommuniqueRecord, CDSLScraper


FIXTURE_DIR = Path("tests/fixtures/cdsl")
HTML_FIXTURE_PATH = FIXTURE_DIR / "communique_fixture_sample.html"
API_FIXTURE_PATH = FIXTURE_DIR / "communique_api_sample.json"
INDEX_TEXT_FIXTURE_PATH = FIXTURE_DIR / "dp_communique_index_sample.txt"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> CDSLScraper:
    return CDSLScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_cdsl_html_fixture() -> None:
    html = load_text(HTML_FIXTURE_PATH)
    assert "GetOnLoadCommunique" in html
    assert "CommuniquePost" in html
    assert 'id="tblCommuniqueDtl"' in html


def test_parse_saved_cdsl_api_fixture_mapping() -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_communique_records(payload, "https://www.cdslindia.com/eservices/Publications/Communique")

    assert len(records) == 3
    record_by_no = {record.circular_no: record for record in records}
    assert record_by_no["DP2026-320"].date == "2026-05-12"
    assert record_by_no["DP2026-320"].subject == "DETAILS OF SECURITIES ADMITTED WITH CDSL"
    assert "DownloadFile?eventID=DP2026-320" in record_by_no["DP2026-320"].link


def test_parse_index_sample_text() -> None:
    scraper = make_scraper()
    rows = scraper.parse_index_rows_from_text(load_text(INDEX_TEXT_FIXTURE_PATH))

    assert len(rows) == 5
    assert rows[0].date == "1999-02-04"
    assert rows[-1].circular_no == "DP2026-321"


def test_absolute_url_and_download_link_generation() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_cdsl_link("\\communiques\\dp\\DP34.pdf", "https://www.cdslindia.com/eservices/Publications/Communique")
        == "https://www.cdslindia.com/communiques/dp/DP34.pdf"
    )
    assert (
        scraper.build_download_link("DP34", "https://www.cdslindia.com/eservices/Publications/Communique")
        == "https://www.cdslindia.com/eservices/Publications/DownloadFile?eventID=DP34&method=communique"
    )


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_communique_records(payload, "https://www.cdslindia.com/eservices/Publications/Communique")

    csv_path = tmp_path / "cdsl.csv"
    json_path = tmp_path / "cdsl.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 3


def test_deduplication_and_missing_circular_number_fallback() -> None:
    scraper = make_scraper()
    record = CDSLCommuniqueRecord(
        date="2026-05-12",
        subject="Same subject",
        circular_no="",
        link="https://www.cdslindia.com/eservices/Publications/DownloadFile?eventID=X&method=communique",
        source_url="https://www.cdslindia.com/eservices/Publications/Communique",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    assert scraper.record_dedup_key(record) == (
        "2026-05-12",
        "same subject",
        "https://www.cdslindia.com/eservices/Publications/DownloadFile?eventID=X&method=communique",
    )


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = CDSLCheckpoint(
        source_url="https://www.cdslindia.com/eservices/Publications/Communique",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-15",
        oldest_available_date="1999-02-04",
        total_records_detected=12335,
        chunk_strategy="public_onload_active_archive_feeds",
        last_completed_chunk=2,
        records_written=10,
        unique_records_written=10,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(checkpoint_path, checkpoint)
    loaded = scraper.load_checkpoint(checkpoint_path)
    assert loaded.last_completed_chunk == 2


def test_retry_handling(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.cdsl.time.sleep", lambda _: None)

    def fake_post(url, data=None, headers=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("disconnect")
        return httpx.Response(200, request=httpx.Request("POST", url), json=[])

    monkeypatch.setattr(scraper, "fetch_page", lambda *args, **kwargs: httpx.Response(200, request=httpx.Request("GET", "https://x")))
    monkeypatch.setattr(scraper.client, "post", fake_post)
    payload = scraper.fetch_onload_payload("A", "3")

    assert payload == []
    assert attempts["count"] == 2


def test_zero_record_feed_handling() -> None:
    scraper = make_scraper()
    assert scraper.parse_communique_records([], "https://www.cdslindia.com/eservices/Publications/Communique") == []


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
    record = CDSLCommuniqueRecord(
        date="2026-05-12",
        subject="DETAILS OF SECURITIES ADMITTED WITH CDSL",
        circular_no="DP2026-320",
        link="https://www.cdslindia.com/eservices/Publications/DownloadFile?eventID=DP2026-320&method=communique",
        source_url="https://www.cdslindia.com/eservices/Publications/Communique",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    export_path = tmp_path / "cdsl.csv"
    scraper.write_output([record], export_path)

    report = scraper.validate_export(export_path)
    assert report["total_rows"] == 1
    assert report["duplicate_key_count"] == 0
    assert (tmp_path / "cdsl_communiques_validation_report.json").exists()
    assert (tmp_path / "cdsl_communiques_year_counts.csv").exists()


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = CDSLCommuniqueRecord(
        date="2026-05-12",
        subject="DETAILS OF SECURITIES ADMITTED WITH CDSL",
        circular_no="DP2026-320",
        link="https://www.cdslindia.com/eservices/Publications/DownloadFile?eventID=DP2026-320&method=communique",
        source_url="https://www.cdslindia.com/eservices/Publications/Communique",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    csv_path = tmp_path / "cdsl.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)

    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1
