import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scrapers.nerl import NERLCheckpoint, NERLRecord, NERLScraper


FIXTURE_DIR = Path("tests/fixtures/nerl")
HTML_FIXTURE_PATH = FIXTURE_DIR / "circulars_fixture_sample.html"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> NERLScraper:
    return NERLScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_nerl_html_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")
    assert len(records) == 4
    assert records[0].date == "2026-01-20"
    assert records[-1].date == "2018-04-17"


def test_parse_table_headers() -> None:
    html = load_text(HTML_FIXTURE_PATH)
    assert "Date" in html
    assert "Department" in html
    assert "English (PDF)" in html


def test_mapping_date_title_department_link() -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")
    assert records[0].subject.startswith("Technical maintenance of E-Repository")
    assert records[0].department == "Technology"
    assert records[0].link == "https://www.nerlindia.com/wp-content/uploads/2026/01/20260127-Circular-NERL.pdf"


def test_blank_circular_no_behavior() -> None:
    scraper = make_scraper()
    record = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")[0]
    assert record.circular_no == ""


def test_preserve_department_and_include_department_export(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")[:2]
    csv_path = tmp_path / "nerl.csv"
    dept_csv_path = tmp_path / "nerl_department.csv"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, dept_csv_path, include_department=True)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert dept_csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,department,subject,circular_no,link,source_url,scraped_at"


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_nerl_link("/wp-content/uploads/2026/01/file test.pdf", "https://www.nerlindia.com/circulars/")
        == "https://www.nerlindia.com/wp-content/uploads/2026/01/file%20test.pdf"
    )


def test_image_icon_anchor_link_extraction() -> None:
    scraper = make_scraper()
    html = load_text(HTML_FIXTURE_PATH)
    records = scraper.parse_circular_records(html, "https://www.nerlindia.com/circulars/")
    assert records[0].link.endswith(".pdf")


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")[:2]
    csv_path = tmp_path / "nerl.csv"
    json_path = tmp_path / "nerl.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe() -> None:
    scraper = make_scraper()
    a = NERLRecord("2026-01-20", "Same title", "", "https://www.nerlindia.com/a.pdf", "u", "s")
    b = NERLRecord("2026-01-20", "Same title", "", "https://www.nerlindia.com/a.pdf", "u", "s")
    assert scraper.record_dedup_key(a) == scraper.record_dedup_key(b)


def test_date_normalization_from_dd_mm_yyyy() -> None:
    scraper = make_scraper()
    assert scraper.parse_nerl_date("17-04-2018") == "2018-04-17"


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = NERLCheckpoint(
        source_url="https://www.nerlindia.com/circulars/",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-01-20",
        oldest_available_date="2018-04-17",
        total_records_detected=4,
        count_by_department={"Technology": 2},
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
    assert loaded.count_by_department["Technology"] == 2


def test_direct_single_page_traversal(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    html = load_text(HTML_FIXTURE_PATH)
    monkeypatch.setattr(scraper, "fetch_page_html", lambda url: html)
    output = tmp_path / "nerl.csv"
    records = scraper.scrape_listing_url(
        url="https://www.nerlindia.com/circulars/",
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
    records = scraper.parse_circular_records(load_text(HTML_FIXTURE_PATH), "https://www.nerlindia.com/circulars/")
    export_path = tmp_path / "nerl.csv"
    scraper.write_output(records, export_path)
    scraper.write_metadata_sidecar(records, export_path)
    report = scraper.validate_export(export_path)
    assert report["total_rows"] == 4
    assert report["rows_per_year"][2026] == 3
    assert (tmp_path / "nerl_circulars_validation_report.json").exists()
    assert (tmp_path / "nerl_circulars_year_counts.csv").exists()
    assert (tmp_path / "nerl_circulars_department_counts.csv").exists()


def test_text_decoding_cleanup() -> None:
    scraper = make_scraper()
    assert scraper.clean_subject("Tariff â€“ eNWR / eNNWR") == "Tariff - eNWR / eNNWR"
