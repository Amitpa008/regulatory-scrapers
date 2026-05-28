from __future__ import annotations

import csv
import json
from pathlib import Path

from scrapers.pfrda import (
    PFRDACircularsActiveScraper,
    PFRDANotificationsScraper,
    PFRDARecentUpdatesScraper,
    canonicalize_pfrda_url,
    clean_pfrda_text,
)


def make_active_scraper() -> PFRDACircularsActiveScraper:
    return PFRDACircularsActiveScraper(config={"source": "pfrda-circulars-active"})


def make_recent_scraper() -> PFRDARecentUpdatesScraper:
    return PFRDARecentUpdatesScraper(config={"source": "pfrda-recent-updates"})


def make_notifications_scraper() -> PFRDANotificationsScraper:
    return PFRDANotificationsScraper(config={"source": "pfrda-notifications"})


def test_parse_active_circulars_fixture() -> None:
    scraper = make_active_scraper()
    html = Path("tests/fixtures/pfrda/active_circulars_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_cards(html, "https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars", default_category="Active Circulars")
    assert len(records) == 2
    assert records[0].date == "2026-05-15"
    assert records[0].circular_no == "PFRDA/2026/31/MWnR/01"
    assert records[0].category == "Annuity Service Provider | Pension Fund"
    assert records[0].link == "https://www.pfrda.org.in/w/sample-active-circular"


def test_parse_recent_updates_fixture_blank_reference_and_valid_till() -> None:
    scraper = make_recent_scraper()
    html = Path("tests/fixtures/pfrda/recent_updates_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_cards(html, "https://www.pfrda.org.in/web/pfrda/recent-updates", default_category="Recent Updates")
    assert len(records) == 2
    assert records[0].date == "2026-05-21"
    assert records[0].circular_no == ""
    assert records[0].valid_till == "20-06-2026"
    assert records[1].category == "Tender Document"


def test_parse_notifications_fixture_and_reference_prefix() -> None:
    scraper = make_notifications_scraper()
    html = Path("tests/fixtures/pfrda/notifications_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_cards(html, "https://www.pfrda.org.in/web/pfrda/regulatory-framework/notifications", default_category="Notifications")
    assert len(records) == 2
    assert records[0].circular_no == ""
    assert records[1].circular_no == "Notification No. DA/X/17/2014-19 (Pt.-II)"
    assert records[1].date == "2023-06-11"


def test_parse_detail_metadata_fixture() -> None:
    scraper = make_active_scraper()
    html = Path("tests/fixtures/pfrda/detail_fixture_sample.html").read_text(encoding="utf-8")
    detail = scraper.parse_detail_metadata(html, "https://www.pfrda.org.in/w/sample")
    assert detail["date"] == "2026-05-15"
    assert detail["circular_no"] == "PFRDA/2026/31/MWnR/01"
    assert detail["pdf_url"] == "https://www.pfrda.org.in/documents/d/pfrda/sample-pdf"


def test_build_page_url_and_total_entries() -> None:
    scraper = make_active_scraper()
    assert scraper.build_page_url("https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars", page_number=1, delta=10) == "https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars"
    assert scraper.build_page_url("https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars", page_number=22, delta=10).endswith("delta=10&start=22")
    html = Path("tests/fixtures/pfrda/active_circulars_fixture_sample.html").read_text(encoding="utf-8")
    assert scraper.parse_total_entries(html) == 215
    assert scraper.compute_total_pages(215, delta=10) == 22


def test_canonicalize_pfrda_url_removes_navigation_query_and_repairs_embedded_absolute() -> None:
    assert canonicalize_pfrda_url(
        "https://www.pfrda.org.in/w/sample?p_l_back_url=%2Fweb%2Fpfrda%2Frecent-updates&p_l_back_url_title=Recent+Updates"
    ) == "https://www.pfrda.org.in/w/sample"
    assert canonicalize_pfrda_url(
        "https://www.pfrda.org.in/w/https-/www.pfrda.org.in/web/pfrda/w/example"
    ) == "https://www.pfrda.org.in/web/pfrda/w/example"


def test_clean_pfrda_text_repairs_common_mojibake() -> None:
    assert clean_pfrda_text("Result â€“ Test â€˜Valueâ€™") == "Result – Test ‘Value’"


def test_dedupe_blank_reference_fallback() -> None:
    scraper = make_recent_scraper()
    html = Path("tests/fixtures/pfrda/recent_updates_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_cards(html, "https://www.pfrda.org.in/web/pfrda/recent-updates", default_category="Recent Updates")
    combined = records + [records[0]]
    deduped = scraper.deduplicate_records(combined)
    assert len(deduped) == len(records)


def test_validation_report(tmp_path: Path) -> None:
    scraper = make_active_scraper()
    out_path = tmp_path / "pfrda_circulars_active_archive.csv"
    rows = [
        {
            "date": "2026-05-15",
            "subject": "Introduction of Retirement Income Schemes (RIS) and Drawdown options under the National Pension System (NPS)",
            "circular_no": "PFRDA/2026/31/MWnR/01",
            "link": "https://www.pfrda.org.in/w/sample-active-circular",
            "source_url": "https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars",
            "scraped_at": "2026-05-22T00:00:00+00:00",
        }
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"])
        writer.writeheader()
        writer.writerows(rows)
    meta_path = Path(f"{out_path}.meta.json")
    meta_path.write_text(
        json.dumps(
            [
                {
                    "date": "2026-05-15",
                    "subject": "Introduction of Retirement Income Schemes (RIS) and Drawdown options under the National Pension System (NPS)",
                    "circular_no": "PFRDA/2026/31/MWnR/01",
                    "link": "https://www.pfrda.org.in/w/sample-active-circular",
                    "source_url": "https://www.pfrda.org.in/web/pfrda/regulatory-framework/circulars/active-circulars",
                    "scraped_at": "2026-05-22T00:00:00+00:00",
                    "category": "Annuity Service Provider | Pension Fund",
                    "raw_date": "",
                    "detail_url": "",
                    "pdf_url": "",
                    "valid_till": "",
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["total_rows"] == 1
    assert report["rows_per_year"]["2026"] == 1
