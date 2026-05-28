import csv
import json
from pathlib import Path

import pytest

from scrapers.ifsca import IFSCACheckpoint, IFSCARecord, IFSCAScraper


FIXTURE_DIR = Path("tests/fixtures/ifsca")
HTML_FIXTURE_PATH = FIXTURE_DIR / "new_section_fixture_sample.html"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> IFSCAScraper:
    return IFSCAScraper(config={}, rate_limit_seconds=0)


def test_parse_saved_ifsca_html_fixture() -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")
    assert len(records) == 8
    assert records[0].date == "2026-05-18"
    assert records[0].type == "Informal Guidance"


def test_mapping_date_type_title_link() -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")
    tender = records[1]
    assert tender.subject.startswith("Notification on Vacant Space")
    assert tender.link == "https://www.ifsca.gov.in/Tender/Index?MId=Gsd%20eWf40iU="
    assert tender.circular_no == "IFSCA-Admn0IHBP/9/2025-GA"


def test_preserve_type_and_filtering() -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")
    filtered = scraper.filter_records(records, from_date=None, to_date=None, type_filter="Circular")
    assert len(filtered) == 4
    assert all(record.type == "Circular" for record in filtered)


def test_blank_circular_number_behavior_for_circulars() -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")
    subject_map = {record.subject: record for record in records}
    assert subject_map["Implementation services by Investment Advisers in the IFSC"].circular_no == ""
    assert subject_map["Master Circular for Broker Dealers and Clearing Members"].circular_no == ""


def test_absolute_url_conversion() -> None:
    scraper = make_scraper()
    assert (
        scraper.normalize_ifsca_link("/Legal/Index?MId=test value", "https://www.ifsca.gov.in/home/NewSection")
        == "https://www.ifsca.gov.in/Legal/Index?MId=test%20value"
    )


def test_csv_and_json_writing_and_include_type(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")[:2]
    csv_path = tmp_path / "ifsca.csv"
    json_path = tmp_path / "ifsca.json"
    typed_csv_path = tmp_path / "ifsca_typed.csv"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)
    scraper.append_output(records, typed_csv_path, include_type=True)

    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert typed_csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,type,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedup_uses_type() -> None:
    scraper = make_scraper()
    a = IFSCARecord("2026-05-12", "Same title", "", "https://www.ifsca.gov.in/x", "u", "s", type="Circular")
    b = IFSCARecord("2026-05-12", "Same title", "", "https://www.ifsca.gov.in/x", "u", "s", type="Press Release")
    assert scraper.record_dedup_key(a) != scraper.record_dedup_key(b)


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = IFSCACheckpoint(
        source_url="https://www.ifsca.gov.in/home/NewSection",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-18",
        oldest_available_date="1934-03-06",
        total_records_detected=1285,
        count_by_type={"Circular": 384, "Act": 15},
        chunk_strategy="single_listing_page",
        last_completed_chunk=1,
        records_written=10,
        unique_records_written=10,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=True,
        errors=[],
    )
    path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(path, checkpoint)
    loaded = scraper.load_checkpoint(path)
    assert loaded.total_records_detected == 1285
    assert loaded.count_by_type["Circular"] == 384


def test_direct_single_page_traversal(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    html = load_text(HTML_FIXTURE_PATH)
    monkeypatch.setattr(scraper, "fetch_page_html", lambda url: html)
    output = tmp_path / "ifsca.csv"
    records = scraper.scrape_listing_url(
        url="https://www.ifsca.gov.in/home/NewSection",
        out_path=output,
        from_date=None,
        to_date=None,
        all_available=True,
        delay_seconds=0,
    )
    assert len(records) == 8
    assert output.exists()
    assert scraper.metadata_sidecar_path(output).exists()


def test_retry_handling_direct_page(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    html = load_text(HTML_FIXTURE_PATH)

    def fake_get(url: str, **kwargs):
        del url, kwargs
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        class FakeResponse:
            text = html
        return FakeResponse()

    monkeypatch.setattr(scraper, "get", fake_get)
    with pytest.raises(RuntimeError):
        scraper.fetch_page_html("https://www.ifsca.gov.in/home/NewSection")


def test_zero_record_filter(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")
    filtered = scraper.filter_records(records, from_date=None, to_date=None, type_filter="Guidelines")
    assert filtered == []


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
    records = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")[:3]
    export_path = tmp_path / "ifsca.csv"
    scraper.write_output(records, export_path)
    scraper.write_metadata_sidecar(records, export_path)
    report = scraper.validate_export(export_path)

    assert report["total_rows"] == 3
    assert report["rows_per_year"][2026] == 3
    assert (tmp_path / "ifsca_new_section_validation_report.json").exists()
    assert (tmp_path / "ifsca_new_section_year_counts.csv").exists()
    assert (tmp_path / "ifsca_new_section_type_counts.csv").exists()


def test_append_mode_does_not_duplicate_header(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = scraper.parse_new_section_records(load_text(HTML_FIXTURE_PATH), "https://www.ifsca.gov.in/home/NewSection")[0]
    csv_path = tmp_path / "ifsca.csv"
    scraper.append_output([record], csv_path)
    scraper.append_output([record], csv_path)

    rows = list(csv.reader(csv_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1
