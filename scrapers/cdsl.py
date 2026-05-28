from __future__ import annotations

import csv
import io
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from pypdf import PdfReader

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


CDSL_SOURCE_LABEL = "CDSL"
CDSL_COMMUNIQUE_URL = "https://www.cdslindia.com/eservices/Publications/Communique"
CDSL_ONLOAD_URL = "https://www.cdslindia.com/eservices/Publications/GetOnLoadCommunique"
CDSL_SEARCH_URL = "https://www.cdslindia.com/eservices/Publications/CommuniquePost"
CDSL_DOWNLOAD_BASE = "https://www.cdslindia.com/eservices/Publications/DownloadFile"
CDSL_INDEX_URL = "https://www.cdslindia.com/Publications/DP-COMMUNIQUES-INDEX.aspx"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
INDEX_ROW_RE = re.compile(r"^\s*(\d+)\s+(DP[0-9A-Za-z\-\/]+)\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+(.*\S)?\s*$")
CDSL_TYPE_LABELS = {"3": "DP", "4": "RTA"}
CDSL_CHUNKS = [("A", "3"), ("A", "4"), ("H", "3"), ("H", "4")]


@dataclass
class CDSLCommuniqueRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    raw_date: Optional[str] = None
    archive_status: Optional[str] = None
    entity_type: Optional[str] = None
    attachment_url: Optional[str] = None


@dataclass
class CDSLChunk:
    index: int
    archive_status: str
    entity_type: str


@dataclass
class CDSLCheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    total_records_detected: Optional[int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


@dataclass
class CDSLIndexRow:
    serial_no: int
    circular_no: str
    date: str
    subject: str


@dataclass
class CDSLInspectionResult:
    url: str
    method: str
    status_code: Optional[int]
    content_type: Optional[str]
    response_size: int
    format: str
    record_count: int
    sample_records: list[dict[str, Any]]
    keys_or_headers: list[str]
    error: Optional[str] = None


class CDSLScraper(BaseScraper):
    source = "cdsl"
    regulator = CDSL_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del to_date
        return self.fetch_onload_payload(archive_status="A", entity_type="3", from_date=from_date)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_communique_records(response, CDSL_COMMUNIQUE_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Communique",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": "Communiques / Sebi Circulars",
                "pdf_url": None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "Communique"),
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

    def inspect_communiques(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/cdsl")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        response = self.fetch_page(url)
        raw_fixture_path = fixture_dir / "communique.html"
        raw_fixture_path.write_text(response.text, encoding="utf-8")
        soup = BeautifulSoup(response.text, "html.parser")

        raw_rows = self.parse_rendered_rows(response.text, url)
        endpoint_results = [
            self.inspect_onload_endpoint("A", "3"),
            self.inspect_onload_endpoint("H", "3"),
            self.inspect_onload_endpoint("A", "4"),
        ]
        sample_payload = self.fetch_onload_payload("A", "3")
        sample_records = self.parse_communique_records(sample_payload, url)

        print(f"Page title: {soup.title.get_text(' ', strip=True) if soup.title else ''}")
        print(f"Communique table/list present: {bool(soup.select('#tblCommuniqueDtl, #tblCommuniquDtlBody'))}")
        print(
            "Search/filter controls found: "
            f"{[selector for selector in ['#cno', '#fromDate', '#toDate', '#Keyword', '#Subject', '#btnSubmit'] if soup.select(selector)]}"
        )
        print(
            "Date/year/category/search controls found: "
            f"{[selector for selector in ['#fromDate', '#toDate', '#dpSelect', '#rtaSelect', '#Keyword', '#Subject'] if soup.select(selector)]}"
        )
        print(
            "Pagination controls found: "
            f"{[selector for selector in ['#tblCommuniqueDtl_paginate', '.paginate_button'] if soup.select(selector)]}"
        )
        print(
            "Table/listing selectors found: "
            f"{[selector for selector in ['#tblCommuniqueDtl', '#tblCommuniquDtlBody'] if soup.select(selector)]}"
        )
        print("Any API URLs found in scripts:")
        for endpoint in sorted(self.extract_api_urls_from_html(response.text)):
            print(endpoint)
        print("Any communique PDF/detail/download URL patterns found:")
        for pattern in [
            "/eservices/Publications/DownloadFile?eventID=<communique_no>&method=communique",
            "../Publications/DownloadFile?eventID=<communique_no>&method=communique",
        ]:
            print(pattern)
        print("First 10 listed/rendered rows:")
        if raw_rows:
            for record in raw_rows[:10]:
                print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        else:
            for record in sample_records[:10]:
                print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        for result in endpoint_results:
            print(json.dumps(asdict(result), indent=2))

        return {
            "raw_fixture_path": str(raw_fixture_path),
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "raw_rows_detected": len(raw_rows),
            "api_urls_found": sorted(self.extract_api_urls_from_html(response.text)),
        }

    def inspect_communique_index(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/cdsl")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        response = self.fetch_page(url)
        content_type = (response.headers.get("content-type") or "").lower()
        extension = ".pdf" if "pdf" in content_type or response.content.startswith(b"%PDF") else ".html"
        fixture_path = fixture_dir / f"dp_communique_index{extension}"
        if extension == ".pdf":
            fixture_path.write_bytes(response.content)
            text = self.extract_pdf_text_preserve_lines(response.content)
        else:
            fixture_path.write_text(response.text, encoding="utf-8")
            text = response.text
        rows = self.parse_index_rows_from_text(text)

        print(f"Response format: {'pdf' if extension == '.pdf' else 'html'}")
        print(f"Saved index file: {fixture_path}")
        print(f"Total index rows detected: {len(rows)}")
        print("First 10 index rows:")
        for row in rows[:10]:
            print(f"{row.date} | {row.subject} | {row.circular_no}")
        print("Last 10 index rows:")
        for row in rows[-10:]:
            print(f"{row.date} | {row.subject} | {row.circular_no}")
        print(f"Oldest communique date in index: {rows[0].date if rows else None}")
        print(f"Newest communique date in index: {rows[-1].date if rows else None}")

        return {
            "format": "pdf" if extension == ".pdf" else "html",
            "fixture_path": str(fixture_path),
            "row_count": len(rows),
            "oldest_date": rows[0].date if rows else None,
            "newest_date": rows[-1].date if rows else None,
        }

    def discover_communique_range(self, url: str) -> dict[str, Any]:
        feed_records = self.fetch_all_feed_records(url)
        unique_records = self.deduplicate_records(feed_records)
        if not unique_records:
            raise RuntimeError("CDSL returned zero communique rows from the public on-load endpoint")

        page_newest = max(date.fromisoformat(item.date) for item in unique_records)
        page_oldest = min(date.fromisoformat(item.date) for item in unique_records)
        index_summary = self.fetch_index_summary(CDSL_INDEX_URL)

        result = {
            "working_endpoint_or_page_flow": {
                "page": url,
                "onload_endpoint": CDSL_ONLOAD_URL,
                "search_endpoint": CDSL_SEARCH_URL,
            },
            "working_query_parameters_or_form_fields": [
                "m_arch_status",
                "type",
                "cno",
                "fromDate",
                "toDate",
                "Keyword",
                "Subject",
                "GCaptcha",
            ],
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_communique_date_found_from_page_api": page_newest.isoformat(),
            "oldest_communique_date_found_from_page_api": page_oldest.isoformat(),
            "oldest_communique_date_found_from_index_pdf": index_summary["oldest_date"],
            "total_count": len(unique_records),
            "sample_earliest_records": [asdict(item) for item in sorted(unique_records, key=lambda item: (item.date, item.circular_no))[:5]],
            "limitation": (
                "CDSL's captcha-protected CommuniquePost search flow was not needed because the public "
                "GetOnLoadCommunique endpoint returned the full DP archive directly. RTA currently returned zero rows."
            ),
        }

        print(f"Working endpoint or page-flow: {json.dumps(result['working_endpoint_or_page_flow'])}")
        print(f"Working query parameters/form fields: {json.dumps(result['working_query_parameters_or_form_fields'])}")
        print(f"Direct HTTP worked: {result['direct_http_worked']}")
        print(f"Playwright used: {result['playwright_used']}")
        print(f"Newest communique date found from page/API: {result['newest_communique_date_found_from_page_api']}")
        print(f"Oldest communique date found from page/API: {result['oldest_communique_date_found_from_page_api']}")
        print(f"Oldest communique date found from index PDF: {result['oldest_communique_date_found_from_index_pdf']}")
        print(f"Total count: {result['total_count']}")
        print("Sample earliest 5 records:")
        for item in sorted(unique_records, key=lambda row: (row.date, row.circular_no))[:5]:
            print(f"{item.date} | {item.subject} | {item.circular_no} | {item.link}")
        print(f"Limitation: {result['limitation']}")
        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[CDSLCommuniqueRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        all_records = self.fetch_all_feed_records(
            url,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        unique_records = self.deduplicate_records(all_records)
        if not unique_records:
            raise RuntimeError("CDSL public on-load endpoint returned zero communique rows")

        newest_available_date = max(date.fromisoformat(item.date) for item in unique_records)
        oldest_available_date = min(date.fromisoformat(item.date) for item in unique_records)

        if all_available and from_date is None:
            from_date = oldest_available_date
        if from_date is None:
            from_date = date(newest_available_date.year, 1, 1)
        if to_date is None:
            to_date = newest_available_date
        if from_date > to_date:
            raise ValueError("from_date must be less than or equal to to_date")

        chunks = [CDSLChunk(index=index, archive_status=arch, entity_type=type_code) for index, (arch, type_code) in enumerate(CDSL_CHUNKS, start=1)]
        existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
        existing_keys = {self.record_dedup_key(item) for item in existing_records}
        existing_count = len(existing_records)
        output_mode = "append" if resume and out_path.exists() else "overwrite"

        if resume and checkpoint_file.exists():
            checkpoint = self.load_checkpoint(checkpoint_file)
        else:
            checkpoint = CDSLCheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat(),
                oldest_available_date=oldest_available_date.isoformat(),
                total_records_detected=len(unique_records),
                chunk_strategy="public_onload_active_archive_feeds",
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

        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        start_chunk = int(chunk_window["resume_from_chunk"])
        expected_end_chunk = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])
        target_chunks = [chunk for chunk in chunks if start_chunk <= chunk.index <= expected_end_chunk]

        print(f"Oldest date: {from_date.isoformat()}")
        print(f"Newest date: {to_date.isoformat()}")
        print(f"Expected records: {len(unique_records)}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {existing_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {start_chunk}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {expected_end_chunk}")
        print(f"actual chunk range: {start_chunk}-{expected_end_chunk}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return []

        if not resume and output_mode == "overwrite":
            self.write_output([], out_path)

        written_records: list[CDSLCommuniqueRecord] = []
        duplicates_skipped = 0
        for chunk in target_chunks:
            payload = self.fetch_onload_payload(
                archive_status=chunk.archive_status,
                entity_type=chunk.entity_type,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            records = self.filter_records(
                self.parse_communique_records(payload, url, archive_status=chunk.archive_status, entity_type=chunk.entity_type),
                from_date=from_date,
                to_date=to_date,
            )
            fresh_records: list[CDSLCommuniqueRecord] = []
            for record in records:
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
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        self.last_fetch_transport = "httpx"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "cdsl_communiques_validation_report.json"
        year_counts_path = file_path.parent / "cdsl_communiques_year_counts.csv"

        malformed_csv_rows = 0
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        empty_links = 0
        pdf_download_links = 0
        detail_links = 0
        other_links = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        year_counts: dict[int, int] = {}
        dates_seen: list[str] = []

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise RuntimeError(f"CDSL export is empty: {file_path}") from exc
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
                elif len(subject) < 5:
                    suspicious_rows.append({"row_number": row_number, "reason": "subject_shorter_than_5", "subject": subject})

                if not circular_no:
                    missing_circular_no += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_circular_no"})

                if not link:
                    missing_link += 1
                    empty_links += 1
                else:
                    lowered = link.lower().split("?", 1)[0]
                    if "downloadfile" in lowered or lowered.endswith(".pdf"):
                        pdf_download_links += 1
                    elif lowered.endswith(".html") or lowered.endswith(".aspx"):
                        detail_links += 1
                    else:
                        other_links += 1
                    if not link.startswith("https://www.cdslindia.com/"):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})

                dedupe_key = self.record_dedup_key(
                    CDSLCommuniqueRecord(
                        date=row_data["date"],
                        subject=row_data["subject"],
                        circular_no=row_data["circular_no"],
                        link=row_data["link"],
                        source_url=row_data["source_url"],
                        scraped_at=row_data["scraped_at"],
                    )
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

        sorted_year_counts = dict(sorted(year_counts.items()))
        report = {
            "file": str(file_path),
            "headers_ok": headers_ok,
            "expected_headers": EXPECTED_OUTPUT_HEADERS,
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "missing_date": missing_date,
            "missing_subject": missing_subject,
            "missing_circular_no": missing_circular_no,
            "missing_link": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": {
                "pdf_or_download": pdf_download_links,
                "detail_page": detail_links,
                "other": other_links,
                "empty": empty_links,
            },
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "rows_per_year": sorted_year_counts,
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        with open(year_counts_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(["year", "row_count"])
            for year, row_count in sorted_year_counts.items():
                writer.writerow([year, row_count])
        print(json.dumps(report, indent=2))
        print(f"Validation report saved: {report_path}")
        print(f"Year counts saved: {year_counts_path}")
        return report

    def fetch_page(self, url: str, *, retries: int = 5, retry_base_delay: float = 3.0, retry_max_delay: float = 60.0) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self.client.get(url)
                response.raise_for_status()
                self.last_fetch_transport = "httpx"
                return response
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                logger.warning("CDSL GET failed for {}. Retry {}/{} after {:.1f}s.", url, attempt, retries, delay)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def fetch_onload_payload(
        self,
        archive_status: str,
        entity_type: str,
        *,
        from_date: date | None = None,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> list[dict[str, Any]]:
        payload = {
            "m_arch_status": archive_status,
            "type": entity_type,
            "cno": "DP%",
            "fromDate": from_date.strftime("%d-%b-%Y") if from_date else "01-Jan-1990",
            "toDate": "",
            "Keyword": "%",
            "Subject": "%",
            "GCaptcha": "%",
        }
        last_exc: Exception | None = None
        self.fetch_page(CDSL_COMMUNIQUE_URL, retries=retries, retry_base_delay=retry_base_delay, retry_max_delay=retry_max_delay)
        for attempt in range(1, retries + 1):
            try:
                response = self.client.post(CDSL_ONLOAD_URL, data=payload, headers={"X-Requested-With": "XMLHttpRequest"})
                response.raise_for_status()
                self.last_fetch_transport = "httpx"
                return response.json()
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                logger.warning(
                    "CDSL on-load feed failed for arch={} type={}. Retry {}/{} after {:.1f}s.",
                    archive_status,
                    entity_type,
                    attempt,
                    retries,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def parse_communique_records(
        self,
        payload: list[dict[str, Any]],
        source_url: str,
        *,
        archive_status: str | None = None,
        entity_type: str | None = None,
    ) -> list[CDSLCommuniqueRecord]:
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[CDSLCommuniqueRecord] = []
        for row in payload:
            circular_no = normalize_text(row.get("comM_ID")) or ""
            raw_date = normalize_text(row.get("comM_DATE")) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None:
                continue
            subject = normalize_text(row.get("subject") or row.get("description")) or ""
            if not subject:
                continue
            attachment_url = normalize_text(row.get("attachmenT_URL")) or ""
            link = self.build_download_link(circular_no, source_url) if circular_no else self.normalize_cdsl_link(attachment_url, source_url)
            records.append(
                CDSLCommuniqueRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date or None,
                    archive_status=archive_status,
                    entity_type=CDSL_TYPE_LABELS.get(entity_type, entity_type) if entity_type else None,
                    attachment_url=self.normalize_cdsl_link(attachment_url, source_url) if attachment_url else None,
                )
            )
        records.sort(key=lambda item: (item.date, item.circular_no, item.subject), reverse=True)
        return records

    def parse_rendered_rows(self, html: str, source_url: str) -> list[CDSLCommuniqueRecord]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("#tblCommuniquDtlBody tr")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[CDSLCommuniqueRecord] = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            circular_no = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            subject = normalize_text(cells[1].get_text(" ", strip=True)) or ""
            raw_date = normalize_text(cells[2].get_text(" ", strip=True)) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None or not subject:
                continue
            records.append(
                CDSLCommuniqueRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=self.build_download_link(circular_no, source_url),
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date,
                )
            )
        return records

    def build_download_link(self, circular_no: str, source_url: str) -> str:
        query = urlencode({"eventID": circular_no, "method": "communique"})
        return urljoin(source_url, f"/eservices/Publications/DownloadFile?{query}")

    def normalize_cdsl_link(self, raw_value: str, source_url: str) -> str:
        value = normalize_text(raw_value) or ""
        if not value:
            return ""
        absolute = urljoin(source_url, value.replace("\\", "/"))
        return absolute.replace("http://www.cdslindia.com/", "https://www.cdslindia.com/")

    def filter_records(
        self,
        records: list[CDSLCommuniqueRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[CDSLCommuniqueRecord]:
        filtered: list[CDSLCommuniqueRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: CDSLCommuniqueRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = (normalize_text(record.subject) or "").casefold()
        if record.circular_no:
            return (record.date, normalized_subject, (normalize_text(record.circular_no) or "").casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def deduplicate_records(self, records: list[CDSLCommuniqueRecord]) -> list[CDSLCommuniqueRecord]:
        deduped: list[CDSLCommuniqueRecord] = []
        seen: set[tuple[str, ...]] = set()
        for record in sorted(records, key=lambda item: (item.date, item.circular_no, item.subject), reverse=True):
            dedupe_key = self.record_dedup_key(record)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(record)
        return deduped

    def fetch_all_feed_records(
        self,
        source_url: str,
        *,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> list[CDSLCommuniqueRecord]:
        all_records: list[CDSLCommuniqueRecord] = []
        for arch, type_code in CDSL_CHUNKS:
            payload = self.fetch_onload_payload(
                archive_status=arch,
                entity_type=type_code,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            all_records.extend(self.parse_communique_records(payload, source_url, archive_status=arch, entity_type=type_code))
        return all_records

    def fetch_index_summary(self, url: str) -> dict[str, Any]:
        response = self.fetch_page(url)
        if response.content.startswith(b"%PDF") or "pdf" in (response.headers.get("content-type") or "").lower():
            text = self.extract_pdf_text_preserve_lines(response.content)
        else:
            text = response.text
        rows = self.parse_index_rows_from_text(text or "")
        return {
            "row_count": len(rows),
            "oldest_date": rows[0].date if rows else None,
            "newest_date": rows[-1].date if rows else None,
            "rows": rows,
        }

    def extract_pdf_text_preserve_lines(self, pdf_bytes: bytes) -> str:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception as exc:  # pragma: no cover
                logger.warning("CDSL index PDF extraction failed on one page: {}", exc)
        return "\n".join(parts)

    def parse_index_rows_from_text(self, text: str) -> list[CDSLIndexRow]:
        rows: list[CDSLIndexRow] = []
        current: CDSLIndexRow | None = None
        for raw_line in text.splitlines():
            line = normalize_text(raw_line) or ""
            if not line:
                continue
            if line.startswith("Page ") or line.startswith("DP-COMMUNIQUE-INDEX") or line.startswith("Sr.No"):
                continue
            match = INDEX_ROW_RE.match(line)
            if match:
                if current is not None:
                    rows.append(current)
                subject = normalize_text(match.group(4)) or ""
                parsed_date = parse_indian_date(match.group(3))
                if parsed_date is None:
                    current = None
                    continue
                current = CDSLIndexRow(
                    serial_no=int(match.group(1)),
                    circular_no=match.group(2),
                    date=parsed_date.isoformat(),
                    subject=subject,
                )
                continue
            if current is not None:
                current.subject = normalize_text(f"{current.subject} {line}") or current.subject
        if current is not None:
            rows.append(current)
        rows.sort(key=lambda item: (item.date, item.circular_no))
        return rows

    def inspect_onload_endpoint(self, archive_status: str, entity_type: str) -> CDSLInspectionResult:
        try:
            payload = self.fetch_onload_payload(archive_status, entity_type)
            return CDSLInspectionResult(
                url=CDSL_ONLOAD_URL,
                method="POST",
                status_code=200,
                content_type="application/json",
                response_size=len(json.dumps(payload)),
                format="json",
                record_count=len(payload),
                sample_records=payload[:3],
                keys_or_headers=sorted(payload[0].keys()) if payload else [],
            )
        except Exception as exc:
            return CDSLInspectionResult(
                url=CDSL_ONLOAD_URL,
                method="POST",
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                content_type=getattr(getattr(exc, "response", None), "headers", {}).get("content-type") if getattr(exc, "response", None) else None,
                response_size=0,
                format="json",
                record_count=0,
                sample_records=[],
                keys_or_headers=[],
                error=str(exc),
            )

    def extract_api_urls_from_html(self, html: str) -> set[str]:
        urls: set[str] = set()
        if "GetOnLoadCommunique" in html:
            urls.add(CDSL_ONLOAD_URL)
        if "CommuniquePost" in html:
            urls.add(CDSL_SEARCH_URL)
        return urls

    def load_existing_output_records(self, out_path: str | Path) -> list[CDSLCommuniqueRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    CDSLCommuniqueRecord(
                        date=row["date"],
                        subject=row["subject"],
                        circular_no=row["circular_no"],
                        link=row["link"],
                        source_url=row["source_url"],
                        scraped_at=row["scraped_at"],
                    )
                    for row in reader
                ]
        if out_path.suffix.lower() == ".json":
            return [CDSLCommuniqueRecord(**row) for row in json.loads(out_path.read_text(encoding="utf-8"))]
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

    def append_output(self, records: list[CDSLCommuniqueRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item) for item in records]
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
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

    def write_output(self, records: list[CDSLCommuniqueRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item) for item in records]
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def record_to_output_row(self, record: CDSLCommuniqueRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: str | Path) -> CDSLCheckpoint:
        return CDSLCheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: CDSLCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
