from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NERL_SOURCE_LABEL = "NERL"
NERL_CIRCULARS_URL = "https://www.nerlindia.com/circulars/"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_OUTPUT_HEADERS = ["date", "department", "subject", "circular_no", "link", "source_url", "scraped_at"]
NERL_LINK_PREFIX = "https://www.nerlindia.com/"


@dataclass
class NERLRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    department: str = ""
    raw_date: Optional[str] = None


@dataclass
class NERLEndpointResult:
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


@dataclass
class NERLChunk:
    index: int
    label: str


@dataclass
class NERLCheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    total_records_detected: Optional[int]
    count_by_department: dict[str, int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class NERLScraper(BaseScraper):
    source = "nerl"
    regulator = NERL_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(NERL_CIRCULARS_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_circular_records(str(response), NERL_CIRCULARS_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": record.department or None,
                "category": "Circulars",
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
        fixture_dir = Path("tests/fixtures/nerl")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        raw_html = self.fetch_page_html(url)
        raw_fixture_path = fixture_dir / "circulars.html"
        raw_fixture_path.write_text(raw_html, encoding="utf-8")

        soup = BeautifulSoup(raw_html, "html.parser")
        table = soup.find("table")
        parsed_records = self.parse_circular_records(raw_html, url)
        raw_row_count = max(0, len(table.find_all("tr")) - 1) if table else 0
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")] if table else []
        departments = sorted({record.department for record in parsed_records if record.department})
        link_counts = self.count_link_types(parsed_records)
        endpoint_results = [
            NERLEndpointResult(
                url=url,
                method="GET",
                status_code=200,
                content_type="text/html; charset=utf-8",
                response_size=len(raw_html.encode("utf-8")),
                format="html",
                record_count=len(parsed_records),
                sample_records=[asdict(record) for record in parsed_records[:3]],
                keys_or_headers=headers,
            )
        ]

        heading = normalize_text((soup.find(["h1", "h2"]) or {}).get_text(" ", strip=True)) or ""
        print(f"page title: {soup.title.get_text(' ', strip=True) if soup.title else ''}")
        print(f"page heading: {heading}")
        print(f"whether circular table is present: {bool(table)}")
        print(f"table headers found: {headers}")
        print(f"row count found in raw HTML: {raw_row_count}")
        print("first 10 listed rows:")
        for record in parsed_records[:10]:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print("last 10 listed rows:")
        for record in parsed_records[-10:]:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print(f"department values discovered: {departments}")
        print(f"link patterns found: {self.collect_link_patterns(soup)}")
        print(f"whether links are PDF, HTML/detail, DOC/DOCX, ZIP, image-icon anchors, or other: {link_counts}")
        print(f"whether pagination exists: {self.detect_pagination(raw_html)}")
        print(f"whether direct HTTP exposes all rows: {bool(parsed_records)}")
        for result in endpoint_results:
            print(json.dumps(asdict(result), indent=2))

        return {
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "page_heading": heading,
            "raw_row_count": raw_row_count,
            "parsed_row_count": len(parsed_records),
            "departments": departments,
            "pagination_exists": self.detect_pagination(raw_html),
            "direct_http_exposes_all_rows": bool(parsed_records),
            "endpoint_results": [asdict(item) for item in endpoint_results],
        }

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        html = self.fetch_page_html(url)
        records = self.parse_circular_records(html, url)
        if not records:
            raise RuntimeError("NERL circular page returned zero rows")

        valid_dates = [date.fromisoformat(record.date) for record in records if record.date]
        count_by_department = self.count_by_department(records)
        earliest_records = sorted(records, key=lambda row: (row.date, row.subject, row.link))[:10]
        result = {
            "working_route": url,
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_circular_date": max(valid_dates).isoformat(),
            "oldest_circular_date": min(valid_dates).isoformat(),
            "total_record_count": len(records),
            "count_by_department": count_by_department,
            "earliest_records": [asdict(item) for item in earliest_records],
            "limitation": "Single raw HTML table currently exposes the full accessible NERL archive; no separate API or pagination was required.",
        }

        print(f"working route: {result['working_route']}")
        print(f"whether direct HTTP worked: {result['direct_http_worked']}")
        print(f"whether Playwright was used: {result['playwright_used']}")
        print(f"newest circular date found: {result['newest_circular_date']}")
        print(f"oldest circular date found: {result['oldest_circular_date']}")
        print(f"total record count: {result['total_record_count']}")
        print(f"count by department: {result['count_by_department']}")
        print("earliest 10 records:")
        for record in earliest_records:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print(f"limitation: {result['limitation']}")
        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        include_department: bool = False,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[NERLRecord]:
        del retries, retry_base_delay, retry_max_delay
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        html = self.fetch_page_html(url)
        all_records = self.parse_circular_records(html, url)
        if not all_records:
            raise RuntimeError("NERL circular page returned zero rows")

        valid_dates = [date.fromisoformat(record.date) for record in all_records if record.date]
        newest_available_date = max(valid_dates)
        oldest_available_date = min(valid_dates)
        if all_available and from_date is None:
            from_date = oldest_available_date
        if from_date is None:
            from_date = oldest_available_date
        if to_date is None:
            to_date = newest_available_date

        filtered_records = self.filter_records(all_records, from_date=from_date, to_date=to_date)
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
            checkpoint = NERLCheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat(),
                oldest_available_date=oldest_available_date.isoformat(),
                total_records_detected=len(all_records),
                count_by_department=self.count_by_department(all_records),
                chunk_strategy="single_listing_page",
                last_completed_chunk=0,
                records_written=existing_count,
                unique_records_written=existing_count,
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
                self.write_output([], out_path, include_department=include_department)
                self.write_metadata_sidecar([], out_path)
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

        chunks = [NERLChunk(index=1, label="circulars_page")]
        target_chunks = chunks[:1]
        if max_chunks_this_run is not None and max_chunks_this_run <= 0:
            target_chunks = []

        print(f"Oldest date: {from_date.isoformat()}")
        print(f"Newest date: {to_date.isoformat()}")
        print(f"Expected records: {len(all_records)}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"CSV rows detected: {existing_count}")
        print(f"checkpoint last_completed_chunk: {checkpoint.last_completed_chunk}")
        print("resume_from_chunk: 1")
        print(f"expected end chunk: {target_chunks[-1].index if target_chunks else checkpoint.last_completed_chunk}")
        print(f"estimated chunks this run: {len(target_chunks)}")

        written_records: list[NERLRecord] = []
        duplicates_skipped = 0
        for chunk in target_chunks:
            del chunk
            fresh_records: list[NERLRecord] = []
            for record in filtered_records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path, include_department=include_department)
            self.append_metadata_sidecar(fresh_records, out_path)
            written_records.extend(fresh_records)
            checkpoint.last_completed_chunk = 1
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = checkpoint.records_written
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = True
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
        report_path = file_path.parent / "nerl_circulars_validation_report.json"
        year_counts_path = file_path.parent / "nerl_circulars_year_counts.csv"
        department_counts_path = file_path.parent / "nerl_circulars_department_counts.csv"

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
        image_icon_links = 0
        other_links = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        year_counts: dict[int, int] = {}
        dates_seen: list[str] = []

        metadata_rows = self.load_metadata_sidecar(file_path)
        metadata_by_index = {index + 2: item for index, item in enumerate(metadata_rows)}
        department_counts = self.count_by_department(metadata_rows)

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise RuntimeError(f"NERL export is empty: {file_path}") from exc
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

                if not row_data["circular_no"].strip():
                    missing_circular_no += 1

                if not link:
                    missing_link += 1
                    empty_links += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_link"})
                else:
                    lowered = link.lower().split("?", 1)[0]
                    if lowered.endswith(".pdf"):
                        pdf_links += 1
                        image_icon_links += 1
                    elif lowered.endswith(".zip"):
                        zip_links += 1
                    elif lowered.endswith(".doc") or lowered.endswith(".docx"):
                        doc_links += 1
                    elif lowered.endswith(".htm") or lowered.endswith(".html"):
                        html_links += 1
                    else:
                        other_links += 1
                    if not link.startswith(NERL_LINK_PREFIX):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})
                    if link.startswith("/") or link.startswith("./") or link.startswith("../"):
                        suspicious_rows.append({"row_number": row_number, "reason": "broken_looking_relative_url", "link": link})

                dedupe_key = self.record_dedup_key(
                    NERLRecord(
                        date=row_data["date"],
                        subject=row_data["subject"],
                        circular_no=row_data["circular_no"],
                        link=row_data["link"],
                        source_url=row_data["source_url"],
                        scraped_at=row_data["scraped_at"],
                        department=metadata_by_index.get(row_number).department if metadata_by_index.get(row_number) else "",
                    )
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

        sorted_year_counts = dict(sorted(year_counts.items()))
        sorted_department_counts = dict(sorted(department_counts.items()))
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
                "image_icon_anchor": image_icon_links,
                "other": other_links,
                "empty": empty_links,
            },
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "rows_per_year": sorted_year_counts,
            "count_by_department": sorted_department_counts,
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", sorted_year_counts)
        self.write_count_csv(department_counts_path, "department", sorted_department_counts)
        print(json.dumps(report, indent=2))
        print(f"Validation report saved: {report_path}")
        print(f"Year counts saved: {year_counts_path}")
        print(f"Department counts saved: {department_counts_path}")
        return report

    def fetch_page_html(self, url: str) -> str:
        response = self.get(url)
        self.last_fetch_transport = "httpx"
        return response.text

    def parse_circular_records(self, html: str, source_url: str) -> list[NERLRecord]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return []
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NERLRecord] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            raw_date = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            parsed_date = self.parse_nerl_date(raw_date)
            subject = self.clean_subject(cells[1].get_text(" ", strip=True))
            department = self.clean_department(cells[2].get_text(" ", strip=True))
            link = self.extract_pdf_link(cells[3], source_url)
            if not parsed_date or not subject or not link:
                continue
            records.append(
                NERLRecord(
                    date=parsed_date,
                    subject=subject,
                    circular_no="",
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    department=department,
                    raw_date=raw_date,
                )
            )
        return records

    def parse_nerl_date(self, raw_value: str) -> str:
        try:
            parsed = parse_indian_date(normalize_text(raw_value) or "")
        except Exception:
            return ""
        return parsed.isoformat() if parsed else ""

    def normalize_nerl_link(self, raw_value: Optional[str], source_url: str) -> str:
        value = normalize_text(raw_value) or ""
        if not value:
            return ""
        absolute = urljoin(source_url, value.replace(" ", "%20"))
        absolute = absolute.replace("https://nerlindia.com/", NERL_LINK_PREFIX)
        return absolute

    def extract_pdf_link(self, cell: BeautifulSoup, source_url: str) -> str:
        anchor = cell.find("a", href=True)
        if anchor is None:
            return ""
        return self.normalize_nerl_link(anchor.get("href"), source_url)

    def clean_subject(self, raw_value: str) -> str:
        text = normalize_text(raw_value) or ""
        return (
            text.replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u00a0", " ")
            .replace("â€“", "-")
            .replace("â€”", "-")
            .replace("�", "-")
        )

    def clean_department(self, raw_value: str) -> str:
        return normalize_text(raw_value) or ""

    def filter_records(
        self,
        records: list[NERLRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[NERLRecord]:
        filtered: list[NERLRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: NERLRecord) -> tuple[str, str, str]:
        return (record.date, normalize_text(record.subject).casefold(), record.link)

    def count_by_department(self, records: list[NERLRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            key = record.department or ""
            counts[key] = counts.get(key, 0) + 1
        return counts

    def collect_link_patterns(self, soup: BeautifulSoup) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "zip": 0, "image_icon_anchor": 0, "other": 0}
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").lower().split("?", 1)[0]
            if anchor.find("img"):
                counts["image_icon_anchor"] += 1
            if href.endswith(".pdf"):
                counts["pdf"] += 1
            elif href.endswith(".zip"):
                counts["zip"] += 1
            elif href.endswith(".doc") or href.endswith(".docx"):
                counts["doc"] += 1
            elif href.endswith(".htm") or href.endswith(".html"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def count_link_types(self, records: list[NERLRecord]) -> dict[str, int]:
        counts = {"pdf": 0, "html": 0, "doc": 0, "zip": 0, "image_icon_anchor": 0, "other": 0, "empty": 0}
        for record in records:
            href = record.link.lower().split("?", 1)[0]
            if not href:
                counts["empty"] += 1
            elif href.endswith(".pdf"):
                counts["pdf"] += 1
                counts["image_icon_anchor"] += 1
            elif href.endswith(".zip"):
                counts["zip"] += 1
            elif href.endswith(".doc") or href.endswith(".docx"):
                counts["doc"] += 1
            elif href.endswith(".htm") or href.endswith(".html"):
                counts["html"] += 1
            else:
                counts["other"] += 1
        return counts

    def detect_pagination(self, html: str) -> bool:
        lowered = html.casefold()
        return "paginate" in lowered or "datatable" in lowered or "page-item" in lowered or "pagination" in lowered

    def metadata_sidecar_path(self, out_path: str | Path) -> Path:
        return Path(f"{out_path}.meta.json")

    def load_existing_output_records(self, out_path: str | Path) -> list[NERLRecord]:
        out_path = Path(out_path)
        metadata_rows = self.load_metadata_sidecar(out_path)
        metadata_by_index = {index: item for index, item in enumerate(metadata_rows)}
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                records: list[NERLRecord] = []
                for index, row in enumerate(reader):
                    metadata = metadata_by_index.get(index)
                    records.append(
                        NERLRecord(
                            date=row["date"],
                            subject=row["subject"],
                            circular_no=row["circular_no"],
                            link=row["link"],
                            source_url=row["source_url"],
                            scraped_at=row["scraped_at"],
                            department=metadata.department if metadata else "",
                        )
                    )
                return records
        if out_path.suffix.lower() == ".json":
            items = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                NERLRecord(
                    date=item["date"],
                    subject=item["subject"],
                    circular_no=item.get("circular_no", ""),
                    link=item["link"],
                    source_url=item["source_url"],
                    scraped_at=item["scraped_at"],
                    department=item.get("department", ""),
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

    def append_output(self, records: list[NERLRecord], out_path: str | Path, *, include_department: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_department=include_department) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_department else EXPECTED_OUTPUT_HEADERS
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

    def write_output(self, records: list[NERLRecord], out_path: str | Path, *, include_department: bool = False) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item, include_department=include_department) for item in records]
        fieldnames = ENRICHED_OUTPUT_HEADERS if include_department else EXPECTED_OUTPUT_HEADERS
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

    def record_to_output_row(self, record: NERLRecord, *, include_department: bool = False) -> dict[str, str]:
        row = {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_department:
            return {
                "date": record.date,
                "department": record.department,
                **{key: value for key, value in row.items() if key != "date"},
            }
        return row

    def append_metadata_sidecar(self, records: list[NERLRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        existing = []
        if sidecar_path.exists():
            existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
        existing.extend([asdict(item) for item in records])
        sidecar_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_metadata_sidecar(self, records: list[NERLRecord], out_path: str | Path) -> None:
        sidecar_path = self.metadata_sidecar_path(out_path)
        sidecar_path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")

    def load_metadata_sidecar(self, out_path: str | Path) -> list[NERLRecord]:
        sidecar_path = self.metadata_sidecar_path(out_path)
        if not sidecar_path.exists():
            return []
        return [NERLRecord(**item) for item in json.loads(sidecar_path.read_text(encoding="utf-8"))]

    def write_count_csv(self, path: str | Path, label: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([label, "row_count"])
            for key, value in counts.items():
                writer.writerow([key, value])

    def load_checkpoint(self, checkpoint_path: str | Path) -> NERLCheckpoint:
        return NERLCheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: NERLCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
