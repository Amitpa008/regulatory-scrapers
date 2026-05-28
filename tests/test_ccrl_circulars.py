import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scrapers.ccrl import CCRLCheckpoint, CCRLRecord, CCRLScraper


FIXTURE_DIR = Path("tests/fixtures/ccrl")
HTML_FIXTURE_PATH = FIXTURE_DIR / "circulars_fixture_sample.html"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> CCRLScraper:
    return CCRLScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_ccrl_html_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")
    assert len(records) == 4
    assert records[0].date == "2026-04-24"
    assert records[-1].date == "2018-08-01"


def test_parse_table_headers() -> None:
    html = load_text(HTML_FIXTURE_PATH)
    assert "Circular No." in html
    assert "Department" in html
    assert "Circulars" in html


def test_mapping_date_subject_circular_no_link() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")
    assert records[0].circular_no == "CCRL/RP/AP/2026-2027/001"
    assert records[0].department == "OPERATIONS"
    assert records[0].link == "https://www.ccrl.co.in/downloads/pdf/Communiques/Tariff%20eNWRs%20eNNWRs%20charges%20for%20Exchange%20Agri%20Segment.pdf"


def test_preserve_department_and_include_department_export(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")[:2]
    csv_path = tmp_path / "ccrl.csv"
    dept_csv_path = tmp_path / "ccrl_department.csv"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, dept_csv_path, include_department=True)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert dept_csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,department,subject,circular_no,link,source_url,scraped_at"


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_ccrl_link("downloads/communiques/file test.pdf", "https://www.ccrl.co.in/circulars.html")
        == "https://www.ccrl.co.in/downloads/communiques/file%20test.pdf"
    )


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")[:2]
    csv_path = tmp_path / "ccrl.csv"
    json_path = tmp_path / "ccrl.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe() -> None:
    scraper = make_scraper()
    a = CCRLRecord("2026-02-01", "Revision in eNNWRs charges", "CCRL/OPS/RP/GEN/2025-2026/034", "https://www.ccrl.co.in/a.pdf", "u", "s")
    b = CCRLRecord("2026-02-01", "Revision in eNNWRs charges", "CCRL/OPS/RP/GEN/2025-2026/034", "https://www.ccrl.co.in/a.pdf", "u", "s")
    assert scraper.record_dedup_key(a) == scraper.record_dedup_key(b)


def test_date_normalization_from_dd_mm_yyyy() -> None:
    scraper = make_scraper()
    assert scraper.parse_ccrl_date("01-08-2018") == "2018-08-01"


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = CCRLCheckpoint(
        source_url="https://www.ccrl.co.in/circulars.html",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-04-24",
        oldest_available_date="2018-08-01",
        total_records_detected=4,
        count_by_department={"OPERATIONS": 4},
        chunk_strategy="single_listing_page",
        last_completed_chunk=1,
        records_written=4,
        unique_records_written=4,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=True,
        errors=[],
    )
    path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(path, checkpoint)
    loaded = scraper.load_checkpoint(path)
    assert loaded.count_by_department["OPERATIONS"] == 4


def test_direct_single_page_traversal(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    html = load_text(HTML_FIXTURE_PATH)
    monkeypatch.setattr(scraper, "fetch_page_html", lambda url: html)
    output = tmp_path / "ccrl.csv"
    records = scraper.scrape_listing_url(
        url="https://www.ccrl.co.in/circulars.html",
        out_path=output,
        from_date=None,
        to_date=None,
        all_available=True,
        delay_seconds=0,
    )
    assert len(records) == 4
    assert output.exists()
    assert scraper.metadata_sidecar_path(output).exists()


def test_zero_row_table_handling() -> None:
    scraper = make_scraper()
    assert scraper.parse_circular_records("<html><body><table><tr><th>Date</th></tr></table></body></html>", "u") == []


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
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")
    export_path = tmp_path / "ccrl.csv"
    scraper.write_output(records, export_path)
    scraper.write_metadata_sidecar(records, export_path)
    report = scraper.validate_export(export_path)
    assert report["total_rows"] == 4
    assert report["rows_per_year"][2026] == 2
    assert (tmp_path / "ccrl_circulars_validation_report.json").exists()
    assert (tmp_path / "ccrl_circulars_year_counts.csv").exists()
    assert (tmp_path / "ccrl_circulars_department_counts.csv").exists()


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.ccrl.co.in/circulars.html")[0]
    csv_path = tmp_path / "ccrl.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)
    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1
