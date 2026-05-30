from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


AMFI_BASE_URL = "https://www.amfiindia.com"
AMFI_HOME_URL = f"{AMFI_BASE_URL}/"
AMFI_IMPORTANT_UPDATES_URL = f"{AMFI_BASE_URL}/important-updates"
AMFI_DISTRIBUTOR_URL = f"{AMFI_BASE_URL}/distributor"
AMFI_MFD_CIRCULARS_URL = f"{AMFI_BASE_URL}/distributor/amfi-circulars"
AMFI_RESEARCH_INFORMATION_URL = f"{AMFI_BASE_URL}/research-information"
AMFI_ABOUT_AMFI_URL = f"{AMFI_BASE_URL}/aboutamfi"
AMFI_DOWNLOADS_URL = f"{AMFI_BASE_URL}/downloads"

EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
VALID_AMFI_HOST_MARKERS = ("amfiindia.com",)

AMFI_SOURCE_LABELS = {
    "amfi-important-updates": "Important Updates",
    "amfi-mfd-circulars": "AMFI MFD Circulars",
}

AMFI_LISTING_URLS = {
    "amfi-important-updates": AMFI_IMPORTANT_UPDATES_URL,
    "amfi-mfd-circulars": AMFI_MFD_CIRCULARS_URL,
}

AMFI_SCOUT_ROUTES = [
    ("home.html", AMFI_HOME_URL),
    ("important_updates.html", AMFI_IMPORTANT_UPDATES_URL),
    ("distributor.html", AMFI_DISTRIBUTOR_URL),
    ("mfd_circulars.html", AMFI_MFD_CIRCULARS_URL),
    ("research_information.html", AMFI_RESEARCH_INFORMATION_URL),
    ("aboutamfi.html", AMFI_ABOUT_AMFI_URL),
    ("downloads.html", AMFI_DOWNLOADS_URL),
]


@dataclass
class AMFIRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    category: str = ""
    link_type: str = ""
    raw_date: str = ""
    raw_reference: str = ""
    visible_date: bool = False


@dataclass
class AMFICheckpoint:
    source_url: str
    output_path: str
    newest_available_date: str | None
    oldest_available_date: str | None
    total_records_detected: int | None
    count_by_year: dict[str, int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class AMFIScraper(BaseScraper):
    source: str
    regulator = "AMFI"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    @property
    def source_label(self) -> str:
        return AMFI_SOURCE_LABELS[self.source]

    @property
    def listing_url(self) -> str:
        return AMFI_LISTING_URLS[self.source]

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(self.listing_url)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_records(str(response), self.listing_url)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": self.source_label.rstrip("s"),
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date) if record.date else None,
                "department": None,
                "category": record.category or self.source_label,
                "pdf_url": record.link if record.link_type == "pdf" else None,
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

    def scout_site(self, base_url: str) -> list[dict[str, Any]]:
        del base_url
        fixture_dir = Path("tests/fixtures/amfi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        probes: list[dict[str, Any]] = []
        for fixture_name, url in AMFI_SCOUT_ROUTES:
            try:
                self.rate_limit()
                response = self.client.get(url)
                html = response.text
                (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
                probe = self.inspect_route(url=url, html=html, status_code=response.status_code, final_url=str(response.url))
            except Exception as exc:  # pragma: no cover - live failure
                probe = {
                    "url": url,
                    "status_code": None,
                    "final_url": url,
                    "page_title": "",
                    "page_heading": "",
                    "direct_http_worked": False,
                    "react_or_next_rendered": False,
                    "records_present_in_raw_html": False,
                    "scripts_found": [],
                    "json_api_endpoints_found": [],
                    "selectors_found": [],
                    "filters_found": [],
                    "first_records": [],
                    "link_type_counts": {},
                    "error": str(exc),
                }
            probes.append(probe)
            self.print_probe(probe)
        return probes

    def inspect_important_updates(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="important_updates.html", dataset_key="updates")

    def inspect_mfd_circulars(self, url: str) -> dict[str, Any]:
        return self.inspect_source(url, fixture_name="mfd_circulars.html", dataset_key="circulars")

    def discover_important_updates_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("amfi-important-updates", url)
        return self.print_discovery_result(url, records, dates_optional=True)

    def discover_mfd_circular_range(self, url: str) -> dict[str, Any]:
        records = self.collect_records_for_source("amfi-mfd-circulars", url)
        return self.print_discovery_result(url, records, dates_optional=False)

    def inspect_source(self, url: str, *, fixture_name: str, dataset_key: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/amfi")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        html = self.fetch_page_html(url)
        (fixture_dir / fixture_name).write_text(html, encoding="utf-8")
        records = self.parse_records(html, url)
        soup = BeautifulSoup(html, "html.parser")
        embedded_count = len(self.extract_embedded_dataset(soup, dataset_key))
        print(f"working page-flow: {url}")
        print("request method: GET")
        print("cookies/session requirements: none beyond normal public page fetch")
        print("whether Playwright was required: no")
        print(f"records available in raw HTML: {bool(records)}")
        print(f"embedded JSON/static payload used: {embedded_count > 0}")
        print(f"sample 10 records ({len(records)} total parsed):")
        for record in records[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        return {
            "record_count": len(records),
            "embedded_payload_count": embedded_count,
            "sample_records": [asdict(item) for item in records[:10]],
        }

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
    ) -> list[AMFIRecord]:
        del retries, retry_base_delay, retry_max_delay, all_available, include_category
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        html = self.fetch_page_html(url)
        all_records = self.parse_records(html, url)
        total_records = len(all_records)
        total_chunks = 1 if total_records else 0
        newest_available_date, oldest_available_date = self.date_bounds(all_records)

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
            checkpoint = AMFICheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat() if newest_available_date else None,
                oldest_available_date=oldest_available_date.isoformat() if oldest_available_date else None,
                total_records_detected=total_records,
                count_by_year=self.count_by_year(all_records),
                chunk_strategy="single_page",
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
                self.write_output([], out_path)
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

        window = self.compute_chunk_window(
            total_chunks=total_chunks,
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )

        print(f"total_chunks: {window['total_chunks']}")
        print(f"previous last_completed_chunk: {window['previous_last_completed_chunk']}")
        print(f"resume_from_chunk: {window['resume_from_chunk']}")
        print(f"max_chunks_this_run: {max_chunks_this_run if max_chunks_this_run is not None else window['chunks_this_run']}")
        print(f"expected_end_chunk: {window['expected_end_chunk']}")

        if window["completed"]:
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

        previous_last_completed_chunk = checkpoint.last_completed_chunk
        records = self.filter_records(all_records, from_date=from_date, to_date=to_date)
        fresh_records: list[AMFIRecord] = []
        duplicates_skipped = 0
        for record in records:
            dedupe_key = self.record_dedup_key(record)
            if dedupe_key in existing_keys:
                duplicates_skipped += 1
                continue
            existing_keys.add(dedupe_key)
            fresh_records.append(record)

        self.append_output(fresh_records, out_path)

        new_last_completed_chunk = expected_end_chunk if (fresh_records or records or total_records) else previous_last_completed_chunk
        self.assert_non_regressing_checkpoint(
            previous_last_completed_chunk=previous_last_completed_chunk,
            new_last_completed_chunk=new_last_completed_chunk,
        )
        checkpoint.last_completed_chunk = new_last_completed_chunk
        checkpoint.records_written = existing_count + len(fresh_records)
        checkpoint.unique_records_written = len(existing_keys)
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        checkpoint.completed = checkpoint.last_completed_chunk >= total_chunks
        checkpoint.total_records_detected = total_records
        checkpoint.count_by_year = self.count_by_year(all_records)
        if (resume or checkpoint_path) and (fresh_records or records or total_records):
            self.save_checkpoint(checkpoint_file, checkpoint)

        if delay_seconds > 0:
            time.sleep(delay_seconds)

        print(f"Rows written: {len(fresh_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(fresh_records)}")
        self.last_fetch_transport = "httpx"
        return fresh_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        prefix = self.source.replace("-", "_")
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
        external_links = 0
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
                raise RuntimeError(f"AMFI export is empty: {file_path}") from exc
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
                    elif link_type == "external":
                        external_links += 1
                    else:
                        other_links += 1
                    lowered = link.lower()
                    if link_type != "external" and not any(marker in lowered for marker in VALID_AMFI_HOST_MARKERS):
                        suspicious_rows.append({"row_number": row_number, "reason": "non_amfi_link", "link": link})

                dedupe_key = self.record_dedup_key(
                    AMFIRecord(
                        date=row_date,
                        subject=subject,
                        circular_no=circular_no,
                        link=link,
                        source_url=row_data["source_url"].strip(),
                        scraped_at=row_data["scraped_at"].strip(),
                    )
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
                "external": external_links,
                "other": other_links,
                "empty": empty_links,
            },
            "rows_per_year": {str(key): year_counts[key] for key in sorted(year_counts)},
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", {str(key): year_counts[key] for key in sorted(year_counts)})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def parse_records(self, html: str, source_url: str) -> list[AMFIRecord]:
        if self.source == "amfi-important-updates":
            return self.parse_important_updates(html, source_url)
        return self.parse_mfd_circulars(html, source_url)

    def parse_important_updates(self, html: str, source_url: str) -> list[AMFIRecord]:
        soup = BeautifulSoup(html, "html.parser")
        updates = self.extract_embedded_dataset(soup, "updates")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[AMFIRecord] = []
        for item in updates:
            title = self.clean_text(item.get("title"))
            if not title:
                continue
            link = self.normalize_link(item.get("URL") or item.get("url") or "", source_url)
            circular_no = self.extract_important_update_reference(title)
            raw_created_at = self.clean_text(item.get("createdAt")) or ""
            parsed_created_at = parse_indian_date(raw_created_at.replace("T", " ")) if raw_created_at else None
            link_type = self.detect_link_type(link)
            records.append(
                AMFIRecord(
                    date=parsed_created_at.isoformat() if parsed_created_at else "",
                    subject=title,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category="Important Updates",
                    link_type=link_type,
                    raw_date=raw_created_at,
                    raw_reference=circular_no,
                    visible_date=False,
                )
            )
        return self.deduplicate_records(records)

    def parse_mfd_circulars(self, html: str, source_url: str) -> list[AMFIRecord]:
        soup = BeautifulSoup(html, "html.parser")
        circulars = self.extract_embedded_dataset(soup, "circulars")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[AMFIRecord] = []
        for item in circulars:
            circular_no = self.clean_text(item.get("title"))
            raw_date = self.clean_text(item.get("date")) or ""
            parsed_date = parse_indian_date(raw_date)
            subject = self.extract_mfd_subject(item)
            link = self.normalize_link(self.extract_description_link(item), source_url)
            if not subject and not circular_no:
                continue
            records.append(
                AMFIRecord(
                    date=parsed_date.isoformat() if parsed_date else "",
                    subject=subject or circular_no or "",
                    circular_no=circular_no or "",
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category="AMFI Circulars related to MFDs",
                    link_type=self.detect_link_type(link),
                    raw_date=raw_date,
                    raw_reference=circular_no or "",
                    visible_date=bool(parsed_date),
                )
            )
        return self.deduplicate_records(records)

    def extract_embedded_dataset(self, soup: BeautifulSoup, dataset_key: str) -> list[dict[str, Any]]:
        markers = [f'"{dataset_key}":[', f'\\"{dataset_key}\\":[' ]
        for script in soup.find_all("script"):
            script_text = script.string or script.get_text() or ""
            for marker in markers:
                marker_index = script_text.find(marker)
                if marker_index == -1:
                    continue
                start = marker_index + len(marker) - 1
                array_text = self.extract_balanced_json_array(script_text, start)
                if not array_text:
                    continue
                try:
                    payload = json.loads(array_text)
                except json.JSONDecodeError:
                    try:
                        payload = json.loads(array_text.encode("utf-8").decode("unicode_escape"))
                    except Exception:
                        continue
                if isinstance(payload, list):
                    return [item for item in payload if isinstance(item, dict)]
        return []

    def extract_balanced_json_array(self, text: str, bracket_start: int) -> str:
        if bracket_start < 0 or bracket_start >= len(text) or text[bracket_start] != "[":
            return ""
        depth = 0
        in_string = False
        escaped = False
        for index in range(bracket_start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return text[bracket_start : index + 1]
        return ""

    def extract_mfd_subject(self, item: dict[str, Any]) -> str:
        description = item.get("description")
        if not isinstance(description, list):
            return ""
        for block in description:
            if not isinstance(block, dict):
                continue
            children = block.get("children")
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_text = self.collect_text_from_node(child)
                if child_text:
                    return child_text
        return ""

    def extract_description_link(self, item: dict[str, Any]) -> str:
        description = item.get("description")
        if not isinstance(description, list):
            return ""
        for block in description:
            if not isinstance(block, dict):
                continue
            children = block.get("children")
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict) and child.get("url"):
                    return str(child["url"])
        return ""

    def collect_text_from_node(self, node: dict[str, Any]) -> str:
        parts: list[str] = []

        def walk(current: dict[str, Any]) -> None:
            text_value = self.clean_text(current.get("text"))
            if text_value:
                parts.append(text_value)
            children = current.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        walk(child)

        walk(node)
        return self.clean_text(" ".join(parts))

    def inspect_route(self, *, url: str, html: str, status_code: int, final_url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        page_title = self.clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        heading = ""
        for tag_name in ("h1", "h2", "h3"):
            heading_node = soup.find(tag_name)
            if heading_node:
                heading = self.clean_text(heading_node.get_text(" ", strip=True))
                if heading:
                    break

        selector_candidates = []
        if soup.select_one("h5.title"):
            selector_candidates.append("h5.title")
        if soup.select_one("div.css-1f8ksoa"):
            selector_candidates.append("div.css-1f8ksoa")
        if soup.select_one("a[href*='/uploads/']"):
            selector_candidates.append("a[href*='/uploads/']")

        filters_found = []
        page_text = soup.get_text(" ", strip=True)
        if "Search" in page_text:
            filters_found.append("search")
        if "Sort" in page_text:
            filters_found.append("sort")
        if "Date of Circular" in page_text:
            filters_found.append("date-of-circular")

        scripts_found = [src for src in (script.get("src") for script in soup.find_all("script", src=True)) if src][:10]
        json_markers = []
        html_text = html
        if "_next/static" in html_text:
            json_markers.append("next-assets")
        if "self.__next_f.push" in html_text:
            json_markers.append("embedded-next-flight-payload")
        if '"updates":[' in html_text:
            json_markers.append("embedded-updates-json")
        if '"circulars":[' in html_text:
            json_markers.append("embedded-circulars-json")

        records = self.parse_route_records(url, html)
        link_type_counts = self.count_link_types(records)
        probe = {
            "url": url,
            "status_code": status_code,
            "final_url": final_url,
            "page_title": page_title,
            "page_heading": heading,
            "direct_http_worked": True,
            "react_or_next_rendered": "_next/static" in html_text or "self.__next_f.push" in html_text,
            "records_present_in_raw_html": bool(records),
            "scripts_found": scripts_found,
            "json_api_endpoints_found": json_markers,
            "selectors_found": selector_candidates,
            "filters_found": filters_found,
            "first_records": [self.record_preview(record) for record in records[:10]],
            "link_type_counts": link_type_counts,
        }
        return probe

    def parse_route_records(self, url: str, html: str) -> list[AMFIRecord]:
        if "important-updates" in url:
            return self.parse_important_updates(html, url)
        if "amfi-circulars" in url:
            return self.parse_mfd_circulars(html, url)

        soup = BeautifulSoup(html, "html.parser")
        records: list[AMFIRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for anchor in soup.find_all("a", href=True):
            title = self.clean_text(anchor.get_text(" ", strip=True))
            href = self.normalize_link(anchor.get("href"), url)
            if not title or not href:
                continue
            if len(title) < 5:
                continue
            records.append(
                AMFIRecord(
                    date="",
                    subject=title,
                    circular_no=self.extract_important_update_reference(title),
                    link=href,
                    source_url=url,
                    scraped_at=scraped_at,
                    category=heading_from_url(url),
                    link_type=self.detect_link_type(href),
                )
            )
        return self.deduplicate_records(records)

    def collect_records_for_source(self, source_name: str, url: str) -> list[AMFIRecord]:
        previous_source = self.source
        try:
            self.source = source_name
            html = self.fetch_page_html(url)
            return self.parse_records(html, url)
        finally:
            self.source = previous_source

    def print_discovery_result(self, url: str, records: list[AMFIRecord], *, dates_optional: bool) -> dict[str, Any]:
        dated_records = [record for record in records if record.date]
        newest = max((record.date for record in dated_records), default=None)
        oldest = min((record.date for record in dated_records), default=None)
        count_by_year = self.count_by_year(records)
        print(f"working page-flow: {url}")
        print(f"newest date found: {newest or 'not visible'}")
        print(f"oldest date found: {oldest or 'not visible'}")
        print(f"total count: {len(records)}")
        print(f"count by year: {json.dumps(count_by_year, ensure_ascii=False)}")
        print("sample earliest 10 records:")
        sample_records = sorted(
            records,
            key=lambda item: (item.date or "9999-99-99", normalize_text(item.subject or "") or ""),
        )[:10]
        for record in sample_records:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        limitation = ""
        if dates_optional and not dated_records:
            limitation = "Important Updates does not expose visible listing dates in raw HTML; output date coverage is partial."
        print(f"limitation: {limitation or 'none'}")
        return {
            "newest_date": newest,
            "oldest_date": oldest,
            "total_count": len(records),
            "count_by_year": count_by_year,
            "limitation": limitation,
        }

    def filter_records(
        self,
        records: list[AMFIRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[AMFIRecord]:
        if from_date is None and to_date is None:
            return list(records)
        filtered: list[AMFIRecord] = []
        for record in records:
            if not record.date:
                if self.source == "amfi-important-updates":
                    continue
                filtered.append(record)
                continue
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def deduplicate_records(self, records: list[AMFIRecord]) -> list[AMFIRecord]:
        seen: set[tuple[str, ...]] = set()
        deduped: list[AMFIRecord] = []
        for record in records:
            key = self.record_dedup_key(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def record_dedup_key(self, record: AMFIRecord) -> tuple[str, ...]:
        normalized_subject = (normalize_text(record.subject) or "").lower()
        normalized_circular_no = (normalize_text(record.circular_no) or "").lower()
        normalized_link = (normalize_text(record.link) or "").lower()
        if record.date or normalized_circular_no:
            return (record.date, normalized_subject, normalized_circular_no, normalized_link)
        return (normalized_subject, normalized_link)

    def date_bounds(self, records: list[AMFIRecord]) -> tuple[date | None, date | None]:
        dated_records = [date.fromisoformat(record.date) for record in records if record.date]
        if not dated_records:
            return None, None
        return max(dated_records), min(dated_records)

    def count_by_year(self, records: list[AMFIRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year = record.date[:4]
            counts[year] = counts.get(year, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def count_link_types(self, records: list[AMFIRecord]) -> dict[str, int]:
        counts = {
            "pdf": 0,
            "html/detail": 0,
            "doc/docx": 0,
            "xls/xlsx": 0,
            "zip": 0,
            "external": 0,
            "other": 0,
            "empty": 0,
        }
        for record in records:
            counts[self.detect_link_type(record.link)] += 1
        return counts

    def record_preview(self, record: AMFIRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
        }

    def print_probe(self, probe: dict[str, Any]) -> None:
        print(f"URL: {probe['url']}")
        print(f"HTTP status: {probe['status_code']}")
        print(f"Final URL: {self.console_safe(probe['final_url'])}")
        print(f"Page title: {self.console_safe(probe['page_title'])}")
        print(f"Page heading: {self.console_safe(probe['page_heading'])}")
        print(f"Direct HTTP worked: {probe['direct_http_worked']}")
        print(f"React/Next/JS-rendered: {probe['react_or_next_rendered']}")
        print(f"Records present in raw HTML: {probe['records_present_in_raw_html']}")
        print(f"Scripts/assets found: {json.dumps(probe['scripts_found'], ensure_ascii=False)}")
        print(f"JSON/API endpoints found: {json.dumps(probe['json_api_endpoints_found'], ensure_ascii=False)}")
        print(f"Table/list/card selectors found: {json.dumps(probe['selectors_found'], ensure_ascii=False)}")
        print(f"Filter/search/pagination controls found: {json.dumps(probe['filters_found'], ensure_ascii=False)}")
        print("First 10 listed records:")
        for row in probe["first_records"][:10]:
            print(
                f"- {self.console_safe(row.get('date', ''))} | {self.console_safe(row.get('subject', ''))} | "
                f"{self.console_safe(row.get('circular_no', ''))} | {self.console_safe(row.get('link', ''))}"
            )
        print(f"Link type counts: {json.dumps(probe['link_type_counts'], ensure_ascii=False)}")
        if probe.get("error"):
            print(f"Error: {self.console_safe(probe['error'])}")

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool = False) -> None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if resume or not path.exists():
            return
        with open(path, "w", encoding="utf-8", newline=""):
            pass

    def load_existing_output_records(self, out_path: Path) -> list[AMFIRecord]:
        if not out_path.exists():
            return []
        records: list[AMFIRecord] = []
        with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                records.append(
                    AMFIRecord(
                        date=(row.get("date") or "").strip(),
                        subject=(row.get("subject") or "").strip(),
                        circular_no=(row.get("circular_no") or "").strip(),
                        link=(row.get("link") or "").strip(),
                        source_url=(row.get("source_url") or "").strip(),
                        scraped_at=(row.get("scraped_at") or "").strip(),
                    )
                )
        return records

    def write_output(self, records: list[AMFIRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".json":
            payload = [self.output_row(record) for record in records]
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
            writer.writeheader()
            for record in records:
                writer.writerow(self.output_row(record))

    def append_output(self, records: list[AMFIRecord], out_path: Path) -> None:
        if out_path.suffix.lower() == ".json":
            existing: list[dict[str, str]] = []
            if out_path.exists():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend(self.output_row(record) for record in records)
            out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        file_exists = out_path.exists() and out_path.stat().st_size > 0
        with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
            if not file_exists:
                writer.writeheader()
            for record in records:
                writer.writerow(self.output_row(record))

    def output_row(self, record: AMFIRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def save_checkpoint(self, checkpoint_path: Path, checkpoint: AMFICheckpoint) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_checkpoint(self, checkpoint_path: Path) -> AMFICheckpoint:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return AMFICheckpoint(**payload)

    def write_count_csv(self, path: Path, key_name: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=[key_name, "count"])
            writer.writeheader()
            for key, value in counts.items():
                writer.writerow({key_name: key, "count": value})

    def clean_text(self, value: Any) -> str:
        normalized = normalize_text(str(value)) if value is not None else None
        if not normalized:
            return ""
        cleaned = (
            normalized.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u00a0", " ")
        )
        # Repair common quote-loss damage seen in AMFI titles such as ???Provisional???
        cleaned = re.sub(r"\?{3}\s*([^?]+?)\s*\?{3}", r'"\1"', cleaned)
        cleaned = re.sub(r"\?{2,}\s*", '"', cleaned)
        cleaned = re.sub(r"\s*\?{2,}", '"', cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def normalize_link(self, link: str | None, source_url: str) -> str:
        cleaned = self.clean_text(link)
        if not cleaned:
            return ""
        return urljoin(source_url, cleaned)

    def detect_link_type(self, link: str) -> str:
        lowered = (link or "").strip().lower()
        if not lowered:
            return "empty"
        parsed = urlparse(lowered)
        if parsed.netloc and "amfiindia.com" not in parsed.netloc:
            return "external"
        if lowered.endswith(".pdf"):
            return "pdf"
        if lowered.endswith(".doc") or lowered.endswith(".docx"):
            return "doc/docx"
        if lowered.endswith(".xls") or lowered.endswith(".xlsx"):
            return "xls/xlsx"
        if lowered.endswith(".zip"):
            return "zip"
        if lowered.endswith(".html") or lowered.endswith(".htm") or parsed.path.startswith("/"):
            return "html/detail"
        return "other"

    def extract_important_update_reference(self, title: str) -> str:
        title = self.clean_text(title)
        patterns = [
            r"\bARN Circular\s*no\.?\s*\d+\b",
            r"\bAMFI/MFD-CIR/\d+/\d{4}-\d{2}\b",
            r"\bCIR/ARN-\d+/\d{4}-\d{2}\b",
            r"\bCircular No\.?\s*[A-Za-z0-9/\-]+\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, title, flags=re.IGNORECASE)
            if match:
                return self.clean_text(match.group(0))
        return ""

    def is_navigation_text(self, subject: str) -> bool:
        lowered = subject.lower()
        navigation_markers = {"home", "about amfi", "contact us", "terms of use", "privacy notice"}
        return lowered in navigation_markers

    def console_safe(self, value: Any) -> str:
        text = self.clean_text(value)
        return text.encode("ascii", "replace").decode("ascii")


class AMFIImportantUpdatesScraper(AMFIScraper):
    source = "amfi-important-updates"


class AMFIMFDCircularsScraper(AMFIScraper):
    source = "amfi-mfd-circulars"


def heading_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/").split("/")
    if not path:
        return "home"
    return normalize_text(path[-1].replace("-", " ")) or "page"
