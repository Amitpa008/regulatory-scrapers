from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from scrapers.irdai import EXPECTED_OUTPUT_HEADERS, ENRICHED_OUTPUT_HEADERS, IRDAIRecord, IRDAIScraper


FIXTURE_PATH = Path("tests/fixtures/irdai/whats_new_fixture_sample.html")


def build_scraper() -> IRDAIScraper:
    return IRDAIScraper(config={"base_url": "https://irdai.gov.in"})


def test_parse_saved_irdai_raw_html_fixture() -> None:
    scraper = build_scraper()
    records = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")
    assert len(records) == 3
    assert records[0].date == "2026-05-18"
    assert records[0].subject == "Corrigendum - Digital Accessibility Audit of IRDAI websites"
    assert records[0].link == "https://irdai.gov.in/web/guest/document-detail?documentId=9501186"


def test_detect_filter_controls_year_month_archive_and_pagination() -> None:
    scraper = build_scraper()
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    controls = scraper.extract_filter_controls(__import__("bs4").BeautifulSoup(html, "html.parser"), html)
    assert controls["controls"]["form_id"] == "filterFormByirdai"
    assert controls["controls"]["year_select"] == "selectedYear"
    assert controls["controls"]["month_select"] == "selectedMonth"
    assert controls["archive_values"] == ["Archive Only", "Include Archives"]
    assert controls["pagination"]["page_size"] == 20
    assert controls["year_options"][0] >= "2026"
    assert controls["year_options"][-1] == "2000"


def test_mapping_date_title_link_and_blank_circular_no() -> None:
    scraper = build_scraper()
    record = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")[1]
    assert record.date == "2026-05-15"
    assert record.subject == "quotation for supply of 20-litre packaged drinking water jars for one year at irdai head office"
    assert record.circular_no == ""
    assert record.link.endswith("documentId=9501348")


def test_safe_circular_reference_extraction_only_when_visible() -> None:
    scraper = build_scraper()
    assert scraper.extract_reference_no(subject="Circular No. IRDAI/GEN/12/2026 test", subtitle="", link="") == "IRDAI/GEN/12/2026"
    assert scraper.extract_reference_no(subject="Minutes of 134th Meeting of Authority", subtitle="", link="") == ""


def test_optional_include_type_export(tmp_path: Path) -> None:
    scraper = build_scraper()
    record = IRDAIRecord(
        date="2026-05-18",
        subject="Test subject",
        circular_no="",
        link="https://irdai.gov.in/web/guest/document-detail?documentId=1",
        source_url="https://irdai.gov.in/web/guest/whats-new",
        scraped_at="2026-05-19T00:00:00+00:00",
        type="Circular",
    )
    out_path = tmp_path / "typed.csv"
    scraper.append_output([record], out_path, include_type=True)
    with open(out_path, newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        assert reader.fieldnames == ENRICHED_OUTPUT_HEADERS
        rows = list(reader)
    assert rows[0]["type"] == "Circular"


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = build_scraper()
    records = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")[:2]
    csv_path = tmp_path / "sample.csv"
    json_path = tmp_path / "sample.json"
    scraper.write_output(records, csv_path)
    scraper.write_output(records, json_path)
    with open(csv_path, newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        assert reader.fieldnames == EXPECTED_OUTPUT_HEADERS
        assert len(list(reader)) == 2
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload) == 2


def test_dedupe_key_uses_date_subject_and_link() -> None:
    scraper = build_scraper()
    record = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")[0]
    key = scraper.record_dedup_key(record)
    assert key == (
        "2026-05-18",
        "corrigendum - digital accessibility audit of irdai websites",
        "https://irdai.gov.in/web/guest/document-detail?documentId=9501186",
    )


def test_checkpoint_resume_and_csv_disagreement_preference(tmp_path: Path) -> None:
    scraper = build_scraper()
    records = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")
    out_path = tmp_path / "archive.csv"
    checkpoint_path = tmp_path / "archive.checkpoint.json"
    scraper.write_output(records[:1], out_path)
    scraper.write_metadata_sidecar(records[:1], out_path)
    checkpoint_payload = {
        "source_url": "https://irdai.gov.in/web/guest/whats-new",
        "output_path": str(out_path),
        "newest_available_date": "2026-05-18",
        "oldest_available_date": "2000-01-01",
        "years_discovered": ["2026", "2025", "2000"],
        "total_records_detected": 3,
        "count_by_year": {"2000": 1, "2026": 2},
        "count_by_type": {},
        "chunk_strategy": "test",
        "last_completed_chunk": 1,
        "records_written": 99,
        "unique_records_written": 99,
        "started_at": "2026-05-19T00:00:00+00:00",
        "updated_at": "2026-05-19T00:00:00+00:00",
        "completed": False,
        "errors": [],
    }
    checkpoint_path.write_text(json.dumps(checkpoint_payload), encoding="utf-8")
    calls = {"count": 0}

    def fake_collect(url: str, *, from_date=None, to_date=None) -> dict:
        calls["count"] += 1
        return {
            "records": records,
            "years_discovered": ["2026", "2000"],
            "chunks": [type("Chunk", (), {"index": 1, "year": "", "month": "", "archive_flag": "Include Archives"})()],
            "archive_behavior": "test",
            "limitation": "test",
        }

    scraper.collect_relevant_records = fake_collect  # type: ignore[method-assign]
    written = scraper.scrape_listing_url(
        url="https://irdai.gov.in/web/guest/whats-new",
        out_path=out_path,
        resume=True,
        checkpoint_path=checkpoint_path,
        delay_seconds=0,
    )
    assert calls["count"] == 1
    assert len(written) == 0


def test_zero_record_filter() -> None:
    scraper = build_scraper()
    records = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")
    filtered = scraper.filter_records(records, from_date=date(1999, 1, 1), to_date=date(1999, 12, 31), type_filter=None)
    assert filtered == []


def test_file_lock_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scraper = build_scraper()
    locked_path = tmp_path / "locked.csv"

    def fake_open(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(RuntimeError, match="Output file is locked"):
        scraper.ensure_output_writable(locked_path, resume=False)


def test_validation_and_type_counts(tmp_path: Path) -> None:
    scraper = build_scraper()
    records = scraper.parse_whats_new_records(FIXTURE_PATH.read_text(encoding="utf-8"), "https://irdai.gov.in/web/guest/whats-new")
    for item in records:
        item.type = "Whats New"
    out_path = tmp_path / "irdai_whats_new_archive.csv"
    scraper.write_output(records, out_path)
    scraper.write_metadata_sidecar(records, out_path)
    report = scraper.validate_export(out_path)
    assert report["total_rows"] == 3
    assert report["missing_circular_no"] == 3
    assert report["min_date"] == "2000-01-01"
    assert report["max_date"] == "2026-05-18"
    assert report["count_by_type"] == {"Whats New": 3}


def test_text_decoding_cleanup() -> None:
    scraper = build_scraper()
    item = {"dateId": "18-05-2026", "title": " - / eNWR \u2013 update", "subTitle": "eNWR \u2013 Update", "fileentryId": "1"}
    record = scraper.parse_whats_new_records(
        f'<script>var DLFileEntryArray = {json.dumps([item], ensure_ascii=False)};</script>',
        "https://irdai.gov.in/web/guest/whats-new",
    )[0]
    assert record.subject == "eNWR – Update"


def test_chunk_window_fresh_resume_near_end_completed() -> None:
    scraper = build_scraper()
    fresh = scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=0, max_chunks_this_run=50)
    assert fresh["resume_from_chunk"] == 1
    assert fresh["expected_end_chunk"] == 50
    assert fresh["chunks_this_run"] == 50
    assert fresh["completed"] is False

    resume = scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=100, max_chunks_this_run=50)
    assert resume["resume_from_chunk"] == 101
    assert resume["expected_end_chunk"] == 150
    assert resume["chunks_this_run"] == 50

    near_end = scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=300, max_chunks_this_run=50)
    assert near_end["resume_from_chunk"] == 301
    assert near_end["expected_end_chunk"] == 325
    assert near_end["chunks_this_run"] == 25

    completed = scraper.compute_chunk_window(total_chunks=325, previous_last_completed_chunk=325, max_chunks_this_run=50)
    assert completed["completed"] is True
    assert completed["chunks_this_run"] == 0
    assert completed["resume_from_chunk"] == 326
    assert completed["expected_end_chunk"] == 325


def test_checkpoint_cannot_move_backwards() -> None:
    scraper = build_scraper()
    with pytest.raises(RuntimeError, match="Checkpoint regression detected"):
        scraper.assert_non_regressing_checkpoint(previous_last_completed_chunk=100, new_last_completed_chunk=50)


def test_invalid_expected_end_chunk_raises_before_fetch(tmp_path: Path) -> None:
    scraper = build_scraper()
    called = {"count": 0}

    def fake_collect(url: str, *, from_date=None, to_date=None) -> dict:
        called["count"] += 1
        return {"records": [], "years_discovered": [], "chunks": [], "archive_behavior": "", "limitation": ""}

    scraper.collect_relevant_records = fake_collect  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="max_chunks_this_run must be positive"):
        scraper.scrape_listing_url(
            url="https://irdai.gov.in/web/guest/whats-new",
            out_path=tmp_path / "archive.csv",
            from_date=date(2026, 1, 1),
            to_date=date(2026, 5, 18),
            max_chunks_this_run=0,
            delay_seconds=0,
        )
    assert called["count"] == 0


def test_zero_new_work_run_does_not_overwrite_checkpoint(tmp_path: Path) -> None:
    scraper = build_scraper()
    out_path = tmp_path / "archive.csv"
    checkpoint_path = tmp_path / "archive.checkpoint.json"
    metadata_path = Path(f"{out_path}.meta.json")

    existing_record = IRDAIRecord(
        date="2026-05-18",
        subject="Existing",
        circular_no="",
        link="https://irdai.gov.in/web/guest/document-detail?documentId=1",
        source_url="https://irdai.gov.in/web/guest/whats-new",
        scraped_at="2026-05-19T00:00:00+00:00",
    )
    scraper.write_output([existing_record], out_path)
    scraper.write_metadata_sidecar([existing_record], out_path)

    checkpoint_payload = {
        "source_url": "https://irdai.gov.in/web/guest/whats-new",
        "output_path": str(out_path),
        "newest_available_date": "2026-05-18",
        "oldest_available_date": "2026-01-01",
        "years_discovered": ["2026"],
        "total_records_detected": 1,
        "count_by_year": {"2026": 1},
        "count_by_type": {},
        "chunk_strategy": "test",
        "last_completed_chunk": 1,
        "records_written": 1,
        "unique_records_written": 1,
        "started_at": "2026-05-19T00:00:00+00:00",
        "updated_at": "2026-05-19T00:00:00+00:00",
        "completed": True,
        "errors": [],
    }
    checkpoint_path.write_text(json.dumps(checkpoint_payload), encoding="utf-8")
    original_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    original_csv = out_path.read_text(encoding="utf-8")
    original_meta = metadata_path.read_text(encoding="utf-8")

    def fake_collect(url: str) -> dict:
        return {
            "records": [existing_record],
            "years_discovered": ["2026"],
            "chunks": [type("Chunk", (), {"index": 1, "year": "", "month": "", "archive_flag": "Include Archives"})()],
            "archive_behavior": "test",
            "limitation": "test",
        }

    scraper.collect_all_accessible_records = fake_collect  # type: ignore[method-assign]
    written = scraper.scrape_listing_url(
        url="https://irdai.gov.in/web/guest/whats-new",
        out_path=out_path,
        resume=True,
        checkpoint_path=checkpoint_path,
        all_available=True,
        max_chunks_this_run=50,
        delay_seconds=0,
    )
    assert written == []
    assert checkpoint_path.read_text(encoding="utf-8") == original_checkpoint
    assert out_path.read_text(encoding="utf-8") == original_csv
    assert metadata_path.read_text(encoding="utf-8") == original_meta
