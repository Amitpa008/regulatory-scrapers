from __future__ import annotations

import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


RBI_BASE_URL = "https://www.rbi.org.in"
RBI_HOME_URL = f"{RBI_BASE_URL}/home.aspx"
RBI_NOTIFICATIONS_URL = f"{RBI_BASE_URL}/Scripts/NotificationUser.aspx"
RBI_NOTIFICATIONS_ARCHIVE_URL = f"{RBI_BASE_URL}/commonperson/English/Scripts/Notification.aspx"
RBI_PRESS_RELEASES_URL = f"{RBI_BASE_URL}/commonman/English/Scripts/PressReleases.aspx"
RBI_PRESS_RELEASES_ARCHIVE_URL = f"{RBI_BASE_URL}/commonperson/English/Scripts/PressReleases.aspx"
RBI_MASTER_DIRECTIONS_URL = f"{RBI_BASE_URL}/Scripts/BS_ViewMasterDirections.aspx"
RBI_MASTER_DIRECTIONS_ARCHIVE_URL = f"{RBI_BASE_URL}/commonperson/English/Scripts/MasterDirection.aspx"
RBI_MASTER_CIRCULARS_URL = f"{RBI_BASE_URL}/commonman/English/Scripts/MasterCircular.aspx"
RBI_FAQS_URL = f"{RBI_BASE_URL}/commonman/english/scripts/FAQs.aspx"
RBI_SPEECHES_URL = f"{RBI_BASE_URL}/Scripts/BS_ViewSpeeches.aspx"
RBI_PRESS_RELEASE_DETAIL_PATTERN = "PressReleases.aspx?Id="
RBI_NOTIFICATION_DETAIL_PATTERN = "Notification.aspx?Id="
RBI_NOTIFICATION_USER_DETAIL_PATTERN = "NotificationUser.aspx?Id="
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_OUTPUT_HEADERS = ["date", "category", "subject", "circular_no", "link", "source_url", "scraped_at"]
VALID_RBI_HOST_MARKERS = ("rbi.org.in", "rbidocs.rbi.org.in")

RBI_SOURCE_LABELS = {
    "rbi-notifications": "Notifications",
    "rbi-press-releases": "Press Releases",
    "rbi-master-directions": "Master Directions",
    "rbi-master-circulars": "Master Circulars",
}

RBI_PRIMARY_LISTING_URLS = {
    "rbi-notifications": RBI_NOTIFICATIONS_URL,
    "rbi-press-releases": RBI_PRESS_RELEASES_ARCHIVE_URL,
    "rbi-master-directions": RBI_MASTER_DIRECTIONS_ARCHIVE_URL,
    "rbi-master-circulars": RBI_MASTER_CIRCULARS_URL,
}

RBI_SCRAPE_ROUTES = {
    "rbi-notifications": [
        ("notifications_current", RBI_NOTIFICATIONS_URL),
        ("notifications_archive", RBI_NOTIFICATIONS_ARCHIVE_URL),
    ],
    "rbi-press-releases": [
        ("press_releases_archive", RBI_PRESS_RELEASES_ARCHIVE_URL),
    ],
    "rbi-master-directions": [
        ("master_directions_archive", RBI_MASTER_DIRECTIONS_ARCHIVE_URL),
    ],
    "rbi-master-circulars": [
        ("master_circulars_archive", RBI_MASTER_CIRCULARS_URL),
    ],
}

RBI_SCOUT_ROUTES = [
    ("home.html", RBI_HOME_URL),
    ("notifications.html", RBI_NOTIFICATIONS_URL),
    ("notifications_commonperson.html", RBI_NOTIFICATIONS_ARCHIVE_URL),
    ("press_releases.html", RBI_PRESS_RELEASES_URL),
    ("press_releases_commonperson.html", RBI_PRESS_RELEASES_ARCHIVE_URL),
    ("master_directions.html", RBI_MASTER_DIRECTIONS_URL),
    ("master_directions_commonperson.html", RBI_MASTER_DIRECTIONS_ARCHIVE_URL),
    ("master_circulars.html", RBI_MASTER_CIRCULARS_URL),
    ("speeches.html", RBI_SPEECHES_URL),
    ("faqs.html", RBI_FAQS_URL),
]


@dataclass
class RBIRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    category: str = ""
    raw_date: str = ""
    detail_url: str = ""
    pdf_url: str = ""


@dataclass
class RBIRouteProbe:
    url: str
    status_code: Optional[int]
    final_url: str
    page_title: str
    page_heading: str
    direct_http_worked: bool
    blocked_or_human_check: bool
    rows_present: bool
    commonman_variant_exists: bool
    commonperson_variant_exists: bool
    old_archive_records_visible: bool
    selectors_found: list[str]
    filters_found: list[str]
    pagination_found: list[str]
    link_type_counts: dict[str, int]
    first_records: list[dict[str, str]]
    last_records: list[dict[str, str]]
    error: Optional[str] = None


@dataclass
class RBICheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    years_discovered: list[str]
    total_records_detected: Optional[int]
    count_by_year: dict[str, int]
    count_by_category: dict[str, int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class RBIScraper(BaseScraper):
    source: str
    regulator = "Reserve Bank of India"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"
        self._detail_reference_cache: dict[str, str] = {}

    @property
    def source_label(self) -> str:
        return RBI_SOURCE_LABELS[self.source]

    @property
    def listing_url(self) -> str:
        return RBI_PRIMARY_LISTING_URLS[self.source]

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(self.listing_url)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_records_from_html(str(response), self.listing_url, source=self.source)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": self.source_label.rstrip("s"),
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": record.category or self.source_label,
                "pdf_url": record.pdf_url or None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", self.source_label),
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record["published_date"],
            department=record.get("department"),
            category=record.get("category"),
            url=record["url"],
            pdf_url=record.get("pdf_url"),
            pdf_sha256=None,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def scout_site(self, base_url: str) -> list[RBIRouteProbe]:
        del base_url
        fixture_dir = Path("tests/fixtures/rbi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        probes: list[RBIRouteProbe] = []
        for fixture_name, url in RBI_SCOUT_ROUTES:
            try:
                response = self.get(url)
                html = response.text
                (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
                probe = self.inspect_route(url=url, html=html, status_code=response.status_code, final_url=str(response.url))
            except Exception as exc:  # pragma: no cover - live network failure
                probe = RBIRouteProbe(
                    url=url,
                    status_code=None,
                    final_url=url,
                    page_title="",
                    page_heading="",
                    direct_http_worked=False,
                    blocked_or_human_check=False,
                    rows_present=False,
                    commonman_variant_exists="commonman" in url.lower(),
                    commonperson_variant_exists="commonperson" in url.lower(),
                    old_archive_records_visible=False,
                    selectors_found=[],
                    filters_found=[],
                    pagination_found=[],
                    link_type_counts={},
                    first_records=[],
                    last_records=[],
                    error=str(exc),
                )
            probes.append(probe)
            self.print_probe(probe)
        return probes

    def inspect_notifications(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/rbi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        current_html = self.fetch_page_html(url)
        archive_html = self.fetch_page_html(RBI_NOTIFICATIONS_ARCHIVE_URL)
        (fixture_dir / "notifications.html").write_text(current_html, encoding="utf-8")
        (fixture_dir / "notifications_commonperson.html").write_text(archive_html, encoding="utf-8")

        current_records = self.parse_notification_user_records(current_html, url)
        archive_records = self.parse_grouped_records(
            archive_html,
            RBI_NOTIFICATIONS_ARCHIVE_URL,
            detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
            category="Notification",
        )
        self.populate_reference_numbers(current_records[:10] + archive_records[:10])

        print(f"working endpoint/page-flow: {url} (current) + {RBI_NOTIFICATIONS_ARCHIVE_URL} (archive)")
        print("request method: GET")
        print("query params/payload: none required for the accessible public listing flow")
        print("cookies/session requirements: none beyond normal public page fetch")
        print("whether Playwright was required: no")
        print("fields available: date, title, detail html link, pdf link, detail-page reference number")
        print("sample 10 records:")
        for record in (current_records + archive_records)[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)} | {self.console_safe(record.source_url)}"
            )
        return {
            "current_record_count": len(current_records),
            "archive_record_count": len(archive_records),
            "working_flow": [url, RBI_NOTIFICATIONS_ARCHIVE_URL],
        }

    def inspect_press_releases(self, url: str) -> dict[str, Any]:
        return self.inspect_simple_source(
            source="rbi-press-releases",
            url=RBI_PRESS_RELEASES_ARCHIVE_URL,
            fixture_name="press_releases.html",
            detail_substrings=(RBI_PRESS_RELEASE_DETAIL_PATTERN,),
            category="Press Release",
        )

    def inspect_master_directions(self, url: str) -> dict[str, Any]:
        del url
        return self.inspect_simple_source(
            source="rbi-master-directions",
            url=RBI_MASTER_DIRECTIONS_ARCHIVE_URL,
            fixture_name="master_directions.html",
            detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
            category="Master Direction",
        )

    def inspect_master_circulars(self, url: str) -> dict[str, Any]:
        del url
        return self.inspect_simple_source(
            source="rbi-master-circulars",
            url=RBI_MASTER_CIRCULARS_URL,
            fixture_name="master_circulars.html",
            detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
            category="Master Circular",
        )

    def inspect_speeches(self, url: str) -> dict[str, Any]:
        return self.inspect_secondary_page(url, fixture_name="speeches.html")

    def inspect_faqs(self, url: str) -> dict[str, Any]:
        return self.inspect_secondary_page(url, fixture_name="faqs.html")

    def discover_notification_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("rbi-notifications", enrich_reference_numbers=False)
        return self.print_discovery_result(
            source_label="notifications",
            working_flow=f"{url} + {RBI_NOTIFICATIONS_ARCHIVE_URL}",
            records=records,
            count_by_category=self.count_by_category(records),
            limitation=(
                "Current notifications are listed on Scripts/NotificationUser.aspx while older notifications are listed on "
                "commonperson/English/Scripts/Notification.aspx, so full coverage requires both official routes."
            ),
        )

    def discover_press_release_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("rbi-press-releases")
        return self.print_discovery_result(
            source_label="press releases",
            working_flow=url,
            records=records,
            count_by_category=self.count_by_category(records),
            limitation="The accessible commonperson press release page exposes the public archive in a single raw HTML listing.",
        )

    def discover_master_direction_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("rbi-master-directions")
        return self.print_discovery_result(
            source_label="master directions",
            working_flow=url,
            records=records,
            count_by_category=self.count_by_category(records),
            limitation="The accessible commonperson Master Direction page exposes the public listing in raw HTML; no deeper pagination was required.",
        )

    def discover_master_circular_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("rbi-master-circulars")
        return self.print_discovery_result(
            source_label="master circulars",
            working_flow=url,
            records=records,
            count_by_category=self.count_by_category(records),
            limitation="The accessible Master Circular page exposes the public archive in a single grouped HTML listing.",
        )

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        include_category: bool = False,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[RBIRecord]:
        del retries, retry_base_delay, retry_max_delay
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        route_specs = RBI_SCRAPE_ROUTES[self.source]
        preview_records = self.collect_records_for_source(self.source, enrich_reference_numbers=False)
        if not preview_records:
            raise RuntimeError(f"{self.source} returned zero rows")

        valid_dates = [date.fromisoformat(record.date) for record in preview_records if record.date]
        newest_available_date = max(valid_dates)
        oldest_available_date = min(valid_dates)
        if all_available and from_date is None:
            from_date = oldest_available_date
        if from_date is None:
            from_date = oldest_available_date
        if to_date is None:
            to_date = newest_available_date

        existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
        existing_keys = {self.record_dedup_key(item) for item in existing_records}
        existing_count = len(existing_records)
        output_mode = "append" if resume and out_path.exists() else "overwrite"

        if resume and checkpoint_file.exists():
            checkpoint = self.load_checkpoint(checkpoint_file)
            if checkpoint.records_written != existing_count:
                print(
                    "Warning: CSV and checkpoint disagree. "
                    f"CSV rows={existing_count}, checkpoint rows={checkpoint.records_written}. Preferring CSV for dedupe safety."
                )
                checkpoint.records_written = existing_count
                checkpoint.unique_records_written = len(existing_keys)
        else:
            checkpoint = RBICheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat(),
                oldest_available_date=oldest_available_date.isoformat(),
                years_discovered=sorted(self.count_by_year(preview_records).keys()),
                total_records_detected=len(preview_records),
                count_by_year=self.count_by_year(preview_records),
                count_by_category=self.count_by_category(preview_records),
                chunk_strategy="source_routes",
                last_completed_chunk=0,
                records_written=existing_count,
                unique_records_written=len(existing_keys),
                started_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                completed=False,
                errors=[],
            )
            if not resume:
                if out_path.exists():
                    out_path.unlink()
                if checkpoint_file.exists():
                    checkpoint_file.unlink()
                self.write_output([], out_path, include_category=include_category)
                self.write_metadata_sidecar([], out_path)
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

        window = self.compute_chunk_window(
            total_chunks=len(route_specs),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )

        print(f"total_chunks: {window['total_chunks']}")
        print(f"previous last_completed_chunk: {window['previous_last_completed_chunk']}")
        print(f"resume_from_chunk: {window['resume_from_chunk']}")
        print(f"max_chunks_this_run: {max_chunks_this_run if max_chunks_this_run is not None else window['chunks_this_run']}")
        print(f"expected_end_chunk: {window['expected_end_chunk']}")

        if window["completed"]:
            checkpoint.completed = True
            print("actual chunk range: none")
            print("chunks_processed_this_run: 0")
            print("Rows written: 0")
            print("Duplicates skipped: 0")
            print(f"Final CSV row count: {existing_count}")
            return []

        resume_from_chunk = int(window["resume_from_chunk"])
        expected_end_chunk = int(window["expected_end_chunk"])
        if expected_end_chunk < resume_from_chunk:
            raise RuntimeError(
                f"Invalid chunk range for {self.source}: expected_end_chunk={expected_end_chunk}, "
                f"resume_from_chunk={resume_from_chunk}"
            )

        selected_chunks = route_specs[resume_from_chunk - 1 : expected_end_chunk]
        print(f"actual chunk range: {resume_from_chunk}-{expected_end_chunk}")
        print(f"chunks_processed_this_run: {len(selected_chunks)}")

        written_records: list[RBIRecord] = []
        duplicates_skipped = 0
        previous_last_completed_chunk = checkpoint.last_completed_chunk

        for chunk_index, (_chunk_label, chunk_url) in enumerate(selected_chunks, start=resume_from_chunk):
            chunk_records = self.parse_records_from_html(self.fetch_page_html(chunk_url), chunk_url, source=self.source)
            filtered_records = self.filter_records(chunk_records, from_date=from_date, to_date=to_date)
            if self.source == "rbi-notifications":
                self.populate_reference_numbers(filtered_records, allow_detail_fetch=False)
            fresh_records: list[RBIRecord] = []
            for record in filtered_records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path, include_category=include_category)
            self.append_metadata_sidecar(fresh_records, out_path)
            written_records.extend(fresh_records)

            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=previous_last_completed_chunk,
                new_last_completed_chunk=chunk_index,
            )
            checkpoint.last_completed_chunk = chunk_index
            previous_last_completed_chunk = chunk_index
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = len(existing_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = checkpoint.last_completed_chunk >= len(route_specs)
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        print(f"Rows written: {len(written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(written_records)}")
        if resume or checkpoint_path:
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        self.last_fetch_transport = "httpx"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        prefix = {
            "rbi-notifications": "rbi_notifications",
            "rbi-press-releases": "rbi_press_releases",
            "rbi-master-directions": "rbi_master_directions",
            "rbi-master-circulars": "rbi_master_circulars",
        }[self.source]
        report_path = file_path.parent / f"{prefix}_validation_report.json"
        year_counts_path = file_path.parent / f"{prefix}_year_counts.csv"

        malformed_csv_rows = 0
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        empty_links = 0
        pdf_links = 0
        html_links = 0
        doc_links = 0
        xls_links = 0
        zip_links = 0
        other_links = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        year_counts: dict[int, int] = {}
        dates_seen: list[str] = []

        metadata_rows = self.load_metadata_sidecar(file_path)
        category_counts = self.count_by_category(metadata_rows)

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise RuntimeError(f"RBI export is empty: {file_path}") from exc
            headers_ok = headers == EXPECTED_OUTPUT_HEADERS

            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(EXPECTED_OUTPUT_HEADERS):
                    malformed_csv_rows += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "malformed_csv_row", "row": row})
                    continue
                total_rows += 1
                row_data = dict(zip(EXPECTED_OUTPUT_HEADERS, row, strict=True))
                row_date = row_data["date"].strip()
                subject = row_data["subject"].strip()
                circular_no = row_data["circular_no"].strip()
                link = row_data["link"].strip()

                if not row_date:
                    missing_date += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_date"})
                else:
                    try:
                        parsed_date = date.fromisoformat(row_date)
                        dates_seen.append(row_date)
                        year_counts[parsed_date.year] = year_counts.get(parsed_date.year, 0) + 1
                    except ValueError:
                        missing_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_iso_date", "date": row_date})

                if not subject:
                    missing_subject += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_subject"})
                elif len(subject) < 5:
                    suspicious_rows.append({"row_number": row_number, "reason": "very_short_subject", "subject": subject})
                if self.is_navigation_text(subject):
                    suspicious_rows.append({"row_number": row_number, "reason": "navigation_menu_text", "subject": subject})

                if not circular_no:
                    missing_circular_no += 1

                if not link:
                    missing_link += 1
                    empty_links += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_link"})
                else:
                    link_type = self.detect_link_type(link)
                    if link_type == "pdf":
                        pdf_links += 1
                    elif link_type == "html/detail":
                        html_links += 1
                    elif link_type == "doc/docx":
                        doc_links += 1
                    elif link_type == "xls/xlsx":
                        xls_links += 1
                    elif link_type == "zip":
                        zip_links += 1
                    else:
                        other_links += 1
                    lowered = link.lower()
                    if not any(marker in lowered for marker in VALID_RBI_HOST_MARKERS):
                        suspicious_rows.append({"row_number": row_number, "reason": "non_rbi_link", "link": link})
                    if lowered.startswith("../") or lowered.startswith("./"):
                        suspicious_rows.append({"row_number": row_number, "reason": "broken_relative_link", "link": link})

                dedupe_key = (
                    row_date,
                    (normalize_text(subject or "") or "").lower(),
                    (normalize_text(circular_no or "") or "").lower(),
                    (normalize_text(link or "") or "").lower(),
                )
                if not circular_no:
                    dedupe_key = (
                        row_date,
                        (normalize_text(subject or "") or "").lower(),
                        (normalize_text(link or "") or "").lower(),
                    )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "duplicate_key"})
                else:
                    seen_keys.add(dedupe_key)

        report = {
            "file": str(file_path),
            "source": self.source,
            "headers_ok": headers_ok,
            "expected_headers": EXPECTED_OUTPUT_HEADERS,
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "missing_date_count": missing_date,
            "missing_subject_count": missing_subject,
            "missing_circular_no_count": missing_circular_no,
            "missing_link_count": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": {
                "pdf": pdf_links,
                "html/detail": html_links,
                "doc/docx": doc_links,
                "xls/xlsx": xls_links,
                "zip": zip_links,
                "other": other_links,
                "empty": empty_links,
            },
            "rows_per_year": {str(key): year_counts[key] for key in sorted(year_counts)},
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "count_by_category": category_counts,
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", {str(key): year_counts[key] for key in sorted(year_counts)})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def fetch_page_html(self, url: str) -> str:
        response = self.get(url)
        self.last_fetch_transport = "httpx"
        return response.text

    def inspect_secondary_page(self, url: str, *, fixture_name: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/rbi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        html = self.fetch_page_html(url)
        (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
        probe = self.inspect_route(url=url, html=html, status_code=200, final_url=url)
        self.print_probe(probe)
        return asdict(probe)

    def inspect_simple_source(
        self,
        *,
        source: str,
        url: str,
        fixture_name: str,
        detail_substrings: tuple[str, ...],
        category: str,
    ) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/rbi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        html = self.fetch_page_html(url)
        (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
        records = self.parse_grouped_records(html, url, detail_substrings=detail_substrings, category=category)
        print(f"working endpoint/page-flow: {url}")
        print("request method: GET")
        print("query params/payload: none required for the accessible public listing flow")
        print("cookies/session requirements: none beyond normal public page fetch")
        print("whether Playwright was required: no")
        print("fields available: date, title, detail html link, pdf link")
        print("sample 10 records:")
        for record in records[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)} | {self.console_safe(record.source_url)}"
            )
        return {
            "record_count": len(records),
            "sample_records": [asdict(record) for record in records[:10]],
        }

    def inspect_route(self, *, url: str, html: str, status_code: int, final_url: str) -> RBIRouteProbe:
        soup = BeautifulSoup(html, "html.parser")
        parsed_records = self.parse_records_for_known_route(url, html)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        heading_node = soup.find(["h1", "h2", "h3"])
        heading = heading_node.get_text(" ", strip=True) if heading_node else ""
        selectors = []
        if soup.find(id="lblData"):
            selectors.append("#lblData")
        if soup.find("table"):
            selectors.append("table")
        if soup.find("form"):
            selectors.append("form")
        filters = []
        if soup.find("select", attrs={"name": re.compile("drYear", re.I)}):
            filters.append("year")
        if soup.find("select", attrs={"name": re.compile("drMonth", re.I)}):
            filters.append("month")
        if soup.find("input", attrs={"name": re.compile("strFrom", re.I)}):
            filters.append("from_date")
        if soup.find("input", attrs={"name": re.compile("strTo", re.I)}):
            filters.append("to_date")
        if soup.find("input", attrs={"name": re.compile("txtSearch", re.I)}):
            filters.append("search")
        link_counts = self.count_link_types(parsed_records)
        oldest = min((record.date for record in parsed_records if record.date), default=None)
        probe = RBIRouteProbe(
            url=url,
            status_code=status_code,
            final_url=final_url,
            page_title=title,
            page_heading=heading,
            direct_http_worked=True,
            blocked_or_human_check=self.is_human_check_page(html),
            rows_present=bool(parsed_records),
            commonman_variant_exists="commonman" in url.lower(),
            commonperson_variant_exists="commonperson" in url.lower(),
            old_archive_records_visible=bool(oldest and oldest < "2020-01-01"),
            selectors_found=selectors,
            filters_found=filters,
            pagination_found=self.detect_pagination(html),
            link_type_counts=link_counts,
            first_records=[self.record_preview(item) for item in parsed_records[:10]],
            last_records=[self.record_preview(item) for item in parsed_records[-10:]],
        )
        return probe

    def parse_records_for_known_route(self, url: str, html: str) -> list[RBIRecord]:
        lowered = url.lower()
        if "notificationuser.aspx" in lowered:
            return self.parse_notification_user_records(html, url)
        if "notification.aspx" in lowered:
            return self.parse_grouped_records(
                html,
                url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Notification",
            )
        if "pressreleases.aspx" in lowered:
            return self.parse_grouped_records(
                html,
                url,
                detail_substrings=(RBI_PRESS_RELEASE_DETAIL_PATTERN,),
                category="Press Release",
            )
        if "masterdirection" in lowered or "bs_viewmasterdirections.aspx" in lowered:
            return self.parse_grouped_records(
                html,
                url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Master Direction",
            )
        if "mastercircular" in lowered:
            return self.parse_grouped_records(
                html,
                url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Master Circular",
            )
        if "bs_viewspeeches.aspx" in lowered:
            return self.parse_grouped_records(
                html,
                url,
                detail_substrings=("BS_PressReleaseDisplay.aspx?prid=", "BS_ViewBulletin.aspx", "BS_SpeechesView.aspx", "BS_ViewSpeech.aspx", "SpeechesView.aspx"),
                category="Speech",
            )
        if "faqs.aspx" in lowered:
            return self.parse_faq_inventory_records(html, url)
        return []

    def parse_records_from_html(self, html: str, source_url: str, *, source: str) -> list[RBIRecord]:
        if source == "rbi-notifications" and "NotificationUser.aspx" in source_url:
            return self.parse_notification_user_records(html, source_url)
        if source == "rbi-notifications":
            return self.parse_grouped_records(
                html,
                source_url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Notification",
            )
        if source == "rbi-press-releases":
            return self.parse_grouped_records(
                html,
                source_url,
                detail_substrings=(RBI_PRESS_RELEASE_DETAIL_PATTERN,),
                category="Press Release",
            )
        if source == "rbi-master-directions":
            return self.parse_grouped_records(
                html,
                source_url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Master Direction",
            )
        if source == "rbi-master-circulars":
            return self.parse_grouped_records(
                html,
                source_url,
                detail_substrings=(RBI_NOTIFICATION_DETAIL_PATTERN,),
                category="Master Circular",
            )
        return []

    def parse_notification_user_records(self, html: str, source_url: str) -> list[RBIRecord]:
        soup = BeautifulSoup(html, "html.parser")
        current_date = ""
        records: list[RBIRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for tr in soup.find_all("tr"):
            date_td = tr.find("td", class_="tableheader")
            if date_td:
                current_date = self.normalize_rbi_date(date_td.get_text(" ", strip=True)) or current_date
                continue
            detail_anchor = tr.find("a", href=lambda href: href and RBI_NOTIFICATION_USER_DETAIL_PATTERN.lower() in href.lower())
            if detail_anchor is None:
                continue
            subject = normalize_text(detail_anchor.get_text(" ", strip=True)) or ""
            detail_url = urljoin(source_url, detail_anchor.get("href", ""))
            pdf_anchor = tr.find("a", href=lambda href: href and ".pdf" in href.lower())
            pdf_url = urljoin(source_url, pdf_anchor.get("href", "")) if pdf_anchor else ""
            circular_no = self.extract_reference_no_from_text(subject)
            records.append(
                RBIRecord(
                    date=current_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=detail_url or pdf_url,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category="Notification",
                    raw_date=current_date,
                    detail_url=detail_url,
                    pdf_url=pdf_url,
                )
            )
        return [record for record in records if record.date and record.subject and record.link]

    def parse_grouped_records(
        self,
        html: str,
        source_url: str,
        *,
        detail_substrings: tuple[str, ...],
        category: str,
    ) -> list[RBIRecord]:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find(id="lblData") or soup
        current_date = ""
        records: list[RBIRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for tr in container.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            if len(tds) == 1 and ("tableheader" in " ".join(tds[0].get("class", [])) or "textHead" in " ".join(tds[0].get("class", []))):
                candidate_date = self.normalize_rbi_date(tds[0].get_text(" ", strip=True))
                if candidate_date:
                    current_date = candidate_date
                continue
            anchors = tr.find_all("a", href=True)
            detail_anchor = None
            pdf_anchor = None
            for anchor in anchors:
                href = anchor.get("href", "")
                absolute_href = urljoin(source_url, href)
                if self.detect_link_type(absolute_href) == "pdf":
                    pdf_anchor = anchor
                    continue
                if any(token.lower() in href.lower() for token in detail_substrings):
                    detail_anchor = anchor
            if detail_anchor is None and pdf_anchor is None:
                continue
            subject_anchor = detail_anchor or pdf_anchor
            subject = normalize_text(subject_anchor.get_text(" ", strip=True) if subject_anchor else "")
            if not subject:
                text_cells = [normalize_text(td.get_text(" ", strip=True)) or "" for td in tds]
                subject = next((value for value in text_cells if value and value != current_date), "")
            detail_url = urljoin(source_url, detail_anchor.get("href", "")) if detail_anchor else ""
            pdf_url = urljoin(source_url, pdf_anchor.get("href", "")) if pdf_anchor else ""
            circular_no = self.extract_reference_no_from_text(subject)
            records.append(
                RBIRecord(
                    date=current_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=detail_url or pdf_url,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category=category,
                    raw_date=current_date,
                    detail_url=detail_url,
                    pdf_url=pdf_url,
                )
            )
        return [record for record in records if record.date and record.subject and record.link]

    def parse_faq_inventory_records(self, html: str, source_url: str) -> list[RBIRecord]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[RBIRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for anchor in soup.find_all("a", href=True):
            text = normalize_text(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")
            if not text or not href:
                continue
            if len(text) < 5:
                continue
            if "FAQ" not in text and "/FAQs" not in href and "faq" not in href.lower():
                continue
            records.append(
                RBIRecord(
                    date="",
                    subject=text,
                    circular_no="",
                    link=urljoin(source_url, href),
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category="FAQ",
                )
            )
        return records

    def collect_records_for_source(self, source: str, *, enrich_reference_numbers: bool = True) -> list[RBIRecord]:
        route_specs = RBI_SCRAPE_ROUTES[source]
        collected: list[RBIRecord] = []
        for _label, route_url in route_specs:
            html = self.fetch_page_html(route_url)
            records = self.parse_records_from_html(html, route_url, source=source)
            if source == "rbi-notifications" and enrich_reference_numbers:
                self.populate_reference_numbers(records)
            collected.extend(records)
        return self.deduplicate_records(collected)

    def populate_reference_numbers(self, records: list[RBIRecord], *, allow_detail_fetch: bool = True) -> None:
        for record in records:
            if record.circular_no:
                continue
            candidate = self.extract_reference_no_from_text(record.subject)
            if candidate:
                record.circular_no = candidate
                continue
            if allow_detail_fetch and record.detail_url:
                reference = self.extract_reference_from_detail(record.detail_url)
                if reference:
                    record.circular_no = reference

    def extract_reference_from_detail(self, detail_url: str) -> str:
        if detail_url in self._detail_reference_cache:
            return self._detail_reference_cache[detail_url]
        try:
            html = self.fetch_page_html(detail_url)
        except Exception:
            self._detail_reference_cache[detail_url] = ""
            return ""
        soup = BeautifulSoup(html, "html.parser")
        reference = ""
        for token in soup.stripped_strings:
            normalized = normalize_text(token) or ""
            if len(normalized) > 180:
                continue
            reference = self.extract_reference_no_from_text(normalized)
            if reference:
                break
        self._detail_reference_cache[detail_url] = reference
        return reference

    def extract_reference_no_from_text(self, text: str) -> str:
        normalized = normalize_text(text) or ""
        if not normalized:
            return ""
        compact = normalized.replace("–", "-").replace("—", "-")
        patterns = [
            re.compile(r"\bRBI/\d{4}-\d{2}/\d+\s+[A-Z][A-Za-z0-9./()\-]+(?:/[A-Za-z0-9./()\-]+)*"),
            re.compile(r"\bA\.?\s*P\.?\s*\(DIR Series\)\s*Circular\s*No\.?\s*[A-Za-z0-9./()\- ]+", re.I),
            re.compile(r"\b(?:Circular|Notification)\s*No\.?\s*[A-Za-z0-9./()\- ]+", re.I),
            re.compile(r"\bF\.\s*No\.?\s*[A-Za-z0-9./()\-]+", re.I),
            re.compile(r"\bS\.?O\.?\s*[A-Za-z0-9./()\-]+", re.I),
            re.compile(r"\bG\.?S\.?R\.?\s*[A-Za-z0-9./()\-]+", re.I),
            re.compile(r"\b(?:DOR|DBR|DBOD|FMRD|CO\.DPSS|CO\.IDMD|RPCD|DNBR|DGS|DMD)\.[A-Za-z0-9./()\-]+", re.I),
        ]
        for pattern in patterns:
            match = pattern.search(compact)
            if match:
                return normalize_text(match.group(0)) or ""
        return ""

    def print_discovery_result(
        self,
        *,
        source_label: str,
        working_flow: str,
        records: list[RBIRecord],
        count_by_category: dict[str, int],
        limitation: str,
    ) -> dict[str, Any]:
        if not records:
            raise RuntimeError(f"No RBI {source_label} rows discovered")
        records = self.deduplicate_records(records)
        valid_dates = [date.fromisoformat(record.date) for record in records if record.date]
        count_by_year = self.count_by_year(records)
        earliest_records = sorted(records, key=lambda item: (item.date, item.subject, item.link))[:10]
        result = {
            "working_flow": working_flow,
            "newest_date": max(valid_dates).isoformat(),
            "oldest_date": min(valid_dates).isoformat(),
            "total_count": len(records),
            "count_by_year": count_by_year,
            "count_by_category": count_by_category,
            "earliest_records": [asdict(item) for item in earliest_records],
            "limitation": limitation,
        }
        print(f"working route/page-flow: {working_flow}")
        print(f"newest date found: {result['newest_date']}")
        print(f"oldest date found: {result['oldest_date']}")
        print(f"total count: {result['total_count']}")
        print(f"count by year: {json.dumps(count_by_year, indent=2)}")
        print(f"count by category/department if visible: {json.dumps(count_by_category, indent=2)}")
        print("sample earliest 10 records:")
        for record in earliest_records:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        print(f"limitation: {limitation}")
        return result

    def filter_records(self, records: list[RBIRecord], *, from_date: date, to_date: date) -> list[RBIRecord]:
        filtered: list[RBIRecord] = []
        for record in records:
            try:
                row_date = date.fromisoformat(record.date)
            except ValueError:
                continue
            if row_date < from_date or row_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def deduplicate_records(self, records: list[RBIRecord]) -> list[RBIRecord]:
        seen: set[tuple[str, ...]] = set()
        output: list[RBIRecord] = []
        for record in records:
            key = self.record_dedup_key(record)
            if key in seen:
                continue
            seen.add(key)
            output.append(record)
        return output

    def record_dedup_key(self, record: RBIRecord) -> tuple[str, ...]:
        normalized_subject = (normalize_text(record.subject) or "").lower()
        normalized_link = (normalize_text(record.link) or "").lower()
        normalized_ref = (normalize_text(record.circular_no) or "").lower()
        if normalized_ref:
            return (record.date, normalized_subject, normalized_ref, normalized_link)
        return (record.date, normalized_subject, normalized_link)

    def count_by_year(self, records: list[RBIRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year_key = record.date[:4]
            counts[year_key] = counts.get(year_key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def count_by_category(self, records: list[RBIRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            key = record.category or self.source_label
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def count_link_types(self, records: list[RBIRecord]) -> dict[str, int]:
        counts = {
            "pdf": 0,
            "html/detail": 0,
            "doc/docx": 0,
            "xls/xlsx": 0,
            "zip": 0,
            "other": 0,
            "empty": 0,
        }
        for record in records:
            link_type = self.detect_link_type(record.link)
            counts[link_type] = counts.get(link_type, 0) + 1
        return counts

    def detect_link_type(self, value: str) -> str:
        lowered_full = (value or "").lower()
        lowered = lowered_full.split("?", 1)[0]
        if not lowered_full:
            return "empty"
        if lowered.endswith(".pdf"):
            return "pdf"
        if lowered.endswith(".doc") or lowered.endswith(".docx"):
            return "doc/docx"
        if lowered.endswith(".xls") or lowered.endswith(".xlsx"):
            return "xls/xlsx"
        if lowered.endswith(".zip"):
            return "zip"
        if "notification.aspx?id=" in lowered_full or "notificationuser.aspx?id=" in lowered_full or "pressreleases.aspx?id=" in lowered_full or "/scripts/bs_pressreleasedisplay.aspx" in lowered_full or "/w/" in lowered_full:
            return "html/detail"
        return "other"

    def detect_pagination(self, html: str) -> list[str]:
        markers: list[str] = []
        lowered = html.lower()
        if "__dopostback" in lowered:
            markers.append("__doPostBack")
        if "prev" in lowered and "next" in lowered:
            markers.append("prev_next_text")
        if "page" in lowered and "paging" in lowered:
            markers.append("page_text")
        return markers

    def normalize_rbi_date(self, raw_value: str) -> str:
        normalized = normalize_text(raw_value) or ""
        if not normalized:
            return ""
        normalized = normalized.replace("Feburary", "February").replace("Sept ", "September ")
        normalized = normalized.replace("Apr ", "April ").replace("Aug ", "August ")
        parsed = parse_indian_date(normalized)
        return parsed.isoformat() if parsed else ""

    def is_human_check_page(self, html: str) -> bool:
        lowered = html.lower().lstrip()
        return lowered.startswith("617") or 'id="f5_cspm"' in lowered

    def is_navigation_text(self, text: str) -> bool:
        lowered = (normalize_text(text) or "").lower()
        if not lowered:
            return False
        navigation_markers = [
            "home about us notifications notifications master directions",
            "selected selected language search the website",
            "site map contact us disclaimer",
        ]
        return any(marker in lowered for marker in navigation_markers)

    def record_preview(self, record: RBIRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
        }

    def print_probe(self, probe: RBIRouteProbe) -> None:
        print(f"URL: {probe.url}")
        print(f"HTTP status: {probe.status_code}")
        print(f"final URL after redirects: {probe.final_url}")
        print(f"page title: {self.console_safe(probe.page_title)}")
        print(f"page heading: {self.console_safe(probe.page_heading)}")
        print(f"whether direct HTTP worked: {probe.direct_http_worked}")
        print(f"whether page is blocked/human-check/anti-spam: {probe.blocked_or_human_check}")
        print(f"whether records are present in raw HTML: {probe.rows_present}")
        print(f"table/list/card selectors found: {probe.selectors_found}")
        print(f"year/date/category/filter controls found: {probe.filters_found}")
        print(f"pagination/archive controls found: {probe.pagination_found}")
        print(f"whether commonman/commonperson variant exists: commonman={probe.commonman_variant_exists}, commonperson={probe.commonperson_variant_exists}")
        print(f"whether page exposes old archive records: {probe.old_archive_records_visible}")
        print("first 10 listed rows if present:")
        for record in probe.first_records:
            print(
                f"- {self.console_safe(record['date'])} | {self.console_safe(record['subject'])} | "
                f"{self.console_safe(record['circular_no'])} | {self.console_safe(record['link'])}"
            )
        print("last 10 listed rows if present:")
        for record in probe.last_records:
            print(
                f"- {self.console_safe(record['date'])} | {self.console_safe(record['subject'])} | "
                f"{self.console_safe(record['circular_no'])} | {self.console_safe(record['link'])}"
            )
        print(f"link type counts: {probe.link_type_counts}")
        if probe.error:
            print(f"error: {probe.error}")
        print("---")

    def metadata_sidecar_path(self, out_path: str | Path) -> Path:
        return Path(f"{out_path}.meta.json")

    def load_existing_output_records(self, out_path: str | Path) -> list[RBIRecord]:
        out_path = Path(out_path)
        metadata_rows = self.load_metadata_sidecar(out_path)
        metadata_by_index = {index: item for index, item in enumerate(metadata_rows)}
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                records: list[RBIRecord] = []
                for index, row in enumerate(reader):
                    metadata = metadata_by_index.get(index)
                    records.append(
                        RBIRecord(
                            date=row["date"],
                            subject=row["subject"],
                            circular_no=row.get("circular_no", ""),
                            link=row["link"],
                            source_url=row["source_url"],
                            scraped_at=row["scraped_at"],
                            category=metadata.category if metadata else "",
                        )
                    )
                return records
        if out_path.suffix.lower() == ".json":
            items = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                RBIRecord(
                    date=item["date"],
                    subject=item["subject"],
                    circular_no=item.get("circular_no", ""),
                    link=item["link"],
                    source_url=item["source_url"],
                    scraped_at=item["scraped_at"],
                    category=item.get("category", ""),
                )
                for item in items
            ]
        raise ValueError("Output path must end with .csv or .json")

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool) -> None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists():
                with open(path, "a", encoding="utf-8"):
                    pass
            elif not resume:
                with open(path, "a", encoding="utf-8"):
                    pass
                path.unlink(missing_ok=True)
        except PermissionError as exc:
            raise RuntimeError("Output file is locked. Close Excel/VS Code/OneDrive preview and rerun.") from exc

    def append_output(self, records: list[RBIRecord], out_path: str | Path, *, include_category: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_category=include_category) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_category else EXPECTED_OUTPUT_HEADERS
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            existing = []
            if out_path.exists():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend(rows)
            out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def write_output(self, records: list[RBIRecord], out_path: str | Path, *, include_category: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_category=include_category) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_category else EXPECTED_OUTPUT_HEADERS
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def record_to_output_row(self, record: RBIRecord, *, include_category: bool = False) -> dict[str, str]:
        row = {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_category:
            return {"date": record.date, "category": record.category, **{key: value for key, value in row.items() if key != "date"}}
        return row

    def append_metadata_sidecar(self, records: list[RBIRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        existing = []
        if sidecar_path.exists():
            existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
        existing.extend([asdict(item) for item in records])
        sidecar_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_metadata_sidecar(self, records: list[RBIRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        sidecar_path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")

    def load_metadata_sidecar(self, out_path: str | Path) -> list[RBIRecord]:
        sidecar_path = self.metadata_sidecar_path(out_path)
        if not sidecar_path.exists():
            return []
        return [RBIRecord(**item) for item in json.loads(sidecar_path.read_text(encoding="utf-8"))]

    def write_count_csv(self, path: str | Path, label: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([label, "row_count"])
            for key, value in counts.items():
                writer.writerow([key, value])

    def load_checkpoint(self, checkpoint_path: str | Path) -> RBICheckpoint:
        return RBICheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: RBICheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")

    def console_safe(self, text: str) -> str:
        return (text or "").encode("ascii", errors="replace").decode("ascii")


class RBINotificationsScraper(RBIScraper):
    source = "rbi-notifications"


class RBIPressReleasesScraper(RBIScraper):
    source = "rbi-press-releases"


class RBIMasterDirectionsScraper(RBIScraper):
    source = "rbi-master-directions"


class RBIMasterCircularsScraper(RBIScraper):
    source = "rbi-master-circulars"
