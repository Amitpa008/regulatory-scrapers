from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scrapers.rbi import (
    MASTER_DIRECTION_OCCURRENCE_HEADERS,
    RBIOccurrence,
    RBIActsScraper,
    RBIAmendmentDirectionsScraper,
    RBICircularIndexScraper,
    RBIDraftDirectionsREWiseScraper,
    RBIDraftNotificationsGuidelinesScraper,
    RBIRecord,
    RBIRegulationsScraper,
    RBIRulesScraper,
    RBISchemesScraper,
    RBIStandaloneCircularsScraper,
    RBIMasterCircularsScraper,
    RBIMasterDirectionsScraper,
    RBINotificationsScraper,
    RBIPressReleasesScraper,
    RBIWithdrawnCircularsScraper,
)


def make_notifications_scraper() -> RBINotificationsScraper:
    return RBINotificationsScraper(config={"source": "rbi-notifications"})


def make_press_scraper() -> RBIPressReleasesScraper:
    return RBIPressReleasesScraper(config={"source": "rbi-press-releases"})


def make_md_scraper() -> RBIMasterDirectionsScraper:
    return RBIMasterDirectionsScraper(config={"source": "rbi-master-directions"})


def make_mc_scraper() -> RBIMasterCircularsScraper:
    return RBIMasterCircularsScraper(config={"source": "rbi-master-circulars"})


def make_circular_index_scraper() -> RBICircularIndexScraper:
    return RBICircularIndexScraper(config={"source": "rbi-circular-index"})


def make_standalone_scraper() -> RBIStandaloneCircularsScraper:
    return RBIStandaloneCircularsScraper(config={"source": "rbi-standalone-circulars"})


def make_withdrawn_scraper() -> RBIWithdrawnCircularsScraper:
    return RBIWithdrawnCircularsScraper(config={"source": "rbi-withdrawn-circulars"})


def make_amendment_scraper() -> RBIAmendmentDirectionsScraper:
    return RBIAmendmentDirectionsScraper(config={"source": "rbi-amendment-directions"})


def make_acts_scraper() -> RBIActsScraper:
    return RBIActsScraper(config={"source": "rbi-acts"})


def make_rules_scraper() -> RBIRulesScraper:
    return RBIRulesScraper(config={"source": "rbi-rules"})


def make_regulations_scraper() -> RBIRegulationsScraper:
    return RBIRegulationsScraper(config={"source": "rbi-regulations"})


def make_schemes_scraper() -> RBISchemesScraper:
    return RBISchemesScraper(config={"source": "rbi-schemes"})


def make_draft_notifications_scraper() -> RBIDraftNotificationsGuidelinesScraper:
    return RBIDraftNotificationsGuidelinesScraper(config={"source": "rbi-draft-notifications-guidelines"})


def make_draft_rewise_scraper() -> RBIDraftDirectionsREWiseScraper:
    return RBIDraftDirectionsREWiseScraper(config={"source": "rbi-draft-directions-re-wise"})


def test_detect_human_check_page() -> None:
    scraper = make_notifications_scraper()
    assert scraper.is_human_check_page('617 <script id="f5_cspm">x</script>')


def test_notification_controls_cover_2020_to_2026() -> None:
    scraper = make_notifications_scraper()
    html = Path("tests/fixtures/rbi/notification_user_fixture_sample.html").read_text(encoding="utf-8")
    chunk_specs = scraper.extract_year_month_chunks(html, "https://www.rbi.org.in/Scripts/NotificationUser.aspx")
    years = {chunk["year"] for chunk in chunk_specs}
    assert years == {2020, 2021, 2022, 2023, 2024, 2025, 2026}


def test_parse_notification_user_fixture() -> None:
    scraper = make_notifications_scraper()
    html = Path("tests/fixtures/rbi/notification_user_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_notification_user_records(html, "https://www.rbi.org.in/Scripts/NotificationUser.aspx")
    assert len(records) == 2
    assert records[0].date == "2026-05-18"
    assert records[0].subject.startswith("Reserve Bank of India (Local Area Banks")
    assert records[0].link == "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13459&Mode=0"
    assert records[0].pdf_url.endswith("NOTI92.PDF")
    assert records[1].date == "2026-04-29"


def test_parse_press_release_fixture_uses_bs_pressreleasedisplay() -> None:
    scraper = make_press_scraper()
    html = Path("tests/fixtures/rbi/press_releases_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx",
        detail_substrings=("BS_PressReleaseDisplay.aspx?prid=",),
        category="Press Release",
    )
    assert len(records) == 2
    assert records[0].date == "2026-05-14"
    assert records[0].circular_no == ""
    assert records[0].link.endswith("BS_PressReleaseDisplay.aspx?prid=3906")


def test_parse_master_direction_fixture_includes_2026_entries_from_official_page() -> None:
    scraper = make_md_scraper()
    html = Path("tests/fixtures/rbi/master_directions_fixture_sample.html").read_text(encoding="utf-8")
    chunk_specs = scraper.extract_year_chunks(html, "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx")
    assert {chunk["year"] for chunk in chunk_specs} == {2025, 2026}

    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
        detail_substrings=("BS_ViewMasDirections.aspx?id=",),
        category="Master Direction",
    )
    assert len(records) == 2
    assert records[0].date == "2026-04-30"
    assert records[0].link.endswith("BS_ViewMasDirections.aspx?id=13443")
    assert records[0].category == "Banker to Governments and Banks"
    assert records[1].date == "2026-01-14"


def test_parse_master_circular_fixture_includes_2025_entries_from_official_page() -> None:
    scraper = make_mc_scraper()
    html = Path("tests/fixtures/rbi/master_circulars_fixture_sample.html").read_text(encoding="utf-8")
    chunk_specs = scraper.extract_year_chunks(html, "https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx")
    assert {chunk["year"] for chunk in chunk_specs} == {2024, 2025}

    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx",
        detail_substrings=("BS_ViewMasCirculardetails.aspx?id=",),
        category="Master Circular",
    )
    assert len(records) == 2
    assert records[0].date == "2025-04-01"
    assert records[0].link.endswith("BS_ViewMasCirculardetails.aspx?id=12812")
    assert records[0].category == "Banker to Governments and Banks"


def test_extract_rbi_reference_from_detail_fixture(monkeypatch) -> None:
    scraper = make_notifications_scraper()
    detail_html = Path("tests/fixtures/rbi/notification_detail_fixture_sample.html").read_text(encoding="utf-8")
    monkeypatch.setattr(scraper, "fetch_page_html", lambda _url: detail_html)
    reference = scraper.extract_reference_from_detail("https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=3312")
    assert reference == "RBI/2021-22/47 DOR.STR.REC.21/21.04.048/2021-22"


def test_absolute_url_and_link_type_detection() -> None:
    scraper = make_notifications_scraper()
    assert scraper.detect_link_type("https://rbidocs.rbi.org.in/rdocs/notification/PDFs/test.pdf") == "pdf"
    assert scraper.detect_link_type("https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=1&Mode=0") == "html/detail"
    assert scraper.detect_link_type("https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=3906") == "html/detail"
    assert scraper.detect_link_type("https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13443") == "html/detail"
    assert scraper.detect_link_type("https://www.rbi.org.in/Scripts/BS_ViewMasCirculardetails.aspx?id=12812") == "html/detail"


def test_deduplicate_records_uses_canonical_detail_url() -> None:
    scraper = make_press_scraper()
    html = Path("tests/fixtures/rbi/press_releases_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx",
        detail_substrings=("BS_PressReleaseDisplay.aspx?prid=",),
        category="Press Release",
    )
    combined = records + [records[0]]
    deduped = scraper.deduplicate_records(combined)
    assert len(deduped) == len(records)


def test_master_directions_are_unique_by_canonical_url_source_url() -> None:
    scraper = make_md_scraper()
    base_record = {
        "date": "2026-04-30",
        "subject": "Master Direction - Sample",
        "circular_no": "",
        "source_url": "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
        "scraped_at": "2026-05-29T00:00:00+00:00",
        "category": "Master Direction",
        "raw_date": "2026-04-30",
        "pdf_url": "",
    }
    records = [
        RBIRecord(
            link="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10190&fn=5&Mode=0",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10190&fn=5&Mode=0",
            **base_record,
        ),
        RBIRecord(
            link="https://rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10190&fn=5&Mode=0 ",
            detail_url="https://rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10190&fn=5&Mode=0 ",
            **base_record,
        ),
    ]
    deduped = scraper.deduplicate_records(records)
    assert len(deduped) == 1
    assert deduped[0].link == "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10190&fn=5&Mode=0"


def test_master_directions_collapse_reused_detail_url_to_single_export_row() -> None:
    scraper = make_md_scraper()
    records = [
        RBIRecord(
            date="2025-03-24",
            subject="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            circular_no="",
            link="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
            source_url="https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
            scraped_at="2026-05-30T00:00:00+00:00",
            category="Master Direction",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
        ),
        RBIRecord(
            date="2020-09-04",
            subject="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            circular_no="",
            link="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
            source_url="https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
            scraped_at="2026-05-30T00:00:00+00:00",
            category="Master Direction",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
        ),
    ]
    deduped = scraper.deduplicate_records(records)
    assert len(deduped) == 1
    assert deduped[0].date == "2025-03-24"


def test_master_direction_occurrences_preserve_reused_detail_url_history(tmp_path: Path) -> None:
    scraper = make_md_scraper()
    records = [
        RBIRecord(
            date="2025-03-24",
            subject="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            circular_no="",
            link="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
            source_url="https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
            scraped_at="2026-05-30T00:00:00+00:00",
            category="Master Direction",
            raw_title="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
        ),
        RBIRecord(
            date="2020-09-04",
            subject="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            circular_no="",
            link="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
            source_url="https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
            scraped_at="2026-05-30T00:00:00+00:00",
            category="Master Direction",
            raw_title="Master Directions - Reserve Bank of India (Priority Sector Lending - Targets and Classification) Directions, 2025",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12799",
        ),
    ]
    occurrence_a = scraper.records_to_occurrences([records[0]], {"year": 2025, "month": None})[0]
    occurrence_b = scraper.records_to_occurrences([records[1]], {"year": 2020, "month": None})[0]
    assert occurrence_a.canonical_url == occurrence_b.canonical_url
    assert scraper.occurrence_dedup_key(occurrence_a) != scraper.occurrence_dedup_key(occurrence_b)

    out_path = tmp_path / "rbi_master_directions_occurrences.csv"
    scraper.write_occurrences([occurrence_a, occurrence_b], out_path)
    with open(out_path, newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        rows = list(reader)
    assert reader.fieldnames == MASTER_DIRECTION_OCCURRENCE_HEADERS
    assert len(rows) == 2
    assert {row["archive_year"] for row in rows} == {"2020", "2025"}


def test_checkpoint_chunk_window_for_notifications_chunks() -> None:
    scraper = make_notifications_scraper()
    window = scraper.compute_chunk_window(total_chunks=24, previous_last_completed_chunk=1, max_chunks_this_run=50)
    assert window["resume_from_chunk"] == 2
    assert window["expected_end_chunk"] == 24
    assert window["chunks_this_run"] == 23


def test_validation_report_passes_with_expected_recent_years(tmp_path: Path) -> None:
    scraper = make_notifications_scraper()
    out_path = tmp_path / "rbi_notifications_archive.csv"
    rows = []
    for year in range(2020, 2027):
        rows.append(
            {
                "date": f"{year}-01-15",
                "subject": f"Reserve Bank sample notification {year}",
                "circular_no": f"RBI/{year}-X/1",
                "link": f"https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id={year}&Mode=0",
                "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
                "scraped_at": "2026-05-20T00:00:00+00:00",
            }
        )
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"])
        writer.writeheader()
        writer.writerows(rows)
    meta_path = Path(f"{out_path}.meta.json")
    meta_path.write_text(
        json.dumps(
            [
                {
                    **row,
                    "category": "Notification",
                    "raw_date": "",
                    "detail_url": row["link"],
                    "pdf_url": "",
                }
                for row in rows
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["quality_gate_passed"] is True
    assert report["rows_per_year"]["2026"] == 1


def test_validation_report_fails_when_expected_recent_year_missing(tmp_path: Path) -> None:
    scraper = make_notifications_scraper()
    out_path = tmp_path / "rbi_notifications_archive.csv"
    rows = []
    for year in range(2020, 2026):
        rows.append(
            {
                "date": f"{year}-01-15",
                "subject": f"Reserve Bank sample notification {year}",
                "circular_no": f"RBI/{year}-X/1",
                "link": f"https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id={year}&Mode=0",
                "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
                "scraped_at": "2026-05-20T00:00:00+00:00",
            }
        )
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"])
        writer.writeheader()
        writer.writerows(rows)
    meta_path = Path(f"{out_path}.meta.json")
    meta_path.write_text(
        json.dumps(
            [
                {
                    **row,
                    "category": "Notification",
                    "raw_date": "",
                    "detail_url": row["link"],
                    "pdf_url": "",
                }
                for row in rows
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="missing rows for expected recent year\\(s\\) 2026"):
        scraper.validate_export(out_path)


def test_parse_circular_index_fixture_sample() -> None:
    scraper = make_circular_index_scraper()
    html = Path("tests/fixtures/rbi/circular_index_fixture_sample.html").read_text(encoding="utf-8")
    chunk_specs = scraper.extract_year_month_chunks(html, "https://www.rbi.org.in/scripts/bs_circularindexdisplay.aspx")
    assert {item["year"] for item in chunk_specs} == {2025, 2026}
    rows = scraper.parse_inventory_rows(
        html,
        {"url": "https://www.rbi.org.in/scripts/bs_circularindexdisplay.aspx", "year": 2026, "month": 5},
    )
    assert len(rows) == 2
    assert rows[0]["circular_number"].startswith("RBI/DOR/2026-2027/95")
    assert rows[0]["date"] == "2026-05-25"
    assert rows[0]["department"] == "Department of Regulation"
    assert rows[0]["archive_year"] == "2026"
    assert rows[0]["archive_month"] == "5"
    assert rows[0]["detail_url"].endswith("BS_CircularIndexDisplay.aspx?Id=13462")


def test_parse_standalone_circular_fixture_sample() -> None:
    scraper = make_standalone_scraper()
    html = Path("tests/fixtures/rbi/standalone_circulars_fixture_sample.html").read_text(encoding="utf-8")
    rows = scraper.parse_inventory_rows(html, {"url": "https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx"})
    assert len(rows) == 3
    assert rows[0]["serial_number"] == "1"
    assert rows[0]["circular_number"] == "DOR.ACC.REC.No.426/21.02.067/2025-26"
    assert rows[0]["title"].startswith("Reserve Bank of India (Local Area Banks")
    assert rows[0]["date"] == "2026-03-10"
    assert rows[0]["detail_url"].endswith("NotificationUser.aspx?Id=13325&Mode=0")


def test_standalone_circulars_preserve_distinct_circular_numbers_even_when_detail_url_reused() -> None:
    scraper = make_standalone_scraper()
    rows = [
        {
            "serial_number": "16",
            "circular_number": "DoR.RET.REC.24/12.01.001/2025-26",
            "title": "Notification on Maintenance of Cash Reserve Ratio (CRR)",
            "date": "2025-06-06",
            "detail_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12858&Mode=0#A_1",
            "canonical_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12858&Mode=0",
            "source_url": "https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        },
        {
            "serial_number": "17",
            "circular_number": "DoR.RET.REC.23/12.01.001/2025-26",
            "title": "Maintenance of Cash Reserve Ratio (CRR)",
            "date": "2025-06-06",
            "detail_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12858&Mode=0",
            "canonical_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12858&Mode=0",
            "source_url": "https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        },
    ]
    assert scraper.inventory_row_dedup_key(rows[0]) != scraper.inventory_row_dedup_key(rows[1])


def test_parse_withdrawn_circulars_fixture_sample_and_categories() -> None:
    scraper = make_withdrawn_scraper()
    html = Path("tests/fixtures/rbi/withdrawn_circulars_fixture_sample.html").read_text(encoding="utf-8")
    rows = scraper.parse_inventory_rows(html, {"url": "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx"})
    assert len(rows) == 4
    categories = {row["withdrawal_category"] for row in rows}
    assert categories == {"RRA 2.0", "Department of Regulation"}
    rra_row = next(row for row in rows if row["withdrawal_category"] == "RRA 2.0")
    dor_row = next(row for row in rows if row["withdrawal_category"] == "Department of Regulation")
    assert rra_row["department"] == "Department of Payment and Settlement Systems"
    assert rra_row["date"] == "2019-08-21"
    assert dor_row["serial_number"] == "1"
    assert dor_row["circular_number"] == "DOR.CRE.REC.402/07-01-001/2025-26"
    assert dor_row["date"] == "2026-02-13"


def test_withdrawn_circular_occurrences_are_preserved_across_categories() -> None:
    scraper = make_withdrawn_scraper()
    base = {
        "serial_number": "",
        "circular_number": "DOR.CRE.REC.402/07-01-001/2025-26",
        "title_or_subject": "Review of Regulatory Guidelines - Withdrawal of Circulars",
        "date": "2026-02-13",
        "department": "Department of Regulation",
        "detail_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12830&Mode=0",
        "canonical_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12830&Mode=0",
        "source_url": "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx",
        "scraped_at": "2026-05-30T00:00:00+00:00",
    }
    row_a = {**base, "withdrawal_category": "RRA 2.0"}
    row_b = {**base, "withdrawal_category": "Department of Regulation"}
    assert scraper.inventory_row_dedup_key(row_a) != scraper.inventory_row_dedup_key(row_b)


def test_parse_amendment_directions_fixture_live_page() -> None:
    scraper = make_amendment_scraper()
    html = Path("tests/fixtures/rbi/fs_amendmentdirections.html").read_text(encoding="utf-8")
    rows = scraper.parse_inventory_rows(html, {"url": "https://www.rbi.org.in/Scripts/Fs_AmendmentDirections.aspx", "year": 2026, "month": 5})
    assert rows
    assert rows[0]["date"] >= "2025-01-01"
    assert rows[0]["title"]
    assert "NotificationUser.aspx?Id=" in rows[0]["detail_url"]


def test_parse_legal_inventory_fixtures() -> None:
    acts_rows = make_acts_scraper().parse_inventory_rows(
        Path("tests/fixtures/rbi/act.html").read_text(encoding="utf-8"),
        {"year": None, "month": None},
    )
    rules_rows = make_rules_scraper().parse_inventory_rows(
        Path("tests/fixtures/rbi/rules.html").read_text(encoding="utf-8"),
        {"year": None, "month": None},
    )
    regulations_rows = make_regulations_scraper().parse_inventory_rows(
        Path("tests/fixtures/rbi/regulations.html").read_text(encoding="utf-8"),
        {"year": None, "month": None},
    )
    schemes_rows = make_schemes_scraper().parse_inventory_rows(
        Path("tests/fixtures/rbi/schemes.html").read_text(encoding="utf-8"),
        {"year": None, "month": None},
    )
    assert acts_rows and any("Banking Regulation Act" in row["title"] for row in acts_rows)
    assert rules_rows and any("Rules" in row["title"] or "Rule" in row["title"] for row in rules_rows)
    assert regulations_rows and any(row["detail_url"] for row in regulations_rows)
    assert schemes_rows and any("Scheme" in row["title"] for row in schemes_rows)


def test_parse_draft_notifications_guidelines_fixture_live_page() -> None:
    scraper = make_draft_notifications_scraper()
    html = Path("tests/fixtures/rbi/draftnotificationsguildelines.html").read_text(encoding="utf-8")
    rows = scraper.parse_inventory_rows(
        html,
        {"url": "https://www.rbi.org.in/Scripts/DraftNotificationsGuildelines.aspx", "year": 2026, "month": 5},
    )
    assert rows
    assert rows[0]["date"] >= "2025-01-01"
    assert rows[0]["title"]
    assert "BS_PressReleaseDisplay.aspx?prid=" in rows[0]["detail_url"]


def test_parse_draft_directions_rewise_fixture_live_page() -> None:
    scraper = make_draft_rewise_scraper()
    html = Path("tests/fixtures/rbi/bs_viewrewisedraftdirections.html").read_text(encoding="utf-8")
    rows = scraper.parse_inventory_rows(
        html,
        {"url": "https://www.rbi.org.in/Scripts/BS_ViewREwiseDraftDirections.aspx", "year": 2026},
    )
    assert rows
    assert rows[0]["regulated_entity"]
    assert rows[0]["archive_year"] == "2026"
    assert rows[0]["date"] == "2026-04-08"
    assert "BS_ViewREwiseDraftDirections.aspx?id=" in rows[0]["detail_url"]


def test_validate_circular_index_export_passes(tmp_path: Path) -> None:
    scraper = make_circular_index_scraper()
    out_path = tmp_path / "rbi_circular_index_archive.csv"
    rows = [
        {
            "circular_number": "RBI/DOR/2026-2027/95 DOR.GOV.REC.No.83/18.10.015/2026-27",
            "date": "2026-05-25",
            "department": "Department of Regulation",
            "subject": "Reserve Bank of India (Rural Co-operative Banks - Governance) Amendment Directions, 2026",
            "meant_for": "",
            "detail_url": "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=13462",
            "canonical_url": "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=13462",
            "source_url": "https://www.rbi.org.in/scripts/bs_circularindexdisplay.aspx",
            "archive_year": "2026",
            "archive_month": "5",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        }
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=scraper.output_headers)
        writer.writeheader()
        writer.writerows(rows)
    report = scraper.validate_export(out_path)
    assert report["headers_ok"] is True
    assert report["duplicate_key_count"] == 0


def test_validate_amendment_directions_expected_recent_years_pass(tmp_path: Path) -> None:
    scraper = make_amendment_scraper()
    out_path = tmp_path / "rbi_amendment_directions_archive.csv"
    rows = [
        {
            "date": "2025-10-10",
            "title": "Reserve Bank of India (Sample Amendment Directions), 2025",
            "detail_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=1000&Mode=0",
            "canonical_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=1000&Mode=0",
            "pdf_url": "https://rbidocs.rbi.org.in/rdocs/notification/PDFs/SAMPLE2025.PDF",
            "source_url": "https://www.rbi.org.in/Scripts/Fs_AmendmentDirections.aspx",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        },
        {
            "date": "2026-05-25",
            "title": "Reserve Bank of India (Sample Amendment Directions), 2026",
            "detail_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=1001&Mode=0",
            "canonical_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=1001&Mode=0",
            "pdf_url": "https://rbidocs.rbi.org.in/rdocs/notification/PDFs/SAMPLE2026.PDF",
            "source_url": "https://www.rbi.org.in/Scripts/Fs_AmendmentDirections.aspx",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        },
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=scraper.output_headers)
        writer.writeheader()
        writer.writerows(rows)
    report = scraper.validate_export(out_path)
    assert report["quality_gate_passed"] is True


def test_validate_draft_rewise_expected_recent_years_fail(tmp_path: Path) -> None:
    scraper = make_draft_rewise_scraper()
    out_path = tmp_path / "rbi_draft_directions_re_wise_archive.csv"
    rows = [
        {
            "regulated_entity": "Commercial Banks",
            "archive_year": "2025",
            "date": "2025-10-10",
            "title": "Reserve Bank of India (Commercial Banks - Sample) Directions, 2025",
            "detail_url": "https://www.rbi.org.in/Scripts/BS_ViewREwiseDraftDirections.aspx?id=1",
            "canonical_url": "https://www.rbi.org.in/Scripts/BS_ViewREwiseDraftDirections.aspx?id=1",
            "pdf_url": "https://rbidocs.rbi.org.in/rdocs/notification/PDFs/SAMPLE.PDF",
            "source_url": "https://www.rbi.org.in/Scripts/BS_ViewREwiseDraftDirections.aspx",
            "scraped_at": "2026-05-30T00:00:00+00:00",
        },
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=scraper.output_headers)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(RuntimeError, match="missing rows for expected recent year"):
        scraper.validate_export(out_path)
