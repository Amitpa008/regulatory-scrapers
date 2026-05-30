from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NSEKRA_BASE_URL = "https://www.nsekra.com"
NSEKRA_CIRCULARS_URL = f"{NSEKRA_BASE_URL}/circulars"
NSEKRA_PREAUTH_API = f"{NSEKRA_BASE_URL}/api/preloginMgmt/preLogin/preauth"
NSEKRA_CIRCULARS_API = f"{NSEKRA_BASE_URL}/api/preloginMgmt/preLogin/circulars"
NSEKRA_FIXTURE_DIR = Path("tests/fixtures/nsekra")
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
VALID_NSEKRA_HOST_MARKERS = ("nsekra.com",)
REACT_APP_NEO = "U2FsdGVkX1+UzdwJNtaj7B+V7FMWGK2yPu1t4fS45RN/aybZQTj0HSfLK7Qtv9qJsemQQoO5kRl1S5BVxaTzFw=="
NODE_CRYPTO_SCRIPT = r"""
const crypto = require("crypto");
const neo = process.env.NSEKRA_REACT_APP_NEO;
function evpBytesToKey(password, salt, keyLen, ivLen) {
  let data = Buffer.alloc(0);
  let prev = Buffer.alloc(0);
  while (data.length < keyLen + ivLen) {
    const md5 = crypto.createHash("md5");
    md5.update(prev);
    md5.update(Buffer.from(password, "utf8"));
    md5.update(salt);
    prev = md5.digest();
    data = Buffer.concat([data, prev]);
  }
  return { key: data.subarray(0, keyLen), iv: data.subarray(keyLen, keyLen + ivLen) };
}
function decryptOpenSslBase64(cipherText, password) {
  const raw = Buffer.from(cipherText, "base64");
  const salt = raw.subarray(8, 16);
  const enc = raw.subarray(16);
  const material = evpBytesToKey(password, salt, 32, 16);
  const decipher = crypto.createDecipheriv("aes-256-cbc", material.key, material.iv);
  const out = Buffer.concat([decipher.update(enc), decipher.final()]);
  return out.toString("utf8");
}
const inc = decryptOpenSslBase64(neo, "hideme");
const key = Buffer.from(inc, "base64");
const mode = process.argv[1];
const value = process.argv[2] || "";
if (mode === "inc") {
  process.stdout.write(inc);
} else if (mode === "encrypt") {
  const cipher = crypto.createCipheriv("aes-256-ecb", key, null);
  cipher.setAutoPadding(true);
  const out = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]).toString("base64");
  process.stdout.write(out);
} else if (mode === "decrypt") {
  const decipher = crypto.createDecipheriv("aes-256-ecb", key, null);
  decipher.setAutoPadding(true);
  const out = Buffer.concat([decipher.update(Buffer.from(value, "base64")), decipher.final()]).toString("utf8");
  process.stdout.write(out);
} else {
  throw new Error("Unsupported mode");
}
"""


@dataclass
class NSEKRARecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    raw_date: str = ""
    raw_reference: str = ""
    link_type: str = ""


@dataclass
class NSEKRACheckpoint:
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


class NSEKRAScraper(BaseScraper):
    source = "nsekra-circulars"
    regulator = "NSE KRA"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"
        self._preauth_token: str | None = None
        self._preauth_session_state: str | None = None
        self._txn_id: str | None = None
        self._bundle_endpoint_hints: dict[str, Any] | None = None

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(NSEKRA_CIRCULARS_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        del response
        records, _ = self.collect_all_records(NSEKRA_CIRCULARS_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date) if record.date else None,
                "department": None,
                "category": "circulars",
                "pdf_url": record.link if self.detect_link_type(record.link) == "pdf" else None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type="circular",
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record.get("published_date"),
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
        NSEKRA_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        response = self.client.get(url)
        response.raise_for_status()
        html = response.text
        (NSEKRA_FIXTURE_DIR / "circulars.html").write_text(html, encoding="utf-8")

        soup = BeautifulSoup(html, "html.parser")
        page_title = self.clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        script_assets = self.extract_script_assets(soup, str(response.url))
        bundle_urls = [asset for asset in script_assets if asset.lower().endswith(".js")]
        raw_rows = self.parse_rendered_circular_rows(html, str(response.url))
        is_js_shell_only = "enable javascript to run this app" in html.lower()
        bundle_hints = self.inspect_js_bundles(bundle_urls)
        api_payload = self.fetch_api_page(0, source_url=url)
        sample_records = api_payload["records"][:10]
        total_records = api_payload["total_records"]

        api_fixture = {
            "endpoint": NSEKRA_CIRCULARS_API,
            "records": [self.output_row(item) for item in sample_records],
            "total_records": total_records,
        }
        (NSEKRA_FIXTURE_DIR / "circulars_api_live_sample.json").write_text(
            json.dumps(api_fixture, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"URL: {self.console_safe(url)}")
        print(f"HTTP status: {response.status_code}")
        print(f"final URL: {self.console_safe(str(response.url))}")
        print(f"page title: {self.console_safe(page_title)}")
        print(f"whether raw HTML is JS shell only: {is_js_shell_only}")
        print(f"scripts/assets found: {json.dumps(script_assets, ensure_ascii=False)}")
        print(f"JS bundle URLs found: {json.dumps(bundle_urls, ensure_ascii=False)}")
        print(f"whether circular data exists in raw HTML: {bool(raw_rows)}")
        print(f"table/list/card selectors found: {json.dumps(self.detect_selectors(soup), ensure_ascii=False)}")
        print(f"link patterns found: {json.dumps(self.collect_link_patterns(sample_records), ensure_ascii=False)}")
        print("first 10 circular rows if present:")
        for record in (raw_rows[:10] if raw_rows else sample_records):
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        print(f"Playwright used: no")
        print(f"API endpoint if any: {self.console_safe(bundle_hints.get('api_endpoint', NSEKRA_CIRCULARS_API))}")
        print("API method: POST")
        print(f"headers required: {json.dumps({'Content-Type': 'text/plain', 'Authorization': 'Bearer <preauth token>'}, ensure_ascii=False)}")
        print(f"query params/payload: {json.dumps({'pageNum': 0, 'recordsperpage': 10, 'txnId': '<generated prelogin transaction id>'}, ensure_ascii=False)}")
        print(
            "response JSON keys or table structure: "
            f"{json.dumps(bundle_hints.get('response_keys', ['CircularView', 'createdDate', 'circularReference', 'subject', 'filePath', 'TotalRecords']), ensure_ascii=False)}"
        )
        print(f"total record count if exposed: {total_records if total_records is not None else ''}")
        print("sample 10 records:")
        for record in sample_records:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )

        return {
            "url": url,
            "status_code": response.status_code,
            "final_url": str(response.url),
            "page_title": page_title,
            "js_shell_only": is_js_shell_only,
            "script_assets": script_assets,
            "bundle_urls": bundle_urls,
            "raw_html_record_count": len(raw_rows),
            "api_endpoint": bundle_hints.get("api_endpoint", NSEKRA_CIRCULARS_API),
            "total_records": total_records,
            "sample_records": [asdict(item) for item in sample_records],
        }

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        records, total_records = self.collect_all_records(url)
        count_by_year = self.count_by_year(records)
        newest_date, oldest_date = self.date_bounds(records)
        earliest_records = sorted([item for item in records if item.date], key=lambda item: item.date)[:10]
        print(f"working endpoint/page-flow: {self.console_safe(NSEKRA_CIRCULARS_API)} via POST after public preauth bootstrap")
        print("whether direct HTTP worked: yes")
        print("whether Playwright was used: no")
        print(f"newest circular date found: {newest_date.isoformat() if newest_date else ''}")
        print(f"oldest circular date found: {oldest_date.isoformat() if oldest_date else ''}")
        print(f"total record count if available: {total_records if total_records is not None else len(records)}")
        print(f"count by year: {json.dumps(count_by_year, ensure_ascii=False)}")
        print("sample earliest 10 records:")
        for record in earliest_records:
            print(
                f"- {self.console_safe(record.date)} | {self.console_safe(record.subject)} | "
                f"{self.console_safe(record.circular_no)} | {self.console_safe(record.link)}"
            )
        print("limitation, if any: archive completeness is proven only as far as the public paginated API exposes rows.")
        return {
            "newest_date": newest_date.isoformat() if newest_date else "",
            "oldest_date": oldest_date.isoformat() if oldest_date else "",
            "total_records": total_records if total_records is not None else len(records),
            "count_by_year": count_by_year,
        }

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
    ) -> list[NSEKRARecord]:
        del all_available
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        preview_records, total_records = self.collect_all_records(url, retries=retries, retry_base_delay=retry_base_delay, retry_max_delay=retry_max_delay)
        total_chunks = self.total_pages_from_total_records(total_records or len(preview_records))
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
            checkpoint = NSEKRACheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=newest_available_date.isoformat() if newest_available_date else None,
                oldest_available_date=oldest_available_date.isoformat() if oldest_available_date else None,
                total_records_detected=total_records,
                count_by_year=self.count_by_year(preview_records),
                chunk_strategy="api_page",
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

        all_written_records: list[NSEKRARecord] = []
        duplicates_skipped = 0
        previous_last_completed_chunk = checkpoint.last_completed_chunk

        for page_number in range(resume_from_chunk, expected_end_chunk + 1):
            payload = self.fetch_api_page(
                page_number - 1,
                source_url=url,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            page_records = self.filter_records(payload["records"], from_date=from_date, to_date=to_date)
            fresh_records: list[NSEKRARecord] = []
            for record in page_records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)
            self.append_output(fresh_records, out_path)
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
        report_path = file_path.parent / "nsekra_circulars_validation_report.json"
        year_counts_path = file_path.parent / "nsekra_circulars_year_counts.csv"

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
            "pdf": 0,
            "html/detail": 0,
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
                    normalized_date = self.normalize_date(date_value)
                    if normalized_date is None:
                        suspicious_rows.append({"row": total_rows, "reason": "invalid_date", "value": date_value})
                    else:
                        dates_seen.append(normalized_date)
                        rows_per_year[normalized_date[:4]] = rows_per_year.get(normalized_date[:4], 0) + 1
                if not subject:
                    missing_subject += 1
                if not circular_no:
                    missing_circular_no += 1
                    suspicious_rows.append({"row": total_rows, "reason": "missing_circular_no", "value": ""})
                if not link:
                    missing_link += 1

                link_type = self.detect_link_type(link)
                link_type_counts[link_type if link_type in link_type_counts else "other"] += 1

                dedupe_key = self.record_dedup_key(
                    NSEKRARecord(
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

                if link and not any(marker in link.lower() for marker in VALID_NSEKRA_HOST_MARKERS):
                    suspicious_rows.append({"row": total_rows, "reason": "non_nsekra_link", "value": link})
                if subject and len(subject) < 8:
                    suspicious_rows.append({"row": total_rows, "reason": "very_short_subject", "value": subject})
                if not link:
                    suspicious_rows.append({"row": total_rows, "reason": "missing_link", "value": ""})
                if subject and subject.lower() in {"home", "circulars", "login", "contact us"}:
                    suspicious_rows.append({"row": total_rows, "reason": "navigation_menu_only_row", "value": subject})

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

    def extract_script_assets(self, soup: BeautifulSoup, source_url: str) -> list[str]:
        assets: list[str] = []
        for tag in soup.select("script[src], link[href]"):
            href = tag.get("src") or tag.get("href") or ""
            href = self.normalize_link(href, source_url)
            if href and href not in assets:
                assets.append(href)
        return assets

    def inspect_js_bundles(self, bundle_urls: list[str]) -> dict[str, Any]:
        hints = {
            "api_endpoint": NSEKRA_CIRCULARS_API,
            "response_keys": ["CircularView", "createdDate", "circularReference", "subject", "filePath", "TotalRecords"],
        }
        for bundle_url in bundle_urls[:3]:
            try:
                bundle_text = self.fetch_page_html(bundle_url)
            except Exception:
                continue
            if "/preloginMgmt/preLogin/circulars" in bundle_text:
                hints["api_endpoint"] = NSEKRA_CIRCULARS_API
            keys = []
            for key in ("CircularView", "createdDate", "circularReference", "subject", "filePath", "TotalRecords"):
                if key in bundle_text:
                    keys.append(key)
            if keys:
                hints["response_keys"] = keys
            self._bundle_endpoint_hints = hints
            break
        return hints

    def fetch_api_page(
        self,
        page_num: int,
        *,
        source_url: str,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> dict[str, Any]:
        token = self.ensure_preauth_token(retries=retries, retry_base_delay=retry_base_delay, retry_max_delay=retry_max_delay)
        payload = {
            "pageNum": page_num,
            "recordsperpage": 10,
            "txnId": self._txn_id,
        }
        encrypted_payload = self.node_encrypt_json(payload)
        response = self.post_json_with_retries(
            NSEKRA_CIRCULARS_API,
            body=encrypted_payload,
            headers={
                "Content-Type": "text/plain",
                "Accept": "text/plain, */*",
                "Origin": NSEKRA_BASE_URL,
                "Referer": NSEKRA_CIRCULARS_URL,
                "Authorization": f"Bearer {token}",
            },
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        decrypted = self.node_decrypt_json(response.text.strip())
        records, total_records = self.parse_api_payload(decrypted, source_url)
        return {
            "records": records,
            "total_records": total_records,
            "raw_payload": decrypted,
        }

    def collect_all_records(
        self,
        source_url: str,
        *,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> tuple[list[NSEKRARecord], int | None]:
        first_page = self.fetch_api_page(
            0,
            source_url=source_url,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        records = list(first_page["records"])
        total_records = first_page["total_records"] if first_page["total_records"] is not None else len(records)
        total_pages = self.total_pages_from_total_records(total_records)
        for page_num in range(1, total_pages):
            page = self.fetch_api_page(
                page_num,
                source_url=source_url,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            records.extend(page["records"])
        return self.deduplicate_records(records), total_records

    def ensure_preauth_token(
        self,
        *,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> str:
        if self._preauth_token and self._txn_id:
            return self._preauth_token
        txn_uuid = str(uuid.uuid4())
        self._txn_id = f"{txn_uuid[:-4]}_1"
        encrypted_payload = self.node_encrypt_json({"txnId": self._txn_id})
        response = self.post_json_with_retries(
            NSEKRA_PREAUTH_API,
            body=encrypted_payload,
            headers={
                "Content-Type": "text/plain",
                "Accept": "text/plain, */*",
                "Origin": NSEKRA_BASE_URL,
                "Referer": NSEKRA_CIRCULARS_URL,
            },
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        decrypted = self.node_decrypt_json(response.text.strip())
        token = self.extract_preauth_token(decrypted)
        if not token:
            raise RuntimeError(f"Unable to obtain NSE KRA prelogin access token: {decrypted}")
        self._preauth_token = token
        self._preauth_session_state = self.extract_preauth_session_state(decrypted)
        return token

    def extract_preauth_token(self, payload: dict[str, Any]) -> str:
        data = payload.get("data") or {}
        body = payload.get("body") or {}
        if isinstance(data, dict):
            for key in ("accessToken", "access_token"):
                value = (data.get(key) or "").strip()
                if value:
                    return value
        if isinstance(body, dict):
            for key in ("accessToken", "access_token"):
                value = (body.get(key) or "").strip()
                if value:
                    return value
        return ""

    def extract_preauth_session_state(self, payload: dict[str, Any]) -> str:
        data = payload.get("data") or {}
        body = payload.get("body") or {}
        if isinstance(data, dict):
            for key in ("sessionID", "session_state", "sessionState"):
                value = (data.get(key) or "").strip()
                if value:
                    return value
        if isinstance(body, dict):
            for key in ("sessionID", "session_state", "sessionState"):
                value = (body.get(key) or "").strip()
                if value:
                    return value
        return ""

    def post_json_with_retries(
        self,
        url: str,
        *,
        body: str,
        headers: dict[str, str],
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self.rate_limit()
                response = self.client.post(url, content=body, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except Exception as exc:  # pragma: no cover - live retry path
                last_exc = exc
                if not self.is_retryable_http_exception(exc) or attempt >= retries:
                    raise
                delay = self.compute_retry_delay(
                    attempt,
                    base_delay=retry_base_delay,
                    max_delay=retry_max_delay,
                )
                time.sleep(delay)
        if last_exc is not None:  # pragma: no cover - defensive
            raise last_exc
        raise RuntimeError(f"Unknown POST failure for {url}")

    def node_encrypt_json(self, payload: dict[str, Any]) -> str:
        return self.node_crypto("encrypt", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))

    def node_decrypt_json(self, payload: str) -> dict[str, Any]:
        return json.loads(self.node_crypto("decrypt", payload))

    def node_crypto(self, mode: str, value: str) -> str:
        result = subprocess.run(
            ["node", "-e", NODE_CRYPTO_SCRIPT, mode, value],
            cwd=Path.cwd(),
            env={**os.environ, "NSEKRA_REACT_APP_NEO": REACT_APP_NEO},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"NSE KRA crypto helper failed: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout.strip()

    def parse_api_payload(self, payload: dict[str, Any], source_url: str) -> tuple[list[NSEKRARecord], int | None]:
        circular_view = ((payload.get("data") or {}).get("CircularView")) or []
        rows = list(circular_view)
        total_records: int | None = None
        if rows and isinstance(rows[-1], dict) and "TotalRecords" in rows[-1]:
            total_row = rows.pop()
            try:
                total_records = int(total_row.get("TotalRecords"))
            except (TypeError, ValueError):
                total_records = None

        records: list[NSEKRARecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for row in rows:
            raw_date = self.clean_text(row.get("createdDate", ""))
            circular_no = self.clean_text(row.get("circularReference", ""))
            subject = self.clean_text(row.get("subject", ""))
            file_path = self.clean_text(row.get("filePath", ""))
            normalized_date = self.normalize_date(raw_date) or ""
            if not circular_no:
                circular_no = self.extract_reference(subject)
            link = self.normalize_link(file_path, NSEKRA_BASE_URL) if file_path else ""
            records.append(
                NSEKRARecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=raw_date,
                    raw_reference=self.clean_text(row.get("circularReference", "")),
                    link_type=self.detect_link_type(link),
                )
            )
        return records, total_records

    def parse_rendered_circular_rows(self, html: str, source_url: str) -> list[NSEKRARecord]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[NSEKRARecord] = []
        rows = soup.select("table tbody tr")
        if not rows:
            return records
        scraped_at = datetime.now(timezone.utc).isoformat()
        for row in rows:
            cells = [self.clean_text(cell.get_text(" ", strip=True)) for cell in row.select("td")]
            if len(cells) < 3:
                continue
            href = ""
            for anchor in row.select("a[href]"):
                href = self.normalize_link(anchor.get("href") or "", source_url)
                if href:
                    break
            records.append(
                NSEKRARecord(
                    date=self.normalize_date(cells[0]) or "",
                    subject=cells[2],
                    circular_no=cells[1],
                    link=href,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    raw_date=cells[0],
                    raw_reference=cells[1],
                    link_type=self.detect_link_type(href),
                )
            )
        return records

    def detect_selectors(self, soup: BeautifulSoup) -> list[str]:
        selectors: list[str] = []
        if soup.select("table"):
            selectors.append("table")
        if soup.select("table tbody tr"):
            selectors.append("table tbody tr")
        if soup.select("[class*=card]"):
            selectors.append("[class*=card]")
        if soup.select("[class*=list]"):
            selectors.append("[class*=list]")
        return selectors

    def collect_link_patterns(self, records: list[NSEKRARecord]) -> list[str]:
        patterns: list[str] = []
        for record in records:
            link = record.link
            if not link:
                continue
            parsed = urlparse(link)
            pattern = parsed.path
            if pattern not in patterns:
                patterns.append(pattern)
        return patterns

    def clean_text(self, value: str) -> str:
        text = normalize_text(value) or ""
        text = text.replace("\xa0", " ")
        text = text.replace("â€“", "-").replace("â€”", "-").replace("’", "'")
        text = text.replace("“", '"').replace("”", '"').replace("‘", "'")
        text = re.sub(r"�{1,3}\s*([^�]+?)\s*�{1,3}", r'"\1"', text)
        text = text.replace("�", "")
        text = re.sub(r"\s+", " ", text)
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
        for parser in (
            self._parse_iso_date,
            self._parse_datetime_date,
            self._parse_dash_month_date,
            self._parse_space_month_date,
        ):
            parsed = parser(raw)
            if parsed:
                return parsed
        return None

    def _parse_iso_date(self, value: str) -> str | None:
        candidate = value.replace("Z", "+00:00")
        try:
            if "T" in candidate:
                return datetime.fromisoformat(candidate).date().isoformat()
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return None

    def _parse_datetime_date(self, value: str) -> str | None:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _parse_dash_month_date(self, value: str) -> str | None:
        for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def _parse_space_month_date(self, value: str) -> str | None:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def extract_reference(self, text: str) -> str:
        cleaned = self.clean_text(text)
        patterns = [
            r"\b(NSE/KRA/\d{4}/\d{2})\b",
            r"\b(NSE/KRA/\d{4}/\d{1,2})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.I)
            if match:
                return self.clean_text(match.group(1))
        return ""

    def filter_records(self, records: list[NSEKRARecord], *, from_date: date | None, to_date: date | None) -> list[NSEKRARecord]:
        filtered: list[NSEKRARecord] = []
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

    def deduplicate_records(self, records: list[NSEKRARecord]) -> list[NSEKRARecord]:
        deduped: list[NSEKRARecord] = []
        seen: set[tuple[str, ...]] = set()
        for record in records:
            key = self.record_dedup_key(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def record_dedup_key(self, record: NSEKRARecord) -> tuple[str, ...]:
        subject = (normalize_text(record.subject) or "").lower()
        circular_no = (normalize_text(record.circular_no) or "").lower()
        link = (normalize_text(record.link) or "").lower()
        if link:
            return (record.date, subject, circular_no, link)
        return (record.date, subject, circular_no)

    def date_bounds(self, records: list[NSEKRARecord]) -> tuple[date | None, date | None]:
        dates = [date.fromisoformat(item.date) for item in records if item.date]
        if not dates:
            return None, None
        return max(dates), min(dates)

    def count_by_year(self, records: list[NSEKRARecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            if not record.date:
                continue
            year = record.date[:4]
            counts[year] = counts.get(year, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def total_pages_from_total_records(self, total_records: int) -> int:
        if total_records <= 0:
            return 0
        return (total_records + 9) // 10

    def detect_link_type(self, link: str) -> str:
        if not link:
            return "empty"
        lowered = link.lower()
        parsed = urlparse(link)
        if parsed.netloc and not any(marker in parsed.netloc.lower() for marker in VALID_NSEKRA_HOST_MARKERS):
            return "external"
        if lowered.endswith(".pdf"):
            return "pdf"
        if lowered.endswith(".doc") or lowered.endswith(".docx"):
            return "doc/docx"
        if lowered.endswith(".xls") or lowered.endswith(".xlsx"):
            return "xls/xlsx"
        if lowered.endswith(".zip"):
            return "zip"
        if lowered.endswith(".html") or lowered.endswith("/") or "/circulars" in lowered:
            return "html/detail"
        return "other"

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

    def load_existing_output_records(self, out_path: Path) -> list[NSEKRARecord]:
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".json":
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                NSEKRARecord(
                    date=(row.get("date") or "").strip(),
                    subject=(row.get("subject") or "").strip(),
                    circular_no=(row.get("circular_no") or "").strip(),
                    link=(row.get("link") or "").strip(),
                    source_url=(row.get("source_url") or "").strip(),
                    scraped_at=(row.get("scraped_at") or "").strip(),
                )
                for row in payload
            ]
        records: list[NSEKRARecord] = []
        with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                records.append(
                    NSEKRARecord(
                        date=(row.get("date") or "").strip(),
                        subject=(row.get("subject") or "").strip(),
                        circular_no=(row.get("circular_no") or "").strip(),
                        link=(row.get("link") or "").strip(),
                        source_url=(row.get("source_url") or "").strip(),
                        scraped_at=(row.get("scraped_at") or "").strip(),
                    )
                )
        return records

    def write_output(self, records: list[NSEKRARecord], out_path: str | Path) -> None:
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

    def append_output(self, records: list[NSEKRARecord], out_path: Path) -> None:
        if out_path.suffix.lower() == ".json":
            existing: list[dict[str, Any]] = []
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

    def output_row(self, record: NSEKRARecord) -> dict[str, Any]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: Path) -> NSEKRACheckpoint:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return NSEKRACheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: Path, checkpoint: NSEKRACheckpoint) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_count_csv(self, path: Path, key_name: str, counts: dict[str, int]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([key_name, "count"])
            for key, value in counts.items():
                writer.writerow([key, value])

    def console_safe(self, value: Any) -> str:
        text = "" if value is None else str(value)
        return text.encode("ascii", "replace").decode("ascii")
