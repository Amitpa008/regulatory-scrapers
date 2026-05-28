import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scrapers.nse_press_releases import (
    NSEPressReleaseCheckpoint,
    NSEPressReleaseRecord,
    NSEPressReleasesScraper,
)


FIXTURE_DIR = Path("tests/fixtures/nse")
API_FIXTURE_PATH = FIXTURE_DIR / "press_releases_api_sample.json"
ARCHIVE_FIXTURE_PATH = FIXTURE_DIR / "press_releases_archives_fixture.html"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> NSEPressReleasesScraper:
    return NSEPressReleasesScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_nse_press_release_api_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_press_release_records(
        load_json(API_FIXTURE_PATH),
        "https://www.nseindia.com/resources/exchange-communication-press-releases",
    )
    assert len(records) == 3
    assert records[0].date == "2026-05-18"
    assert records[0].category == "Corporate Communications"


def test_mapping_date_title_link_to_output_fields() -> None:
    scraper = make_scraper()
    records = scraper.parse_press_release_records(load_json(API_FIXTURE_PATH), "https://www.nseindia.com/resources/exchange-communication-press-releases")
    assert records[0].subject.startswith("National Stock Exchange and Higher Education Department")
    assert records[0].link == "https://nsearchives.nseindia.com//web/pressrelease/2026-05/PR_cc_18052026_0_20260518111000.pdf"
    assert records[0].circular_no == ""


def test_blank_circular_no_behavior() -> None:
    scraper = make_scraper()
    record = scraper.parse_press_release_records(load_json(API_FIXTURE_PATH), "u")[0]
    assert record.circular_no == ""


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert scraper.normalize_nse_link("/web/pressrelease/2026-05/file.pdf") == "https://www.nseindia.com/web/pressrelease/2026-05/file.pdf"


def test_archive_fixture_parsing() -> None:
    scraper = make_scraper()
    rows = scraper.parse_archive_index_rows(load_text(ARCHIVE_FIXTURE_PATH), "https://www.nseindia.com/resources/exchange-communication-press-releases-archives")
    assert len(rows) == 4
    assert rows[0].date == "2000-01-01"
    assert rows[-1].link.endswith("13082015.htm")


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_press_release_records(load_json(API_FIXTURE_PATH), "u")[:2]
    csv_path = tmp_path / "press.csv"
    json_path = tmp_path / "press.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe_by_date_subject_link() -> None:
    scraper = make_scraper()
    a = NSEPressReleaseRecord("2026-05-18", "Same title", "", "https://nsearchives.nseindia.com/a.pdf", "u", "s")
    b = NSEPressReleaseRecord("2026-05-18", "Same title", "", "https://nsearchives.nseindia.com/a.pdf", "u", "s")
    assert scraper.record_dedup_key(a) == scraper.record_dedup_key(b)


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = NSEPressReleaseCheckpoint(
        source_url="https://www.nseindia.com/resources/exchange-communication-press-releases",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-18",
        oldest_available_date="2011-03-24",
        total_records_detected=5809,
        chunk_strategy="yearly_date_windows",
        last_completed_chunk=2,
        records_written=123,
        unique_records_written=123,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(path, checkpoint)
    loaded = scraper.load_checkpoint(path)
    assert loaded.total_records_detected == 5809
    assert loaded.last_completed_chunk == 2


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = scraper.parse_press_release_records(load_json(API_FIXTURE_PATH), "u")[0]
    csv_path = tmp_path / "press.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)
    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1


def test_zero_record_chunk(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    monkeypatch.setattr(scraper, "get_api_range", lambda: (date(2026, 5, 18), date(2011, 3, 24), 0))
    monkeypatch.setattr(scraper, "fetch_chunk_payload_with_retry", lambda **kwargs: ([], False))
    output = tmp_path / "press.csv"
    records = scraper.scrape_listing_url(
        url="https://www.nseindia.com/resources/exchange-communication-press-releases",
        out_path=output,
        from_date=date(2026, 5, 18),
        to_date=date(2026, 5, 18),
        all_available=False,
        delay_seconds=0,
    )
    assert records == []


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
    records = scraper.parse_press_release_records(load_json(API_FIXTURE_PATH), "u")
    export_path = tmp_path / "press.csv"
    scraper.write_output(records, export_path)
    report = scraper.validate_export(export_path)

    assert report["total_rows"] == 3
    assert report["duplicate_key_count"] == 0
    assert (tmp_path / "nse_press_releases_validation_report.json").exists()
    assert (tmp_path / "nse_press_releases_year_counts.csv").exists()
