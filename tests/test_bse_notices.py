import csv
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from scrapers.bse import BSECheckpoint, BSENoticeRecord, BSEScraper, BSE_ARCHIVE_BETA_URL


FIXTURE_DIR = Path("tests/fixtures/bse")
ARCHIVE_FIXTURE_PATH = FIXTURE_DIR / "archive_search_result_sample.html"
SEARCH_FIXTURE_PATH = FIXTURE_DIR / "archive_search_result_sample.html"
CURRENT_API_FIXTURE_PATH = FIXTURE_DIR / "current_notices_api_sample.json"
NETWORK_CAPTURE_FIXTURE_PATH = FIXTURE_DIR / "browser_network_capture.json"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> BSEScraper:
    return BSEScraper(config={}, rate_limit_seconds=0)


def test_parse_rendered_archive_html_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_notice_records(load_text(ARCHIVE_FIXTURE_PATH), BSE_ARCHIVE_BETA_URL)

    assert len(records) >= 10
    assert records[0].date == "2026-03-13"
    assert records[0].circular_no == "20260313-60"
    assert records[0].subject == "Daily Bulletin"


def test_parse_current_api_fixture_mapping() -> None:
    scraper = make_scraper()
    payload = json.loads(CURRENT_API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_current_api_records(payload, "https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject=")

    assert len(records) == 2
    assert records[0].date == "2026-05-17"
    assert records[0].subject.startswith("Non-Competitive Bidding Facility")
    assert records[0].circular_no == "20260517-2"
    assert records[0].link.endswith("20260517-2.pdf")


def test_derive_date_from_notice_number() -> None:
    scraper = make_scraper()
    assert scraper.derive_bse_date("20260313-60") == "2026-03-13"


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    record = scraper.parse_notice_records(load_text(ARCHIVE_FIXTURE_PATH), BSE_ARCHIVE_BETA_URL)[0]

    assert record.link == "https://beta.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=20260313-60"


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_notice_records(load_text(ARCHIVE_FIXTURE_PATH), BSE_ARCHIVE_BETA_URL)[:3]

    csv_path = tmp_path / "bse.csv"
    json_path = tmp_path / "bse.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 3


def test_deduplication_and_missing_circular_number_fallback() -> None:
    scraper = make_scraper()
    record_a = BSENoticeRecord(
        date="2026-03-13",
        subject="Test Subject",
        circular_no="",
        link="https://www.bseindia.com/example.pdf",
        source_url=BSE_ARCHIVE_BETA_URL,
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    record_b = BSENoticeRecord(**record_a.__dict__)

    assert scraper.record_dedup_key(record_a) == scraper.record_dedup_key(record_b)


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = BSECheckpoint(
        source_url="https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject=",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-17",
        oldest_available_date="1996-01-01",
        total_records_detected=None,
        chunk_strategy="current_api_plus_archive_form",
        last_completed_chunk=3,
        records_written=120,
        unique_records_written=120,
        started_at="2026-05-17T00:00:00+00:00",
        updated_at="2026-05-17T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(checkpoint_path, checkpoint)
    loaded = scraper.load_checkpoint(checkpoint_path)

    assert loaded.last_completed_chunk == 3


def test_archive_chunking_by_date_range() -> None:
    scraper = make_scraper()
    chunks = scraper.build_archive_chunks(date(2020, 1, 1), date(2026, 3, 13))

    assert chunks[0]["from_date"] == "2020-01-01"
    assert chunks[-1]["to_date"] == "2026-03-13"
    assert len(chunks) >= 2


def test_transient_retry_handling(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.bse.time.sleep", lambda _: None)

    def fake_get(page_url: str, headers=None, params=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("disconnect")
        return httpx.Response(200, request=httpx.Request("GET", page_url), text="<html></html>")

    monkeypatch.setattr(scraper.client, "get", fake_get)
    response = scraper.fetch_page(BSE_ARCHIVE_BETA_URL)

    assert response.status_code == 200
    assert attempts["count"] == 2


def test_non_retryable_403_is_reported(monkeypatch) -> None:
    scraper = make_scraper()

    def fake_get(page_url: str, headers=None, params=None):
        request = httpx.Request("GET", page_url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr(scraper.client, "get", fake_get)
    with pytest.raises(httpx.HTTPStatusError):
        scraper.fetch_page(BSE_ARCHIVE_BETA_URL)


def test_zero_record_chunk_handling() -> None:
    scraper = make_scraper()
    empty_html = "<html><body><table id='ContentPlaceHolder1_GridView2'><tr><th>Notice No</th><th>Subject</th></tr><tr><td class='ErrorRow'>No Record Found</td></tr></table></body></html>"
    assert scraper.parse_notice_records(empty_html, BSE_ARCHIVE_BETA_URL) == []


def test_browser_network_capture_fixture_exists() -> None:
    payload = json.loads(NETWORK_CAPTURE_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload


def test_find_next_page_number_from_search_fixture() -> None:
    scraper = make_scraper()
    assert scraper.find_next_page_number(load_text(SEARCH_FIXTURE_PATH), current_page=1) == 2


def test_old_unresolved_row_extraction_and_no_fake_date() -> None:
    scraper = make_scraper()
    html = """
    <html><body>
    <table id="ContentPlaceHolder1_GridView2">
      <tr><th>Notice No</th><th>Subject</th></tr>
      <tr>
        <td>109521/2001</td>
        <td><a href="/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=109521/2001">HAPPY NEW YEAR</a></td>
      </tr>
    </table>
    </body></html>
    """
    exact = scraper.parse_notice_records(html, BSE_ARCHIVE_BETA_URL)
    unresolved = scraper.parse_unresolved_notice_records(html, BSE_ARCHIVE_BETA_URL)

    assert exact == []
    assert len(unresolved) == 1
    assert unresolved[0].reason == "listing_does_not_expose_exact_date"


def test_detail_page_date_recovery() -> None:
    scraper = make_scraper()
    unresolved = scraper.parse_unresolved_notice_records(
        """
        <html><body><table id="ContentPlaceHolder1_GridView2">
        <tr><th>Notice No</th><th>Subject</th></tr>
        <tr><td>109521/2001</td><td><a href="/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=109521/2001">HAPPY NEW YEAR</a></td></tr>
        </table></body></html>
        """,
        BSE_ARCHIVE_BETA_URL,
        source_url="https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject=",
    )[0]
    detail_html = """
    <html><body>
    <td id="tc11">109521/2001</td>
    <td id="tc12">31 Dec 2001</td>
    <td id="tc31">HAPPY NEW YEAR</td>
    </body></html>
    """
    recovered = scraper.parse_notice_detail_page(detail_html, unresolved)

    assert recovered is not None
    assert recovered.date == "2001-12-31"
    assert recovered.circular_no == "109521/2001"


def test_merge_recovered_rows(tmp_path: Path) -> None:
    scraper = make_scraper()
    main_record = BSENoticeRecord(
        date="2026-03-13",
        subject="Archive",
        circular_no="20260313-1",
        link="https://www.bseindia.com/archive.pdf",
        source_url=BSE_ARCHIVE_BETA_URL,
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    add_record = BSENoticeRecord(
        date="2001-12-31",
        subject="HAPPY NEW YEAR",
        circular_no="109521/2001",
        link="https://beta.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx?page=109521/2001",
        source_url=BSE_ARCHIVE_BETA_URL,
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    main_path = tmp_path / "main.csv"
    add_path = tmp_path / "add.csv"
    out_path = tmp_path / "merged.csv"
    scraper.write_output([main_record], main_path)
    scraper.write_output([add_record], add_path)

    merged = scraper.merge_export(main_path=main_path, add_path=add_path, out_path=out_path)

    assert len(merged) == 2
    assert out_path.exists()


def test_validate_export_report(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = BSENoticeRecord(
        date="2026-03-13",
        subject="Daily Bulletin",
        circular_no="20260313-60",
        link="https://www.bseindia.com/downloads/UploadDocs/Notices/20260313-60/20260313-60.pdf",
        source_url="https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject=",
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    export_path = tmp_path / "bse.csv"
    scraper.write_output([record], export_path)

    report = scraper.validate_export(export_path)

    assert report["total_rows"] == 1
    assert report["duplicate_key_count"] == 0
    assert (tmp_path / "bse_notices_validation_report.json").exists()
    assert (tmp_path / "bse_notices_year_counts.csv").exists()


def test_file_lock_preflight(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    locked_path = tmp_path / "locked.csv"
    locked_path.write_text("", encoding="utf-8")

    def fake_open(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(RuntimeError, match="Output file is locked"):
        scraper.ensure_output_writable(locked_path, resume=True)


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = BSENoticeRecord(
        date="2026-03-13",
        subject="Archive",
        circular_no="20260313-1",
        link="https://www.bseindia.com/archive.pdf",
        source_url=BSE_ARCHIVE_BETA_URL,
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    csv_path = tmp_path / "bse.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)

    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1
