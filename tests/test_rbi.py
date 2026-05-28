from __future__ import annotations

import csv
import json
from pathlib import Path

from scrapers.rbi import (
    RBIMasterCircularsScraper,
    RBIMasterDirectionsScraper,
    RBINotificationsScraper,
    RBIPressReleasesScraper,
)


def make_notifications_scraper() -> RBINotificationsScraper:
    return RBINotificationsScraper(config={"source": "rbi-notifications"})


def make_press_scraper() -> RBIPressReleasesScraper:
    return RBIPressReleasesScraper(config={"source": "rbi-press-releases"})


def make_md_scraper() -> RBIMasterDirectionsScraper:
    return RBIMasterDirectionsScraper(config={"source": "rbi-master-directions"})


def make_mc_scraper() -> RBIMasterCircularsScraper:
    return RBIMasterCircularsScraper(config={"source": "rbi-master-circulars"})


def test_detect_human_check_page() -> None:
    scraper = make_notifications_scraper()
    assert scraper.is_human_check_page('617 <script id="f5_cspm">x</script>')


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


def test_parse_notification_archive_fixture_and_feburary_cleanup() -> None:
    scraper = make_notifications_scraper()
    html = Path("tests/fixtures/rbi/notification_archive_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/commonperson/English/Scripts/Notification.aspx",
        detail_substrings=("Notification.aspx?Id=",),
        category="Notification",
    )
    assert len(records) == 2
    assert records[0].date == "2021-06-04"
    assert records[1].date == "2018-02-23"
    assert records[0].link == "https://www.rbi.org.in/commonperson/English/Scripts/Notification.aspx?Id=3312"


def test_parse_press_release_fixture() -> None:
    scraper = make_press_scraper()
    html = Path("tests/fixtures/rbi/press_releases_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/commonperson/English/Scripts/PressReleases.aspx",
        detail_substrings=("PressReleases.aspx?Id=",),
        category="Press Release",
    )
    assert len(records) == 2
    assert records[0].date == "2026-05-14"
    assert records[0].circular_no == ""
    assert records[0].link.endswith("PressReleases.aspx?Id=3906")


def test_parse_master_direction_fixture() -> None:
    scraper = make_md_scraper()
    html = Path("tests/fixtures/rbi/master_directions_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/commonperson/English/Scripts/MasterDirection.aspx",
        detail_substrings=("Notification.aspx?Id=",),
        category="Master Direction",
    )
    assert len(records) == 2
    assert records[0].date == "2025-03-25"
    assert "Updated as on March 25, 2025" in records[0].subject


def test_parse_master_circular_fixture() -> None:
    scraper = make_mc_scraper()
    html = Path("tests/fixtures/rbi/master_circulars_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/commonman/English/Scripts/MasterCircular.aspx",
        detail_substrings=("Notification.aspx?Id=",),
        category="Master Circular",
    )
    assert len(records) == 2
    assert records[1].date == "2007-03-28"


def test_extract_rbi_reference_from_detail_fixture(monkeypatch) -> None:
    scraper = make_notifications_scraper()
    detail_html = Path("tests/fixtures/rbi/notification_detail_fixture_sample.html").read_text(encoding="utf-8")
    monkeypatch.setattr(scraper, "fetch_page_html", lambda _url: detail_html)
    reference = scraper.extract_reference_from_detail("https://www.rbi.org.in/commonperson/English/Scripts/Notification.aspx?Id=3312")
    assert reference == "RBI/2021-22/47 DOR.STR.REC.21/21.04.048/2021-22"


def test_absolute_url_and_link_type_detection() -> None:
    scraper = make_notifications_scraper()
    assert scraper.detect_link_type("https://rbidocs.rbi.org.in/rdocs/notification/PDFs/test.pdf") == "pdf"
    assert scraper.detect_link_type("https://www.rbi.org.in/commonperson/English/Scripts/Notification.aspx?Id=1") == "html/detail"
    assert scraper.detect_link_type("https://www.rbi.org.in/file.docx") == "doc/docx"
    assert scraper.detect_link_type("https://www.rbi.org.in/file.xlsx") == "xls/xlsx"
    assert scraper.detect_link_type("https://www.rbi.org.in/file.zip") == "zip"


def test_deduplicate_records_uses_blank_reference_fallback() -> None:
    scraper = make_press_scraper()
    html = Path("tests/fixtures/rbi/press_releases_fixture_sample.html").read_text(encoding="utf-8")
    records = scraper.parse_grouped_records(
        html,
        "https://www.rbi.org.in/commonperson/English/Scripts/PressReleases.aspx",
        detail_substrings=("PressReleases.aspx?Id=",),
        category="Press Release",
    )
    combined = records + [records[0]]
    deduped = scraper.deduplicate_records(combined)
    assert len(deduped) == len(records)


def test_checkpoint_chunk_window_for_notifications_routes() -> None:
    scraper = make_notifications_scraper()
    window = scraper.compute_chunk_window(total_chunks=2, previous_last_completed_chunk=1, max_chunks_this_run=50)
    assert window["resume_from_chunk"] == 2
    assert window["expected_end_chunk"] == 2
    assert window["chunks_this_run"] == 1


def test_validation_report(tmp_path: Path) -> None:
    scraper = make_notifications_scraper()
    out_path = tmp_path / "rbi_notifications_archive.csv"
    rows = [
        {
            "date": "2026-05-18",
            "subject": "Reserve Bank sample notification",
            "circular_no": "",
            "link": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13459&Mode=0",
            "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
            "scraped_at": "2026-05-20T00:00:00+00:00",
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
                    "date": "2026-05-18",
                    "subject": "Reserve Bank sample notification",
                    "circular_no": "",
                    "link": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13459&Mode=0",
                    "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
                    "scraped_at": "2026-05-20T00:00:00+00:00",
                    "category": "Notification",
                    "raw_date": "",
                    "detail_url": "",
                    "pdf_url": "",
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
