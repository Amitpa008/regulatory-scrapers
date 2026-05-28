from __future__ import annotations

import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


PFRDA_BASE_URL = "https://www.pfrda.org.in"
PFRDA_HOME_URL = f"{PFRDA_BASE_URL}/"
PFRDA_RECENT_UPDATES_URL = f"{PFRDA_BASE_URL}/web/pfrda/recent-updates"
PFRDA_RECENT_UPDATES_ALT_URL = f"{PFRDA_BASE_URL}/recent-updates"
PFRDA_CIRCULARS_ACTIVE_URL = f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/circulars/active-circulars"
PFRDA_CIRCULARS_INOPERATIVE_URL = f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/circulars/inoperative"
PFRDA_MASTER_CIRCULARS_ACTIVE_URL = (
    f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/master-circulars/active-master-circulars"
)
PFRDA_NOTIFICATIONS_URL = f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/notifications"
PFRDA_REGULATIONS_URL = f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/regulations"
PFRDA_GUIDELINES_URL = f"{PFRDA_BASE_URL}/web/pfrda/regulatory-framework/guidelines"
PFRDA_PRESS_RELEASES_URL = f"{PFRDA_BASE_URL}/web/pfrda/media/press-releases"
PFRDA_TENDERS_URL = f"{PFRDA_BASE_URL}/web/pfrda/tenders"

EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_OUTPUT_HEADERS = ["date", "category", "subject", "circular_no", "link", "source_url", "scraped_at"]
VALID_PFRDA_HOST_MARKERS = ("pfrda.org.in",)

PFRDA_SOURCE_LABELS = {
    "pfrda-recent-updates": "Recent Updates",
    "pfrda-circulars-active": "Active Circulars",
    "pfrda-circulars-inoperative": "Archived Circulars",
    "pfrda-master-circulars-active": "Active Master Circulars",
    "pfrda-notifications": "Notifications",
    "pfrda-regulations": "Regulations",
    "pfrda-guidelines": "Guidelines",
    "pfrda-press-releases": "Press Releases",
    "pfrda-tenders": "Tenders",
}

PFRDA_PRIMARY_LISTING_URLS = {
    "pfrda-recent-updates": PFRDA_RECENT_UPDATES_URL,
    "pfrda-circulars-active": PFRDA_CIRCULARS_ACTIVE_URL,
    "pfrda-circulars-inoperative": PFRDA_CIRCULARS_INOPERATIVE_URL,
    "pfrda-master-circulars-active": PFRDA_MASTER_CIRCULARS_ACTIVE_URL,
    "pfrda-notifications": PFRDA_NOTIFICATIONS_URL,
    "pfrda-regulations": PFRDA_REGULATIONS_URL,
    "pfrda-guidelines": PFRDA_GUIDELINES_URL,
    "pfrda-press-releases": PFRDA_PRESS_RELEASES_URL,
    "pfrda-tenders": PFRDA_TENDERS_URL,
}

PFRDA_SCOUT_ROUTES = [
    ("home.html", PFRDA_HOME_URL),
    ("recent_updates.html", PFRDA_RECENT_UPDATES_URL),
    ("recent_updates_alt.html", PFRDA_RECENT_UPDATES_ALT_URL),
    ("active_circulars.html", PFRDA_CIRCULARS_ACTIVE_URL),
    ("inoperative_circulars.html", PFRDA_CIRCULARS_INOPERATIVE_URL),
    ("active_master_circulars.html", PFRDA_MASTER_CIRCULARS_ACTIVE_URL),
    ("notifications.html", PFRDA_NOTIFICATIONS_URL),
    ("regulations.html", PFRDA_REGULATIONS_URL),
    ("guidelines.html", PFRDA_GUIDELINES_URL),
    ("press_releases.html", PFRDA_PRESS_RELEASES_URL),
    ("tenders.html", PFRDA_TENDERS_URL),
]


@dataclass
class PFRDARecord:
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
    valid_till: str = ""


@dataclass
class PFRDARouteProbe:
    url: str
    status_code: int | None
    final_url: str
    page_title: str
    page_heading: str
    direct_http_worked: bool
    records_present_in_raw_html: bool
    liferay_or_portal_rendered: bool
    selectors_found: list[str]
    filters_found: list[str]
    pagination_found: list[str]
    first_records: list[dict[str, str]]
    last_records: list[dict[str, str]]
    link_type_counts: dict[str, int]
    error: str | None = None


@dataclass
class PFRDACheckpoint:
    source_url: str
    output_path: str
    newest_available_date: str | None
    oldest_available_date: str | None
    years_discovered: list[str]
    total_records_detected: int | None
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


class PFRDAScraper(BaseScraper):
    source: str
    regulator = "PFRDA"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"
        self._detail_cache: dict[str, dict[str, str]] = {}

    @property
    def source_label(self) -> str:
        return PFRDA_SOURCE_LABELS[self.source]

    @property
    def listing_url(self) -> str:
        return PFRDA_PRIMARY_LISTING_URLS[self.source]

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(self.listing_url)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_cards(str(response), self.listing_url, default_category=self.source_label)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": self.source_label.rstrip("s"),
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date) if record.date else None,
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

    def fetch_page_html(self, url: str) -> str:
        response = self.get(url)
        self.last_fetch_transport = "httpx"
        return response.text

    def scout_site(self, base_url: str) -> list[PFRDARouteProbe]:
        del base_url
        fixture_dir = Path("tests/fixtures/pfrda")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        probes: list[PFRDARouteProbe] = []
        for fixture_name, url in PFRDA_SCOUT_ROUTES:
            try:
                response = self.get(url)
                html = response.text
                (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
                probe = self.inspect_route(url=url, html=html, status_code=response.status_code, final_url=str(response.url))
            except Exception as exc:  # pragma: no cover - live failure
                probe = PFRDARouteProbe(
                    url=url,
                    status_code=None,
                    final_url=url,
                    page_title="",
                    page_heading="",
                    direct_http_worked=False,
                    records_present_in_raw_html=False,
                    liferay_or_portal_rendered=False,
                    selectors_found=[],
                    filters_found=[],
                    pagination_found=[],
                    first_records=[],
                    last_records=[],
                    link_type_counts={},
                    error=str(exc),
                )
            probes.append(probe)
            self.print_probe(probe)
        return probes

    def inspect_recent_updates(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="recent_updates.html")

    def inspect_circulars_active(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="active_circulars.html")

    def inspect_circulars_inoperative(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="inoperative_circulars.html")

    def inspect_master_circulars_active(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="active_master_circulars.html")

    def inspect_notifications(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="notifications.html")

    def inspect_regulations(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="regulations.html")

    def inspect_guidelines(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="guidelines.html")

    def inspect_press_releases(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="press_releases.html")

    def inspect_source(self, url: str, *, fixture_name: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/pfrda")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        html = self.fetch_page_html(url)
        (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
        records = self.parse_cards(html, url, default_category=self.source_label)
        for record in records[:3]:
            self.enrich_from_detail(record)
        print(f"working endpoint/page-flow: {url}")
        print("request method: GET")
        print("query params/payload: public query params include delta/start and page filter widgets in raw HTML")
        print("cookies/session requirements: none beyond normal public page fetch")
        print("whether Playwright was required: no")
        print("fields available: date, title, stakeholder/category tags, reference number when present, detail link")
        print("sample 10 records:")
        for record in records[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)} | {self.console_safe(record.source_url)}"
            )
        return {"record_count": len(records), "sample_records": [asdict(item) for item in records[:10]]}

    def discover_recent_updates_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("recent updates", url, self.collect_records_for_source("pfrda-recent-updates"))

    def discover_circulars_active_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("active circulars", url, self.collect_records_for_source("pfrda-circulars-active"))

    def discover_circulars_inoperative_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result(
            "archived circulars",
            url,
            self.collect_records_for_source("pfrda-circulars-inoperative"),
        )

    def discover_master_circulars_active_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result(
            "active master circulars",
            url,
            self.collect_records_for_source("pfrda-master-circulars-active"),
        )

    def discover_notifications_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("notifications", url, self.collect_records_for_source("pfrda-notifications"))

    def discover_regulations_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("regulations", url, self.collect_records_for_source("pfrda-regulations"))

    def discover_guidelines_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("guidelines", url, self.collect_records_for_source("pfrda-guidelines"))

    def discover_press_releases_range(self, url: str) -> dict[str, Any]:
        return self.print_discovery_result("press releases", url, self.collect_records_for_source("pfrda-press-releases"))

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
    ) -> list[PFRDARecord]:
        del retries, retry_base_delay, retry_max_delay
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        first_html = self.fetch_page_html(url)
        preview_records = self.parse_cards(first_html, url, default_category=self.source_label)
        total_records = self.parse_total_entries(first_html) or len(preview_records)
        total_pages = self.compute_total_pages(total_records, delta=10)
        if not preview_records:
            raise RuntimeError(f"{self.source} returned zero rows")

        preview_page_records = self.collect_records_for_source(self.source, first_page_html=first_html, enrich_details=False)
        valid_dates = [date.fromisoformat(record.date) for record in preview_page_records if record.date]
        newest_available_date = max(valid_dates) if valid_dates else None
        oldest_available_date = min(valid_dates) if valid_dates else None
        if all_available and from_date is None and oldest_available_date is not None:
            from_date = oldest_available_date
        if from_date is None:
            from_date = oldest_available_date
        if to_date is None:
            to_date = newest_available_date

        existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
        existing_keys = {self.record_dedup_key(item) for item in existing_records}
        existing_count = len(existing_records)

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
            checkpoint = PFRDACheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat() if newest_available_date else None,
                oldest_available_date=oldest_available_date.isoformat() if oldest_available_date else None,
                years_discovered=sorted(self.count_by_year(preview_page_records).keys()),
                total_records_detected=total_records,
                count_by_year=self.count_by_year(preview_page_records),
                count_by_category=self.count_by_category(preview_page_records),
                chunk_strategy="portal_pagination",
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
            total_chunks=total_pages,
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

        print(f"actual chunk range: {resume_from_chunk}-{expected_end_chunk}")
        print(f"chunks_processed_this_run: {expected_end_chunk - resume_from_chunk + 1}")

        written_records: list[PFRDARecord] = []
        duplicates_skipped = 0
        previous_last_completed_chunk = checkpoint.last_completed_chunk

        for page_number in range(resume_from_chunk, expected_end_chunk + 1):
            page_url = self.build_page_url(url, page_number=page_number, delta=10)
            html = first_html if page_number == 1 else self.fetch_page_html(page_url)
            page_records = self.parse_cards(html, page_url, default_category=self.source_label)
            filtered_records = self.filter_records(page_records, from_date=from_date, to_date=to_date)
            fresh_records: list[PFRDARecord] = []
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
                new_last_completed_chunk=page_number,
            )
            checkpoint.last_completed_chunk = page_number
            previous_last_completed_chunk = page_number
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = len(existing_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = checkpoint.last_completed_chunk >= total_pages
            checkpoint.total_records_detected = total_records
            checkpoint.count_by_year = self.count_by_year(preview_page_records)
            checkpoint.count_by_category = self.count_by_category(preview_page_records)
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
        prefix = self.source.replace("-", "_")
        report_path = file_path.parent / f"{prefix}_validation_report.json"
        year_counts_path = file_path.parent / f"{prefix}_year_counts.csv"
        category_counts_path = file_path.parent / f"{prefix}_category_counts.csv"

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
                raise RuntimeError(f"PFRDA export is empty: {file_path}") from exc
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
                    if not any(marker in lowered for marker in VALID_PFRDA_HOST_MARKERS):
                        suspicious_rows.append({"row_number": row_number, "reason": "non_pfrda_link", "link": link})

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
        if category_counts:
            self.write_count_csv(category_counts_path, "category", category_counts)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def parse_cards(self, html: str, source_url: str, *, default_category: str) -> list[PFRDARecord]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.basic-card")
        records: list[PFRDARecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for card in cards:
            anchor = card.select_one("a.basic-link[href]")
            title_node = card.select_one(".basic-title")
            if anchor is None or title_node is None:
                continue
            link = canonicalize_pfrda_url(urljoin(source_url, anchor.get("href", "").strip()))
            subject = clean_pfrda_text(normalize_text(title_node.get_text(" ", strip=True)) or "")
            if not subject or self.is_navigation_text(subject):
                continue
            tags = [clean_pfrda_text(normalize_text(tag.get_text(" ", strip=True)) or "") for tag in card.select(".basic-tag")]
            tags = [tag for tag in tags if tag]
            category = " | ".join(tags) if tags else default_category
            circular_no = ""
            raw_date = ""
            valid_till = ""
            for meta_item in card.select(".meta-item"):
                text = clean_pfrda_text(normalize_text(meta_item.get_text(" ", strip=True)) or "")
                if text.lower().startswith("ref:"):
                    circular_no = clean_pfrda_reference(text.split(":", 1)[1].strip())
                elif text.lower().startswith("reference number:"):
                    circular_no = clean_pfrda_reference(text.split(":", 1)[1].strip())
                elif text.lower().startswith("issue date:"):
                    raw_date = text.split(":", 1)[1].strip()
                elif text.lower().startswith("published on:"):
                    raw_date = text.split(":", 1)[1].strip()
                elif text.lower().startswith("release date:"):
                    raw_date = text.split(":", 1)[1].strip()
                elif text.lower().startswith("valid till:"):
                    valid_till = text.split(":", 1)[1].strip()
            if not circular_no:
                circular_no = self.extract_reference_no_from_text(subject)
            normalized_date = normalize_pfrda_date(raw_date)
            records.append(
                PFRDARecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category=category,
                    raw_date=raw_date,
                    detail_url=link,
                    valid_till=valid_till,
                )
            )
        return self.deduplicate_records(records)

    def enrich_from_detail(self, record: PFRDARecord) -> PFRDARecord:
        if not record.detail_url or not record.link or "/w/" not in record.link:
            return record
        if record.detail_url in self._detail_cache:
            cached = self._detail_cache[record.detail_url]
        else:
            try:
                html = self.fetch_page_html(record.detail_url)
            except Exception:
                self._detail_cache[record.detail_url] = {}
                return record
            cached = self.parse_detail_metadata(html, record.detail_url)
            self._detail_cache[record.detail_url] = cached
        if cached.get("date") and not record.date:
            record.date = cached["date"]
        if cached.get("circular_no") and not record.circular_no:
            record.circular_no = cached["circular_no"]
        if cached.get("pdf_url"):
            record.pdf_url = cached["pdf_url"]
        if cached.get("category") and record.category == self.source_label:
            record.category = cached["category"]
        return record

    def parse_detail_metadata(self, html: str, detail_url: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        body_text = clean_pfrda_text(normalize_text(soup.get_text(" ", strip=True)) or "")
        pdf_url = ""
        for anchor in soup.find_all("a", href=True):
            text = clean_pfrda_text(normalize_text(anchor.get_text(" ", strip=True)) or "")
            href = canonicalize_pfrda_url(urljoin(detail_url, anchor["href"]))
            if "pdf" in text.lower() or href.lower().endswith(".pdf") or "/documents/" in href.lower():
                pdf_url = href
                break
        category_bits = [clean_pfrda_text(normalize_text(item.get_text(" ", strip=True)) or "") for item in soup.select(".badge, .basic-tag")]
        category_bits = [item for item in category_bits if item]
        raw_date = extract_labeled_value(body_text, ("Published on:", "Issue Date:", "Release Date:"))
        circular_no = extract_labeled_value(body_text, ("Reference Number:", "Ref:", "Notification No.:", "Circular No.:"))
        if not circular_no:
            circular_no = self.extract_reference_no_from_text(body_text)
        return {
            "date": normalize_pfrda_date(raw_date),
            "circular_no": clean_pfrda_reference(circular_no),
            "pdf_url": pdf_url,
            "category": " | ".join(category_bits),
        }

    def collect_records_for_source(
        self,
        source: str,
        *,
        first_page_html: str | None = None,
        enrich_details: bool = False,
    ) -> list[PFRDARecord]:
        route_url = PFRDA_PRIMARY_LISTING_URLS[source]
        html = first_page_html if first_page_html is not None else self.fetch_page_html(route_url)
        first_page_records = self.parse_cards(html, route_url, default_category=PFRDA_SOURCE_LABELS[source])
        total_records = self.parse_total_entries(html) or len(first_page_records)
        total_pages = self.compute_total_pages(total_records, delta=10)
        collected: list[PFRDARecord] = list(first_page_records)
        for page_number in range(2, total_pages + 1):
            page_url = self.build_page_url(route_url, page_number=page_number, delta=10)
            page_html = self.fetch_page_html(page_url)
            collected.extend(self.parse_cards(page_html, page_url, default_category=PFRDA_SOURCE_LABELS[source]))
        deduped = self.deduplicate_records(collected)
        if enrich_details:
            for record in deduped:
                self.enrich_from_detail(record)
        return deduped

    def inspect_route(self, *, url: str, html: str, status_code: int, final_url: str) -> PFRDARouteProbe:
        soup = BeautifulSoup(html, "html.parser")
        records = self.parse_cards(html, final_url, default_category=self.source_label)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        heading_node = soup.find(["h1", "h2", "h3"])
        heading = heading_node.get_text(" ", strip=True) if heading_node else ""
        probe = PFRDARouteProbe(
            url=url,
            status_code=status_code,
            final_url=final_url,
            page_title=title,
            page_heading=heading,
            direct_http_worked=True,
            records_present_in_raw_html=bool(records),
            liferay_or_portal_rendered=self.is_liferay_portal_page(html),
            selectors_found=self.detect_selectors(soup),
            filters_found=self.detect_filters(html),
            pagination_found=self.detect_pagination(html),
            first_records=[self.record_preview(item) for item in records[:10]],
            last_records=[self.record_preview(item) for item in records[-10:]],
            link_type_counts=self.count_link_types(records),
        )
        return probe

    def print_probe(self, probe: PFRDARouteProbe) -> None:
        print(f"URL: {probe.url}")
        print(f"HTTP status: {probe.status_code}")
        print(f"final URL after redirects: {probe.final_url}")
        print(f"page title: {self.console_safe(probe.page_title)}")
        print(f"page heading: {self.console_safe(probe.page_heading)}")
        print(f"whether direct HTTP worked: {probe.direct_http_worked}")
        print(f"whether records are present in raw HTML: {probe.records_present_in_raw_html}")
        print(f"whether page is Liferay/portal rendered: {probe.liferay_or_portal_rendered}")
        print(f"table/list/card selectors found: {probe.selectors_found}")
        print(f"filters found: {probe.filters_found}")
        print(f"pagination controls found: {probe.pagination_found}")
        print("first 10 listed records if present:")
        for record in probe.first_records:
            print(
                f"- {self.console_safe(record['date'])} | {self.console_safe(record['subject'])} | "
                f"{self.console_safe(record['circular_no'])} | {self.console_safe(record['link'])}"
            )
        print("last 10 listed records if present:")
        for record in probe.last_records:
            print(
                f"- {self.console_safe(record['date'])} | {self.console_safe(record['subject'])} | "
                f"{self.console_safe(record['circular_no'])} | {self.console_safe(record['link'])}"
            )
        print(f"link type counts: {probe.link_type_counts}")
        if probe.error:
            print(f"error: {probe.error}")
        print("---")

    def print_discovery_result(self, label: str, working_flow: str, records: list[PFRDARecord]) -> dict[str, Any]:
        year_counts = self.count_by_year(records)
        category_counts = self.count_by_category(records)
        valid_dates = [record.date for record in records if record.date]
        payload = {
            "working_flow": working_flow,
            "newest_date": max(valid_dates) if valid_dates else None,
            "oldest_date": min(valid_dates) if valid_dates else None,
            "total_count": len(records),
            "count_by_year": year_counts,
            "count_by_category": category_counts,
            "sample_earliest_10": [asdict(item) for item in sorted(records, key=lambda item: item.date)[:10]],
        }
        print(f"working route/page-flow: {working_flow}")
        print(f"newest date found: {payload['newest_date']}")
        print(f"oldest date found: {payload['oldest_date']}")
        print(f"total count: {payload['total_count']}")
        print(f"count by year: {json.dumps(year_counts, indent=2)}")
        print(f"count by stakeholder/category if visible: {json.dumps(category_counts, indent=2)}")
        print("sample earliest 10 records:")
        for record in payload["sample_earliest_10"]:
            print(
                f"- {self.console_safe(record['date'])} | {self.console_safe(record['subject'])} | "
                f"{self.console_safe(record['circular_no'])} | {self.console_safe(record['link'])}"
            )
        print("limitation: Public direct HTTP HTML already exposes the paginated records, so no hidden API was required.")
        return payload

    def build_page_url(self, base_url: str, *, page_number: int, delta: int) -> str:
        if page_number <= 1:
            return base_url
        parsed = urlparse(base_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["delta"] = [str(delta)]
        query["start"] = [str(page_number)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def parse_total_entries(self, html: str) -> int | None:
        match = re.search(r"Showing\s+\d+\s+to\s+\d+\s+of\s+(\d+)\s+entries", html, flags=re.I)
        if match:
            return int(match.group(1))
        return None

    def compute_total_pages(self, total_records: int, *, delta: int) -> int:
        if total_records <= 0:
            return 0
        return max(1, math.ceil(total_records / delta))

    def filter_records(
        self,
        records: list[PFRDARecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[PFRDARecord]:
        filtered: list[PFRDARecord] = []
        for record in records:
            if not record.date:
                continue
            row_date = date.fromisoformat(record.date)
            if from_date and row_date < from_date:
                continue
            if to_date and row_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def count_by_year(self, records: list[PFRDARecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year = record.date[:4]
            counts[year] = counts.get(year, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def count_by_category(self, records: list[PFRDARecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            key = (normalize_text(record.category or "") or "").strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def record_preview(self, record: PFRDARecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
        }

    def record_dedup_key(self, record: PFRDARecord) -> tuple[str, ...]:
        row_date = record.date.strip()
        subject = (normalize_text(record.subject or "") or "").lower()
        circular_no = (normalize_text(record.circular_no or "") or "").lower()
        link = (normalize_text(record.link or "") or "").lower()
        if circular_no:
            return (row_date, subject, circular_no, link)
        return (row_date, subject, link)

    def deduplicate_records(self, records: list[PFRDARecord]) -> list[PFRDARecord]:
        deduped: list[PFRDARecord] = []
        seen: set[tuple[str, ...]] = set()
        for record in records:
            dedupe_key = self.record_dedup_key(record)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(record)
        return deduped

    def detect_selectors(self, soup: BeautifulSoup) -> list[str]:
        selectors: list[str] = []
        if soup.select("div.basic-card"):
            selectors.append("div.basic-card")
        if soup.select(".basic-title"):
            selectors.append(".basic-title")
        if soup.find(id="accordionListing"):
            selectors.append("#accordionListing")
        if soup.find("form"):
            selectors.append("form")
        return selectors

    def detect_filters(self, html: str) -> list[str]:
        filters: list[str] = []
        lowered = html.lower()
        marker_map = {
            "year/month": ("year & month", "year:", "month:"),
            "custom date range": ("custom range", "start date:", "end date:"),
            "modifiedFrom": ("modifiedfrom",),
            "modifiedTo": ("modifiedto",),
            "departments": ("departments",),
            "stakeholder": ("stakeholder",),
            "category": ("category",),
            "sort": ("relevance", "newest", "oldest"),
            "delta/start pagination": ("delta=", "start="),
        }
        for label, markers in marker_map.items():
            if any(marker in lowered for marker in markers):
                filters.append(label)
        return filters

    def detect_pagination(self, html: str) -> list[str]:
        pagination: list[str] = []
        lowered = html.lower()
        if "showing " in lowered and " entries" in lowered:
            pagination.append("showing_count")
        if "page 2" in lowered:
            pagination.append("page_links")
        if "delta=" in lowered or "start=" in lowered:
            pagination.append("delta/start")
        return pagination

    def count_link_types(self, records: list[PFRDARecord]) -> dict[str, int]:
        counts = {"pdf": 0, "html/detail": 0, "doc/docx": 0, "xls/xlsx": 0, "zip": 0, "other": 0, "empty": 0}
        for record in records:
            counts[self.detect_link_type(record.link)] += 1
        return counts

    def detect_link_type(self, value: str) -> str:
        if not value:
            return "empty"
        lowered = value.lower()
        parsed = urlparse(lowered)
        path = parsed.path
        if path.endswith(".pdf") or "/documents/" in lowered:
            return "pdf"
        if path.endswith(".doc") or path.endswith(".docx"):
            return "doc/docx"
        if path.endswith(".xls") or path.endswith(".xlsx"):
            return "xls/xlsx"
        if path.endswith(".zip"):
            return "zip"
        if "/w/" in path or "/web/pfrda/" in path:
            return "html/detail"
        return "other"

    def is_navigation_text(self, text: str) -> bool:
        lowered = (normalize_text(text or "") or "").lower()
        navigation_markers = {
            "home",
            "regulatory framework",
            "about us",
            "media",
            "financial literacy",
            "get to know",
            "share this page",
            "copy url",
            "print this page",
        }
        return lowered in navigation_markers

    def is_liferay_portal_page(self, html: str) -> bool:
        lowered = html.lower()
        return "liferay" in lowered or "portlet-search-results" in lowered or "themeid=" in lowered

    def extract_reference_no_from_text(self, text: str) -> str:
        normalized = normalize_text(text or "") or ""
        patterns = [
            r"(PFRDA/[A-Za-z0-9./()_-]+)",
            r"(Notification\s+No\.\s*[A-Za-z0-9./() _-]+)",
            r"(Circular\s+No\.\s*[A-Za-z0-9./() _-]+)",
            r"(Reference\s+Number[:\s]+[A-Za-z0-9./() _-]+)",
            r"(Ref[:\s]+[A-Za-z0-9./() _-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.I)
            if match:
                cleaned = clean_pfrda_reference(match.group(1))
                if cleaned:
                    return cleaned
        return ""

    def console_safe(self, value: Any) -> str:
        text = str(value or "")
        return text.encode("ascii", "replace").decode("ascii")

    def metadata_sidecar_path(self, out_path: str | Path) -> Path:
        return Path(f"{out_path}.meta.json")

    def write_output(self, records: list[PFRDARecord], out_path: str | Path, *, include_category: bool) -> None:
        headers = ENRICHED_OUTPUT_HEADERS if include_category else EXPECTED_OUTPUT_HEADERS
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=headers)
            writer.writeheader()
            for record in records:
                writer.writerow(self.record_to_output_row(record, include_category=include_category))

    def append_output(self, records: list[PFRDARecord], out_path: str | Path, *, include_category: bool) -> None:
        if not records:
            return
        headers = ENRICHED_OUTPUT_HEADERS if include_category else EXPECTED_OUTPUT_HEADERS
        file_exists = Path(out_path).exists()
        with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            for record in records:
                writer.writerow(self.record_to_output_row(record, include_category=include_category))

    def record_to_output_row(self, record: PFRDARecord, *, include_category: bool) -> dict[str, str]:
        row = {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_category:
            return {
                "date": record.date,
                "category": record.category,
                "subject": record.subject,
                "circular_no": record.circular_no,
                "link": record.link,
                "source_url": record.source_url,
                "scraped_at": record.scraped_at,
            }
        return row

    def write_metadata_sidecar(self, records: list[PFRDARecord], out_path: str | Path) -> None:
        path = self.metadata_sidecar_path(out_path)
        path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")

    def append_metadata_sidecar(self, records: list[PFRDARecord], out_path: str | Path) -> None:
        if not records:
            return
        existing = self.load_metadata_sidecar(out_path)
        existing.extend(records)
        self.write_metadata_sidecar(existing, out_path)

    def load_metadata_sidecar(self, out_path: str | Path) -> list[PFRDARecord]:
        path = self.metadata_sidecar_path(out_path)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [PFRDARecord(**item) for item in payload]

    def load_existing_output_records(self, out_path: str | Path) -> list[PFRDARecord]:
        path = Path(out_path)
        if not path.exists():
            return []
        metadata_rows = self.load_metadata_sidecar(out_path)
        if metadata_rows:
            return metadata_rows
        rows: list[PFRDARecord] = []
        with open(path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                rows.append(
                    PFRDARecord(
                        date=row.get("date", ""),
                        subject=row.get("subject", ""),
                        circular_no=row.get("circular_no", ""),
                        link=row.get("link", ""),
                        source_url=row.get("source_url", ""),
                        scraped_at=row.get("scraped_at", ""),
                    )
                )
        return rows

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool) -> None:
        path = Path(out_path)
        if path.exists():
            try:
                with open(path, "a", encoding="utf-8"):
                    pass
            except OSError as exc:  # pragma: no cover - platform-specific lock behavior
                raise RuntimeError(f"Output file appears locked: {path}") from exc
            return
        if resume:
            return
        path.parent.mkdir(parents=True, exist_ok=True)

    def load_checkpoint(self, checkpoint_path: str | Path) -> PFRDACheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return PFRDACheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: PFRDACheckpoint) -> None:
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_count_csv(self, path: str | Path, header: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([header, "count"])
            for key, value in counts.items():
                writer.writerow([key, value])


class PFRDARecentUpdatesScraper(PFRDAScraper):
    source = "pfrda-recent-updates"


class PFRDACircularsActiveScraper(PFRDAScraper):
    source = "pfrda-circulars-active"


class PFRDACircularsInoperativeScraper(PFRDAScraper):
    source = "pfrda-circulars-inoperative"


class PFRDAMasterCircularsActiveScraper(PFRDAScraper):
    source = "pfrda-master-circulars-active"


class PFRDANotificationsScraper(PFRDAScraper):
    source = "pfrda-notifications"


class PFRDARegulationsScraper(PFRDAScraper):
    source = "pfrda-regulations"


class PFRDAGuidelinesScraper(PFRDAScraper):
    source = "pfrda-guidelines"


class PFRDAPressReleasesScraper(PFRDAScraper):
    source = "pfrda-press-releases"


class PFRDATendersScraper(PFRDAScraper):
    source = "pfrda-tenders"


def clean_pfrda_reference(value: str) -> str:
    normalized = normalize_text(value or "") or ""
    normalized = re.sub(r"^(Ref:|Reference Number:)\s*", "", normalized, flags=re.I).strip()
    if normalized.upper().startswith("PFRDA/"):
        match = re.match(r"(PFRDA/[A-Za-z0-9./()_-]+)", normalized, flags=re.I)
        if match:
            candidate = match.group(1)
            if candidate.count("/") >= 2 and re.search(r"\d", candidate):
                return candidate
            return ""
    return normalized


def canonicalize_pfrda_url(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    normalized = normalized.replace(
        "https://www.pfrda.org.in/w/https-/www.pfrda.org.in/",
        "https://www.pfrda.org.in/",
    )
    normalized = normalized.replace(
        "https://www.pfrda.org.in/w/http-/www.pfrda.org.in/",
        "http://www.pfrda.org.in/",
    )
    parsed = urlparse(normalized)
    filtered_query = [
        (key, item)
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if key not in {"p_l_back_url", "p_l_back_url_title"}
        for item in values
    ]
    return urlunparse(parsed._replace(query=urlencode(filtered_query, doseq=True)))


def clean_pfrda_text(value: str) -> str:
    text = value or ""
    try:
        repaired = text.encode("latin-1").decode("utf-8")
        if repaired:
            text = repaired
    except Exception:
        pass
    replacements = {
        "â€“": "–",
        "â€”": "—",
        "â€˜": "‘",
        "â€™": "’",
        "â€œ": "“",
        "â€": "”",
        "Â ": " ",
        "â€": "\"",
        "ï¿½": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text.strip()


def normalize_pfrda_date(raw_value: str) -> str:
    normalized = normalize_text(raw_value or "") or ""
    if not normalized:
        return ""
    direct_match = re.search(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b", normalized)
    if direct_match:
        try:
            return parse_indian_date(direct_match.group(0)).isoformat()
        except Exception:
            pass
    try:
        return parse_indian_date(normalized).isoformat()
    except Exception:
        return ""


def extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    stop_labels = ("Published on:", "Issue Date:", "Release Date:", "Reference Number:", "Ref:", "Valid Till:")
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    for label in labels:
        pattern = re.escape(label) + r"\s*(.+?)(?=(?:" + stop_pattern + r")|$)"
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(1).strip()
    return ""
