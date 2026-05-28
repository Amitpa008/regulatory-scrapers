from __future__ import annotations

import json
from pathlib import Path

import pytest

from scrapers.incometax import (
    EXPECTED_OUTPUT_HEADERS,
    IncomeTaxCircularsScraper,
    IncomeTaxNotificationsScraper,
)


@pytest.fixture()
def circulars_scraper() -> IncomeTaxCircularsScraper:
    return IncomeTaxCircularsScraper(config={"source": "incometax-circulars"})


@pytest.fixture()
def notifications_scraper() -> IncomeTaxNotificationsScraper:
    return IncomeTaxNotificationsScraper(config={"source": "incometax-notifications"})


def test_extract_scope_group_and_structure(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    html = Path("tests/fixtures/incometax/circulars_page_fixture_sample.html").read_text(encoding="utf-8")
    assert circulars_scraper.extract_scope_group_id(html) == 20117
    structure_id, structure_key = circulars_scraper.extract_structure_descriptor(html)
    assert structure_id == 36050
    assert structure_key == "CIRCULAR_KEY"


def test_parse_circular_api_record(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    payload = json.loads(Path("tests/fixtures/incometax/circulars_api_sample.json").read_text(encoding="utf-8"))
    record = circulars_scraper.parse_structured_content_item(payload["items"][0], source_url="https://www.incometaxindia.gov.in/circulars")
    assert record is not None
    assert record.date == "2026-03-31"
    assert record.subject.startswith("Circular No. 4/2026")
    assert record.circular_no == "Circular No. 4/2026"
    assert record.link == "https://www.incometaxindia.gov.in/documents/d/guest/circular-4-2026-pdf"


def test_parse_notification_api_record(notifications_scraper: IncomeTaxNotificationsScraper) -> None:
    payload = json.loads(Path("tests/fixtures/incometax/notifications_api_sample.json").read_text(encoding="utf-8"))
    record = notifications_scraper.parse_structured_content_item(payload["items"][0], source_url="https://www.incometaxindia.gov.in/notifications")
    assert record is not None
    assert record.date == "2026-05-14"
    assert record.circular_no.startswith("Notification No. 6/2026")
    assert record.link.endswith("/documents/d/guest/notification-6-2026-pdf")


def test_parse_html_detail_fallback_link(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    payload = json.loads(Path("tests/fixtures/incometax/circulars_api_sample.json").read_text(encoding="utf-8"))
    record = circulars_scraper.parse_structured_content_item(payload["items"][1], source_url="https://www.incometaxindia.gov.in/circulars")
    assert record is not None
    assert record.date == "2013-12-16"
    assert record.link == "https://www.incometaxindia.gov.in/w/circular-10/dv/2013-section-40-a-ia-clarification"


def test_reference_extraction_safe(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    assert circulars_scraper.extract_reference_no_from_text("Circular No. 3/2026 : Example title") == "3/2026"
    assert circulars_scraper.extract_reference_no_from_text("General update without a number") == ""


def test_detect_link_types(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    assert circulars_scraper.detect_link_type("/documents/d/guest/file-pdf") == "pdf"
    assert circulars_scraper.detect_link_type("https://www.incometaxindia.gov.in/w/example") == "html/detail"
    assert circulars_scraper.detect_link_type("https://www.incometaxindia.gov.in/file.docx") == "doc/docx"
    assert circulars_scraper.detect_link_type("https://www.incometaxindia.gov.in/file.xlsx") == "xls/xlsx"
    assert circulars_scraper.detect_link_type("https://www.incometaxindia.gov.in/file.zip") == "zip"


def test_write_csv_and_json_outputs(circulars_scraper: IncomeTaxCircularsScraper, tmp_path: Path) -> None:
    payload = json.loads(Path("tests/fixtures/incometax/circulars_api_sample.json").read_text(encoding="utf-8"))
    records = [
        circulars_scraper.parse_structured_content_item(item, source_url="https://www.incometaxindia.gov.in/circulars")
        for item in payload["items"]
    ]
    records = [record for record in records if record]
    csv_path = tmp_path / "out.csv"
    json_path = tmp_path / "out.json"
    circulars_scraper.write_output(records, csv_path)
    circulars_scraper.write_output(records, json_path)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == ",".join(EXPECTED_OUTPUT_HEADERS)
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe_key_with_and_without_reference(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    payload = json.loads(Path("tests/fixtures/incometax/circulars_api_sample.json").read_text(encoding="utf-8"))
    first = circulars_scraper.parse_structured_content_item(payload["items"][0], source_url="x")
    second = circulars_scraper.parse_structured_content_item(payload["items"][1], source_url="x")
    assert first is not None and second is not None
    assert circulars_scraper.record_dedup_key(first)[2] == "Circular No. 4/2026"
    assert circulars_scraper.record_dedup_key(second)[2] == "Circular 10/DV/2013"


def test_zero_record_validation_headers(circulars_scraper: IncomeTaxCircularsScraper, tmp_path: Path) -> None:
    out = tmp_path / "empty.csv"
    circulars_scraper.write_output([], out)
    report = circulars_scraper.validate_export(out)
    assert report["total_rows"] == 0
    assert report["headers"] == EXPECTED_OUTPUT_HEADERS


def test_inspect_page_html_detects_shell(circulars_scraper: IncomeTaxCircularsScraper) -> None:
    blocked_html = "<html><head><title>Access Denied</title></head><body>Access Denied</body></html>"
    probe = circulars_scraper.inspect_page_html(url="https://www.incometaxindia.gov.in/circulars", html=blocked_html, status_code=403, final_url="https://www.incometaxindia.gov.in/circulars")
    assert probe.shell_only is True
    assert probe.direct_http_worked is True
