from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NISM_BASE_URL = "https://www.nism.ac.in"
NISM_CIRCULARS_URL = f"{NISM_BASE_URL}/circulars/"
NISM_CIRCULARS_3_URL = f"{NISM_BASE_URL}/circulars-3/"
NISM_ARCHIVE_DEFAULT_URL = f"{NISM_BASE_URL}/circular-archive-list/?type=circular"
NISM_FIXTURE_DIR = Path("tests/fixtures/nism")

EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "download_links", "source_url", "scraped_at"]
VALID_NISM_HOST_MARKERS = ("nism.ac.in", "sebi.gov.in")


@dataclass
class NISMRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    raw_date: str = ""
    detail_url: str = ""
    download_links: list[str] = field(default_factory=list)
    last_updated: str = ""


@dataclass
class NISMCheckpoint:
    source_url: str
    output_path: str
    newest_available_date: str | None
    oldest_available_date: str | None
    total_records_detected: int | None
    count_by_year: dict[str, int]
    archive_url: str
    pagination_pattern: str
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


class NISMScraper(BaseScraper):
    source = "nism-circulars"
    regulator = "NISM"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"
        self._detail_cache: dict[str, dict[str, Any]] = {}

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(NISM_CIRCULARS_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_recent_listing(str(response), NISM_CIRCULARS_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date) if record.date else None,
                "department": None,
                "category": "circulars",
                "pdf_url": None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "circular"),
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record["published_date"],
            department=None,
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

    def inspect_circulars(self, url: str) -> dict[str, Any]:
        del url
        NISM_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        pages = [
            ("circulars.html", NISM_CIRCULARS_URL),
            ("circulars_3.html", NISM_CIRCULARS_3_URL),
        ]
        results: dict[str, Any] = {"pages": []}
        archive_url = ""
        sample_detail_url = ""

        for fixture_name, page_url in pages:
            response = self.get(page_url)
            html = response.text
            (NISM_FIXTURE_DIR / fixture_name).write_text(html, encoding="utf-8")
            probe = self.inspect_page(page_url, html, response.status_code, str(response.url))
            results["pages"].append(probe)
            archive_url = archive_url or probe.get("archive_url", "")
            if not sample_detail_url and probe.get("first_records"):
                sample_detail_url = probe["first_records"][0].get("link", "")
            self.print_inspect_probe(probe)

        archive_target = archive_url or NISM_ARCHIVE_DEFAULT_URL
        archive_response = self.get(archive_target)
        archive_html = archive_response.text
        (NISM_FIXTURE_DIR / "circulars_archive.html").write_text(archive_html, encoding="utf-8")
        archive_probe = self.inspect_page(archive_target, archive_html, archive_response.status_code, str(archive_response.url))
        results["archive"] = archive_probe
        self.print_inspect_probe(archive_probe)

        sample_detail_url = sample_detail_url or (archive_probe.get("first_records") or [{}])[0].get("link", "")
        if sample_detail_url:
            detail_response = self.get(sample_detail_url)
            detail_html = detail_response.text
            (NISM_FIXTURE_DIR / "circular_detail_sample.html").write_text(detail_html, encoding="utf-8")
            detail_meta = self.parse_detail_page(detail_html, sample_detail_url)
            print(f"sample detail URL: {self.console_safe(sample_detail_url)}")
            print(f"detail title: {self.console_safe(detail_meta.get('title', ''))}")
            print(f"detail displayed date: {self.console_safe(detail_meta.get('date', ''))}")
            print(f"detail last updated: {self.console_safe(detail_meta.get('last_updated', ''))}")
            print(f"detail download links: {len(detail_meta.get('download_links', []))}")
            for href in detail_meta.get("download_links", [])[:10]:
                print(f"- {self.console_safe(href)}")
            results["detail"] = detail_meta

        return results

    def inspect_circular_archive(self, url: str) -> dict[str, Any]:
        archive_url = self.discover_archive_url(url)
        page_urls = self.discover_archive_page_urls(archive_url)
        first_page_html = self.fetch_page_html(page_urls[0]) if page_urls else self.fetch_page_html(archive_url)
        first_page_records = self.parse_archive_listing(first_page_html, page_urls[0] if page_urls else archive_url)
        print(f"archive URL: {self.console_safe(archive_url)}")
        print("pagination pattern: /page/N/?type=circular")
        print(f"first page row count: {len(first_page_records)}")
        print(f"total pages visible: {len(page_urls)}")
        print("sample 10 records:")
        for record in first_page_records[:10]:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        if first_page_records:
            dates = [item.date for item in first_page_records if item.date]
            if dates:
                print(f"oldest visible date on first/archive pages: {min(dates)}")
        return {
            "archive_url": archive_url,
            "pagination_pattern": "/page/N/?type=circular",
            "page_count": len(page_urls),
            "first_page_row_count": len(first_page_records),
            "sample_records": [asdict(item) for item in first_page_records[:10]],
        }

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        archive_url = self.discover_archive_url(url)
        page_urls = self.discover_archive_page_urls(archive_url)
        records = self.collect_all_records(page_urls)
        dates = [date.fromisoformat(item.date) for item in records if item.date]
        newest_date = max(dates).isoformat() if dates else ""
        oldest_date = min(dates).isoformat() if dates else ""
        count_by_year = self.count_by_year(records)
        earliest_records = sorted([item for item in records if item.date], key=lambda item: item.date)[:10]
        print(f"working page-flow: archive listing via {self.console_safe(archive_url)}")
        print("whether direct HTTP worked: yes")
        print("whether Playwright was used: no")
        print(f"archive URL/pagination pattern: {self.console_safe(archive_url)} | /page/N/?type=circular")
        print(f"newest date found: {newest_date}")
        print(f"oldest date found: {oldest_date}")
        print(f"total record count: {len(records)}")
        print(f"count by year: {json.dumps(count_by_year, ensure_ascii=False)}")
        print("sample earliest 10 records:")
        for record in earliest_records:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        print("limitation: detail-page fallback is only used when listing metadata is missing or ambiguous.")
        return {
            "archive_url": archive_url,
            "pagination_pattern": "/page/N/?type=circular",
            "newest_date": newest_date,
            "oldest_date": oldest_date,
            "total_records": len(records),
            "count_by_year": count_by_year,
        }

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        include_downloads: bool = False,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[NISMRecord]:
        del retries, retry_base_delay, retry_max_delay, all_available
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        archive_url = self.discover_archive_url(url)
        page_urls = self.discover_archive_page_urls(archive_url)
        preview_records = self.collect_all_records(page_urls)
        total_records = len(preview_records)
        total_chunks = len(page_urls)
        newest_available_date, oldest_available_date = self.date_bounds(preview_records)

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
            checkpoint = NISMCheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat() if newest_available_date else None,
                oldest_available_date=oldest_available_date.isoformat() if oldest_available_date else None,
                total_records_detected=total_records,
                count_by_year=self.count_by_year(preview_records),
                archive_url=archive_url,
                pagination_pattern="/page/N/?type=circular",
                chunk_strategy="archive_page",
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
                self.write_output([], out_path, include_downloads=include_downloads)
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

        all_written_records: list[NISMRecord] = []
        duplicates_skipped = 0
        previous_last_completed_chunk = checkpoint.last_completed_chunk

        for page_number in range(resume_from_chunk, expected_end_chunk + 1):
            page_url = page_urls[page_number - 1]
            page_html = self.fetch_page_html(page_url)
            page_records = self.parse_archive_listing(page_html, page_url)
            page_records = self.enrich_missing_records(page_records, include_downloads=include_downloads)
            page_records = self.filter_records(page_records, from_date=from_date, to_date=to_date)
            fresh_records: list[NISMRecord] = []
            for record in page_records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path, include_downloads=include_downloads)
            all_written_records.extend(fresh_records)

            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=previous_last_completed_chunk,
                new_last_completed_chunk=page_number,
            )
            checkpoint.last_completed_chunk = page_number
            checkpoint.records_written = existing_count + len(all_written_records)
            checkpoint.unique_records_written = len(existing_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = checkpoint.last_completed_chunk >= total_chunks
            checkpoint.total_records_detected = total_records
            checkpoint.count_by_year = self.count_by_year(preview_records)
            checkpoint.archive_url = archive_url
            checkpoint.pagination_pattern = "/page/N/?type=circular"
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

            if delay_seconds > 0:
                time.sleep(delay_seconds)

        print(f"Rows written: {len(all_written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(all_written_records)}")
        self.last_fetch_transport = "httpx"
        return all_written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "nism_circulars_validation_report.json"
        year_counts_path = file_path.parent / "nism_circulars_year_counts.csv"

        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        rows_per_year: dict[str, int] = {}
        dates_seen: list[str] = []
        link_type_counts = {
            "html/detail": 0,
            "pdf": 0,
            "doc/docx": 0,
            "xls/xlsx": 0,
            "zip": 0,
            "external": 0,
            "other": 0,
            "empty": 0,
        }

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            headers_ok = reader.fieldnames == EXPECTED_OUTPUT_HEADERS
            for row in reader:
                total_rows += 1
                date_value = (row.get("date") or "").strip()
                subject = (row.get("subject") or "").strip()
                circular_no = (row.get("circular_no") or "").strip()
                link = (row.get("link") or "").strip()

                if not date_value:
                    missing_date += 1
                else:
                    if self.normalize_date(date_value) is None:
                        suspicious_rows.append({"row": total_rows, "reason": "invalid_date", "value": date_value})
                    else:
                        dates_seen.append(date_value)
                        rows_per_year[date_value[:4]] = rows_per_year.get(date_value[:4], 0) + 1
                if not subject:
                    missing_subject += 1
                if not circular_no:
                    missing_circular_no += 1
                if not link:
                    missing_link += 1

                link_type = self.detect_link_type(link)
                link_type_counts[link_type if link_type in link_type_counts else "other"] += 1

                dedupe_key = self.record_dedup_key(
                    NISMRecord(
                        date=date_value,
                        subject=subject,
                        circular_no=circular_no,
                        link=link,
                        source_url=(row.get("source_url") or "").strip(),
                        scraped_at=(row.get("scraped_at") or "").strip(),
                    )
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

                if link and not any(marker in link.lower() for marker in VALID_NISM_HOST_MARKERS):
                    suspicious_rows.append({"row": total_rows, "reason": "non_nism_link", "value": link})
                if subject and len(subject) < 8:
                    suspicious_rows.append({"row": total_rows, "reason": "very_short_subject", "value": subject})
                if not link:
                    suspicious_rows.append({"row": total_rows, "reason": "missing_link", "value": ""})

        report = {
            "headers_ok": headers_ok,
            "total_rows": total_rows,
            "missing_date_count": missing_date,
            "missing_subject_count": missing_subject,
            "missing_circular_no_count": missing_circular_no,
            "missing_link_count": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": link_type_counts,
            "rows_per_year": {key: rows_per_year[key] for key in sorted(rows_per_year)},
            "min_date": min(dates_seen) if dates_seen else "",
            "max_date": max(dates_seen) if dates_seen else "",
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", report["rows_per_year"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def inspect_page(self, url: str, html: str, status_code: int, final_url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        heading = ""
        for tag in soup.select("h1, h2"):
            text = self.clean_text(tag.get_text(" ", strip=True))
            if text and text.lower() != "video modal":
                heading = text
                break
        archive_url = self.find_archive_url(soup, url)
        recent_records = self.parse_recent_listing(html, url)
        archive_records = self.parse_archive_listing(html, url)
        records = archive_records or recent_records
        pagination_links = self.extract_archive_pagination_links(soup)
        link_counts = self.link_type_counts(soup)
        search_found = bool(soup.select("input[type='search'], input[name='s'], select"))
        selectors_found = []
        if soup.select("div.card.circular-card"):
            selectors_found.append("div.card.circular-card")
        if soup.select("div.circulars-page li.item"):
            selectors_found.append("div.circulars-page li.item")
        if soup.select("div.notification-card .notif-item"):
            selectors_found.append("div.notification-card .notif-item")
        return {
            "url": url,
            "status_code": status_code,
            "final_url": final_url,
            "page_title": title,
            "page_heading": heading,
            "direct_http_worked": True,
            "rows_present_in_raw_html": bool(records),
            "wordpress_like": "wp-content" in html or "wp-" in html,
            "archive_pagination_exists": bool(pagination_links),
            "search_or_filter_exists": search_found,
            "archive_url": archive_url,
            "selectors_found": selectors_found,
            "first_records": [self.output_row(item) for item in records[:10]],
            "last_records": [self.output_row(item) for item in records[-10:]] if len(records) > 10 else [],
            "link_type_counts": link_counts,
        }

    def parse_recent_listing(self, html: str, source_url: str) -> list[NISMRecord]:
        soup = BeautifulSoup(html, "html.parser")
        scraped_at = datetime.now(timezone.utc).isoformat()
        seen_links: set[str] = set()
        records: list[NISMRecord] = []
        for anchor in soup.select("div.card.circular-card a[href]"):
            href = self.normalize_link(anchor.get("href") or "", source_url)
            if not href or href in seen_links:
                continue
            seen_links.add(href)
            subject = self.clean_text(" ".join(tag.get_text(" ", strip=True) for tag in anchor.select("p")) or anchor.get_text(" ", strip=True))
            raw_date = self.clean_text(" ".join(tag.get_text(" ", strip=True) for tag in anchor.select("h6 span")))
            parsed_date = self.normalize_date(raw_date) or self.extract_date_from_text(subject) or ""
            circular_no = self.extract_reference(subject)
            records.append(
                NISMRecord(
                    date=parsed_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=href,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date,
                    detail_url=href,
                )
            )
        return records

    def parse_archive_listing(self, html: str, source_url: str) -> list[NISMRecord]:
        soup = BeautifulSoup(html, "html.parser")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NISMRecord] = []
        for item in soup.select("div.circulars-page li.item"):
            anchor = item.select_one("div.title a[href]")
            if anchor is None:
                continue
            href = self.normalize_link(anchor.get("href") or "", source_url)
            subject = self.clean_text(anchor.get_text(" ", strip=True))
            raw_date = self.clean_text(" ".join(node.get_text(" ", strip=True) for node in item.select("div.post-date span")))
            parsed_date = self.normalize_date(raw_date) or self.extract_date_from_text(subject) or ""
            circular_no = self.extract_reference(subject)
            records.append(
                NISMRecord(
                    date=parsed_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=href,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date,
                    detail_url=href,
                )
            )
        return self.deduplicate_records(records)

    def enrich_missing_records(self, records: list[NISMRecord], *, include_downloads: bool = False) -> list[NISMRecord]:
        enriched: list[NISMRecord] = []
        for record in records:
            if record.date and record.circular_no and not include_downloads:
                enriched.append(record)
                continue
            detail = self.fetch_detail_metadata(record.link)
            if not record.date:
                record.date = detail.get("date", "") or self.extract_date_from_text(detail.get("title", "")) or ""
            if not record.circular_no:
                detail_ref = self.extract_reference(detail.get("title", ""))
                if not detail_ref:
                    detail_ref = self.extract_explicit_reference(detail.get("body_snippet", ""))
                record.circular_no = detail_ref or record.circular_no
            if include_downloads or detail.get("download_links"):
                record.download_links = detail.get("download_links", []) or []
            record.last_updated = detail.get("last_updated", "") or ""
            enriched.append(record)
        return enriched

    def collect_all_records(self, page_urls: list[str]) -> list[NISMRecord]:
        records: list[NISMRecord] = []
        for page_url in page_urls:
            html = self.fetch_page_html(page_url)
            page_records = self.parse_archive_listing(html, page_url)
            page_records = self.enrich_missing_records(page_records)
            records.extend(page_records)
        return self.deduplicate_records(records)

    def discover_archive_url(self, url: str) -> str:
        html = self.fetch_page_html(url)
        soup = BeautifulSoup(html, "html.parser")
        return self.find_archive_url(soup, url) or NISM_ARCHIVE_DEFAULT_URL

    def find_archive_url(self, soup: BeautifulSoup, base_url: str) -> str:
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            text = self.clean_text(anchor.get_text(" ", strip=True)).lower()
            if "view all archive" in text or "circular-archive-list" in href:
                return self.normalize_link(href, base_url)
        return ""

    def discover_archive_page_urls(self, archive_url: str) -> list[str]:
        html = self.fetch_page_html(archive_url)
        soup = BeautifulSoup(html, "html.parser")
        pagination_links = self.extract_archive_pagination_links(soup)
        max_page = 1
        for href in pagination_links:
            match = re.search(r"/page/(\d+)/", href)
            if match:
                max_page = max(max_page, int(match.group(1)))
        return [self.build_archive_page_url(archive_url, page_no) for page_no in range(1, max_page + 1)]

    def extract_archive_pagination_links(self, soup: BeautifulSoup) -> list[str]:
        links: list[str] = []
        for anchor in soup.select("a.page-numbers[href], .pagination a[href], nav a[href]"):
            href = anchor.get("href") or ""
            if "circular-archive-list" in href:
                links.append(href)
        return links

    def build_archive_page_url(self, archive_url: str, page_no: int) -> str:
        parsed = urlparse(archive_url)
        path = re.sub(r"/page/\d+/?", "/", parsed.path)
        if page_no > 1:
            if not path.endswith("/"):
                path = f"{path}/"
            path = f"{path}page/{page_no}/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.setdefault("type", ["circular"])
        return urlunparse(parsed._replace(path=path, query=urlencode(query, doseq=True)))

    def fetch_detail_metadata(self, detail_url: str) -> dict[str, Any]:
        if detail_url in self._detail_cache:
            return self._detail_cache[detail_url]
        html = self.fetch_page_html(detail_url)
        payload = self.parse_detail_page(html, detail_url)
        self._detail_cache[detail_url] = payload
        return payload

    def parse_detail_page(self, html: str, detail_url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        for tag in soup.select("h1, h2, title"):
            text = self.clean_text(tag.get_text(" ", strip=True))
            if text and text.lower() != "video modal":
                title = text.replace(" - National Institute of Securities Markets (NISM)", "").strip()
                break
        content_root = self.find_detail_content_root(soup)
        body_text = self.clean_text(content_root.get_text(" ", strip=True) if content_root else soup.get_text(" ", strip=True))
        for marker in ("Recent Circulars", "Notifications 9", "Search for: Search Button"):
            if marker in body_text:
                body_text = body_text.split(marker, 1)[0].strip()
        last_updated_match = re.search(
            r"Last Updated on\s*:?\s*([A-Za-z]+ \d{1,2},\s*\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})",
            body_text,
            re.I,
        )
        last_updated = self.clean_text(last_updated_match.group(1)) if last_updated_match else ""
        download_links: list[str] = []
        search_root = content_root or soup
        for anchor in search_root.select("a[href]"):
            href = self.normalize_link(anchor.get("href") or "", detail_url)
            text = self.clean_text(anchor.get_text(" ", strip=True))
            if not href:
                continue
            if self.detect_link_type(href) in {"pdf", "doc/docx", "xls/xlsx", "zip"} or "click here" in text.lower():
                if href not in download_links:
                    download_links.append(href)
        date_value = self.extract_date_from_text(title) or self.extract_date_from_text(body_text) or ""
        return {
            "title": title,
            "date": date_value,
            "last_updated": last_updated,
            "download_links": download_links,
            "body_snippet": body_text[:2000],
        }

    def find_detail_content_root(self, soup: BeautifulSoup) -> Tag | None:
        selectors = [
            "div.entry-content",
            "div.post-content",
            "article",
            "main",
            "div.site-content",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node and ("click here" in node.get_text(" ", strip=True).lower() or len(node.get_text(" ", strip=True)) > 200):
                return node
        return None

    def clean_text(self, value: str) -> str:
        text = normalize_text(value) or ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s*�\s*", " - ", text)
        text = text.replace("–", "-").replace("—", "-")
        return text.strip()

    def normalize_link(self, href: str, source_url: str) -> str:
        href = (href or "").strip()
        if not href:
            return ""
        return urljoin(source_url, href)

    def normalize_date(self, raw_date: str) -> str | None:
        raw = self.clean_text(raw_date)
        if not raw:
            return None
        raw = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", raw, flags=re.I)
        raw = raw.replace(".", "")
        for candidate in [raw]:
            parsed = parse_indian_date(candidate)
            if parsed:
                return parsed.isoformat()
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def extract_date_from_text(self, text: str) -> str | None:
        cleaned = self.clean_text(text)
        patterns = [
            r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}\b",
            r"\b[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.I)
            if match:
                parsed = self.normalize_date(match.group(0))
                if parsed:
                    return parsed
        return None

    def extract_reference(self, text: str) -> str:
        cleaned = self.clean_text(text)
        patterns = [
            r"(NISM/Certification/[A-Za-z0-9\-/:()., ]+?/\d{4}/\d{1,2})\b",
            r"(NISM/Certification/[A-Za-z0-9\-/:()., ]+?/\d{4})\b",
            r"(SEBI Circular for [A-Za-z0-9\-/:()., ]+?)\s+dated\b",
            r"(SEBI Notification for [A-Za-z0-9\-/:()., ]+?)\s+dated\b",
            r"(SEBI Notification on [A-Za-z0-9\-/:()., ]+?)\s+dated\b",
            r"(Circular No\.\s*[A-Za-z0-9\-/:(). ]+)",
            r"(Notification No\.\s*[A-Za-z0-9\-/:(). ]+)",
            r"\b(NISM-Series-[A-Za-z0-9\-]+)\b",
            r"\b(NISM Series [A-Za-z0-9\-]+)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.I)
            if match:
                value = self.clean_text(match.group(1) if match.groups() else match.group(0))
                value = re.sub(r"\s+dated$", "", value, flags=re.I)
                return value.strip(" .")
        return ""

    def extract_explicit_reference(self, text: str) -> str:
        cleaned = self.clean_text(text)
        match = re.search(r"(Reference Number|Ref|Circular No\.|Notification No\.)\s*:?\s*([A-Za-z0-9/\-:(). ]+)", cleaned, re.I)
        if not match:
            return ""
        value = self.clean_text(match.group(2))
        value = re.split(r"\b(?:Last Updated on|Click here)\b", value, maxsplit=1, flags=re.I)[0].strip(" .")
        return value

    def filter_records(self, records: list[NISMRecord], *, from_date: date | None, to_date: date | None) -> list[NISMRecord]:
        filtered: list[NISMRecord] = []
        for record in records:
            if not record.date:
                if from_date or to_date:
                    continue
                filtered.append(record)
                continue
            current = date.fromisoformat(record.date)
            if from_date and current < from_date:
                continue
            if to_date and current > to_date:
                continue
            filtered.append(record)
        return filtered

    def deduplicate_records(self, records: list[NISMRecord]) -> list[NISMRecord]:
        deduped: list[NISMRecord] = []
        seen: set[tuple[str, ...]] = set()
        for record in records:
            key = self.record_dedup_key(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def record_dedup_key(self, record: NISMRecord) -> tuple[str, ...]:
        subject = (normalize_text(record.subject) or "").lower()
        link = (normalize_text(record.link) or "").lower()
        circular_no = (normalize_text(record.circular_no) or "").lower()
        if record.date and circular_no:
            return (record.date, subject, circular_no, link)
        return (subject, link)

    def date_bounds(self, records: list[NISMRecord]) -> tuple[date | None, date | None]:
        dates = [date.fromisoformat(item.date) for item in records if item.date]
        if not dates:
            return None, None
        return max(dates), min(dates)

    def count_by_year(self, records: list[NISMRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year = record.date[:4]
            counts[year] = counts.get(year, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def detect_link_type(self, link: str) -> str:
        if not link:
            return "empty"
        lowered = link.lower()
        parsed = urlparse(link)
        if parsed.netloc and not any(marker in parsed.netloc.lower() for marker in VALID_NISM_HOST_MARKERS):
            return "external"
        if lowered.endswith(".pdf"):
            return "pdf"
        if lowered.endswith(".doc") or lowered.endswith(".docx"):
            return "doc/docx"
        if lowered.endswith(".xls") or lowered.endswith(".xlsx"):
            return "xls/xlsx"
        if lowered.endswith(".zip"):
            return "zip"
        if "/circular/" in lowered or lowered.endswith(".html") or lowered.endswith("/"):
            return "html/detail"
        return "other"

    def link_type_counts(self, soup: BeautifulSoup) -> dict[str, int]:
        counts = {"html/detail": 0, "pdf": 0, "doc/docx": 0, "xls/xlsx": 0, "zip": 0, "external": 0, "other": 0}
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            if not href:
                continue
            link_type = self.detect_link_type(href)
            if link_type in counts:
                counts[link_type] += 1
            else:
                counts["other"] += 1
        return counts

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool = False) -> None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not resume:
            if path.suffix.lower() == ".csv":
                with open(path, "a", encoding="utf-8", newline=""):
                    pass
            else:
                with open(path, "a", encoding="utf-8"):
                    pass
            return
        if resume or not path.exists():
            return

    def load_existing_output_records(self, out_path: Path) -> list[NISMRecord]:
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".json":
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                NISMRecord(
                    date=(row.get("date") or "").strip(),
                    subject=(row.get("subject") or "").strip(),
                    circular_no=(row.get("circular_no") or "").strip(),
                    link=(row.get("link") or "").strip(),
                    source_url=(row.get("source_url") or "").strip(),
                    scraped_at=(row.get("scraped_at") or "").strip(),
                    download_links=row.get("download_links") if isinstance(row.get("download_links"), list) else [],
                )
                for row in payload
            ]
        records: list[NISMRecord] = []
        with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                downloads_raw = (row.get("download_links") or "").strip()
                download_links = []
                if downloads_raw:
                    try:
                        parsed = json.loads(downloads_raw)
                        if isinstance(parsed, list):
                            download_links = [str(item) for item in parsed]
                    except json.JSONDecodeError:
                        download_links = []
                records.append(
                    NISMRecord(
                        date=(row.get("date") or "").strip(),
                        subject=(row.get("subject") or "").strip(),
                        circular_no=(row.get("circular_no") or "").strip(),
                        link=(row.get("link") or "").strip(),
                        source_url=(row.get("source_url") or "").strip(),
                        scraped_at=(row.get("scraped_at") or "").strip(),
                        download_links=download_links,
                    )
                )
        return records

    def write_output(self, records: list[NISMRecord], out_path: str | Path, *, include_downloads: bool = False) -> None:
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".json":
            payload = [self.output_row(record, include_downloads=include_downloads) for record in records]
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        headers = ENRICHED_OUTPUT_HEADERS if include_downloads else EXPECTED_OUTPUT_HEADERS
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=headers)
            writer.writeheader()
            for record in records:
                writer.writerow(self.output_row(record, include_downloads=include_downloads))

    def append_output(self, records: list[NISMRecord], out_path: Path, *, include_downloads: bool = False) -> None:
        if out_path.suffix.lower() == ".json":
            existing: list[dict[str, Any]] = []
            if out_path.exists():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend(self.output_row(record, include_downloads=include_downloads) for record in records)
            out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        file_exists = out_path.exists() and out_path.stat().st_size > 0
        headers = ENRICHED_OUTPUT_HEADERS if include_downloads else EXPECTED_OUTPUT_HEADERS
        with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            for record in records:
                writer.writerow(self.output_row(record, include_downloads=include_downloads))

    def output_row(self, record: NISMRecord, *, include_downloads: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_downloads:
            row["download_links"] = json.dumps(record.download_links, ensure_ascii=False)
        return row

    def load_checkpoint(self, checkpoint_path: Path) -> NISMCheckpoint:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return NISMCheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: Path, checkpoint: NISMCheckpoint) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_count_csv(self, path: Path, key_name: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([key_name, "count"])
            for key, value in counts.items():
                writer.writerow([key, value])

    def print_inspect_probe(self, probe: dict[str, Any]) -> None:
        print(f"URL: {self.console_safe(probe['url'])}")
        print(f"HTTP status: {probe['status_code']}")
        print(f"final URL: {self.console_safe(probe['final_url'])}")
        print(f"page title: {self.console_safe(probe['page_title'])}")
        print(f"page heading: {self.console_safe(probe['page_heading'])}")
        print(f"whether direct HTTP worked: {probe['direct_http_worked']}")
        print(f"whether rows are present in raw HTML: {probe['rows_present_in_raw_html']}")
        print(f"whether page is WordPress-like: {probe['wordpress_like']}")
        print(f"whether archive pagination exists: {probe['archive_pagination_exists']}")
        print(f"whether search/filter exists: {probe['search_or_filter_exists']}")
        print(f"View All Archive URL if found: {self.console_safe(probe.get('archive_url', ''))}")
        print(f"table/list/card selectors found: {', '.join(probe['selectors_found'])}")
        print("first 10 listed records:")
        for row in probe["first_records"]:
            print(
                f"- {self.console_safe(row.get('date', ''))} | {self.console_safe(row.get('subject', ''))} | "
                f"{self.console_safe(row.get('circular_no', ''))} | {self.console_safe(row.get('link', ''))}"
            )
        if probe["last_records"]:
            print("last 10 listed records:")
            for row in probe["last_records"]:
                print(
                    f"- {self.console_safe(row.get('date', ''))} | {self.console_safe(row.get('subject', ''))} | "
                    f"{self.console_safe(row.get('circular_no', ''))} | {self.console_safe(row.get('link', ''))}"
                )
        print(f"link type counts: {json.dumps(probe['link_type_counts'], ensure_ascii=False)}")

    def console_safe(self, value: Any) -> str:
        text = "" if value is None else str(value)
        return text.encode("ascii", "replace").decode("ascii")
