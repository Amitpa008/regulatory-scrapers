import json
from pathlib import Path

from scrapers.amfi import AMFIImportantUpdatesScraper, AMFIMFDCircularsScraper


IMPORTANT_FIXTURE = Path("tests/fixtures/amfi/important_updates_fixture_sample.html")
MFD_FIXTURE = Path("tests/fixtures/amfi/mfd_circulars_fixture_sample.html")


def load_important_fixture() -> str:
    return IMPORTANT_FIXTURE.read_text(encoding="utf-8")


def load_mfd_fixture() -> str:
    return MFD_FIXTURE.read_text(encoding="utf-8")


def make_important_scraper() -> AMFIImportantUpdatesScraper:
    return AMFIImportantUpdatesScraper(config={"source": "amfi-important-updates"}, rate_limit_seconds=0)


def make_mfd_scraper() -> AMFIMFDCircularsScraper:
    return AMFIMFDCircularsScraper(config={"source": "amfi-mfd-circulars"}, rate_limit_seconds=0)


def test_parse_important_updates_cards_from_embedded_json() -> None:
    scraper = make_important_scraper()
    records = scraper.parse_important_updates(load_important_fixture(), "https://www.amfiindia.com/important-updates")
    assert len(records) == 4
    assert records[0].subject == "AMFI Master Circular for Mutual Fund Distributors"
    assert records[0].date == "2026-01-14"
    assert records[1].circular_no == "ARN Circular no. 27"


def test_parse_mfd_circulars_table_payload() -> None:
    scraper = make_mfd_scraper()
    records = scraper.parse_mfd_circulars(load_mfd_fixture(), "https://www.amfiindia.com/distributor/amfi-circulars")
    assert len(records) == 3
    assert records[0].circular_no == "AMFI/MFD-CIR/32/2025-26"
    assert records[0].subject == "AMFI Master Circular for Mutual Fund Distributors"
    assert records[0].date == "2026-01-14"


def test_arn_and_amfi_reference_extraction() -> None:
    scraper = make_important_scraper()
    assert (
        scraper.extract_important_update_reference(
            "ARN Circular no. 27 - Discontinuation Biometric Process and Mandatory Online registration"
        )
        == "ARN Circular no. 27"
    )
    assert scraper.extract_important_update_reference("Service platform for investors to trace inactive folios") == ""


def test_absolute_url_conversion_and_link_types() -> None:
    scraper = make_mfd_scraper()
    assert (
        scraper.normalize_link("/Themes/Theme1/downloads/circulars/AMFICircular_ARN29_31Jul2025.pdf", "https://www.amfiindia.com/distributor/amfi-circulars")
        == "https://www.amfiindia.com/Themes/Theme1/downloads/circulars/AMFICircular_ARN29_31Jul2025.pdf"
    )
    assert scraper.detect_link_type("https://www.amfiindia.com/uploads/file.pdf") == "pdf"
    assert scraper.detect_link_type("https://external.example.com/file.pdf") == "external"
    assert scraper.detect_link_type("/risk-parameters") == "html/detail"


def test_text_cleanup_repairs_question_mark_quote_damage() -> None:
    scraper = make_mfd_scraper()
    assert (
        scraper.clean_text("Facility / Option to apply for ???Provisional??? ARN to non-individual entities")
        == 'Facility / Option to apply for "Provisional" ARN to non-individual entities'
    )


def test_blank_date_behavior_is_supported_for_important_updates_output() -> None:
    scraper = make_important_scraper()
    records = scraper.parse_important_updates(load_important_fixture(), "https://www.amfiindia.com/important-updates")
    records[0].date = ""
    assert scraper.output_row(records[0])["date"] == ""


def test_dedupe() -> None:
    scraper = make_mfd_scraper()
    records = scraper.parse_mfd_circulars(load_mfd_fixture(), "https://www.amfiindia.com/distributor/amfi-circulars")
    deduped = scraper.deduplicate_records(records + [records[0]])
    assert len(deduped) == len(records)


def test_json_and_csv_writing(tmp_path: Path) -> None:
    scraper = make_mfd_scraper()
    records = scraper.parse_mfd_circulars(load_mfd_fixture(), "https://www.amfiindia.com/distributor/amfi-circulars")
    csv_path = tmp_path / "amfi_mfd.csv"
    json_path = tmp_path / "amfi_mfd.json"
    scraper.write_output(records, csv_path)
    scraper.write_output(records, json_path)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 3


def test_validation(tmp_path: Path) -> None:
    scraper = make_mfd_scraper()
    records = scraper.parse_mfd_circulars(load_mfd_fixture(), "https://www.amfiindia.com/distributor/amfi-circulars")
    out_path = tmp_path / "amfi_mfd_circulars_archive.csv"
    scraper.write_output(records, out_path)
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["missing_circular_no_count"] == 0
    assert (tmp_path / "amfi_mfd_circulars_archive_validation_report.json").exists() or (
        tmp_path / "amfi_mfd_circulars_validation_report.json"
    ).exists()


def test_inspect_route_detects_next_payload_and_records() -> None:
    scraper = make_important_scraper()
    probe = scraper.inspect_route(
        url="https://www.amfiindia.com/important-updates",
        html=load_important_fixture(),
        status_code=200,
        final_url="https://www.amfiindia.com/important-updates",
    )
    assert probe["direct_http_worked"] is True
    assert probe["react_or_next_rendered"] is True
    assert probe["records_present_in_raw_html"] is True
    assert probe["link_type_counts"]["pdf"] >= 1


def test_extract_embedded_dataset_supports_plain_script_json() -> None:
    scraper = make_important_scraper()
    records = scraper.parse_records(load_important_fixture(), "https://www.amfiindia.com/important-updates")
    assert any(record.link.endswith(".pdf") for record in records)
