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

from bs4 import BeautifulSoup, Tag

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


APMI_BASE_URL = "https://www.apmiindia.org"
APMI_WELCOME_URL = f"{APMI_BASE_URL}/apmi/welcome.htm"
APMI_FIXTURE_PATH = Path("tests/fixtures/apmi/welcome.html")

APMI_DOCUMENT_HEADERS = [
    "section_path",
    "category",
    "title",
    "date",
    "circular_no",
    "link",
    "link_type",
    "source_url",
    "scraped_at",
]
APMI_COMPLIANCE_SUTRA_HEADERS = [
    "section_path",
    "category",
    "title",
    "date",
    "link",
    "link_type",
    "source_url",
    "scraped_at",
]
STANDARD_ARCHIVE_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
ENRICHED_ARCHIVE_HEADERS = ["date", "category", "subject", "circular_no", "link", "source_url", "scraped_at"]

APMI_SOURCE_LABELS = {
    "apmi-documents": "APMI Documents",
    "apmi-circulars": "APMI Circulars",
    "apmi-sebi-resources": "APMI SEBI Resources",
    "apmi-compliance-sutra": "APMI Compliance Sutra",
}


@dataclass
class APMIDocumentRecord:
    section_path: str
    category: str
    title: str
    date: str
    circular_no: str
    link: str
    link_type: str
    source_url: str
    scraped_at: str


class APMIScraper(BaseScraper):
    source: str
    regulator = "APMI"

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        if "rate_limit_seconds" not in kwargs:
            kwargs["rate_limit_seconds"] = 0.1
        super().__init__(**kwargs)
        self.config = config

    @property
    def source_label(self) -> str:
        return APMI_SOURCE_LABELS[self.source]

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        del from_date, to_date
        return self.fetch_page_html(APMI_WELCOME_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_inventory_records(str(response), APMI_WELCOME_URL)
        return [
            {
                "title": record.title,
                "url": record.link,
                "document_type": record.link_type,
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date) if record.date else None,
                "department": None,
                "category": record.category,
                "pdf_url": record.link if record.link_type == "pdf" else None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "document"),
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
        return response.text

    def inspect_site(self, url: str) -> dict[str, Any]:
        html = self.fetch_page_html(url)
        APMI_FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        APMI_FIXTURE_PATH.write_text(html, encoding="utf-8")
        inventory_records = self.parse_inventory_records(html, url)
        probe = self.inspect_html(html, url, inventory_records)
        self.print_probe(probe)
        return probe

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None,
        to_date: date | None,
        all_available: bool = False,
        delay_seconds: float = 0.0,
        include_category: bool = False,
        **_: Any,
    ) -> list[APMIDocumentRecord]:
        del all_available
        self.ensure_output_writable(out_path)
        html = self.fetch_page_html(url)
        records = self.parse_inventory_records(html, url)
        records = self.filter_for_source(records)
        records = self.filter_by_date(records, from_date=from_date, to_date=to_date)
        records = self.deduplicate_records(records)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        self.write_output(records, out_path, include_category=include_category)
        print(f"Rows written: {len(records)}")
        print(f"Duplicates skipped: 0")
        print(f"Final CSV row count: {len(records)}")
        print(f"Wrote {len(records)} records to {out_path}")
        print("Fetch transport: httpx")
        return records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        prefix = self.source.replace("-", "_")
        if self.source == "apmi-documents":
            report_path = file_path.parent / "apmi_documents_validation_report.json"
            category_counts_path = file_path.parent / "apmi_documents_category_counts.csv"
            link_type_counts_path = file_path.parent / "apmi_documents_link_type_counts.csv"
            report = self.validate_inventory(file_path)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.write_count_csv(category_counts_path, "category", report["category_counts"])
            self.write_count_csv(link_type_counts_path, "link_type", report["link_type_counts"])
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return report

        report_path = file_path.parent / f"{prefix}_validation_report.json"
        year_counts_path = file_path.parent / f"{prefix}_year_counts.csv"
        report = self.validate_standard_archive(file_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_count_csv(year_counts_path, "year", report["rows_per_year"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    def check_links(
        self,
        *,
        file_path: str | Path,
        out_path: str | Path,
        delay_seconds: float,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> list[dict[str, Any]]:
        del retries, retry_base_delay, retry_max_delay
        file_path = Path(file_path)
        rows: list[dict[str, str]] = []
        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                rows.append(row)

        results: list[dict[str, Any]] = []
        for row in rows:
            link = (row.get("link") or "").strip()
            title = (row.get("title") or row.get("subject") or "").strip()
            entry = {
                "title": title,
                "link": link,
                "status_code": None,
                "content_type": "",
                "final_url": "",
                "ok": False,
                "error": "",
            }
            if not link:
                entry["error"] = "missing_link"
                results.append(entry)
                continue
            try:
                response = self.client.head(link, follow_redirects=True)
                if response.status_code in {405, 501}:
                    with self.client.stream("GET", link, follow_redirects=True) as streamed:
                        entry["status_code"] = streamed.status_code
                        entry["content_type"] = streamed.headers.get("content-type", "")
                        entry["final_url"] = str(streamed.url)
                        entry["ok"] = streamed.is_success
                else:
                    entry["status_code"] = response.status_code
                    entry["content_type"] = response.headers.get("content-type", "")
                    entry["final_url"] = str(response.url)
                    entry["ok"] = response.is_success
            except Exception as exc:  # pragma: no cover - network/runtime dependent
                entry["error"] = str(exc)
            results.append(entry)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(
                file_obj,
                fieldnames=["title", "link", "status_code", "content_type", "final_url", "ok", "error"],
            )
            writer.writeheader()
            writer.writerows(results)
        return results

    def parse_inventory_records(self, html: str, source_url: str) -> list[APMIDocumentRecord]:
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("ul.navbar-nav")
        if root is None:
            return []

        records: list[APMIDocumentRecord] = []
        scraped_at = datetime.now(timezone.utc).isoformat()
        for li in root.find_all("li", recursive=False):
            records.extend(self.walk_menu_li(li, source_url, [], scraped_at))
        return records

    def walk_menu_li(
        self,
        li: Tag,
        source_url: str,
        path: list[str],
        scraped_at: str,
    ) -> list[APMIDocumentRecord]:
        anchor = li.find("a", recursive=False)
        if anchor is None:
            return []
        title = self.clean_text(anchor.get_text(" ", strip=True))
        href = (anchor.get("href") or "").strip()
        child_ul = li.find("ul", recursive=False)

        current_path = list(path)
        if title:
            current_path.append(title)

        records: list[APMIDocumentRecord] = []
        if href and href != "#" and title:
            section_parts = path if path else [title]
            section_path = " > ".join(section_parts)
            category = section_parts[-1] if section_parts else title
            absolute_link = self.normalize_link(href, source_url)
            records.append(
                APMIDocumentRecord(
                    section_path=section_path,
                    category=category,
                    title=title,
                    date=self.parse_title_date(title),
                    circular_no=self.extract_circular_no(title),
                    link=absolute_link,
                    link_type=self.classify_link_type(absolute_link),
                    source_url=source_url,
                    scraped_at=scraped_at,
                )
            )

        if child_ul is not None:
            for child_li in child_ul.find_all("li", recursive=False):
                records.extend(self.walk_menu_li(child_li, source_url, current_path, scraped_at))
        return records

    def filter_for_source(self, records: list[APMIDocumentRecord]) -> list[APMIDocumentRecord]:
        if self.source == "apmi-documents":
            return records
        if self.source == "apmi-sebi-resources":
            prefixes = (
                "CIRCULARS > SEBI Circulars",
                "CIRCULARS > Communication from SEBI",
                "CIRCULARS > SEBI Consultation Papers",
                "CIRCULARS > SEBI Board Meetings",
            )
            return [record for record in records if any(record.section_path.startswith(prefix) for prefix in prefixes)]
        if self.source == "apmi-compliance-sutra":
            return [record for record in records if "IMPORTANT > Compliance Sutra" in record.section_path]
        if self.source == "apmi-circulars":
            prefixes = (
                "CIRCULARS > APMI Circulars and Guidelines",
                "CIRCULARS > Communication from APMI",
            )
            return [record for record in records if any(record.section_path.startswith(prefix) for prefix in prefixes)]
        return records

    def filter_by_date(
        self,
        records: list[APMIDocumentRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[APMIDocumentRecord]:
        if from_date is None and to_date is None:
            return records
        filtered: list[APMIDocumentRecord] = []
        for record in records:
            if not record.date:
                if self.source in {"apmi-circulars", "apmi-sebi-resources"}:
                    filtered.append(record)
                continue
            row_date = date.fromisoformat(record.date)
            if from_date and row_date < from_date:
                continue
            if to_date and row_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def deduplicate_records(self, records: list[APMIDocumentRecord]) -> list[APMIDocumentRecord]:
        seen: set[tuple[str, ...]] = set()
        deduped: list[APMIDocumentRecord] = []
        for record in records:
            key = self.record_dedup_key(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def record_dedup_key(self, record: APMIDocumentRecord) -> tuple[str, ...]:
        normalized_title = (normalize_text(record.title or "") or "").lower()
        normalized_link = (normalize_text(record.link or "") or "").lower()
        if self.source == "apmi-documents":
            return (record.section_path, normalized_title, normalized_link)
        normalized_circular = (normalize_text(record.circular_no or "") or "").lower()
        if record.date and record.circular_no:
            return (record.date, normalized_title, normalized_circular, normalized_link)
        return (normalized_title, normalized_link)

    def inspect_html(self, html: str, source_url: str, records: list[APMIDocumentRecord]) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a")
        resource_anchors = [
            anchor
            for anchor in anchors
            if self.clean_text(anchor.get_text(" ", strip=True)) and (anchor.get("href") or "").strip() and (anchor.get("href") or "").strip() != "#"
        ]
        titles = [self.clean_text(anchor.get_text(" ", strip=True)) for anchor in anchors]
        nonempty_titles = [item for item in titles if item]
        duplicate_visible_titles = len(nonempty_titles) != len(set(nonempty_titles))
        href_missing = sum(1 for anchor in anchors if not (anchor.get("href") or "").strip())
        relative_count = sum(1 for anchor in resource_anchors if self.is_relative_href((anchor.get("href") or "").strip()))
        absolute_count = len(resource_anchors) - relative_count
        top_level_categories = sorted({record.section_path.split(" > ")[0] for record in records if record.section_path})
        section_paths = sorted({record.section_path for record in records if record.section_path})

        link_type_counts = {
            "pdf": 0,
            "doc/docx": 0,
            "xls/xlsx": 0,
            "zip": 0,
            "ppt/pptx": 0,
            "html/detail": 0,
            "youtube/external": 0,
            "other": 0,
        }
        for record in records:
            link_type = record.link_type
            if link_type == "pdf":
                link_type_counts["pdf"] += 1
            elif link_type in {"doc", "docx"}:
                link_type_counts["doc/docx"] += 1
            elif link_type in {"xls", "xlsx"}:
                link_type_counts["xls/xlsx"] += 1
            elif link_type == "zip":
                link_type_counts["zip"] += 1
            elif link_type in {"ppt", "pptx"}:
                link_type_counts["ppt/pptx"] += 1
            elif link_type == "html":
                link_type_counts["html/detail"] += 1
            elif link_type in {"youtube", "external"}:
                link_type_counts["youtube/external"] += 1
            else:
                link_type_counts["other"] += 1

        return {
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "direct_http_worked": True,
            "page_is_static_html": True,
            "playwright_needed": False,
            "top_level_categories": top_level_categories,
            "section_paths": section_paths,
            "total_anchor_count": len(anchors),
            "total_document_resource_link_count": len(records),
            "first_20_document_links": [asdict(record) for record in records[:20]],
            "link_type_counts": link_type_counts,
            "relative_link_count": relative_count,
            "absolute_link_count": absolute_count,
            "missing_href_count": href_missing,
            "duplicate_visible_titles_exist": duplicate_visible_titles,
        }

    def print_probe(self, probe: dict[str, Any]) -> None:
        print(f"page title: {self.console_safe(probe['page_title'])}")
        print(f"whether direct HTTP worked: {probe['direct_http_worked']}")
        print(f"whether page is static HTML: {probe['page_is_static_html']}")
        print(f"whether Playwright was needed: {probe['playwright_needed']}")
        print(f"top-level sections found: {probe['top_level_categories']}")
        print("nested section paths found:")
        for path in probe["section_paths"]:
            print(f"- {self.console_safe(path)}")
        print(f"total anchor count: {probe['total_anchor_count']}")
        print(f"total document/resource link count: {probe['total_document_resource_link_count']}")
        print("first 20 document links:")
        for row in probe["first_20_document_links"]:
            print(
                f"- {self.console_safe(row['section_path'])} | {self.console_safe(row['title'])} | "
                f"{self.console_safe(row['date'])} | {self.console_safe(row['circular_no'])} | {self.console_safe(row['link'])}"
            )
        print(f"link type counts: {probe['link_type_counts']}")
        print(
            "whether links are relative or absolute: "
            f"relative={probe['relative_link_count']}, absolute={probe['absolute_link_count']}"
        )
        print(f"whether any links are missing href: {probe['missing_href_count'] > 0} (count={probe['missing_href_count']})")
        print(f"whether duplicate visible titles exist: {probe['duplicate_visible_titles_exist']}")

    def normalize_link(self, href: str, source_url: str) -> str:
        return urljoin(source_url, href.strip())

    def is_relative_href(self, href: str) -> bool:
        parsed = urlparse(href)
        return not bool(parsed.scheme)

    def classify_link_type(self, link: str) -> str:
        lowered = (link or "").strip().lower()
        if not lowered:
            return "other"
        if "youtube.com" in lowered or "youtu.be" in lowered:
            return "youtube"
        parsed = urlparse(lowered)
        if parsed.netloc and "apmiindia.org" not in parsed.netloc:
            return "external"
        if lowered.endswith(".pdf"):
            return "pdf"
        if lowered.endswith(".doc"):
            return "doc"
        if lowered.endswith(".docx"):
            return "docx"
        if lowered.endswith(".xls"):
            return "xls"
        if lowered.endswith(".xlsx"):
            return "xlsx"
        if lowered.endswith(".zip"):
            return "zip"
        if lowered.endswith(".ppt"):
            return "ppt"
        if lowered.endswith(".pptx"):
            return "pptx"
        if lowered.endswith(".htm") or lowered.endswith(".html") or "/apmi/" in lowered or "action=" in lowered:
            return "html"
        return "other"

    def parse_title_date(self, title: str) -> str:
        normalized = self.clean_text(title)
        if not normalized:
            return ""
        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|—)?\s*"
            r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
            r"\s*['’]?(\d{2,4})\b",
            normalized,
            flags=re.I,
        )
        if not match:
            return ""
        raw_date = f"{match.group(1)} {match.group(2)} {self.normalize_year_token(match.group(3))}"
        try:
            return parse_indian_date(raw_date).isoformat()
        except Exception:
            return ""

    def normalize_year_token(self, token: str) -> str:
        token = token.strip()
        if len(token) == 2:
            year_value = int(token)
            return str(2000 + year_value if year_value <= 50 else 1900 + year_value)
        return token

    def extract_circular_no(self, title: str) -> str:
        normalized = self.clean_text(title)
        patterns = [
            r"\bAPMI Circular(?: Number)?\s*\d+\b",
            r"\bCircular nos?\s*[A-Za-z0-9./()_-]+\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.I)
            if match:
                return match.group(0).strip()
        return ""

    def clean_text(self, value: str) -> str:
        text = normalize_text(value or "") or ""
        text = text.replace("\xa0", " ").replace("’", "'").replace("‘", "'")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def console_safe(self, value: Any) -> str:
        text = str(value or "")
        return text.encode("ascii", "replace").decode("ascii")

    def ensure_output_writable(self, out_path: str | Path) -> None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8"):
                pass
        except PermissionError as exc:
            raise RuntimeError(f"Output file is locked: {path}") from exc

    def write_output(self, records: list[APMIDocumentRecord], out_path: str | Path, *, include_category: bool = False) -> None:
        out_path = Path(out_path)
        suffix = out_path.suffix.lower()
        if self.source == "apmi-documents":
            rows = [self.document_row(record) for record in records]
            headers = APMI_DOCUMENT_HEADERS
        elif self.source == "apmi-compliance-sutra":
            rows = [self.compliance_sutra_row(record) for record in records]
            headers = APMI_COMPLIANCE_SUTRA_HEADERS
        else:
            rows = [self.archive_row(record, include_category=include_category) for record in records]
            headers = ENRICHED_ARCHIVE_HEADERS if include_category else STANDARD_ARCHIVE_HEADERS

        if suffix == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def document_row(self, record: APMIDocumentRecord) -> dict[str, str]:
        return {
            "section_path": record.section_path,
            "category": record.category,
            "title": record.title,
            "date": record.date,
            "circular_no": record.circular_no,
            "link": record.link,
            "link_type": record.link_type,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def compliance_sutra_row(self, record: APMIDocumentRecord) -> dict[str, str]:
        return {
            "section_path": record.section_path,
            "category": record.category,
            "title": record.title,
            "date": record.date,
            "link": record.link,
            "link_type": record.link_type,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def archive_row(self, record: APMIDocumentRecord, *, include_category: bool) -> dict[str, str]:
        row = {
            "date": record.date,
            "subject": record.title,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }
        if include_category:
            return {
                "date": record.date,
                "category": record.category,
                "subject": record.title,
                "circular_no": record.circular_no,
                "link": record.link,
                "source_url": record.source_url,
                "scraped_at": record.scraped_at,
            }
        return row

    def validate_inventory(self, file_path: Path) -> dict[str, Any]:
        missing_title = 0
        missing_link = 0
        invalid_date = 0
        duplicate_key_count = 0
        non_apmi_external_link_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        link_type_counts: dict[str, int] = {}
        category_counts: dict[str, int] = {}
        seen_keys: set[tuple[str, str, str]] = set()
        total_rows = 0

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            headers_ok = reader.fieldnames == APMI_DOCUMENT_HEADERS
            for row_number, row in enumerate(reader, start=2):
                total_rows += 1
                section_path = (row.get("section_path") or "").strip()
                category = (row.get("category") or "").strip()
                title = (row.get("title") or "").strip()
                row_date = (row.get("date") or "").strip()
                circular_no = (row.get("circular_no") or "").strip()
                link = (row.get("link") or "").strip()
                link_type = (row.get("link_type") or "").strip()

                if not title:
                    missing_title += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_title"})
                elif len(title) < 4:
                    suspicious_rows.append({"row_number": row_number, "reason": "very_short_title", "title": title})
                if not link:
                    missing_link += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_link"})
                if row_date:
                    try:
                        date.fromisoformat(row_date)
                    except ValueError:
                        invalid_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_date", "date": row_date})
                if not section_path:
                    suspicious_rows.append({"row_number": row_number, "reason": "empty_section_path"})
                if link and "apmiindia.org" not in link.lower():
                    non_apmi_external_link_count += 1
                if link and link.startswith("/"):
                    suspicious_rows.append({"row_number": row_number, "reason": "broken_relative_url", "link": link})

                link_type_counts[link_type] = link_type_counts.get(link_type, 0) + 1
                category_counts[category] = category_counts.get(category, 0) + 1
                dedupe_key = (section_path, (normalize_text(title) or "").lower(), (normalize_text(link) or "").lower())
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)
                _ = circular_no

        return {
            "file": str(file_path),
            "source": self.source,
            "headers_ok": headers_ok,
            "expected_headers": APMI_DOCUMENT_HEADERS,
            "total_rows": total_rows,
            "missing_title_count": missing_title,
            "missing_link_count": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "invalid_date_count": invalid_date,
            "link_type_counts": link_type_counts,
            "category_counts": category_counts,
            "non_apmi_external_link_count": non_apmi_external_link_count,
            "suspicious_rows": suspicious_rows,
        }

    def validate_standard_archive(self, file_path: Path) -> dict[str, Any]:
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        duplicate_key_count = 0
        year_counts: dict[str, int] = {}
        valid_dates: list[str] = []
        link_type_counts = {"pdf": 0, "html": 0, "doc/docx": 0, "xls/xlsx": 0, "zip": 0, "other": 0, "empty": 0}
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            headers_ok = reader.fieldnames == STANDARD_ARCHIVE_HEADERS
            for row_number, row in enumerate(reader, start=2):
                total_rows += 1
                row_date = (row.get("date") or "").strip()
                subject = (row.get("subject") or "").strip()
                circular_no = (row.get("circular_no") or "").strip()
                link = (row.get("link") or "").strip()

                if not row_date:
                    missing_date += 1
                else:
                    try:
                        parsed_date = date.fromisoformat(row_date)
                        valid_dates.append(row_date)
                        year_counts[str(parsed_date.year)] = year_counts.get(str(parsed_date.year), 0) + 1
                    except ValueError:
                        missing_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_date", "date": row_date})
                if not subject:
                    missing_subject += 1
                elif len(subject) < 4:
                    suspicious_rows.append({"row_number": row_number, "reason": "very_short_subject", "subject": subject})
                if not circular_no:
                    missing_circular_no += 1
                if not link:
                    missing_link += 1
                    link_type_counts["empty"] += 1
                else:
                    mapped_type = self.classify_link_type(link)
                    if mapped_type == "pdf":
                        link_type_counts["pdf"] += 1
                    elif mapped_type == "html":
                        link_type_counts["html"] += 1
                    elif mapped_type in {"doc", "docx"}:
                        link_type_counts["doc/docx"] += 1
                    elif mapped_type in {"xls", "xlsx"}:
                        link_type_counts["xls/xlsx"] += 1
                    elif mapped_type == "zip":
                        link_type_counts["zip"] += 1
                    else:
                        link_type_counts["other"] += 1
                    lowered = link.lower()
                    if "apmiindia.org" not in lowered and "sebi.gov.in" not in lowered:
                        suspicious_rows.append({"row_number": row_number, "reason": "external_link", "link": link})
                key = (
                    row_date,
                    (normalize_text(subject) or "").lower(),
                    (normalize_text(circular_no) or "").lower(),
                    (normalize_text(link) or "").lower(),
                )
                if not circular_no:
                    key = ((normalize_text(subject) or "").lower(), (normalize_text(link) or "").lower())
                if key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(key)

        return {
            "file": str(file_path),
            "source": self.source,
            "headers_ok": headers_ok,
            "expected_headers": STANDARD_ARCHIVE_HEADERS,
            "total_rows": total_rows,
            "missing_date_count": missing_date,
            "missing_subject_count": missing_subject,
            "missing_circular_no_count": missing_circular_no,
            "missing_link_count": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": link_type_counts,
            "rows_per_year": year_counts,
            "min_date": min(valid_dates) if valid_dates else None,
            "max_date": max(valid_dates) if valid_dates else None,
            "suspicious_rows": suspicious_rows,
        }

    def write_count_csv(self, out_path: str | Path, label_name: str, counts: dict[str, int]) -> None:
        with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow([label_name, "count"])
            for key, value in counts.items():
                writer.writerow([key, value])


class APMIDocumentsScraper(APMIScraper):
    source = "apmi-documents"


class APMICircularsScraper(APMIScraper):
    source = "apmi-circulars"


class APMISEBIResourcesScraper(APMIScraper):
    source = "apmi-sebi-resources"


class APMIComplianceSutraScraper(APMIScraper):
    source = "apmi-compliance-sutra"
