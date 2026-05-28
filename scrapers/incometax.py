from __future__ import annotations

import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text
from models import RegulatoryDocument
from scrapers.base import BaseScraper


INCOMETAX_BASE_URL = "https://www.incometaxindia.gov.in"
INCOMETAX_HOME_URL = f"{INCOMETAX_BASE_URL}/"
INCOMETAX_TAX_FEEDS_URL = f"{INCOMETAX_BASE_URL}/tax-feeds"
INCOMETAX_SITEMAP_URL = f"{INCOMETAX_BASE_URL}/sitemap"
INCOMETAX_SITEMAP_XML_URL = f"{INCOMETAX_BASE_URL}/sitemap.xml"
INCOMETAX_CIRCULARS_URL = f"{INCOMETAX_BASE_URL}/circulars"
INCOMETAX_NOTIFICATIONS_URL = f"{INCOMETAX_BASE_URL}/notifications"
INCOMETAX_WHATS_NEW_URL = f"{INCOMETAX_BASE_URL}/what-s-new"
INCOMETAX_PRESS_RELEASE_URL = f"{INCOMETAX_BASE_URL}/press-release"
INCOMETAX_FORMS_URL = f"{INCOMETAX_BASE_URL}/income-tax-forms"
INCOMETAX_ALL_RULES_URL = f"{INCOMETAX_BASE_URL}/all-rules"
INCOMETAX_ALL_ACTS_URL = f"{INCOMETAX_BASE_URL}/all-acts"
INCOMETAX_FINANCE_ACTS_URL = f"{INCOMETAX_BASE_URL}/finance-acts"
INCOMETAX_FINANCE_BILLS_URL = f"{INCOMETAX_BASE_URL}/finance-bills"

EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]

RSS_FEED_BY_SOURCE = {
    "incometax-circulars": f"{INCOMETAX_BASE_URL}/circular-rss-feed/-/asset_publisher/bxhj/rss",
    "incometax-notifications": f"{INCOMETAX_BASE_URL}/notification-rss-feed/-/asset_publisher/bxhj/rss",
}
PAGE_TEXT_BY_SOURCE = {
    "incometax-circulars": "Circulars",
    "incometax-notifications": "Notifications",
}
DEFAULT_STRUCTURE_BY_SOURCE = {
    "incometax-circulars": {"site_id": 20117, "structure_id": 36050, "structure_key": "CIRCULAR_KEY"},
    "incometax-notifications": {"site_id": 20117, "structure_id": 36057, "structure_key": "NOTIFICATION_KEY"},
}

SAFE_REFERENCE_PATTERNS = [
    re.compile(r"\b(?:CBDT\s+)?Circular\s*No\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\- ]+)", re.I),
    re.compile(r"\b(?:CBDT\s+)?Notification\s*No\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\- ]+)", re.I),
    re.compile(r"\bF\.\s*No\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
    re.compile(r"\bS\.?O\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
    re.compile(r"\bG\.?S\.?R\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
]


@dataclass
class IncomeTaxRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    content_type: str = ""
    raw_date: str = ""
    detail_url: str = ""


@dataclass
class IncomeTaxPageProbe:
    url: str
    status_code: Optional[int]
    final_url: str
    page_title: str
    page_heading: str
    direct_http_worked: bool
    shell_only: bool
    rows_present: bool
    uses_liferay_client_extension: bool
    filters_found: list[str]
    pagination_found: list[str]
    year_controls_found: list[str]
    feed_links_found: list[str]
    api_endpoints_found: list[str]
    detail_url_patterns: list[str]
    first_records: list[dict[str, str]]
    link_type_counts: dict[str, int]
    error: Optional[str] = None


@dataclass
class IncomeTaxCheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    years_discovered: list[str]
    total_records_detected: Optional[int]
    count_by_year: dict[str, int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class IncomeTaxBaseScraper(BaseScraper):
    source: str
    regulator = "Income Tax Department / CBDT"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    @property
    def primary_url(self) -> str:
        if self.source == "incometax-circulars":
            return INCOMETAX_CIRCULARS_URL
        return INCOMETAX_NOTIFICATIONS_URL

    @property
    def fixture_slug(self) -> str:
        return "circulars" if self.source == "incometax-circulars" else "notifications"

    @property
    def page_text(self) -> str:
        return PAGE_TEXT_BY_SOURCE[self.source]

    @property
    def feed_url(self) -> str:
        return RSS_FEED_BY_SOURCE[self.source]

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        descriptor = self.resolve_listing_descriptor(self.primary_url)
        return self.fetch_structured_contents_page(
            site_id=descriptor["site_id"],
            structure_id=descriptor["structure_id"],
            page=1,
            page_size=10,
        )

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        descriptor = {
            "source_url": self.primary_url,
            "site_id": response.get("siteId", 20117),
        }
        items = response.get("items", [])
        records = [self.parse_structured_content_item(item, source_url=self.primary_url) for item in items]
        cleaned = [record for record in records if record]
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": self.page_text[:-1] if self.page_text.endswith("s") else self.page_text,
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": self.page_text,
                "pdf_url": record.link if self.detect_link_type(record.link) == "pdf" else None,
            }
            for record in cleaned
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", self.page_text),
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

    def scout_site(self, base_url: str) -> list[IncomeTaxPageProbe]:
        del base_url
        fixture_dir = Path("tests/fixtures/incometax")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        routes = [
            ("home.html", INCOMETAX_HOME_URL),
            ("circulars.html", INCOMETAX_CIRCULARS_URL),
            ("notifications.html", INCOMETAX_NOTIFICATIONS_URL),
            ("whats_new.html", INCOMETAX_WHATS_NEW_URL),
            ("press_release.html", INCOMETAX_PRESS_RELEASE_URL),
            ("sitemap.html", INCOMETAX_SITEMAP_URL),
            ("income_tax_forms.html", INCOMETAX_FORMS_URL),
            ("all_rules.html", INCOMETAX_ALL_RULES_URL),
            ("all_acts.html", INCOMETAX_ALL_ACTS_URL),
            ("finance_acts.html", INCOMETAX_FINANCE_ACTS_URL),
            ("finance_bills.html", INCOMETAX_FINANCE_BILLS_URL),
        ]

        probes: list[IncomeTaxPageProbe] = []
        for fixture_name, url in routes:
            try:
                response = self.fetch_allowing_403(url)
                html = response.text
                (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
                probe = self.inspect_page_html(url=url, html=html, status_code=response.status_code, final_url=str(response.url))
            except Exception as exc:  # pragma: no cover - live fallback
                probe = IncomeTaxPageProbe(
                    url=url,
                    status_code=None,
                    final_url=url,
                    page_title="",
                    page_heading="",
                    direct_http_worked=False,
                    shell_only=True,
                    rows_present=False,
                    uses_liferay_client_extension=False,
                    filters_found=[],
                    pagination_found=[],
                    year_controls_found=[],
                    feed_links_found=[],
                    api_endpoints_found=[],
                    detail_url_patterns=[],
                    first_records=[],
                    link_type_counts={},
                    error=str(exc),
                )
            probes.append(probe)
            self.print_probe(probe)

        print("Discovered public RSS feeds:")
        print(f"- Circulars RSS: {self.feed_url if self.source == 'incometax-circulars' else RSS_FEED_BY_SOURCE['incometax-circulars']}")
        print(f"- Notifications RSS: {RSS_FEED_BY_SOURCE['incometax-notifications']}")
        print(f"- Tax Feeds page: {INCOMETAX_TAX_FEEDS_URL}")
        return probes

    def inspect_listing(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/incometax")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        raw_response = self.fetch_allowing_403(url)
        (fixture_dir / f"{self.fixture_slug}.html").write_text(raw_response.text, encoding="utf-8")
        base_probe = self.inspect_page_html(
            url=url,
            html=raw_response.text,
            status_code=raw_response.status_code,
            final_url=str(raw_response.url),
        )
        self.print_probe(base_probe)

        descriptor = self.resolve_listing_descriptor(url)
        print(f"Working endpoint/page-flow: {descriptor['resolved_url']} -> {descriptor['api_url']}")
        print(f"Request method: GET")
        print(f"Cookies/session requirements: none beyond normal public page fetch")
        print(f"Playwright required: no")
        print(f"Fields available: circularNotificationNumber, selectType, reportFile, circularNotificationDate, uploadDate, title, friendlyUrlPath")
        page_data = self.fetch_structured_contents_page(
            site_id=descriptor["site_id"],
            structure_id=descriptor["structure_id"],
            page=1,
            page_size=10,
        )
        print(f"API total records: {page_data.get('totalCount')}")
        records = [self.parse_structured_content_item(item, source_url=descriptor["resolved_url"]) for item in page_data.get("items", [])]
        records = [record for record in records if record]
        print("Sample 10 records:")
        for record in records[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)} | {self.console_safe(record.source_url)}"
            )
        if records:
            detail_response = self.get(records[0].detail_url or records[0].link)
            print(f"Sample detail status: {detail_response.status_code}")
            print(f"Sample detail content-type: {detail_response.headers.get('content-type')}")
        print(f"RSS feed endpoint: {self.feed_url}")
        return {
            "descriptor": descriptor,
            "sample_records": [asdict(record) for record in records[:10]],
        }

    def discover_range(self, url: str) -> dict[str, Any]:
        descriptor = self.resolve_listing_descriptor(url)
        records = self.fetch_all_records(
            descriptor=descriptor,
            from_date=None,
            to_date=None,
            all_available=True,
            max_chunks_this_run=None,
            delay_seconds=0.0,
            checkpoint_last_completed_chunk=0,
        )["records"]
        if not records:
            raise RuntimeError(f"No records discovered for {self.source}")
        count_by_year = self.count_by_year(records)
        records_sorted = sorted(records, key=lambda item: item.date)
        newest = max(record.date for record in records)
        oldest = min(record.date for record in records)
        print(f"Working route/API/page-flow: {descriptor['resolved_url']} -> {descriptor['api_url']}")
        print(f"Available years discovered: {sorted(count_by_year.keys())}")
        print(f"Whether direct HTTP worked: True")
        print(f"Whether Playwright was used: False")
        print(f"Newest date found: {newest}")
        print(f"Oldest date found: {oldest}")
        print(f"Total record count: {len(records)}")
        print("Count by year:")
        for year_key in sorted(count_by_year):
            print(f"- {year_key}: {count_by_year[year_key]}")
        print("Earliest 10 records:")
        for record in records_sorted[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        print("Limitation: primary friendly route may return 403 unless the official tax-feeds-discovered doAs link is used.")
        return {
            "newest": newest,
            "oldest": oldest,
            "total": len(records),
            "count_by_year": count_by_year,
        }

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: Optional[date],
        to_date: Optional[date],
        resume: bool = False,
        checkpoint_path: str | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[IncomeTaxRecord]:
        del retries, retry_base_delay, retry_max_delay
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else out_path.with_suffix(out_path.suffix + ".checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        descriptor = self.resolve_listing_descriptor(url)
        first_page = self.fetch_structured_contents_page(
            site_id=descriptor["site_id"],
            structure_id=descriptor["structure_id"],
            page=1,
            page_size=descriptor["page_size"],
        )
        total_records = int(first_page.get("totalCount") or 0)
        total_chunks = math.ceil(total_records / descriptor["page_size"]) if total_records else 0
        previous_last_completed_chunk = 0

        if resume or checkpoint_path:
            checkpoint = self.load_checkpoint(checkpoint_file)
            if checkpoint is None:
                checkpoint = self.build_checkpoint(
                    source_url=descriptor["resolved_url"],
                    output_path=out_path,
                    years_discovered=[],
                    total_records_detected=total_records,
                )
            previous_last_completed_chunk = checkpoint.last_completed_chunk
        else:
            checkpoint = self.build_checkpoint(
                source_url=descriptor["resolved_url"],
                output_path=out_path,
                years_discovered=[],
                total_records_detected=total_records,
            )

        if max_chunks_this_run is not None and max_chunks_this_run <= 0:
            raise RuntimeError("max_chunks_this_run must be positive when provided.")

        chunk_window = self.compute_chunk_window(
            total_chunks=total_chunks,
            previous_last_completed_chunk=previous_last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        resume_from_chunk = int(chunk_window["resume_from_chunk"])
        expected_end_chunk = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])

        existing_keys = self.load_existing_keys(out_path, include_circular_no=True)
        existing_count = self.count_existing_rows(out_path)

        print(f"Oldest date: unknown until scanned")
        print(f"Newest date: unknown until scanned")
        print(f"Expected records: {total_records}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {'append' if resume else 'overwrite'}")
        print(f"total_chunks: {total_chunks}")
        print(f"CSV rows detected: {existing_count}")
        print(f"previous last_completed_chunk: {previous_last_completed_chunk}")
        print(f"resume_from_chunk: {resume_from_chunk}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {expected_end_chunk}")
        print(f"actual chunk range: {resume_from_chunk}-{expected_end_chunk}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return []

        if not all_available and from_date is None and to_date is None:
            raise RuntimeError("from_date or --all-available is required for Income Tax scrapes.")

        if not resume:
            self.write_output([], out_path)

        written_records: list[IncomeTaxRecord] = []
        duplicates_skipped = 0
        observed_dates: list[str] = []
        target_from = from_date
        target_to = to_date

        for page_number in range(resume_from_chunk, expected_end_chunk + 1):
            page_payload = first_page if page_number == 1 else self.fetch_structured_contents_page(
                site_id=descriptor["site_id"],
                structure_id=descriptor["structure_id"],
                page=page_number,
                page_size=descriptor["page_size"],
            )
            page_records = [self.parse_structured_content_item(item, source_url=descriptor["resolved_url"]) for item in page_payload.get("items", [])]
            page_records = [record for record in page_records if record]
            observed_dates.extend([record.date for record in page_records])
            fresh_records: list[IncomeTaxRecord] = []
            for record in page_records:
                record_date = date.fromisoformat(record.date)
                if target_from and record_date < target_from:
                    continue
                if target_to and record_date > target_to:
                    continue
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path)
            written_records.extend(fresh_records)
            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=page_number,
            )
            checkpoint.last_completed_chunk = page_number
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = checkpoint.records_written
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = page_number >= total_chunks
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)
            if delay_seconds > 0 and page_number < expected_end_chunk:
                time.sleep(delay_seconds)
            if not all_available and target_from and page_records:
                oldest_page_date = min(date.fromisoformat(record.date) for record in page_records)
                if oldest_page_date < target_from:
                    break

        all_dates = observed_dates + [record.date for record in written_records]
        if all_dates:
            checkpoint.newest_available_date = max(all_dates)
            checkpoint.oldest_available_date = min(all_dates)
            checkpoint.years_discovered = sorted({item[:4] for item in all_dates})
            checkpoint.count_by_year = self.count_by_year(
                [IncomeTaxRecord(date=item, subject="", circular_no="", link="", source_url="", scraped_at="") for item in all_dates]
            )

        print(f"Rows written: {len(written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(written_records)}")
        if resume or checkpoint_path:
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2, ensure_ascii=True)}")
        self.last_fetch_transport = "httpx"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        stem = file_path.stem.replace("_archive", "")
        report_path = file_path.parent / f"{stem}_validation_report.json"
        year_counts_path = file_path.parent / f"{stem}_year_counts.csv"

        total_rows = 0
        malformed_csv_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        duplicate_key_count = 0
        invalid_dates = 0
        pdf_links = 0
        html_links = 0
        doc_links = 0
        xls_links = 0
        zip_links = 0
        other_links = 0
        empty_links = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str, str]] = set()
        row_dates: list[date] = []
        year_counts: dict[str, int] = {}

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            if reader.fieldnames != EXPECTED_OUTPUT_HEADERS:
                raise RuntimeError(f"Unexpected headers: {reader.fieldnames}; expected {EXPECTED_OUTPUT_HEADERS}")
            for row in reader:
                total_rows += 1
                if len(row) != len(EXPECTED_OUTPUT_HEADERS):
                    malformed_csv_rows += 1
                raw_date = row["date"].strip()
                subject = row["subject"].strip()
                circular_no = row["circular_no"].strip()
                link = row["link"].strip()

                if not raw_date:
                    missing_date += 1
                if not subject:
                    missing_subject += 1
                if not circular_no:
                    missing_circular_no += 1
                if not link:
                    missing_link += 1
                    empty_links += 1

                normalized_subject = (normalize_text(subject) or "").casefold()
                key = (raw_date, normalized_subject, circular_no, link) if circular_no else (raw_date, normalized_subject, "", link)
                if key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(key)

                try:
                    parsed_date = date.fromisoformat(raw_date)
                    row_dates.append(parsed_date)
                    year_counts[str(parsed_date.year)] = year_counts.get(str(parsed_date.year), 0) + 1
                except ValueError:
                    if raw_date:
                        invalid_dates += 1
                        suspicious_rows.append({"reason": "invalid_date", "row": row})

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
                elif link_type == "empty":
                    empty_links += 1
                else:
                    other_links += 1

                if link and "incometaxindia.gov.in" not in link.lower():
                    suspicious_rows.append({"reason": "non_incometax_link", "row": row})
                if subject and len(subject.strip()) < 5:
                    suspicious_rows.append({"reason": "very_short_subject", "row": row})
                if not link:
                    suspicious_rows.append({"reason": "missing_link", "row": row})
                if link.startswith("/") and not link.startswith("/documents/") and not link.startswith("/w/"):
                    suspicious_rows.append({"reason": "broken_looking_relative_url", "row": row})
                if subject and subject.casefold() in {"home", "search", "sitemap"}:
                    suspicious_rows.append({"reason": "navigation_menu_text", "row": row})

        report = {
            "file": str(file_path),
            "total_rows": total_rows,
            "headers": EXPECTED_OUTPUT_HEADERS,
            "malformed_csv_rows": malformed_csv_rows,
            "missing_date": missing_date,
            "missing_subject": missing_subject,
            "missing_circular_no": missing_circular_no,
            "missing_link": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "invalid_dates": invalid_dates,
            "min_date": min(row_dates).isoformat() if row_dates else None,
            "max_date": max(row_dates).isoformat() if row_dates else None,
            "year_counts": dict(sorted(year_counts.items())),
            "link_type_counts": {
                "pdf": pdf_links,
                "html/detail": html_links,
                "doc/docx": doc_links,
                "xls/xlsx": xls_links,
                "zip": zip_links,
                "other": other_links,
                "empty": empty_links,
            },
            "suspicious_row_count": len(suspicious_rows),
            "suspicious_rows": suspicious_rows[:200],
        }
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", year_counts)
        print(f"Validation report saved: {report_path}")
        print(f"Year counts saved: {year_counts_path}")
        return report

    def resolve_listing_descriptor(self, url: str) -> dict[str, Any]:
        resolved_url = self.resolve_public_page_url(url)
        defaults = DEFAULT_STRUCTURE_BY_SOURCE[self.source]
        try:
            page_html = self.get(resolved_url).text
            site_id = self.extract_scope_group_id(page_html)
            structure_id, structure_key = self.extract_structure_descriptor(page_html)
        except Exception:
            page_html = ""
            site_id = defaults["site_id"]
            structure_id = defaults["structure_id"]
            structure_key = defaults["structure_key"]
        return {
            "resolved_url": resolved_url,
            "page_html": page_html,
            "site_id": site_id,
            "structure_id": structure_id,
            "structure_key": structure_key,
            "api_url": f"{INCOMETAX_BASE_URL}/o/headless-delivery/v1.0/sites/{site_id}/structured-contents",
            "page_size": 1000,
        }

    def resolve_public_page_url(self, url: str) -> str:
        if "doAsUserId=" in url:
            return url
        try:
            feed_html = self.fetch_fresh_html(INCOMETAX_TAX_FEEDS_URL)
            soup = BeautifulSoup(feed_html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                text = normalize_text(anchor.get_text(" ", strip=True)) or ""
                if text.casefold() == self.page_text.casefold():
                    return urljoin(INCOMETAX_BASE_URL, anchor["href"])
        except Exception:
            pass
        return url

    def fetch_fresh_html(self, url: str) -> str:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=self.timeout,
            headers=self.headers,
        )
        response.raise_for_status()
        return response.text

    def fetch_structured_contents_page(self, *, site_id: int, structure_id: int, page: int, page_size: int) -> dict[str, Any]:
        url = (
            f"{INCOMETAX_BASE_URL}/o/headless-delivery/v1.0/sites/{site_id}/structured-contents"
            f"?page={page}&pageSize={page_size}&flatten=true&sort=datePublished:desc&filter=contentStructureId%20eq%20{structure_id}"
        )
        response = self.get(url, headers={**self.headers, "Accept": "application/json"})
        self.last_fetch_transport = "httpx"
        return response.json()

    def fetch_all_records(
        self,
        *,
        descriptor: dict[str, Any],
        from_date: Optional[date],
        to_date: Optional[date],
        all_available: bool,
        max_chunks_this_run: Optional[int],
        delay_seconds: float,
        checkpoint_last_completed_chunk: int,
    ) -> dict[str, Any]:
        del max_chunks_this_run, checkpoint_last_completed_chunk
        first_page = self.fetch_structured_contents_page(
            site_id=descriptor["site_id"],
            structure_id=descriptor["structure_id"],
            page=1,
            page_size=descriptor["page_size"],
        )
        total_records = int(first_page.get("totalCount") or 0)
        total_pages = math.ceil(total_records / descriptor["page_size"]) if total_records else 0
        records: list[IncomeTaxRecord] = []
        for page_number in range(1, total_pages + 1):
            payload = first_page if page_number == 1 else self.fetch_structured_contents_page(
                site_id=descriptor["site_id"],
                structure_id=descriptor["structure_id"],
                page=page_number,
                page_size=descriptor["page_size"],
            )
            for item in payload.get("items", []):
                parsed = self.parse_structured_content_item(item, source_url=descriptor["resolved_url"])
                if not parsed:
                    continue
                record_date = date.fromisoformat(parsed.date)
                if from_date and record_date < from_date:
                    if not all_available:
                        continue
                if to_date and record_date > to_date:
                    continue
                records.append(parsed)
            if delay_seconds > 0 and page_number < total_pages:
                time.sleep(delay_seconds)
        return {"records": records, "total_records": total_records, "total_pages": total_pages}

    def parse_structured_content_item(self, item: dict[str, Any], *, source_url: str) -> Optional[IncomeTaxRecord]:
        field_map = self.index_content_fields(item.get("contentFields", []))
        raw_date = self.first_value(field_map, "circularNotificationDate", "uploadDate") or ""
        normalized_date = self.normalize_iso_date(raw_date)
        title = normalize_text(item.get("title")) or ""
        circular_no = normalize_text(self.first_value(field_map, "circularNotificationNumber")) or ""
        extracted_reference = self.extract_reference_no_from_text(title)
        if not circular_no:
            circular_no = extracted_reference
        elif extracted_reference and self.reference_is_better(candidate=extracted_reference, current=circular_no):
            circular_no = extracted_reference
        circular_no = normalize_text(circular_no) or ""

        report_document = self.first_document(field_map, "reportFile")
        content_type = normalize_text(self.first_value(field_map, "selectType")) or ""
        detail_url = self.build_detail_url(item)
        link = self.build_primary_link(report_document=report_document, detail_url=detail_url)
        if not normalized_date or not title or not link:
            return None
        return IncomeTaxRecord(
            date=normalized_date,
            subject=title,
            circular_no=circular_no,
            link=link,
            source_url=source_url,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            content_type=content_type,
            raw_date=raw_date,
            detail_url=detail_url,
        )

    @staticmethod
    def index_content_fields(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {field.get("name", ""): field for field in fields if field.get("name")}

    @staticmethod
    def first_value(field_map: dict[str, dict[str, Any]], *names: str) -> str:
        for name in names:
            value = ((field_map.get(name) or {}).get("contentFieldValue") or {}).get("data")
            if value:
                return str(value)
        return ""

    @staticmethod
    def first_document(field_map: dict[str, dict[str, Any]], *names: str) -> Optional[dict[str, Any]]:
        for name in names:
            document = ((field_map.get(name) or {}).get("contentFieldValue") or {}).get("document")
            if document:
                return document
        return None

    @staticmethod
    def build_detail_url(item: dict[str, Any]) -> str:
        friendly = normalize_text(item.get("friendlyUrlPath")) or ""
        if not friendly:
            return ""
        return urljoin(INCOMETAX_BASE_URL, f"/w/{friendly}")

    @staticmethod
    def build_primary_link(*, report_document: Optional[dict[str, Any]], detail_url: str) -> str:
        if report_document and report_document.get("contentUrl"):
            return urljoin(INCOMETAX_BASE_URL, report_document["contentUrl"])
        return detail_url

    @staticmethod
    def normalize_iso_date(raw_value: str) -> str:
        if not raw_value:
            return ""
        cleaned = raw_value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(cleaned).date().isoformat()
        except ValueError:
            return ""

    def extract_reference_no_from_text(self, text: str) -> str:
        normalized = normalize_text(text) or ""
        for pattern in SAFE_REFERENCE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                return normalize_text(match.group(1)) or ""
        return ""

    @staticmethod
    def reference_is_better(*, candidate: str, current: str) -> bool:
        candidate_norm = normalize_text(candidate) or ""
        current_norm = normalize_text(current) or ""
        if not current_norm:
            return bool(candidate_norm)
        current_has_rich_markers = any(token in current_norm.casefold() for token in ["notification", "circular", "f. no", "s.o", "g.s.r", "/"])
        candidate_has_rich_markers = any(token in candidate_norm.casefold() for token in ["notification", "circular", "f. no", "s.o", "g.s.r", "/"])
        return candidate_has_rich_markers and not current_has_rich_markers

    def fetch_allowing_403(self, url: str) -> Any:
        try:
            response = self.client.get(url, follow_redirects=True)
            return response
        except Exception:
            raise

    def inspect_page_html(self, *, url: str, html: str, status_code: Optional[int], final_url: str) -> IncomeTaxPageProbe:
        soup = BeautifulSoup(html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        heading_tag = soup.find(["h1", "h2", "h3"])
        heading = normalize_text(heading_tag.get_text(" ", strip=True)) if heading_tag else ""
        row_like_text = self.extract_first_visible_records(soup)
        feed_links = [urljoin(INCOMETAX_BASE_URL, a["href"]) for a in soup.find_all("a", href=True) if "rss" in a["href"].lower() or "feed" in a["href"].lower()]
        api_endpoints = [script["src"] for script in soup.find_all("script", src=True) if "/o/" in script["src"]]
        link_type_counts = self.count_link_types_from_html(soup)
        filters_found: list[str] = []
        year_controls_found: list[str] = []
        pagination_found: list[str] = []
        html_lower = html.casefold()
        for token in ["selectedyear", "selectedmonth", "filterarchive", "page", "prev", "next", "search", "year", "month"]:
            if token in html_lower:
                filters_found.append(token)
        if "page=" in html_lower or "prev" in html_lower or "next" in html_lower:
            pagination_found = [token for token in ["previous", "next", "page"] if token in html_lower]
        if "2026" in html_lower:
            year_controls_found.append("2026")
        return IncomeTaxPageProbe(
            url=url,
            status_code=status_code,
            final_url=final_url,
            page_title=title or "",
            page_heading=heading or "",
            direct_http_worked=status_code is not None,
            shell_only=(status_code == 403) or ("access denied" in html_lower) or (not row_like_text and "etds-circular-notification" in html_lower),
            rows_present=bool(row_like_text),
            uses_liferay_client_extension="etds-circular-notification" in html_lower,
            filters_found=sorted(set(filters_found)),
            pagination_found=sorted(set(pagination_found)),
            year_controls_found=sorted(set(year_controls_found)),
            feed_links_found=sorted(set(feed_links + [self.feed_url])),
            api_endpoints_found=sorted(set(api_endpoints)),
            detail_url_patterns=["/w/...", "/documents/..."],
            first_records=row_like_text[:10],
            link_type_counts=link_type_counts,
        )

    @staticmethod
    def extract_first_visible_records(soup: BeautifulSoup) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            text = normalize_text(anchor.get_text(" ", strip=True)) or ""
            if not text:
                continue
            if "/w/" in anchor["href"] or "/documents/" in anchor["href"]:
                records.append({"title": text, "href": anchor["href"]})
        return records

    @staticmethod
    def count_link_types_from_html(soup: BeautifulSoup) -> dict[str, int]:
        counts = {"pdf": 0, "html/detail": 0, "doc/docx": 0, "xls/xlsx": 0, "zip": 0, "other": 0}
        for anchor in soup.find_all("a", href=True):
            kind = IncomeTaxBaseScraper.detect_link_type(anchor["href"])
            if kind in counts:
                counts[kind] += 1
            elif kind != "empty":
                counts["other"] += 1
        return counts

    @staticmethod
    def detect_link_type(link: str) -> str:
        if not link:
            return "empty"
        lower = link.lower()
        if ".pdf" in lower or "/documents/" in lower:
            return "pdf"
        if lower.endswith(".doc") or lower.endswith(".docx"):
            return "doc/docx"
        if lower.endswith(".xls") or lower.endswith(".xlsx"):
            return "xls/xlsx"
        if lower.endswith(".zip"):
            return "zip"
        if "/w/" in lower or "document-detail" in lower:
            return "html/detail"
        return "other"

    @staticmethod
    def extract_scope_group_id(html: str) -> int:
        match = re.search(r"getScopeGroupId:\s*function\s*\(\)\s*\{\s*return\s*'(\d+)'", html)
        if not match:
            raise RuntimeError("Could not extract ThemeDisplay scopeGroupId from Income Tax page HTML.")
        return int(match.group(1))

    @staticmethod
    def extract_structure_descriptor(html: str) -> tuple[int, str]:
        match = re.search(r"<etds-circular-notification[^>]*structurekey=\"([^\"]+)\"[^>]*structureid=\"([^\"]+)\"", html, re.I)
        if not match:
            raise RuntimeError("Could not extract etds-circular-notification structure metadata from page HTML.")
        return int(match.group(2)), match.group(1)

    @staticmethod
    def count_by_year(records: list[IncomeTaxRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year_key = record.date[:4]
            counts[year_key] = counts.get(year_key, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def console_safe(value: str) -> str:
        return value.encode("ascii", "ignore").decode()

    def print_probe(self, probe: IncomeTaxPageProbe) -> None:
        print(f"URL: {probe.url}")
        print(f"HTTP status: {probe.status_code}")
        print(f"Final URL: {probe.final_url}")
        print(f"Page title: {self.console_safe(probe.page_title)}")
        print(f"Page heading: {self.console_safe(probe.page_heading)}")
        print(f"Whether direct HTTP worked: {probe.direct_http_worked}")
        print(f"Whether content is a shell only: {probe.shell_only}")
        print(f"Whether listing rows are present in raw HTML: {probe.rows_present}")
        print(f"Whether JavaScript/Liferay asset publisher is used: {probe.uses_liferay_client_extension}")
        print(f"Forms/search controls found: {probe.filters_found}")
        print(f"Pagination controls found: {probe.pagination_found}")
        print(f"Filters found: {probe.filters_found}")
        print(f"Year/month/category controls found: {probe.year_controls_found}")
        print(f"RSS/feed links found: {probe.feed_links_found}")
        print(f"API/XHR/static JS endpoints found: {probe.api_endpoints_found[:10]}")
        print(f"Detail URL patterns found: {probe.detail_url_patterns}")
        print("First 10 listed records:")
        for record in probe.first_records[:10]:
            print(f"- {self.console_safe(record.get('title',''))} | {self.console_safe(record.get('href',''))}")
        print(f"Link type counts: {probe.link_type_counts}")
        if probe.error:
            print(f"Error: {probe.error}")
        print("---")

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if resume else "w"
        try:
            with open(out_path, mode, newline="", encoding="utf-8"):
                pass
        except PermissionError as exc:
            raise RuntimeError("Output file is locked. Close Excel/VS Code/OneDrive preview and rerun.") from exc

    def append_output(self, records: list[IncomeTaxRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".json":
            existing: list[dict[str, Any]] = []
            if out_path.exists() and out_path.read_text(encoding="utf-8").strip():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend([self.record_to_dict(record) for record in records])
            out_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
            return

        write_header = not out_path.exists() or out_path.stat().st_size == 0
        with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
            if write_header:
                writer.writeheader()
            for record in records:
                writer.writerow(self.record_to_dict(record))

    def write_output(self, records: list[IncomeTaxRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps([self.record_to_dict(record) for record in records], indent=2, ensure_ascii=False), encoding="utf-8")
            return
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
            writer.writeheader()
            for record in records:
                writer.writerow(self.record_to_dict(record))

    @staticmethod
    def record_to_dict(record: IncomeTaxRecord) -> dict[str, Any]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    @staticmethod
    def record_dedup_key(record: IncomeTaxRecord) -> tuple[str, str, str, str]:
        normalized_subject = (normalize_text(record.subject) or "").casefold()
        if record.circular_no:
            return (record.date, normalized_subject, record.circular_no, record.link)
        return (record.date, normalized_subject, "", record.link)

    def load_existing_keys(self, out_path: str | Path, *, include_circular_no: bool) -> set[tuple[str, str, str, str]]:
        out_path = Path(out_path)
        if not out_path.exists():
            return set()
        if out_path.suffix.lower() == ".json":
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return set()
            keys: set[tuple[str, str, str, str]] = set()
            for row in payload:
                normalized_subject = (normalize_text(row.get("subject", "")) or "").casefold()
                circular_no = row.get("circular_no", "") if include_circular_no else ""
                keys.add((row.get("date", ""), normalized_subject, circular_no, row.get("link", "")))
            return keys

        keys: set[tuple[str, str, str, str]] = set()
        with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                normalized_subject = (normalize_text(row.get("subject", "")) or "").casefold()
                circular_no = row.get("circular_no", "") if include_circular_no else ""
                keys.add((row.get("date", ""), normalized_subject, circular_no, row.get("link", "")))
        return keys

    @staticmethod
    def count_existing_rows(out_path: str | Path) -> int:
        out_path = Path(out_path)
        if not out_path.exists():
            return 0
        if out_path.suffix.lower() == ".json":
            try:
                return len(json.loads(out_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                return 0
        with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            return sum(1 for _ in reader)

    def build_checkpoint(
        self,
        *,
        source_url: str,
        output_path: str | Path,
        years_discovered: list[str],
        total_records_detected: Optional[int],
    ) -> IncomeTaxCheckpoint:
        now = datetime.now(timezone.utc).isoformat()
        return IncomeTaxCheckpoint(
            source_url=source_url,
            output_path=str(output_path),
            newest_available_date=None,
            oldest_available_date=None,
            years_discovered=years_discovered,
            total_records_detected=total_records_detected,
            count_by_year={},
            chunk_strategy="liferay_headless_structured_contents_pages",
            last_completed_chunk=0,
            records_written=0,
            unique_records_written=0,
            started_at=now,
            updated_at=now,
            completed=False,
            errors=[],
        )

    @staticmethod
    def load_checkpoint(checkpoint_path: str | Path) -> Optional[IncomeTaxCheckpoint]:
        checkpoint_path = Path(checkpoint_path)
        if not checkpoint_path.exists():
            return None
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return IncomeTaxCheckpoint(**payload)

    @staticmethod
    def save_checkpoint(checkpoint_path: str | Path, checkpoint: IncomeTaxCheckpoint) -> None:
        checkpoint_path = Path(checkpoint_path)
        checkpoint_path.write_text(json.dumps(asdict(checkpoint), indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def write_count_csv(path: str | Path, key_name: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([key_name, "count"])
            for key, value in sorted(counts.items()):
                writer.writerow([key, value])


class IncomeTaxCircularsScraper(IncomeTaxBaseScraper):
    source = "incometax-circulars"


class IncomeTaxNotificationsScraper(IncomeTaxBaseScraper):
    source = "incometax-notifications"
