import csv
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from scrapers.mcx import MCXCheckpoint, MCXCircularRecord, MCXScraper


FIXTURE_DIR = Path("tests/fixtures/mcx")
HTML_FIXTURE_PATH = FIXTURE_DIR / "all_circulars.html"
API_FIXTURE_PATH = FIXTURE_DIR / "circulars_api_sample.json"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> MCXScraper:
    return MCXScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_mcx_html_fixture() -> None:
    soup_text = load_text(HTML_FIXTURE_PATH)
    assert "All Circulars" in soup_text
    assert "GetCircularAdvanceSearch" in soup_text
    assert "ddlCircularTypes" in soup_text


def test_parse_saved_mcx_api_fixture_mapping() -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))

    records = scraper.parse_circular_records(payload, "https://www.mcxindia.com/circulars/all-circulars")

    assert len(records) == 3
    assert records[0].date == "2026-05-15"
    assert records[0].subject == "Empanelment as Algo Provider - M/s Bull8.Ai Solutions Pvt. Ltd."
    assert records[0].circular_no == "291"
    assert records[0].category == "CTCL"


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_mcx_link("/docs/default-source/circulars/example.pdf", "https://www.mcxindia.com/circulars/all-circulars")
        == "https://www.mcxindia.com/docs/default-source/circulars/example.pdf"
    )


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    payload = json.loads(API_FIXTURE_PATH.read_text(encoding="utf-8"))
    records = scraper.parse_circular_records(payload, "https://www.mcxindia.com/circulars/all-circulars")

    csv_path = tmp_path / "mcx.csv"
    json_path = tmp_path / "mcx.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 3


def test_deduplication_and_missing_circular_number_fallback() -> None:
    scraper = make_scraper()
    record = MCXCircularRecord(
        date="2026-05-15",
        subject="Same subject",
        circular_no="",
        link="https://www.mcxindia.com/example.pdf",
        source_url="https://www.mcxindia.com/circulars/all-circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )

    assert scraper.record_dedup_key(record) == ("2026-05-15", "same subject", "https://www.mcxindia.com/example.pdf")


def test_date_normalization_from_display_date() -> None:
    scraper = make_scraper()
    payload = {
        "d": [
            {
                "DisplayTitle": "Test circular",
                "CircularNo": "1",
                "DisplayCircularDate": "18 Nov 2003",
                "Documents": "https://www.mcxindia.com/docs/default-source/circulars/example.pdf",
                "CircularTypesName": "Others",
            }
        ]
    }

    records = scraper.parse_circular_records(payload, "https://www.mcxindia.com/circulars/all-circulars")
    assert records[0].date == "2003-11-18"


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = MCXCheckpoint(
        source_url="https://www.mcxindia.com/circulars/all-circulars",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-15",
        oldest_available_date="2003-11-18",
        total_records_detected=3,
        chunk_strategy="yearly_date_chunks",
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


def test_load_existing_output_records_for_resume(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = MCXCircularRecord(
        date="2026-05-15",
        subject="Mock Trading",
        circular_no="288",
        link="https://www.mcxindia.com/docs/default-source/circulars/english/2026/may/circular-288-2026.pdf",
        source_url="https://www.mcxindia.com/circulars/all-circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    csv_path = tmp_path / "resume.csv"
    scraper.write_output([record], csv_path)

    loaded = scraper.load_existing_output_records(csv_path)

    assert len(loaded) == 1
    assert loaded[0].subject == "Mock Trading"


def test_chunking_by_year() -> None:
    scraper = make_scraper()
    chunks = scraper.build_chunks(date(2024, 11, 1), date(2026, 5, 16))

    assert chunks[0].from_date == date(2024, 11, 1)
    assert chunks[0].to_date == date(2024, 12, 31)
    assert chunks[-1].to_date == date(2026, 5, 16)


def test_transient_retry_handling(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.mcx.time.sleep", lambda _: None)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url, headers=None):
            return httpx.Response(200, request=httpx.Request("GET", url), text="ok")

        def post(self, url, headers=None, content=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.RemoteProtocolError("disconnect")
            return httpx.Response(200, request=httpx.Request("POST", url), json={"d": []})

    monkeypatch.setattr("scrapers.mcx.httpx.Client", lambda *args, **kwargs: FakeClient())

    response = scraper.post_json("https://www.mcxindia.com/backpage.aspx/GetCircularAdvanceSearch", {})

    assert response.status_code == 200
    assert attempts["count"] == 2


def test_zero_record_chunk_handling() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records({"d": []}, "https://www.mcxindia.com/circulars/all-circulars")
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
    record = MCXCircularRecord(
        date="2026-05-15",
        subject="Mock Trading",
        circular_no="288",
        link="https://www.mcxindia.com/docs/default-source/circulars/english/2026/may/circular-288-2026.pdf",
        source_url="https://www.mcxindia.com/circulars/all-circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    export_path = tmp_path / "mcx.csv"
    scraper.write_output([record], export_path)

    report = scraper.validate_export(export_path)

    assert report["total_rows"] == 1
    assert report["duplicate_key_count"] == 0
    assert (tmp_path / "mcx_circulars_validation_report.json").exists()
    assert (tmp_path / "mcx_circulars_year_counts.csv").exists()


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = MCXCircularRecord(
        date="2026-05-15",
        subject="Mock Trading",
        circular_no="288",
        link="https://www.mcxindia.com/docs/default-source/circulars/english/2026/may/circular-288-2026.pdf",
        source_url="https://www.mcxindia.com/circulars/all-circulars",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    csv_path = tmp_path / "mcx.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)

    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1
