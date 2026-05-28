from __future__ import annotations

import csv
import json
import math
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NSE_SOURCE_LABEL = "NSE"
NSE_CIRCULARS_PAGE_URL = "https://www.nseindia.com/resources/exchange-communication-circulars"
NSE_HOME_URL = "https://www.nseindia.com/"
NSE_CIRCULARS_API_URL = "https://www.nseindia.com/api/circulars"
NSE_LATEST_CIRCULAR_API_URL = "https://www.nseindia.com/api/latest-circular"


@dataclass
class NSECircularRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    department: Optional[str] = None
    category: Optional[str] = None
    raw_date: Optional[str] = None


@dataclass
class NSEEndpointResult:
    url: str
    method: str
    status_code: int
    content_type: Optional[str]
    response_size: int
    json_keys: list[str]
    csv_headers: list[str]
    record_count: int
    sample_records: list[dict[str, Any]]


@dataclass
class NSEChunk:
    index: int
    from_date: date
    to_date: date


@dataclass
class NSECircularCheckpoint:
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


class NSEScraper(BaseScraper):
    source = "nse"
    regulator = NSE_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        return self.fetch_circulars_payload(from_date=from_date, to_date=to_date)

    def parse_listing(self, response: Any) -> Iterable[dict[str, Any]]:
        for record in self.parse_circular_records(response, NSE_CIRCULARS_PAGE_URL):
            yield {
                "title": record.subject,
                "url": record.link,
                "document_type": "Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": record.department,
                "category": record.category or "Exchange Communication",
                "pdf_url": None,
            }

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "Circular"),
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

    def inspect_circulars(self, url: str) -> dict[str, Any]:
        warmup_results = self.warmup_nse_session(url)
        page_response = self.request_nse_page(url)

        fixture_dir = Path("tests/fixtures/nse")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = fixture_dir / "exchange_communication_circulars.html"
        fixture_path.write_text(page_response.text, encoding="utf-8")

        page_soup = self.parse_html(page_response)
        page_text = page_response.text
        title = page_soup.title.get_text(" ", strip=True) if page_soup.title else ""
        filters_present = bool(page_soup.select("#circularList, #circularKeyword, #go_btn"))
        custom_date_controls_present = bool(page_soup.select("#ex-circular-startDate, #ex-circular-endDate"))
        csv_control_present = bool(page_soup.select("#downloadCSV"))
        api_urls_found = sorted(set(self.extract_api_urls_from_html(page_text)))

        endpoint_results = [
            self.inspect_endpoint(NSE_CIRCULARS_API_URL),
            self.inspect_endpoint(NSE_LATEST_CIRCULAR_API_URL),
        ]

        print(f"Page title: {title}")
        print(f"Fixture saved: {fixture_path}")
        print(f"Circular filters present: {filters_present}")
        print(f"Custom date controls present: {custom_date_controls_present}")
        print(f"CSV download control present: {csv_control_present}")
        print("Warm-up results:")
        for result in warmup_results:
            print(json.dumps(result, indent=2))
        print("API URLs found in page scripts/markup:")
        for api_url in api_urls_found:
            print(api_url)
        print("Endpoint inspection results:")
        for endpoint_result in endpoint_results:
            print(json.dumps(asdict(endpoint_result), indent=2))

        return {
            "title": title,
            "filters_present": filters_present,
            "custom_date_controls_present": custom_date_controls_present,
            "csv_control_present": csv_control_present,
            "api_urls_found": api_urls_found,
            "endpoint_results": [asdict(item) for item in endpoint_results],
        }

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        expected_headers = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
        report_path = file_path.parent / "nse_circulars_validation_report.json"
        year_counts_path = file_path.parent / "nse_circulars_year_counts.csv"

        malformed_csv_rows = 0
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        empty_links = 0
        pdf_links = 0
        zip_links = 0
        other_links = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        year_counts: dict[int, int] = {}
        dates_seen: list[str] = []
        bad_headers = False

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration:
                raise RuntimeError(f"NSE export is empty: {file_path}")
            bad_headers = headers != expected_headers

            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(expected_headers):
                    malformed_csv_rows += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "malformed_csv_row", "row": row})
                    continue

                total_rows += 1
                row_data = dict(zip(expected_headers, row, strict=True))
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
                        if parsed_date < date(1996, 1, 29) or parsed_date > date(2026, 5, 16):
                            suspicious_rows.append(
                                {"row_number": row_number, "reason": "date_out_of_expected_range", "date": row_date}
                            )
                    except ValueError:
                        missing_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_iso_date", "date": row_date})

                if not subject:
                    missing_subject += 1
                elif len(subject) < 5:
                    suspicious_rows.append({"row_number": row_number, "reason": "subject_shorter_than_5", "subject": subject})

                if not circular_no:
                    missing_circular_no += 1
                    if subject and link:
                        suspicious_rows.append(
                            {"row_number": row_number, "reason": "missing_circular_no_with_subject_and_link", "subject": subject, "link": link}
                        )

                if not link:
                    missing_link += 1
                    empty_links += 1
                else:
                    lowered = link.lower()
                    if lowered.endswith(".pdf"):
                        pdf_links += 1
                    elif lowered.endswith(".zip"):
                        zip_links += 1
                    else:
                        other_links += 1
                    if not (
                        link.startswith("https://nsearchives.nseindia.com/")
                        or link.startswith("https://www.nseindia.com/")
                    ):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})

                dedupe_key = self.record_dedup_key(
                    NSECircularRecord(
                        date=row_date,
                        subject=subject,
                        circular_no=circular_no,
                        link=link,
                        source_url=row_data["source_url"],
                        scraped_at=row_data["scraped_at"],
                    )
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

        min_date = min(dates_seen) if dates_seen else None
        max_date = max(dates_seen) if dates_seen else None
        year_counts_rows = [{"year": year, "count": count} for year, count in sorted(year_counts.items())]
        report = {
            "file": str(file_path),
            "headers_exact": not bad_headers,
            "expected_headers": expected_headers,
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "min_date": min_date,
            "max_date": max_date,
            "rows_per_year": year_counts_rows,
            "missing_fields": {
                "date": missing_date,
                "subject": missing_subject,
                "circular_no": missing_circular_no,
                "link": missing_link,
            },
            "link_counts": {
                "pdf": pdf_links,
                "zip": zip_links,
                "other": other_links,
                "empty": empty_links,
            },
            "duplicate_key_count": duplicate_key_count,
            "suspicious_row_count": len(suspicious_rows),
            "suspicious_rows": suspicious_rows[:200],
        }

        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        with open(year_counts_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=["year", "count"])
            writer.writeheader()
            writer.writerows(year_counts_rows)

        print(f"Total rows: {total_rows}")
        print(f"Min date: {min_date}")
        print(f"Max date: {max_date}")
        print("Rows per year summary:")
        for item in year_counts_rows:
            print(f"{item['year']}: {item['count']}")
        print(f"Missing field counts: {json.dumps(report['missing_fields'])}")
        print(f"PDF link count: {pdf_links}")
        print(f"ZIP link count: {zip_links}")
        print(f"Other link count: {other_links}")
        print(f"Duplicate key count: {duplicate_key_count}")
        print(f"Suspicious row count: {len(suspicious_rows)}")
        print(f"Validation report: {report_path}")
        print(f"Year counts CSV: {year_counts_path}")
        return report

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        del url
        default_payload = self.fetch_circulars_payload()
        default_records = self.parse_circular_records(default_payload, NSE_CIRCULARS_PAGE_URL)
        if not default_records:
            raise RuntimeError("NSE circular API returned zero records for the default range")

        newest_available_date = max(date.fromisoformat(record.date) for record in default_records)
        oldest_available_date, max_window_days = self.discover_oldest_available_date(newest_available_date)

        probe_payload = self.fetch_circulars_payload(from_date=oldest_available_date, to_date=newest_available_date)
        probe_records = self.parse_circular_records(probe_payload, NSE_CIRCULARS_PAGE_URL)
        earliest_records = sorted(probe_records, key=lambda record: record.date)[:5]

        result = {
            "working_endpoint": NSE_CIRCULARS_API_URL,
            "working_query_params": {
                "fromDate": oldest_available_date.strftime("%d-%m-%Y"),
                "toDate": newest_available_date.strftime("%d-%m-%Y"),
            },
            "csv_export_works": False,
            "max_accepted_date_window_days": max_window_days,
            "newest_available_date": newest_available_date.isoformat(),
            "oldest_available_date": oldest_available_date.isoformat(),
            "total_count": len(probe_records),
            "sample_earliest_records": [asdict(record) for record in earliest_records],
            "limitation": "No separate CSV export endpoint was discovered in direct HTTP inspection; the page exposes a client-side Download (.csv) control.",
        }

        print(f"Working endpoint: {result['working_endpoint']}")
        print(f"Working query params: {json.dumps(result['working_query_params'])}")
        print(f"CSV export works: {result['csv_export_works']}")
        print(f"Max accepted date window (days): {result['max_accepted_date_window_days']}")
        print(f"Newest available circular date: {result['newest_available_date']}")
        print(f"Oldest available circular date: {result['oldest_available_date']}")
        print(f"Total count in discovery range: {result['total_count']}")
        print("Sample earliest 5 records:")
        for record in earliest_records:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print(f"Limitation: {result['limitation']}")

        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        department: str | None = None,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[NSECircularRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)
        newest_available_date, oldest_available_date = self.get_available_range()
        expected_records = self.count_records_for_range(oldest_available_date, newest_available_date) if all_available else None

        scrape_from = from_date or (oldest_available_date if all_available else newest_available_date - timedelta(days=7))
        scrape_to = to_date or newest_available_date
        if scrape_from > scrape_to:
            raise ValueError("from_date must be less than or equal to to_date")

        chunks = self.build_chunks(scrape_from, scrape_to)
        chunk_strategy = "yearly_date_windows"
        existing_records = self.load_existing_output_records(out_path) if (resume and out_path.exists()) else []
        csv_row_count = len(existing_records)

        started_at = datetime.now(timezone.utc).isoformat()
        checkpoint = NSECircularCheckpoint(
            source_url=url,
            output_path=str(out_path),
            newest_available_date=newest_available_date.isoformat(),
            oldest_available_date=oldest_available_date.isoformat(),
            total_records_detected=None,
            chunk_strategy=chunk_strategy,
            last_completed_chunk=0,
            records_written=0,
            unique_records_written=0,
            started_at=started_at,
            updated_at=started_at,
            completed=False,
            errors=[],
        )
        if resume and checkpoint_file.exists():
            checkpoint = self.load_checkpoint(checkpoint_file)
            if csv_row_count != checkpoint.records_written:
                print(
                    "Warning: CSV and checkpoint disagree. "
                    f"CSV rows={csv_row_count}, checkpoint rows={checkpoint.records_written}. Preferring CSV for dedupe safety."
                )
                checkpoint.records_written = csv_row_count
                checkpoint.unique_records_written = len({self.record_dedup_key(record) for record in existing_records})
        elif not resume:
            if out_path.exists():
                out_path.unlink()
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            existing_records = []
            csv_row_count = 0

        seen_keys = {self.record_dedup_key(record) for record in existing_records}
        collected_records = list(existing_records)
        output_mode = "append" if resume and out_path.exists() else "overwrite"
        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        start_index = int(chunk_window["resume_from_chunk"])
        chunk_stop_index = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])

        print(f"Oldest date: {scrape_from.isoformat()}")
        print(f"Newest date: {scrape_to.isoformat()}")
        print(f"Expected records: {expected_records if expected_records is not None else 'unknown'}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {csv_row_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {start_index}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {chunk_stop_index}")
        print(f"actual chunk range: {start_index}-{chunk_stop_index}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return collected_records

        rows_appended = 0
        duplicates_skipped = 0
        retry_triggered = False
        for chunk in chunks:
            if chunk.index < start_index or chunk.index > chunk_stop_index:
                continue

            try:
                payload, chunk_retried = self.fetch_chunk_payload_with_retry(
                    chunk=chunk,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
            except Exception as exc:
                checkpoint.errors.append(f"chunk {chunk.index}: {exc.__class__.__name__}: {exc}")
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                self.save_checkpoint(checkpoint_file, checkpoint)
                resume_command = self.build_resume_command(
                    url=url,
                    out_path=out_path,
                    checkpoint_path=checkpoint_file,
                    chunk_start=chunk.index,
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                print(f"Chunk {chunk.index} failed after {retries} retries.")
                print(f"Resume with: {resume_command}")
                raise RuntimeError(
                    f"Stopping after repeated failures on chunk {chunk.index}. Resume with: {resume_command}"
                ) from exc
            retry_triggered = retry_triggered or chunk_retried

            records = self.parse_circular_records(payload, url)
            filtered_records = self.filter_records(records, department=department, from_date=from_date, to_date=to_date)
            new_records: list[NSECircularRecord] = []
            for record in filtered_records:
                key = self.record_dedup_key(record)
                if key in seen_keys:
                    duplicates_skipped += 1
                    continue
                seen_keys.add(key)
                new_records.append(record)
                collected_records.append(record)

            if new_records:
                self.append_output(new_records, out_path)
                rows_appended += len(new_records)

            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=chunk.index,
            )
            checkpoint.last_completed_chunk = chunk.index
            checkpoint.records_written = csv_row_count + rows_appended
            checkpoint.unique_records_written = len(seen_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            self.save_checkpoint(checkpoint_file, checkpoint)
            time.sleep(delay_seconds)

        checkpoint.completed = checkpoint.last_completed_chunk >= len(chunks)
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        self.save_checkpoint(checkpoint_file, checkpoint)

        final_row_count = len(collected_records)
        print(f"Rows written: {rows_appended}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {final_row_count}")
        print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        print(f"Completed chunks {start_index}-{checkpoint.last_completed_chunk}.")
        print(f"Rows appended: {rows_appended}.")
        print(f"New checkpoint last_completed_chunk: {checkpoint.last_completed_chunk}.")
        print(f"Retry logic triggered: {retry_triggered}.")
        print("Run the same command again to continue.")
        return collected_records

    def warmup_nse_session(self, url: str) -> list[dict[str, Any]]:
        results = []
        for target in [NSE_HOME_URL, url]:
            try:
                response = self.request_nse_page(target, raise_for_status=False)
                results.append(
                    {
                        "url": target,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type"),
                        "response_size": len(response.content),
                    }
                )
            except Exception as exc:
                results.append({"url": target, "error": f"{exc.__class__.__name__}: {exc}"})
        return results

    def request_nse_page(self, url: str, *, raise_for_status: bool = True) -> httpx.Response:
        headers = dict(self.headers)
        headers["Referer"] = NSE_CIRCULARS_PAGE_URL
        response = self.client.get(url, headers=headers)
        if raise_for_status:
            response.raise_for_status()
        return response

    def inspect_endpoint(self, url: str) -> NSEEndpointResult:
        response = self.request_nse_page(url, raise_for_status=False)
        content_type = response.headers.get("content-type")
        json_keys: list[str] = []
        csv_headers: list[str] = []
        sample_records: list[dict[str, Any]] = []
        record_count = 0

        if content_type and "json" in content_type:
            payload = response.json()
            json_keys = list(payload.keys())
            data = payload.get("data") or []
            record_count = len(data)
            sample_records = data[:3]
        elif content_type and "csv" in content_type:
            lines = response.text.splitlines()
            if lines:
                csv_headers = next(csv.reader([lines[0]]))
                record_count = max(0, len(lines) - 1)

        return NSEEndpointResult(
            url=url,
            method="GET",
            status_code=response.status_code,
            content_type=content_type,
            response_size=len(response.content),
            json_keys=json_keys,
            csv_headers=csv_headers,
            record_count=record_count,
            sample_records=sample_records,
        )

    def fetch_circulars_payload(self, *, from_date: date | None = None, to_date: date | None = None) -> dict[str, Any]:
        params = {}
        if from_date is not None:
            params["fromDate"] = from_date.strftime("%d-%m-%Y")
        if to_date is not None:
            params["toDate"] = to_date.strftime("%d-%m-%Y")
        headers = {
            **self.headers,
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_CIRCULARS_PAGE_URL,
        }
        response = self.client.get(NSE_CIRCULARS_API_URL, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if "data" not in payload:
            raise RuntimeError(f"NSE circular API returned unexpected payload keys: {list(payload.keys())}")
        return payload

    def parse_circular_records(self, payload: dict[str, Any], source_url: str) -> list[NSECircularRecord]:
        records = payload.get("data") or []
        scraped_at = datetime.now(timezone.utc).isoformat()
        parsed_records: list[NSECircularRecord] = []
        for item in records:
            # Live mapping confirmed from NSE /api/circulars:
            # cirDate -> date, sub -> subject, circDisplayNo/circNumber -> circular_no, circFilelink -> link
            raw_date = normalize_text(str(item.get("cirDate") or item.get("cirDisplayDate") or ""))
            normalized_date = self.normalize_nse_date(raw_date)
            subject = normalize_text(item.get("sub") or item.get("subject") or item.get("title") or "")
            circular_no = normalize_text(item.get("circDisplayNo") or item.get("circNumber") or item.get("cirNo") or "")
            link = self.normalize_nse_link(item.get("circFilelink") or item.get("circUrl") or item.get("downloadUrl") or "")
            if not normalized_date or not subject or not link:
                continue
            parsed_records.append(
                NSECircularRecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    department=normalize_text(item.get("circDepartment") or item.get("department") or "") or None,
                    category=normalize_text(item.get("circCategory") or item.get("category") or "") or None,
                    raw_date=raw_date,
                )
            )
        return parsed_records

    def normalize_nse_date(self, raw_value: str) -> str:
        if not raw_value:
            raise RuntimeError("NSE record is missing circular date")
        if len(raw_value) == 8 and raw_value.isdigit():
            return date(int(raw_value[0:4]), int(raw_value[4:6]), int(raw_value[6:8])).isoformat()
        parsed_date = parse_indian_date(raw_value)
        if parsed_date is None:
            raise RuntimeError(f"Unable to parse NSE circular date: {raw_value}")
        return parsed_date.isoformat()

    def normalize_nse_link(self, raw_value: str) -> str:
        value = normalize_text(raw_value)
        if not value:
            raise RuntimeError("NSE record is missing circular link")
        return urljoin("https://www.nseindia.com", value)

    def filter_records(
        self,
        records: list[NSECircularRecord],
        *,
        department: str | None,
        from_date: date | None,
        to_date: date | None,
    ) -> list[NSECircularRecord]:
        normalized_department = normalize_text(department).casefold() if department else None
        filtered: list[NSECircularRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            if normalized_department:
                haystacks = [
                    normalize_text(record.department or "").casefold(),
                    normalize_text(record.category or "").casefold(),
                ]
                if normalized_department not in haystacks:
                    continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: NSECircularRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = normalize_text(record.subject).casefold()
        if record.circular_no:
            return (record.date, normalized_subject, normalize_text(record.circular_no).casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def build_chunks(self, from_date: date, to_date: date) -> list[NSEChunk]:
        chunks: list[NSEChunk] = []
        chunk_start = from_date
        index = 1
        while chunk_start <= to_date:
            chunk_end = min(to_date, chunk_start + timedelta(days=364))
            chunks.append(NSEChunk(index=index, from_date=chunk_start, to_date=chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
            index += 1
        return chunks

    def discover_oldest_available_date(self, newest_available_date: date) -> tuple[date, int]:
        window_days = 365
        previous_oldest: date | None = None
        max_window_days = window_days
        while True:
            probe_start = newest_available_date - timedelta(days=window_days - 1)
            payload = self.fetch_circulars_payload(from_date=probe_start, to_date=newest_available_date)
            records = self.parse_circular_records(payload, NSE_CIRCULARS_PAGE_URL)
            if not records:
                raise RuntimeError(
                    f"NSE returned zero records while probing range {probe_start.isoformat()} to {newest_available_date.isoformat()}"
                )
            oldest_in_probe = min(date.fromisoformat(record.date) for record in records)
            if previous_oldest is not None and oldest_in_probe == previous_oldest and probe_start < oldest_in_probe:
                return oldest_in_probe, max_window_days
            previous_oldest = oldest_in_probe
            window_days *= 2
            max_window_days = max(max_window_days, window_days // 2)
            if window_days > 365 * 256:
                return oldest_in_probe, max_window_days

    def get_available_range(self) -> tuple[date, date]:
        default_payload = self.fetch_circulars_payload()
        default_records = self.parse_circular_records(default_payload, NSE_CIRCULARS_PAGE_URL)
        if not default_records:
            raise RuntimeError("NSE circular API returned zero records in default range")
        newest_available_date = max(date.fromisoformat(record.date) for record in default_records)
        oldest_available_date, _ = self.discover_oldest_available_date(newest_available_date)
        return newest_available_date, oldest_available_date

    def fetch_chunk_payload_with_retry(
        self,
        *,
        chunk: NSEChunk,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> tuple[dict[str, Any], bool]:
        retry_triggered = False
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                payload = self.fetch_circulars_payload(from_date=chunk.from_date, to_date=chunk.to_date)
                return payload, retry_triggered
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                retry_triggered = True
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                message = (
                    f"Chunk {chunk.index} ({chunk.from_date.isoformat()} to {chunk.to_date.isoformat()}) "
                    f"failed with {exc.__class__.__name__}. Retry {attempt}/{retries} after {delay:.1f}s."
                )
                print(message)
                logger.warning(message)
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def extract_api_urls_from_html(self, html: str) -> list[str]:
        urls: list[str] = []
        for token in [
            NSE_CIRCULARS_API_URL,
            NSE_LATEST_CIRCULAR_API_URL,
            "/api/circulars",
            "/api/latest-circular",
        ]:
            if token in html:
                urls.append(token)
        return urls

    def build_resume_command(
        self,
        *,
        url: str,
        out_path: str | Path,
        checkpoint_path: str | Path,
        chunk_start: int,
        delay_seconds: float,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> str:
        return (
            'py -3.13 -m main scrape-url --source nse '
            f'--url "{url}" '
            f'--out "{out_path}" '
            '--resume '
            f'--checkpoint "{checkpoint_path}" '
            f'--max-chunks-this-run 1 '
            f'--delay-seconds {delay_seconds} '
            f'--retries {retries} '
            f'--retry-base-delay {retry_base_delay} '
            f'--retry-max-delay {retry_max_delay}'
        )

    def load_existing_output_records(self, out_path: str | Path) -> list[NSECircularRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    NSECircularRecord(
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
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                NSECircularRecord(
                    date=row["date"],
                    subject=row["subject"],
                    circular_no=row["circular_no"],
                    link=row["link"],
                    source_url=row["source_url"],
                    scraped_at=row["scraped_at"],
                )
                for row in payload
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

    def append_output(self, records: list[NSECircularRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(
                    file_obj,
                    fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"],
                )
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

    def write_output(self, records: list[NSECircularRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(
                    file_obj,
                    fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"],
                )
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def count_records_for_range(self, from_date: date, to_date: date) -> int:
        payload = self.fetch_circulars_payload(from_date=from_date, to_date=to_date)
        return len(payload.get("data") or [])

    def record_to_output_row(self, record: NSECircularRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: str | Path) -> NSECircularCheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return NSECircularCheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: NSECircularCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
