from __future__ import annotations

import csv
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NSDL_SOURCE_LABEL = "NSDL"
NSDL_CIRCULAR_STAT_URL = "https://nsdl.co.in/business/circular_stat.php"
NSDL_CIRCULAR_MAIN_URL = "https://nsdl.co.in/business/circular.php"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]


@dataclass
class NSDLCircularRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    status: Optional[str] = None
    raw_date: Optional[str] = None
    broken_hint: Optional[str] = None


@dataclass
class NSDLCheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    years_discovered: list[int]
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
class NSDLChunk:
    index: int
    kind: str
    year: int | None = None


@dataclass
class NSDLLinkCheckRow:
    circular_no: str
    date: str
    subject: str
    link: str
    status_code: Optional[int]
    content_type: Optional[str]
    final_url: str
    ok: bool
    error: str


class NSDLScraper(BaseScraper):
    source = "nsdl"
    regulator = NSDL_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "playwright"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del to_date
        with self.browser_session(headless=True) as session:
            return self.fetch_year_page_html(from_date.year, session=session)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_year_page_records(str(response), NSDL_CIRCULAR_MAIN_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": "DP Circulars",
                "pdf_url": None,
            }
            for record in records
        ]

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
            pdf_url=None,
            pdf_sha256=None,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def inspect_circulars(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/nsdl")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        routes = [
            ("circular_stat", NSDL_CIRCULAR_STAT_URL),
            ("circular_main", NSDL_CIRCULAR_MAIN_URL),
            ("circular_2026", f"{NSDL_CIRCULAR_MAIN_URL}?yr=2026"),
            ("circular_2025", f"{NSDL_CIRCULAR_MAIN_URL}?yr=2025"),
            ("circular_2018", f"{NSDL_CIRCULAR_MAIN_URL}?yr=2018"),
        ]

        with self.browser_session(headless=True) as session:
            for name, route in routes:
                browser_result = self.fetch_route_with_browser(route, session=session)
                fixture_dir.joinpath(f"{name}.html").write_text(browser_result["html"], encoding="utf-8")

                try:
                    direct_result = self.fetch_route_direct(route)
                    direct_status = direct_result["status"]
                    request_rejected = self.is_request_rejected(direct_result["html"])
                    final_url = direct_result["final_url"]
                except Exception as exc:
                    direct_status = None
                    request_rejected = True
                    final_url = route
                    direct_error = f"{type(exc).__name__}: {exc}"
                else:
                    direct_error = ""

                soup = BeautifulSoup(browser_result["html"], "html.parser")
                year_options = self.extract_year_options(soup)
                listed_rows = self.parse_status_page_records(browser_result["html"], route) if "circular_stat" in route else self.parse_year_page_records(browser_result["html"], route)
                print(f"URL: {route}")
                print(f"HTTP status: {direct_status if direct_status is not None else browser_result['status']}")
                print(f"final URL after redirects: {browser_result['final_url']}")
                print(f"page title: {soup.title.get_text(' ', strip=True) if soup.title else ''}")
                print(f"whether request was rejected: {request_rejected}")
                print(f"whether a year selector exists: {bool(year_options)}")
                print(f"available years discovered: {year_options}")
                print(f"table/list selectors found: {[selector for selector in ['table', 'select#yr', 'select[name=yr]'] if soup.select(selector)]}")
                print(f"circular row count found: {len(listed_rows)}")
                print("first 10 listed rows:")
                for record in listed_rows[:10]:
                    print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
                print(f"link patterns found: {self.collect_link_patterns(soup)}")
                print(f"whether circular links are PDF, HTML, DOC, ZIP, or other: {self.count_link_types(listed_rows)}")
                if direct_error:
                    print(f"direct HTTP error: {direct_error}")
                print("---")
        return {"routes": [route for _, route in routes]}

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        with self.browser_session(headless=True) as session:
            main_html = self.fetch_route_html(NSDL_CIRCULAR_MAIN_URL, session=session)
            main_soup = BeautifulSoup(main_html, "html.parser")
            years = self.extract_year_options(main_soup)
            oldest_tested_year = min(years + [1996]) if years else 1996
            status_html = self.fetch_route_html(NSDL_CIRCULAR_STAT_URL, session=session)
            status_records = self.parse_status_page_records(status_html, NSDL_CIRCULAR_STAT_URL)

            records_by_year: dict[int, list[NSDLCircularRecord]] = {}
            for year in sorted(years, reverse=True):
                records_by_year[year] = self.parse_year_page_records(
                    self.fetch_year_page_html(year, session=session),
                    f"{NSDL_CIRCULAR_MAIN_URL}?yr={year}",
                )
            extra_1996 = self.parse_year_page_records(self.fetch_year_page_html(1996, session=session), f"{NSDL_CIRCULAR_MAIN_URL}?yr=1996")
            all_records = self.deduplicate_records(
                status_records + [record for year in sorted(records_by_year) for record in records_by_year[year]]
            )
            if not all_records:
                raise RuntimeError("NSDL year pages returned zero circular rows")

            index_years = years[:]
            oldest_year_with_rows = min(year for year, records in records_by_year.items() if records)
            newest_date = max(date.fromisoformat(item.date) for item in all_records)
            oldest_date = min(date.fromisoformat(item.date) for item in all_records)

        result = {
            "working_route": NSDL_CIRCULAR_MAIN_URL,
            "available_years_discovered": index_years,
            "newest_circular_date_found": newest_date.isoformat(),
            "oldest_circular_date_found": oldest_date.isoformat(),
            "total_record_count": len(all_records),
            "earliest_records": [asdict(item) for item in sorted(all_records, key=lambda item: (item.date, item.circular_no))[:10]],
            "circular_stat_required_or_broken": "circular_stat.php works as status summary but is not required for archive coverage",
            "circular_year_archive_path": "circular.php?yr=YYYY",
            "oldest_year_tested": oldest_tested_year,
            "oldest_year_with_actual_rows": oldest_year_with_rows,
            "limitation": (
                "Direct HTTP requests were reset by the remote host. Normal browser rendering worked and showed "
                "that circular.php?yr=YYYY is the usable archive path for historical rows, while circular_stat.php "
                "holds the current 2026 status rows. 1996 returned no rows, while 1997 returned rows."
            ),
            "year_1996_has_rows": bool(extra_1996),
        }

        print(f"working route: {result['working_route']}")
        print(f"available years discovered: {result['available_years_discovered']}")
        print(f"newest circular date found: {result['newest_circular_date_found']}")
        print(f"oldest circular date found: {result['oldest_circular_date_found']}")
        print(f"total record count: {result['total_record_count']}")
        print("earliest 10 records:")
        for item in sorted(all_records, key=lambda row: (row.date, row.circular_no))[:10]:
            print(f"{item.date} | {item.subject} | {item.circular_no} | {item.link}")
        print(f"whether circular_stat.php is required or broken: {result['circular_stat_required_or_broken']}")
        print(f"whether circular.php?yr=YYYY is the usable archive path: {result['circular_year_archive_path']}")
        print(f"limitation: {result['limitation']}")
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
    ) -> list[NSDLCircularRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        with self.browser_session(headless=True) as session:
            main_html = self.fetch_route_html(NSDL_CIRCULAR_MAIN_URL, session=session)
            years = self.extract_year_options(BeautifulSoup(main_html, "html.parser"))
            if not years:
                raise RuntimeError("NSDL year selector was not discovered from browser-rendered HTML")
            status_html = self.fetch_route_html(NSDL_CIRCULAR_STAT_URL, session=session)
            status_records = self.parse_status_page_records(status_html, NSDL_CIRCULAR_STAT_URL)

            earliest_records = self.parse_year_page_records(self.fetch_year_page_html(min(years), session=session), f"{NSDL_CIRCULAR_MAIN_URL}?yr={min(years)}")
            if not earliest_records:
                raise RuntimeError("NSDL earliest advertised year returned zero rows")
            all_records_preview = status_records[:]
            for year in years:
                html = self.fetch_year_page_html(year, session=session)
                all_records_preview.extend(self.parse_year_page_records(html, f"{NSDL_CIRCULAR_MAIN_URL}?yr={year}"))
            unique_preview = self.deduplicate_records(all_records_preview)
            newest_available_date = max(date.fromisoformat(item.date) for item in unique_preview)
            oldest_available_date = min(date.fromisoformat(item.date) for item in unique_preview)

            if all_available and from_date is None:
                from_date = oldest_available_date
            if from_date is None:
                from_date = date(newest_available_date.year, 1, 1)
            if to_date is None:
                to_date = newest_available_date
            if from_date > to_date:
                raise ValueError("from_date must be less than or equal to to_date")

            chunk_years = [year for year in years if from_date.year <= year <= to_date.year]
            chunks: list[NSDLChunk] = []
            if any(from_date <= date.fromisoformat(record.date) <= to_date for record in status_records):
                chunks.append(NSDLChunk(index=1, kind="status"))
            chunks.extend(
                NSDLChunk(index=len(chunks) + offset, kind="year", year=year)
                for offset, year in enumerate(chunk_years, start=1)
            )

            existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
            existing_keys = {self.record_dedup_key(item) for item in existing_records}
            existing_count = len(existing_records)
            output_mode = "append" if resume and out_path.exists() else "overwrite"

            if resume and checkpoint_file.exists():
                checkpoint = self.load_checkpoint(checkpoint_file)
            else:
                checkpoint = NSDLCheckpoint(
                    source_url=url,
                    output_path=str(out_path),
                    newest_available_date=newest_available_date.isoformat(),
                    oldest_available_date=oldest_available_date.isoformat(),
                    years_discovered=years,
                    total_records_detected=len(unique_preview),
                    chunk_strategy="year_pages_via_browser",
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
            print(f"Expected records: {len(unique_preview)}")
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

            written_records: list[NSDLCircularRecord] = []
            duplicates_skipped = 0

            for chunk in target_chunks:
                if chunk.kind == "status":
                    records = self.filter_records(status_records, from_date=from_date, to_date=to_date)
                else:
                    assert chunk.year is not None
                    html = self.fetch_year_page_html(
                        chunk.year,
                        session=session,
                        retries=retries,
                        retry_base_delay=retry_base_delay,
                        retry_max_delay=retry_max_delay,
                    )
                    records = self.filter_records(
                        self.parse_year_page_records(html, f"{NSDL_CIRCULAR_MAIN_URL}?yr={chunk.year}"),
                        from_date=from_date,
                        to_date=to_date,
                    )
                fresh_records: list[NSDLCircularRecord] = []
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
        self.last_fetch_transport = "playwright"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "nsdl_circulars_validation_report.json"
        year_counts_path = file_path.parent / "nsdl_circulars_year_counts.csv"

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
        zip_links = 0
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
                raise RuntimeError(f"NSDL export is empty: {file_path}") from exc
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
                    if lowered.endswith(".pdf"):
                        pdf_links += 1
                    elif lowered.endswith(".zip"):
                        zip_links += 1
                    elif lowered.endswith(".doc") or lowered.endswith(".docx"):
                        doc_links += 1
                    elif lowered.endswith(".php") or lowered.endswith(".html"):
                        html_links += 1
                    else:
                        other_links += 1
                    if not link.startswith("https://nsdl.co.in/"):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})
                    if link.startswith("/") or link.startswith("./") or link.startswith("../"):
                        suspicious_rows.append({"row_number": row_number, "reason": "broken_looking_relative_url", "link": link})

                dedupe_key = self.record_dedup_key(
                    NSDLCircularRecord(
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
                "pdf": pdf_links,
                "html_detail": html_links,
                "doc_docx": doc_links,
                "zip": zip_links,
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

    def check_links(
        self,
        *,
        file_path: str | Path,
        out_path: str | Path,
        delay_seconds: float = 1.5,
        retries: int = 3,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> list[NSDLLinkCheckRow]:
        records = self.load_existing_output_records(file_path)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        results: list[NSDLLinkCheckRow] = []

        for record in records:
            status_code: Optional[int] = None
            content_type: Optional[str] = None
            final_url = record.link
            ok = False
            error = ""
            try:
                response = self.request_link_metadata(
                    record.link,
                    method="HEAD",
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                status_code = response.status_code
                content_type = response.headers.get("content-type")
                final_url = str(response.url)
                ok = 200 <= response.status_code < 400
                if response.status_code in {405, 501}:
                    response = self.request_link_metadata(
                        record.link,
                        method="GET",
                        retries=retries,
                        retry_base_delay=retry_base_delay,
                        retry_max_delay=retry_max_delay,
                    )
                    status_code = response.status_code
                    content_type = response.headers.get("content-type")
                    final_url = str(response.url)
                    ok = 200 <= response.status_code < 400
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

            results.append(
                NSDLLinkCheckRow(
                    circular_no=record.circular_no,
                    date=record.date,
                    subject=record.subject,
                    link=record.link,
                    status_code=status_code,
                    content_type=content_type,
                    final_url=final_url,
                    ok=ok,
                    error=error,
                )
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(
                file_obj,
                fieldnames=["circular_no", "date", "subject", "link", "status_code", "content_type", "final_url", "ok", "error"],
            )
            writer.writeheader()
            for row in results:
                writer.writerow(asdict(row))
        print(f"Wrote {len(results)} NSDL link checks to {out_path}")
        return results

    def request_link_metadata(
        self,
        url: str,
        *,
        method: str,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if method == "HEAD":
                    response = self.client.head(url)
                else:
                    response = self.client.get(url, headers={"Range": "bytes=0-0"})
                self.last_fetch_transport = "httpx"
                return response
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                logger.warning("NSDL link check retry for {} after {:.1f}s", url, delay)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def fetch_route_direct(self, url: str) -> dict[str, Any]:
        response = self.client.get(url)
        response.raise_for_status()
        return {"status": response.status_code, "final_url": str(response.url), "html": response.text}

    def fetch_route_html(
        self,
        url: str,
        *,
        session: dict[str, Any] | None = None,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                direct_result = self.fetch_route_direct(url)
                if not self.is_request_rejected(direct_result["html"]):
                    self.last_fetch_transport = "httpx"
                    return direct_result["html"]
                last_exc = RuntimeError(f"NSDL request rejected for {url}")
            except Exception as exc:
                last_exc = exc

            if session is not None:
                try:
                    browser_result = self.fetch_route_with_browser(url, session=session)
                    if not self.is_request_rejected(browser_result["html"]):
                        self.last_fetch_transport = "playwright"
                        return browser_result["html"]
                    last_exc = RuntimeError(f"NSDL browser request rejected for {url}")
                except Exception as exc:
                    last_exc = exc

            if attempt >= retries:
                break
            delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
            logger.warning("NSDL fetch failed for {}. Retry {}/{} after {:.1f}s.", url, attempt, retries, delay)
            time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    @contextmanager
    def browser_session(self, *, headless: bool) -> Iterator[dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                locale="en-IN",
                user_agent=self.headers["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            yield {"page": page}
            browser.close()

    def fetch_route_with_browser(self, url: str, *, session: dict[str, Any]) -> dict[str, Any]:
        page = session["page"]
        response = page.goto(url, wait_until="networkidle", timeout=120000)
        return {
            "status": response.status if response else None,
            "final_url": page.url,
            "html": page.content(),
        }

    def fetch_year_page_html(
        self,
        year: int,
        *,
        session: dict[str, Any] | None = None,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> str:
        url = f"{NSDL_CIRCULAR_MAIN_URL}?yr={year}"
        return self.fetch_route_html(
            url,
            session=session,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )

    def is_request_rejected(self, html: str) -> bool:
        text = normalize_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)) or ""
        lowered = text.casefold()
        return "request rejected" in lowered or "access denied" in lowered

    def extract_year_options(self, soup: BeautifulSoup) -> list[int]:
        years = []
        for option in soup.select("select#yr option, select[name='yr'] option"):
            value = normalize_text(option.get("value")) or ""
            if value.isdigit() and value != "0":
                years.append(int(value))
        return sorted(set(years), reverse=True)

    def parse_year_page_records(self, html: str, source_url: str) -> list[NSDLCircularRecord]:
        soup = BeautifulSoup(html, "html.parser")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NSDLCircularRecord] = []
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) != 2:
                continue
            raw_date = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            try:
                parsed_date = parse_indian_date(raw_date)
            except Exception:
                parsed_date = None
            if parsed_date is None:
                continue
            anchor = cells[1].find("a", href=True)
            subject_text = normalize_text(cells[1].get_text(" ", strip=True)) or ""
            if not subject_text:
                continue
            link = self.normalize_nsdl_link(anchor.get("href"), source_url) if anchor else ""
            circular_no = self.extract_circular_no(subject_text)
            records.append(
                NSDLCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject_text,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date,
                    broken_hint="missing_link" if not link else None,
                )
            )
        return records

    def parse_status_page_records(self, html: str, source_url: str) -> list[NSDLCircularRecord]:
        soup = BeautifulSoup(html, "html.parser")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NSDLCircularRecord] = []
        for row in soup.select("table tr"):
            cells = row.find_all("td")
            if len(cells) != 3:
                continue
            raw_date = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            try:
                parsed_date = parse_indian_date(raw_date)
            except Exception:
                parsed_date = None
            status = normalize_text(cells[1].get_text(" ", strip=True)) or ""
            anchor = cells[2].find("a", href=True)
            subject_text = normalize_text(cells[2].get_text(" ", strip=True)) or ""
            if parsed_date is None or not subject_text:
                continue
            link = self.normalize_nsdl_link(anchor.get("href"), source_url) if anchor else ""
            circular_no = self.extract_circular_no(subject_text)
            records.append(
                NSDLCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject_text,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    status=status,
                    raw_date=raw_date,
                    broken_hint="missing_link" if not link else None,
                )
            )
        return records

    def extract_circular_no(self, subject_text: str) -> str:
        text = normalize_text(subject_text) or ""
        if not text or text.casefold().startswith("circular nos "):
            return ""
        return text.split(" ", 1)[0]

    def normalize_nsdl_link(self, raw_value: Optional[str], source_url: str) -> str:
        value = normalize_text(raw_value) or ""
        if not value:
            return ""
        absolute = urljoin(source_url, value)
        return absolute.replace("http://nsdl.co.in/", "https://nsdl.co.in/")

    def filter_records(
        self,
        records: list[NSDLCircularRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[NSDLCircularRecord]:
        filtered: list[NSDLCircularRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: NSDLCircularRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = (normalize_text(record.subject) or "").casefold()
        if record.circular_no:
            return (record.date, normalized_subject, (normalize_text(record.circular_no) or "").casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def deduplicate_records(self, records: list[NSDLCircularRecord]) -> list[NSDLCircularRecord]:
        deduped: list[NSDLCircularRecord] = []
        seen: set[tuple[str, ...]] = set()
        for record in sorted(records, key=lambda item: (item.date, item.circular_no, item.subject)):
            dedupe_key = self.record_dedup_key(record)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(record)
        return deduped

    def collect_link_patterns(self, soup: BeautifulSoup) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "zip": 0, "other": 0}
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").lower().split("?", 1)[0]
            if href.endswith(".pdf"):
                counts["pdf"] += 1
            elif href.endswith(".zip"):
                counts["zip"] += 1
            elif href.endswith(".doc") or href.endswith(".docx"):
                counts["doc"] += 1
            elif href.endswith(".php") or href.endswith(".html"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def count_link_types(self, records: list[NSDLCircularRecord]) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "zip": 0, "other": 0, "empty": 0}
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
            elif href.endswith(".php") or href.endswith(".html"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def load_existing_output_records(self, out_path: str | Path) -> list[NSDLCircularRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    NSDLCircularRecord(
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
            return [NSDLCircularRecord(**row) for row in json.loads(out_path.read_text(encoding="utf-8"))]
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

    def append_output(self, records: list[NSDLCircularRecord], out_path: str | Path) -> None:
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

    def write_output(self, records: list[NSDLCircularRecord], out_path: str | Path) -> None:
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

    def record_to_output_row(self, record: NSDLCircularRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: str | Path) -> NSDLCheckpoint:
        return NSDLCheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: NSDLCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
