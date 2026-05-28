from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from scrapers.nse import NSECircularCheckpoint, NSECircularRecord, NSEChunk, NSEScraper


FIXTURE_DIR = Path("tests/fixtures/nse")
API_FIXTURE_PATH = FIXTURE_DIR / "circulars_api_sample.json"


def load_api_fixture() -> dict:
    return json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))


def make_scraper() -> NSEScraper:
    return NSEScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_nse_api_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(
        load_api_fixture(),
        "https://www.nseindia.com/resources/exchange-communication-circulars",
    )

    assert len(records) == 3
    assert records[0].date == "2026-05-16"
    assert records[0].subject == "Liquidity Enhancement Scheme - Silver Options on Goods"
    assert records[0].circular_no == "NSE/COM/74234"


def test_field_mapping_and_absolute_url_conversion() -> None:
    scraper = make_scraper()
    record = scraper.parse_circular_records(
        load_api_fixture(),
        "https://www.nseindia.com/resources/exchange-communication-circulars",
    )[1]
    assert record.link == "https://www.nseindia.com/content/circulars/MSD74261.zip"
    assert record.department == "Member Service Department"


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_api_fixture(), "https://www.nseindia.com/resources/exchange-communication-circulars")

    csv_path = tmp_path / "nse.csv"
    json_path = tmp_path / "nse.json"
    scraper.write_output(records, csv_path)
    scraper.write_output(records, json_path)

    csv_lines = csv_path.read_text(encoding="utf-8").splitlines()
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert csv_lines[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json_payload) == 3


def test_deduplication_and_missing_circular_number_fallback() -> None:
    scraper = make_scraper()
    record_a = NSECircularRecord(
        date="2026-05-16",
        subject="Test Subject",
        circular_no="",
        link="https://example.com/a.pdf",
        source_url="https://www.nseindia.com/resources/exchange-communication-circulars",
        scraped_at="2026-05-17T00:00:00+00:00",
    )
    record_b = NSECircularRecord(**record_a.__dict__)
    assert scraper.record_dedup_key(record_a) == scraper.record_dedup_key(record_b)


def test_date_normalization() -> None:
    scraper = make_scraper()
    assert scraper.normalize_nse_date("20260516") == "2026-05-16"
    assert scraper.normalize_nse_date("May 16, 2026") == "2026-05-16"


def test_checkpoint_and_resume_state(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = NSECircularCheckpoint(
        source_url="https://www.nseindia.com/resources/exchange-communication-circulars",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-16",
        oldest_available_date="1994-05-06",
        total_records_detected=3,
        chunk_strategy="yearly_date_windows",
        last_completed_chunk=2,
        records_written=3,
        unique_records_written=3,
        started_at="2026-05-17T00:00:00+00:00",
        updated_at="2026-05-17T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(checkpoint_path, checkpoint)
    loaded = scraper.load_checkpoint(checkpoint_path)
    assert loaded.last_completed_chunk == 2


def test_chunking_by_date_range() -> None:
    scraper = make_scraper()
    chunks = scraper.build_chunks(date(2024, 1, 1), date(2026, 5, 16))
    assert len(chunks) >= 3
    assert chunks[0].from_date == date(2024, 1, 1)
    assert chunks[-1].to_date == date(2026, 5, 16)


def test_transient_retry_handling(monkeypatch) -> None:
    scraper = make_scraper()
    payload = load_api_fixture()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.nse.time.sleep", lambda _: None)

    def fake_fetch(*, from_date=None, to_date=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("disconnect")
        return payload

    monkeypatch.setattr(scraper, "fetch_circulars_payload", fake_fetch)
    chunk_payload, retried = scraper.fetch_chunk_payload_with_retry(
        chunk=NSEChunk(index=1, from_date=date(2026, 5, 8), to_date=date(2026, 5, 16)),
        retries=5,
        retry_base_delay=3,
        retry_max_delay=60,
    )
    assert retried is True
    assert chunk_payload["data"][0]["circNumber"] == "74234"


def test_zero_record_chunk_handling() -> None:
    scraper = make_scraper()
    assert scraper.parse_circular_records({"data": [], "fromDate": "01-01-2026", "toDate": "01-01-2026"}, "x") == []


def test_oldest_date_discovery_with_mocked_fixtures(monkeypatch) -> None:
    scraper = make_scraper()
    calls: list[tuple[date | None, date | None]] = []

    def fake_fetch(*, from_date=None, to_date=None):
        calls.append((from_date, to_date))
        if from_date is None:
            return {
                "data": [
                    {
                        "cirDate": "20260516",
                        "sub": "Newest",
                        "circDisplayNo": "NSE/A/1",
                        "circFilelink": "https://example.com/newest.pdf",
                    }
                ]
            }
        if from_date <= date(1994, 1, 1):
            return {
                "data": [
                    {
                        "cirDate": "19940506",
                        "sub": "Oldest",
                        "circDisplayNo": "NSE/OLD/1",
                        "circFilelink": "https://example.com/oldest.pdf",
                    },
                    {
                        "cirDate": "20260516",
                        "sub": "Newest",
                        "circDisplayNo": "NSE/A/1",
                        "circFilelink": "https://example.com/newest.pdf",
                    },
                ]
            }
        return {
            "data": [
                {
                    "cirDate": from_date.strftime("%Y%m%d"),
                    "sub": "Window oldest",
                    "circDisplayNo": "NSE/W/1",
                    "circFilelink": "https://example.com/window.pdf",
                },
                {
                    "cirDate": "20260516",
                    "sub": "Newest",
                    "circDisplayNo": "NSE/A/1",
                    "circFilelink": "https://example.com/newest.pdf",
                },
            ]
        }

    monkeypatch.setattr(scraper, "fetch_circulars_payload", fake_fetch)
    newest, oldest = scraper.get_available_range()
    assert newest == date(2026, 5, 16)
    assert oldest == date(1994, 5, 6)
    assert calls


@pytest.mark.parametrize("status_code", [429, 503])
def test_retryable_http_status_for_nse(monkeypatch, status_code: int) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.nse.time.sleep", lambda _: None)

    def fake_fetch(*, from_date=None, to_date=None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            request = httpx.Request("GET", "https://www.nseindia.com/api/circulars")
            response = httpx.Response(status_code=status_code, request=request)
            raise httpx.HTTPStatusError(f"{status_code}", request=request, response=response)
        return load_api_fixture()

    monkeypatch.setattr(scraper, "fetch_circulars_payload", fake_fetch)
    payload, retried = scraper.fetch_chunk_payload_with_retry(
        chunk=NSEChunk(index=1, from_date=date(2026, 5, 8), to_date=date(2026, 5, 16)),
        retries=5,
        retry_base_delay=3,
        retry_max_delay=60,
    )
    assert retried is True
    assert payload["data"]


def test_validate_export_report(tmp_path: Path) -> None:
    scraper = make_scraper()
    export_path = tmp_path / "nse_export.csv"
    with open(export_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(["date", "subject", "circular_no", "link", "source_url", "scraped_at"])
        writer.writerow(["2026-05-16", "Liquidity Enhancement Scheme", "NSE/COM/74234", "https://nsearchives.nseindia.com/content/circulars/COM74234.pdf", "https://www.nseindia.com/resources/exchange-communication-circulars", "2026-05-17T00:00:00+00:00"])
        writer.writerow(["2026-05-16", "Liquidity Enhancement Scheme", "NSE/COM/74234", "https://nsearchives.nseindia.com/content/circulars/COM74234.pdf", "https://www.nseindia.com/resources/exchange-communication-circulars", "2026-05-17T00:00:01+00:00"])
        writer.writerow(["2026-05-15", "Abc", "", "https://example.com/odd", "https://www.nseindia.com/resources/exchange-communication-circulars", "2026-05-17T00:00:02+00:00"])

    report = scraper.validate_export(export_path)

    assert report["headers_exact"] is True
    assert report["total_rows"] == 3
    assert report["duplicate_key_count"] == 1
    assert report["missing_fields"]["circular_no"] == 1
    assert report["link_counts"]["pdf"] == 2
    assert report["link_counts"]["other"] == 1
    assert report["suspicious_row_count"] >= 2
    assert (tmp_path / "nse_circulars_validation_report.json").exists()
    assert (tmp_path / "nse_circulars_year_counts.csv").exists()
