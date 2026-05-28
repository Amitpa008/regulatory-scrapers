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
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


IRDAI_SOURCE_LABEL = "IRDAI"
IRDAI_WHATS_NEW_URL = "https://irdai.gov.in/web/guest/whats-new"
IRDAI_BASE_URL = "https://irdai.gov.in"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_OUTPUT_HEADERS = ["date", "type", "subject", "circular_no", "link", "source_url", "scraped_at"]
SAFE_REFERENCE_PATTERNS = [
    re.compile(r"\b(?:Ref(?:erence)?|Circular|Order|Notification)\s*(?:No\.?|Number)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
    re.compile(r"\b(?:File|F)\.?\s*(?:No\.?|Number)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
    re.compile(r"\bF\.\s*No\.?\s*([A-Za-z0-9][A-Za-z0-9\/()._\-]+)", re.I),
]
MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


@dataclass
class IRDAIRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    type: str = ""
    archive_flag: str = ""
    filter_year: str = ""
    filter_month: str = ""
    raw_date: Optional[str] = None
    subtitle: str = ""


@dataclass
class IRDAIEndpointResult:
    url: str
    method: str
    status_code: Optional[int]
    content_type: Optional[str]
    response_size: int
    format: str
    record_count: int
    sample_records: list[dict[str, Any]]
    keys_or_headers: list[str]
    request_params: dict[str, str]
    error: Optional[str] = None


@dataclass
class IRDAIChunk:
    index: int
    label: str
    year: str
    month: str
    archive_flag: str


@dataclass
class IRDAICheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    years_discovered: list[str]
    total_records_detected: Optional[int]
    count_by_year: dict[str, int]
    count_by_type: dict[str, int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class IRDAIScraper(BaseScraper):
    source = "irdai-whats-new"
    regulator = IRDAI_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(IRDAI_WHATS_NEW_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_whats_new_records(str(response), IRDAI_WHATS_NEW_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": record.type or "Whats New",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": record.type or "Whats New",
                "pdf_url": None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "Whats New"),
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record["published_date"],
            department=record.get("department"),
            category=record.get("category"),
            url=record["url"],
            pdf_url=None,
            pdf_sha256=None,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def inspect_whats_new(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/irdai")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        raw_html = self.fetch_page_html(url)
        (fixture_dir / "whats_new.html").write_text(raw_html, encoding="utf-8")

        soup = BeautifulSoup(raw_html, "html.parser")
        heading = normalize_text((soup.find(["h1", "h2"]) or {}).get_text(" ", strip=True)) or ""
        records = self.parse_whats_new_records(raw_html, url)
        type_values = sorted({record.type for record in records if record.type})
        link_counts = self.count_link_types(records)
        filters = self.extract_filter_controls(soup, raw_html)
        endpoint = IRDAIEndpointResult(
            url=url,
            method="GET",
            status_code=200,
            content_type="text/html; charset=utf-8",
            response_size=len(raw_html.encode("utf-8")),
            format="html+embedded-js",
            record_count=len(records),
            sample_records=[asdict(item) for item in records[:3]],
            keys_or_headers=["dateId", "title", "subTitle", "fileentryId"],
            request_params={},
        )

        print(f"page title: {soup.title.get_text(' ', strip=True) if soup.title else ''}")
        print(f"page heading: {heading}")
        print(f"whether listing rows/cards/table are present in raw HTML: {bool(records)}")
        print(f"filter controls found: {filters['controls']}")
        print(f"year options found: {filters['year_options']}")
        print(f"month options found: {filters['month_options']}")
        print(f"archive filter controls found: {filters['archive_values']}")
        print(f"pagination controls found: {filters['pagination']}")
        print("first 10 listed rows:")
        for record in records[:10]:
            print(self.console_safe(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}"))
        print("last 10 listed rows:")
        for record in records[-10:]:
            print(self.console_safe(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}"))
        print(f"all item/category/type labels if visible: {type_values}")
        print(f"link patterns found: {self.collect_link_patterns(records)}")
        print(f"whether links are PDF, HTML/detail, DOC/DOCX, ZIP, or other: {link_counts}")
        print(f"whether direct HTTP exposes records or only filter shell: {'records' if records else 'filter shell'}")
        print(json.dumps(asdict(endpoint), indent=2, ensure_ascii=True))

        return {
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "page_heading": heading,
            "row_count": len(records),
            "type_values": type_values,
            "filters": filters,
            "endpoint_results": [asdict(endpoint)],
        }

    def inspect_whats_new_filters(self, url: str) -> dict[str, Any]:
        raw_html = self.fetch_page_html(url)
        soup = BeautifulSoup(raw_html, "html.parser")
        filters = self.extract_filter_controls(soup, raw_html)
        base_records = self.parse_whats_new_records(raw_html, url)
        page_size = self.extract_page_size(raw_html)
        total_pages = math.ceil(len(base_records) / page_size) if page_size else 1

        sample_year = ""
        sample_month = ""
        for year in filters["year_options"]:
            if year != str(date.today().year):
                sample_year = year
                break
        sample_month = "January"

        include_count = 0
        archive_only_count = 0
        if sample_year:
            include_records = self.fetch_filtered_records(
                url=url,
                selected_year=sample_year,
                selected_month=sample_month,
                archive_flag="Include Archives",
            )
            archive_records = self.fetch_filtered_records(
                url=url,
                selected_year=sample_year,
                selected_month=sample_month,
                archive_flag="Archive Only",
            )
            include_count = len(include_records)
            archive_only_count = len(archive_records)

        response = self.get(url)
        cookie_names = sorted(response.cookies.keys())
        request_params = {
            "selectedYear": "<YYYY>",
            "selectedMonth": "<Month>",
            "filterArchive": "Include Archives|Archive Only",
        }
        archive_behavior = (
            "Include Archives returns data for old months"
            if include_count
            else "Archive Only may be required for archived months"
            if archive_only_count
            else "Archive behavior could not be proven from sampled old month"
        )

        print(f"working request URL: {url}")
        print("method: GET")
        print(f"payload/query params: {request_params}")
        print(f"cookies/session requirements: {cookie_names}")
        print("CSRF/auth token names if public page uses them: []")
        print(f"page-size: {page_size}")
        print(f"current page / total pages if exposed: 1 / {total_pages}")
        print(f"whether old archived records require Archive Only or Include Archives: {archive_behavior}")

        return {
            "working_request_url": url,
            "method": "GET",
            "query_params": request_params,
            "cookies": cookie_names,
            "csrf_tokens": [],
            "page_size": page_size,
            "current_page": 1,
            "total_pages": total_pages,
            "archive_behavior": archive_behavior,
        }

    def discover_whats_new_range(self, url: str) -> dict[str, Any]:
        discovery = self.collect_all_accessible_records(url)
        records = discovery["records"]
        if not records:
            raise RuntimeError("IRDAI Whats New page returned zero rows")

        valid_dates = [date.fromisoformat(item.date) for item in records if item.date]
        count_by_year = self.count_by_year(records)
        count_by_type = self.count_by_type(records)
        earliest_records = sorted(records, key=lambda row: (row.date, row.subject, row.link))[:10]

        result = {
            "working_route_api_page_flow": "GET listing page with query params selectedYear, selectedMonth, filterArchive; rows embedded in DLFileEntryArray and paginated client-side",
            "available_years_discovered": discovery["years_discovered"],
            "available_months_behavior": "Month must be paired with year; current-year records are exposed on the base page while older records are served through year+month GET filters.",
            "archive_filter_behavior": discovery["archive_behavior"],
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_date_found": max(valid_dates).isoformat(),
            "oldest_date_found": min(valid_dates).isoformat(),
            "total_record_count": len(records),
            "count_by_year": count_by_year,
            "count_by_type": count_by_type,
            "earliest_records": [asdict(item) for item in earliest_records],
            "limitation": discovery["limitation"],
        }

        print(f"working route/API/page-flow: {result['working_route_api_page_flow']}")
        print(f"available years discovered: {result['available_years_discovered']}")
        print(f"available months behavior: {result['available_months_behavior']}")
        print(f"archive filter behavior: {result['archive_filter_behavior']}")
        print(f"whether direct HTTP worked: {result['direct_http_worked']}")
        print(f"whether Playwright was used: {result['playwright_used']}")
        print(f"newest date found: {result['newest_date_found']}")
        print(f"oldest date found: {result['oldest_date_found']}")
        print(f"total record count if available: {result['total_record_count']}")
        print(f"count by year: {result['count_by_year']}")
        print(f"count by visible type/category if available: {result['count_by_type']}")
        print("sample earliest 10 records:")
        for record in earliest_records:
            print(self.console_safe(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}"))
        print(f"limitation: {result['limitation']}")
        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        type_filter: str | None = None,
        include_type: bool = False,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[IRDAIRecord]:
        del retries, retry_base_delay, retry_max_delay
        if max_chunks_this_run is not None and max_chunks_this_run <= 0:
            raise RuntimeError("max_chunks_this_run must be positive when provided.")
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        if all_available:
            discovery = self.collect_all_accessible_records(url)
        else:
            scoped_from = from_date or date(datetime.now(timezone.utc).year, 1, 1)
            scoped_to = to_date or date.today()
            discovery = self.collect_relevant_records(url, from_date=scoped_from, to_date=scoped_to)
        all_records = discovery["records"]
        if not all_records:
            raise RuntimeError("IRDAI Whats New page returned zero rows")

        valid_dates = [date.fromisoformat(record.date) for record in all_records if record.date]
        newest_available_date = max(valid_dates)
        oldest_available_date = min(valid_dates)
        if all_available and from_date is None:
            from_date = oldest_available_date
        if from_date is None:
            from_date = oldest_available_date
        if to_date is None:
            to_date = newest_available_date

        filtered_records = self.filter_records(all_records, from_date=from_date, to_date=to_date, type_filter=type_filter)
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
            checkpoint = IRDAICheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat(),
                oldest_available_date=oldest_available_date.isoformat(),
                years_discovered=discovery["years_discovered"],
                total_records_detected=len(all_records),
                count_by_year=self.count_by_year(all_records),
                count_by_type=self.count_by_type(all_records),
                chunk_strategy="base_page_plus_year_month_get_filters",
                last_completed_chunk=0,
                records_written=existing_count,
                unique_records_written=existing_count,
                started_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                completed=False,
                errors=[],
            )
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

        chunks = discovery["chunks"]
        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        resume_from_chunk = int(chunk_window["resume_from_chunk"])
        expected_end_chunk = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])
        target_chunks = [
            chunk for chunk in chunks if resume_from_chunk <= chunk.index <= expected_end_chunk
        ]

        print(f"Oldest date: {from_date.isoformat()}")
        print(f"Newest date: {to_date.isoformat()}")
        print(f"Expected records: {len(all_records)}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {existing_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {resume_from_chunk}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {expected_end_chunk}")
        print(
            f"actual chunk range: {resume_from_chunk}-{expected_end_chunk}"
            if chunks_this_run
            else "actual chunk range: none"
        )
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return []

        if not resume and output_mode == "overwrite":
            self.write_output([], out_path, include_type=include_type)
            self.write_metadata_sidecar([], out_path)

        written_records: list[IRDAIRecord] = []
        duplicates_skipped = 0
        for chunk in target_chunks:
            chunk_records = [
                record
                for record in filtered_records
                if record.filter_year == chunk.year
                and record.filter_month == chunk.month
                and record.archive_flag == chunk.archive_flag
            ]
            fresh_records: list[IRDAIRecord] = []
            for record in chunk_records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path, include_type=include_type)
            self.append_metadata_sidecar(fresh_records, out_path)
            written_records.extend(fresh_records)
            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=chunk.index,
            )
            checkpoint.last_completed_chunk = chunk.index
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = checkpoint.records_written
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = chunk.index == chunks[-1].index
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        print(f"Rows written: {len(written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(written_records)}")
        if resume or checkpoint_path:
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2, ensure_ascii=True)}")
        self.last_fetch_transport = "httpx"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "irdai_whats_new_validation_report.json"
        year_counts_path = file_path.parent / "irdai_whats_new_year_counts.csv"
        type_counts_path = file_path.parent / "irdai_whats_new_type_counts.csv"

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
        invalid_dates = 0
        duplicate_key_count = 0
        duplicate_keys: set[tuple[str, str, str]] = set()
        seen_keys: set[tuple[str, str, str]] = set()
        suspicious_rows: list[dict[str, Any]] = []
        row_dates: list[date] = []
        year_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}

        metadata_by_index = {
            index: item
            for index, item in enumerate(self.load_metadata_sidecar(file_path))
        }

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            expected_headers = EXPECTED_OUTPUT_HEADERS
            if reader.fieldnames != expected_headers:
                raise RuntimeError(f"Unexpected headers: {reader.fieldnames}; expected {expected_headers}")
            for index, row in enumerate(reader):
                total_rows += 1
                if len(row) != len(expected_headers):
                    malformed_csv_rows += 1
                row_type = metadata_by_index[index].type if index in metadata_by_index else ""
                if row_type:
                    type_counts[row_type] = type_counts.get(row_type, 0) + 1

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
                key = (raw_date, normalized_subject, link)
                if key in seen_keys:
                    duplicate_key_count += 1
                    duplicate_keys.add(key)
                else:
                    seen_keys.add(key)

                try:
                    parsed = date.fromisoformat(raw_date)
                    row_dates.append(parsed)
                    year_key = str(parsed.year)
                    year_counts[year_key] = year_counts.get(year_key, 0) + 1
                except ValueError:
                    invalid_dates += 1
                    suspicious_rows.append({"reason": "invalid_date", "row_number": index + 2, "row": row})

                link_lower = link.lower().split("?", 1)[0]
                if not link:
                    pass
                elif link_lower.endswith(".pdf"):
                    pdf_links += 1
                elif link_lower.endswith(".zip"):
                    zip_links += 1
                elif link_lower.endswith(".doc") or link_lower.endswith(".docx"):
                    doc_links += 1
                elif link_lower.endswith(".xls") or link_lower.endswith(".xlsx"):
                    xls_links += 1
                elif "/document-detail" in link_lower or link_lower.endswith(".html") or link_lower.endswith(".htm"):
                    html_links += 1
                else:
                    other_links += 1

                if link and not link.startswith(IRDAI_BASE_URL):
                    suspicious_rows.append({"reason": "non_irdai_link", "row_number": index + 2, "row": row})
                if subject and len(subject) < 5:
                    suspicious_rows.append({"reason": "very_short_subject", "row_number": index + 2, "row": row})
                if link and link.startswith("/"):
                    suspicious_rows.append({"reason": "broken_looking_relative_url", "row_number": index + 2, "row": row})
                if subject and subject.casefold() in {"previous", "next", "select month", "select year"}:
                    suspicious_rows.append({"reason": "navigation_only_text", "row_number": index + 2, "row": row})

        report = {
            "file": str(file_path),
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "missing_date": missing_date,
            "missing_subject": missing_subject,
            "missing_circular_no": missing_circular_no,
            "missing_link": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "duplicate_keys_sample": [list(item) for item in list(duplicate_keys)[:10]],
            "link_type_counts": {
                "pdf": pdf_links,
                "html_detail": html_links,
                "doc_docx": doc_links,
                "xls_xlsx": xls_links,
                "zip": zip_links,
                "other": other_links,
                "empty": empty_links,
            },
            "rows_per_year": year_counts,
            "min_date": min(row_dates).isoformat() if row_dates else None,
            "max_date": max(row_dates).isoformat() if row_dates else None,
            "count_by_type": type_counts,
            "suspicious_rows": suspicious_rows,
        }

        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", year_counts)
        if type_counts:
            self.write_count_csv(type_counts_path, "type", type_counts)
        print(f"Validation report saved: {report_path}")
        print(f"Year counts saved: {year_counts_path}")
        if type_counts:
            print(f"Type counts saved: {type_counts_path}")
        return report

    def fetch_page_html(self, url: str, params: Optional[dict[str, str]] = None) -> str:
        response = self.get(url, params=params)
        self.last_fetch_transport = "httpx"
        return response.text

    def fetch_filtered_records(
        self,
        *,
        url: str,
        selected_year: str,
        selected_month: str,
        archive_flag: str,
    ) -> list[IRDAIRecord]:
        html = self.fetch_page_html(
            url,
            params={
                "selectedYear": selected_year,
                "selectedMonth": selected_month,
                "filterArchive": archive_flag,
            },
        )
        return self.parse_whats_new_records(
            html,
            url,
            type_value="",
            archive_flag=archive_flag,
            filter_year=selected_year,
            filter_month=selected_month,
        )

    def parse_whats_new_records(
        self,
        html: str,
        source_url: str,
        *,
        type_value: str = "",
        archive_flag: str = "Include Archives",
        filter_year: str = "",
        filter_month: str = "",
    ) -> list[IRDAIRecord]:
        payload = self.extract_embedded_array(html)
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[IRDAIRecord] = []
        for item in payload:
            raw_date = normalize_text(item.get("dateId")) or ""
            parsed_date = self.parse_irdai_date(raw_date)
            subject = self.extract_subject(item)
            fileentry_id = normalize_text(item.get("fileentryId")) or ""
            link = self.normalize_irdai_link(fileentry_id=fileentry_id, source_url=source_url)
            circular_no = self.extract_reference_no(subject=subject, subtitle=normalize_text(item.get("subTitle")) or "", link=link)
            if not parsed_date or not subject:
                continue
            records.append(
                IRDAIRecord(
                    date=parsed_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=self.compose_source_url(source_url, filter_year=filter_year, filter_month=filter_month, archive_flag=archive_flag),
                    scraped_at=scraped_at,
                    type=type_value,
                    archive_flag=archive_flag,
                    filter_year=filter_year,
                    filter_month=filter_month,
                    raw_date=raw_date,
                    subtitle=normalize_text(item.get("subTitle")) or "",
                )
            )
        return records

    def extract_embedded_array(self, html: str) -> list[dict[str, Any]]:
        marker = "var DLFileEntryArray = "
        marker_index = html.find(marker)
        if marker_index < 0:
            return []
        array_start = html.find("[", marker_index)
        if array_start < 0:
            return []

        depth = 0
        in_string = False
        escape = False
        array_end = -1
        for position, character in enumerate(html[array_start:], array_start):
            if in_string:
                if escape:
                    escape = False
                elif character == "\\":
                    escape = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth == 0:
                    array_end = position + 1
                    break
        if array_end < 0:
            return []

        try:
            return json.loads(html[array_start:array_end])
        except json.JSONDecodeError:
            return []

    def extract_filter_controls(self, soup: BeautifulSoup, html: str) -> dict[str, Any]:
        year_options = self.extract_year_options(soup, html)
        month_options = [normalize_text(option.get_text(" ", strip=True)) or "" for option in soup.select("#selectedMonth option") if option.get("value")]
        archive_values = [normalize_text(input_tag.get("value")) or "" for input_tag in soup.select("input[name='filterArchive']")]
        controls = {
            "form_id": "filterFormByirdai" if soup.select_one("#filterFormByirdai") else "",
            "year_select": "selectedYear" if soup.select_one("#selectedYear") else "",
            "month_select": "selectedMonth" if soup.select_one("#selectedMonth") else "",
            "archive_radios": bool(soup.select("input[name='filterArchive']")),
            "page_select": "pageNumber" if soup.select_one("#pageNumber") else "",
        }
        pagination = {
            "has_previous": "prevPage()" in html,
            "has_next": "nextPage()" in html,
            "page_select_present": bool(soup.select_one("#pageNumber")),
            "page_size": self.extract_page_size(html),
        }
        return {
            "controls": controls,
            "year_options": year_options,
            "month_options": month_options,
            "archive_values": archive_values,
            "pagination": pagination,
        }

    def extract_year_options(self, soup: BeautifulSoup, html: str) -> list[str]:
        explicit = [normalize_text(option.get_text(" ", strip=True)) or "" for option in soup.select("#selectedYear option") if option.get("value")]
        explicit = [value for value in explicit if re.fullmatch(r"\d{4}", value)]
        if explicit:
            return explicit

        start_year_match = re.search(r"for\s*\(var\s+year\s*=\s*currentYear;\s*year\s*>=\s*(\d{4});\s*year--\)", html)
        current_year_match = re.search(r"new\s+Date\(\)\.getFullYear\(\)", html)
        if start_year_match and current_year_match:
            start_year = int(start_year_match.group(1))
            current_year = datetime.now(timezone.utc).year
            return [str(year) for year in range(current_year, start_year - 1, -1)]
        return []

    def extract_page_size(self, html: str) -> int:
        match = re.search(r"itemsPerPage\s*=\s*(\d+)", html)
        return int(match.group(1)) if match else 20

    def extract_subject(self, item: dict[str, Any]) -> str:
        title = normalize_text(item.get("title")) or ""
        subtitle = normalize_text(item.get("subTitle")) or ""
        title = re.sub(r"^[\W_\/-]+", "", title).strip()
        subtitle = re.sub(r"^[\W_\/-]+", "", subtitle).strip()

        if title and subtitle:
            folded_title = re.sub(r"[\W_]+", " ", title).casefold().strip()
            folded_subtitle = re.sub(r"[\W_]+", " ", subtitle).casefold().strip()
            if folded_title == folded_subtitle:
                return subtitle
        return subtitle if (subtitle and len(title) < 8) else title or subtitle

    def parse_irdai_date(self, raw_value: str) -> str:
        parsed = parse_indian_date(raw_value)
        return parsed.isoformat() if parsed else ""

    def normalize_irdai_link(self, *, fileentry_id: str, source_url: str) -> str:
        if not fileentry_id:
            return ""
        return urljoin(source_url, f"/web/guest/document-detail?documentId={fileentry_id}")

    def extract_reference_no(self, *, subject: str, subtitle: str, link: str) -> str:
        text = " ".join(part for part in [subject, subtitle, link] if part)
        normalized = normalize_text(text) or ""
        for pattern in SAFE_REFERENCE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                return normalize_text(match.group(1)) or ""
        return ""

    def compose_source_url(self, source_url: str, *, filter_year: str, filter_month: str, archive_flag: str) -> str:
        params: dict[str, str] = {}
        if filter_year:
            params["selectedYear"] = filter_year
        if filter_month:
            params["selectedMonth"] = filter_month
        if filter_year or filter_month:
            params["filterArchive"] = archive_flag
        if not params:
            return source_url
        return f"{source_url}?{urlencode(params)}"

    def filter_records(
        self,
        records: list[IRDAIRecord],
        *,
        from_date: date | None,
        to_date: date | None,
        type_filter: str | None = None,
    ) -> list[IRDAIRecord]:
        filtered: list[IRDAIRecord] = []
        wanted_type = (normalize_text(type_filter) or "").casefold() if type_filter else ""
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            if wanted_type and (normalize_text(record.type) or "").casefold() != wanted_type:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: IRDAIRecord) -> tuple[str, str, str]:
        return (
            record.date,
            (normalize_text(record.subject) or "").casefold(),
            record.link,
        )

    def count_by_year(self, records: list[IRDAIRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            year_key = record.date[:4]
            counts[year_key] = counts.get(year_key, 0) + 1
        return dict(sorted(counts.items()))

    def count_by_type(self, records: list[IRDAIRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.type:
                continue
            counts[record.type] = counts.get(record.type, 0) + 1
        return dict(sorted(counts.items()))

    def collect_link_patterns(self, records: list[IRDAIRecord]) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "xls": 0, "zip": 0, "other": 0}
        for record in records:
            href = record.link.lower().split("?", 1)[0]
            if href.endswith(".pdf"):
                counts["pdf"] += 1
            elif href.endswith(".zip"):
                counts["zip"] += 1
            elif href.endswith(".doc") or href.endswith(".docx"):
                counts["doc"] += 1
            elif href.endswith(".xls") or href.endswith(".xlsx"):
                counts["xls"] += 1
            elif "/document-detail" in href or href.endswith(".html") or href.endswith(".htm"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def count_link_types(self, records: list[IRDAIRecord]) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "xls": 0, "zip": 0, "other": 0, "empty": 0}
        for record in records:
            href = record.link.lower().split("?", 1)[0]
            if not href:
                counts["empty"] += 1
            elif href.endswith(".pdf"):
                counts["pdf"] += 1
            elif href.endswith(".zip"):
                counts["zip"] += 1
            elif href.endswith(".doc") or href.endswith(".docx"):
                counts["doc"] += 1
            elif href.endswith(".xls") or href.endswith(".xlsx"):
                counts["xls"] += 1
            elif "/document-detail" in href or href.endswith(".html") or href.endswith(".htm"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def metadata_sidecar_path(self, out_path: str | Path) -> Path:
        return Path(f"{out_path}.meta.json")

    def load_existing_output_records(self, out_path: str | Path) -> list[IRDAIRecord]:
        out_path = Path(out_path)
        metadata_rows = self.load_metadata_sidecar(out_path)
        metadata_by_index = {index: item for index, item in enumerate(metadata_rows)}
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                records: list[IRDAIRecord] = []
                for index, row in enumerate(reader):
                    metadata = metadata_by_index.get(index)
                    records.append(
                        IRDAIRecord(
                            date=row["date"],
                            subject=row["subject"],
                            circular_no=row.get("circular_no", ""),
                            link=row["link"],
                            source_url=row["source_url"],
                            scraped_at=row["scraped_at"],
                            type=metadata.type if metadata else "",
                            archive_flag=metadata.archive_flag if metadata else "",
                            filter_year=metadata.filter_year if metadata else "",
                            filter_month=metadata.filter_month if metadata else "",
                        )
                    )
                return records
        if out_path.suffix.lower() == ".json":
            items = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                IRDAIRecord(
                    date=item["date"],
                    subject=item["subject"],
                    circular_no=item.get("circular_no", ""),
                    link=item["link"],
                    source_url=item["source_url"],
                    scraped_at=item["scraped_at"],
                    type=item.get("type", ""),
                    archive_flag=item.get("archive_flag", ""),
                    filter_year=item.get("filter_year", ""),
                    filter_month=item.get("filter_month", ""),
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

    def append_output(self, records: list[IRDAIRecord], out_path: str | Path, *, include_type: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_type=include_type) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_type else EXPECTED_OUTPUT_HEADERS
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

    def write_output(self, records: list[IRDAIRecord], out_path: str | Path, *, include_type: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_type=include_type) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_type else EXPECTED_OUTPUT_HEADERS
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

    def record_to_output_row(self, record: IRDAIRecord, *, include_type: bool = False) -> dict[str, str]:
        row = {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_type:
            return {"date": record.date, "type": record.type, **{key: value for key, value in row.items() if key != "date"}}
        return row

    def append_metadata_sidecar(self, records: list[IRDAIRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        existing = []
        if sidecar_path.exists():
            existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
        existing.extend([asdict(item) for item in records])
        sidecar_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_metadata_sidecar(self, records: list[IRDAIRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        sidecar_path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")

    def load_metadata_sidecar(self, out_path: str | Path) -> list[IRDAIRecord]:
        sidecar_path = self.metadata_sidecar_path(out_path)
        if not sidecar_path.exists():
            return []
        return [IRDAIRecord(**item) for item in json.loads(sidecar_path.read_text(encoding="utf-8"))]

    def write_count_csv(self, path: str | Path, label: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([label, "row_count"])
            for key, value in counts.items():
                writer.writerow([key, value])

    def console_safe(self, text: str) -> str:
        return text.encode("ascii", errors="replace").decode("ascii")

    def load_checkpoint(self, checkpoint_path: str | Path) -> IRDAICheckpoint:
        return IRDAICheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: IRDAICheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")

    def collect_all_accessible_records(self, url: str) -> dict[str, Any]:
        return self.collect_relevant_records(url, from_date=None, to_date=None)

    def collect_relevant_records(
        self,
        url: str,
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> dict[str, Any]:
        base_html = self.fetch_page_html(url)
        base_soup = BeautifulSoup(base_html, "html.parser")
        filters = self.extract_filter_controls(base_soup, base_html)
        years = filters["year_options"]

        base_records = self.parse_whats_new_records(base_html, url, archive_flag="Include Archives")
        deduped: dict[tuple[str, str, str], IRDAIRecord] = {}
        for record in base_records:
            if self.record_in_date_window(record, from_date=from_date, to_date=to_date):
                deduped[self.record_dedup_key(record)] = record

        chunks = [IRDAIChunk(index=1, label="base_page", year="", month="", archive_flag="Include Archives")]
        chunk_index = 1

        archive_behavior = "Current page exposes current records; older archive months are queried with Include Archives."
        requested_years = set()
        if from_date and to_date:
            requested_years = {str(year) for year in range(from_date.year, to_date.year + 1)}
        for year in years:
            if year == str(datetime.now(timezone.utc).year):
                continue
            if requested_years and year not in requested_years:
                continue
            for month in MONTH_NAMES:
                if from_date and to_date and not self.month_intersects_window(int(year), month, from_date=from_date, to_date=to_date):
                    continue
                chunk_index += 1
                records = self.fetch_filtered_records(
                    url=url,
                    selected_year=year,
                    selected_month=month,
                    archive_flag="Include Archives",
                )
                if not records:
                    continue
                chunks.append(IRDAIChunk(index=chunk_index, label=f"{year}-{month}", year=year, month=month, archive_flag="Include Archives"))
                for record in records:
                    if self.record_in_date_window(record, from_date=from_date, to_date=to_date):
                        deduped[self.record_dedup_key(record)] = record

        # Probe one older year below the visible year selector to confirm the boundary.
        oldest_visible_year = int(years[-1]) if years else datetime.now(timezone.utc).year
        older_probe_count = 0
        older_probe_year = str(oldest_visible_year - 1)
        if from_date is None and to_date is None:
            for month in MONTH_NAMES:
                older_probe_count += len(
                    self.fetch_filtered_records(
                        url=url,
                        selected_year=older_probe_year,
                        selected_month=month,
                        archive_flag="Include Archives",
                    )
                )

        records = sorted(deduped.values(), key=lambda row: (row.date, row.subject, row.link), reverse=True)
        if from_date is None and to_date is None and years and older_probe_count == 0:
            limitation = f"Visible year controls are generated from {years[0]} down to {years[-1]}; probing {older_probe_year} returned no rows."
        else:
            limitation = "Accessible records were collected from the base page and year/month archive filters."

        return {
            "records": records,
            "years_discovered": years,
            "chunks": chunks,
            "archive_behavior": archive_behavior,
            "limitation": limitation,
        }

    def record_in_date_window(self, record: IRDAIRecord, *, from_date: date | None, to_date: date | None) -> bool:
        record_date = date.fromisoformat(record.date)
        if from_date and record_date < from_date:
            return False
        if to_date and record_date > to_date:
            return False
        return True

    def month_intersects_window(self, year: int, month_name: str, *, from_date: date, to_date: date) -> bool:
        month_index = MONTH_NAMES.index(month_name) + 1
        chunk_start = date(year, month_index, 1)
        if month_index == 12:
            chunk_end = date(year + 1, 1, 1)
        else:
            chunk_end = date(year, month_index + 1, 1)
        return not (chunk_end <= from_date or chunk_start > to_date)
