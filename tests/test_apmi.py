import csv
import json
from pathlib import Path

from scrapers.apmi import (
    APMICircularsScraper,
    APMIComplianceSutraScraper,
    APMIDocumentsScraper,
    APMISEBIResourcesScraper,
)


FIXTURE_PATH = Path("tests/fixtures/apmi/welcome_fixture_sample.html")


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def make_documents_scraper() -> APMIDocumentsScraper:
    return APMIDocumentsScraper(config={"source": "apmi-documents"}, rate_limit_seconds=0)


def make_circulars_scraper() -> APMICircularsScraper:
    return APMICircularsScraper(config={"source": "apmi-circulars"}, rate_limit_seconds=0)


def make_sebi_scraper() -> APMISEBIResourcesScraper:
    return APMISEBIResourcesScraper(config={"source": "apmi-sebi-resources"}, rate_limit_seconds=0)


def make_compliance_scraper() -> APMIComplianceSutraScraper:
    return APMIComplianceSutraScraper(config={"source": "apmi-compliance-sutra"}, rate_limit_seconds=0)


def test_parse_static_html_fixture_and_preserve_section_path() -> None:
    scraper = make_documents_scraper()
    records = scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm")
    assert len(records) == 12
    assert records[0].section_path == "CIRCULARS > SEBI Circulars > PMS Regulation + Master Circular + PMS Manual"
    assert records[0].category == "PMS Regulation + Master Circular + PMS Manual"


def test_identify_top_level_categories_and_link_types() -> None:
    scraper = make_documents_scraper()
    records = scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm")
    probe = scraper.inspect_html(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm", records)
    assert "CIRCULARS" in probe["top_level_categories"]
    assert "IMPORTANT" in probe["top_level_categories"]
    assert probe["link_type_counts"]["pdf"] >= 1
    assert probe["link_type_counts"]["youtube/external"] == 1


def test_absolute_url_conversion() -> None:
    scraper = make_documents_scraper()
    assert (
        scraper.normalize_link("/storagebox/images/Circulars/test.pdf", "https://www.apmiindia.org/apmi/welcome.htm")
        == "https://www.apmiindia.org/storagebox/images/Circulars/test.pdf"
    )


def test_date_parsing_examples_and_no_fy_guessing() -> None:
    scraper = make_documents_scraper()
    assert scraper.parse_title_date("23rd March'23") == "2023-03-23"
    assert scraper.parse_title_date("24th Apr'23") == "2023-04-24"
    assert scraper.parse_title_date("31st July'23") == "2023-07-31"
    assert scraper.parse_title_date("18th February'26") == "2026-02-18"
    assert scraper.parse_title_date("March'26") == ""
    assert scraper.parse_title_date("FY 2025-2026") == ""


def test_apmi_circular_number_extraction() -> None:
    scraper = make_documents_scraper()
    assert scraper.extract_circular_no("APMI Circular 10 - 18th February'26") == "APMI Circular 10"
    assert scraper.extract_circular_no("APMI Circular Number 11 - 11th March'26") == "APMI Circular Number 11"
    assert scraper.extract_circular_no("SEBI Circular dated 2nd May'24") == ""


def test_sebi_resource_filtering() -> None:
    scraper = make_sebi_scraper()
    filtered = scraper.filter_for_source(scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm"))
    assert len(filtered) == 5
    assert all(record.section_path.startswith("CIRCULARS >") for record in filtered)


def test_apmi_circular_filtering() -> None:
    scraper = make_circulars_scraper()
    filtered = scraper.filter_for_source(scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm"))
    assert len(filtered) == 3
    assert any(record.circular_no == "APMI Circular 10" for record in filtered)


def test_compliance_sutra_filtering() -> None:
    scraper = make_compliance_scraper()
    filtered = scraper.filter_for_source(scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm"))
    assert len(filtered) == 2
    assert all("Compliance Sutra" in record.section_path for record in filtered)


def test_csv_and_json_writing() -> None:
    scraper = make_documents_scraper()
    records = scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm")[:2]
    out_dir = Path("tests/fixtures/apmi")
    csv_path = out_dir / "tmp_apmi_inventory.csv"
    json_path = out_dir / "tmp_apmi_inventory.json"
    try:
        scraper.write_output(records, csv_path)
        scraper.write_output(records, json_path)
        assert csv_path.read_text(encoding="utf-8").splitlines()[0] == ",".join(
            ["section_path", "category", "title", "date", "circular_no", "link", "link_type", "source_url", "scraped_at"]
        )
        assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2
    finally:
        if csv_path.exists():
            csv_path.unlink()
        if json_path.exists():
            json_path.unlink()


def test_dedupe_inventory_and_archive() -> None:
    doc_scraper = make_documents_scraper()
    circular_scraper = make_circulars_scraper()
    records = doc_scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm")
    assert len(doc_scraper.deduplicate_records(records + [records[0]])) == len(records)
    circular_records = circular_scraper.filter_for_source(records)
    assert len(circular_scraper.deduplicate_records(circular_records + [circular_records[0]])) == len(circular_records)


def test_validation_inventory_and_archive(tmp_path: Path) -> None:
    doc_scraper = make_documents_scraper()
    circular_scraper = make_circulars_scraper()
    records = doc_scraper.parse_inventory_records(load_fixture(), "https://www.apmiindia.org/apmi/welcome.htm")
    inventory_path = tmp_path / "apmi_documents_inventory.csv"
    circular_path = tmp_path / "apmi_circulars_archive.csv"
    doc_scraper.write_output(records, inventory_path)
    circular_scraper.write_output(circular_scraper.filter_for_source(records), circular_path)
    inventory_report = doc_scraper.validate_export(inventory_path)
    circular_report = circular_scraper.validate_export(circular_path)
    assert inventory_report["headers_ok"] is True
    assert circular_report["headers_ok"] is True
    assert (tmp_path / "apmi_documents_validation_report.json").exists()
    assert (tmp_path / "apmi_documents_category_counts.csv").exists()
    assert (tmp_path / "apmi_documents_link_type_counts.csv").exists()


def test_optional_link_check_output(tmp_path: Path) -> None:
    scraper = make_documents_scraper()
    csv_path = tmp_path / "apmi_documents_inventory.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(
            file_obj,
            fieldnames=["section_path", "category", "title", "date", "circular_no", "link", "link_type", "source_url", "scraped_at"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "section_path": "CIRCULARS > SEBI Circulars",
                "category": "SEBI Circulars",
                "title": "Example",
                "date": "",
                "circular_no": "",
                "link": "",
                "link_type": "pdf",
                "source_url": "https://www.apmiindia.org/apmi/welcome.htm",
                "scraped_at": "2026-05-22T00:00:00+00:00",
            }
        )
    out_path = tmp_path / "link_check.csv"
    results = scraper.check_links(
        file_path=csv_path,
        out_path=out_path,
        delay_seconds=0,
        retries=1,
        retry_base_delay=0,
        retry_max_delay=0,
    )
    assert results[0]["error"] == "missing_link"
    assert out_path.exists()
