from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import yaml
from loguru import logger

from scrapers.bse import BSEScraper
from scrapers.amfi import AMFIImportantUpdatesScraper, AMFIMFDCircularsScraper
from scrapers.apmi import (
    APMIComplianceSutraScraper,
    APMICircularsScraper,
    APMIDocumentsScraper,
    APMISEBIResourcesScraper,
)
from scrapers.cdsl import CDSLScraper
from scrapers.ccrl import CCRLScraper
from scrapers.ifsca import IFSCAScraper
from scrapers.incometax import IncomeTaxCircularsScraper, IncomeTaxNotificationsScraper
from scrapers.irdai import IRDAIScraper
from scrapers.mcx import MCXScraper
from scrapers.ncdex import NCDEXScraper
from scrapers.nism import NISMScraper
from scrapers.nsekra import NSEKRAScraper
from scrapers.nse import NSEScraper
from scrapers.nse_press_releases import NSEPressReleasesScraper
from scrapers.nerl import NERLScraper
from scrapers.nsdl import NSDLScraper
from scrapers.pfrda import (
    PFRDACircularsActiveScraper,
    PFRDACircularsInoperativeScraper,
    PFRDAGuidelinesScraper,
    PFRDAMasterCircularsActiveScraper,
    PFRDANotificationsScraper,
    PFRDAPressReleasesScraper,
    PFRDARecentUpdatesScraper,
    PFRDARegulationsScraper,
    PFRDATendersScraper,
)
from scrapers.rbi import (
    RBIActsScraper,
    RBIAmendmentDirectionsScraper,
    RBICircularIndexScraper,
    RBIDraftDirectionsREWiseScraper,
    RBIDraftNotificationsGuidelinesScraper,
    RBIMasterCircularsScraper,
    RBIMasterDirectionsScraper,
    RBINotificationsScraper,
    RBIPressReleasesScraper,
    RBIRegulationsScraper,
    RBIRulesScraper,
    RBISchemesScraper,
    RBIStandaloneCircularsScraper,
    RBIWithdrawnCircularsScraper,
)
from scrapers.base import raise_csv_field_size_limit
from scrapers.sebi import SEBI_LISTING_TYPES, SEBIScraper


SCRAPER_MAP = {
    "amfi-important-updates": AMFIImportantUpdatesScraper,
    "amfi-mfd-circulars": AMFIMFDCircularsScraper,
    "nism-circulars": NISMScraper,
    "nsekra-circulars": NSEKRAScraper,
    "apmi-documents": APMIDocumentsScraper,
    "apmi-circulars": APMICircularsScraper,
    "apmi-sebi-resources": APMISEBIResourcesScraper,
    "apmi-compliance-sutra": APMIComplianceSutraScraper,
    "sebi": SEBIScraper,
    "nse": NSEScraper,
    "nse-press-releases": NSEPressReleasesScraper,
    "bse": BSEScraper,
    "mcx": MCXScraper,
    "ncdex": NCDEXScraper,
    "nerl": NERLScraper,
    "cdsl": CDSLScraper,
    "ccrl": CCRLScraper,
    "nsdl": NSDLScraper,
    "ifsca": IFSCAScraper,
    "irdai-whats-new": IRDAIScraper,
    "incometax-circulars": IncomeTaxCircularsScraper,
    "incometax-notifications": IncomeTaxNotificationsScraper,
    "pfrda-recent-updates": PFRDARecentUpdatesScraper,
    "pfrda-circulars-active": PFRDACircularsActiveScraper,
    "pfrda-circulars-inoperative": PFRDACircularsInoperativeScraper,
    "pfrda-master-circulars-active": PFRDAMasterCircularsActiveScraper,
    "pfrda-notifications": PFRDANotificationsScraper,
    "pfrda-regulations": PFRDARegulationsScraper,
    "pfrda-guidelines": PFRDAGuidelinesScraper,
    "pfrda-press-releases": PFRDAPressReleasesScraper,
    "pfrda-tenders": PFRDATendersScraper,
    "rbi-notifications": RBINotificationsScraper,
    "rbi-press-releases": RBIPressReleasesScraper,
    "rbi-master-directions": RBIMasterDirectionsScraper,
    "rbi-master-circulars": RBIMasterCircularsScraper,
    "rbi-circular-index": RBICircularIndexScraper,
    "rbi-standalone-circulars": RBIStandaloneCircularsScraper,
    "rbi-withdrawn-circulars": RBIWithdrawnCircularsScraper,
    "rbi-amendment-directions": RBIAmendmentDirectionsScraper,
    "rbi-acts": RBIActsScraper,
    "rbi-rules": RBIRulesScraper,
    "rbi-regulations": RBIRegulationsScraper,
    "rbi-schemes": RBISchemesScraper,
    "rbi-draft-notifications-guidelines": RBIDraftNotificationsGuidelinesScraper,
    "rbi-draft-directions-re-wise": RBIDraftDirectionsREWiseScraper,
}

RBI_REBUILD_SOURCE_IDS = [
    "rbi-notifications",
    "rbi-press-releases",
    "rbi-master-directions",
    "rbi-master-circulars",
    "rbi-circular-index",
    "rbi-standalone-circulars",
    "rbi-withdrawn-circulars",
    "rbi-amendment-directions",
    "rbi-acts",
    "rbi-rules",
    "rbi-regulations",
    "rbi-schemes",
    "rbi-draft-notifications-guidelines",
    "rbi-draft-directions-re-wise",
]


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message} | {extra}",
    )


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def load_source_configs(path: str | Path = "config/sources.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as file_obj:
        payload = yaml.safe_load(file_obj) or {}
    return payload.get("sources", {})


def iter_selected_sources(source_arg: str, configs: dict) -> Iterable[tuple[str, dict]]:
    if source_arg == "all":
        yield from configs.items()
        return
    if source_arg not in configs:
        raise KeyError(f"Unknown source: {source_arg}")
    yield source_arg, configs[source_arg]


def archive_existing_rbi_outputs(data_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = data_dir / "archive" / f"rbi_rebuild_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(data_dir.glob("rbi_*")):
        if path.name == "rbi_documents_all.csv":
            shutil.move(str(path), str(archive_dir / path.name))
            continue
        if path.suffix.lower() in {".csv", ".json"}:
            shutil.move(str(path), str(archive_dir / path.name))
            continue
        if path.name.endswith(".meta.json"):
            shutil.move(str(path), str(archive_dir / path.name))
    return archive_dir


def unified_rbi_headers() -> list[str]:
    return [
        "source_id",
        "document_class",
        "title_or_subject",
        "circular_number",
        "serial_number",
        "date",
        "department",
        "meant_for",
        "withdrawal_category",
        "regulated_entity",
        "archive_year",
        "archive_month",
        "detail_url",
        "canonical_url",
        "source_url",
        "occurrence_count",
        "occurrence_history",
        "scraped_at",
    ]


def load_csv_rows(file_path: Path) -> list[dict[str, str]]:
    raise_csv_field_size_limit()
    with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def build_rbi_unified_export(data_dir: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    output_path = data_dir / "rbi_documents_all.csv"
    md_occurrence_path = data_dir / "rbi_master_directions_occurrences.csv"
    occurrence_map: dict[str, list[dict[str, str]]] = {}
    if md_occurrence_path.exists():
        for row in load_csv_rows(md_occurrence_path):
            canonical = (row.get("canonical_url") or "").strip().lower()
            if not canonical:
                continue
            occurrence_map.setdefault(canonical, []).append(row)

    source_files = {source_id: data_dir / f"{source_id.replace('-', '_')}_archive.csv" for source_id in RBI_REBUILD_SOURCE_IDS}
    rows: list[dict[str, str]] = []
    source_audit: dict[str, dict[str, object]] = {}
    seen_keys: set[tuple[str, ...]] = set()

    for source_id, file_path in source_files.items():
        if not file_path.exists():
            raise RuntimeError(f"Missing RBI source export for unified build: {file_path}")
        source_rows = load_csv_rows(file_path)
        year_counts: dict[str, int] = {}
        dates: list[str] = []
        missing_dates = 0
        duplicate_keys = 0
        non_rbi_urls = 0
        for row in source_rows:
            date_value = (row.get("date") or "").strip()
            if date_value:
                year_counts[date_value[:4]] = year_counts.get(date_value[:4], 0) + 1
                dates.append(date_value)
            else:
                missing_dates += 1
            url_candidate = (row.get("canonical_url") or row.get("detail_url") or row.get("link") or "").lower()
            if url_candidate and "rbi.org.in" not in url_candidate and "rbidocs.rbi.org.in" not in url_candidate:
                non_rbi_urls += 1

            canonical_url = (row.get("canonical_url") or row.get("detail_url") or row.get("link") or "").strip()
            occurrence_rows = occurrence_map.get(canonical_url.lower(), [])
            if occurrence_rows:
                history_payload = json.dumps(occurrence_rows, ensure_ascii=False)
                archive_year = ",".join(sorted({item.get("archive_year", "") for item in occurrence_rows if item.get("archive_year")}))
                archive_month = ",".join(sorted({item.get("archive_month", "") for item in occurrence_rows if item.get("archive_month")}))
            else:
                history_payload = ""
                archive_year = (row.get("archive_year") or "").strip()
                archive_month = (row.get("archive_month") or "").strip()

            unified_row = {
                "source_id": source_id,
                "document_class": source_id.replace("rbi-", "").replace("-", " "),
                "title_or_subject": (row.get("subject") or row.get("title") or row.get("title_or_subject") or "").strip(),
                "circular_number": (row.get("circular_no") or row.get("circular_number") or "").strip(),
                "serial_number": (row.get("serial_number") or "").strip(),
                "date": date_value,
                "department": (row.get("department") or "").strip(),
                "meant_for": (row.get("meant_for") or "").strip(),
                "withdrawal_category": (row.get("withdrawal_category") or "").strip(),
                "regulated_entity": (row.get("regulated_entity") or "").strip(),
                "archive_year": archive_year,
                "archive_month": archive_month,
                "detail_url": (row.get("detail_url") or row.get("link") or "").strip(),
                "canonical_url": canonical_url,
                "source_url": (row.get("source_url") or "").strip(),
                "occurrence_count": str(len(occurrence_rows) if occurrence_rows else 1),
                "occurrence_history": history_payload,
                "scraped_at": (row.get("scraped_at") or "").strip(),
            }
            dedupe_key = (
                source_id,
                (unified_row["canonical_url"] or unified_row["detail_url"] or unified_row["title_or_subject"]).lower(),
                unified_row["archive_year"].lower(),
                unified_row["archive_month"].lower(),
                unified_row["withdrawal_category"].lower(),
                unified_row["regulated_entity"].lower(),
            )
            if dedupe_key in seen_keys:
                duplicate_keys += 1
                continue
            seen_keys.add(dedupe_key)
            rows.append(unified_row)

        source_audit[source_id] = {
            "row_count": len(source_rows),
            "year_counts": {key: year_counts[key] for key in sorted(year_counts)},
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
            "missing_dates": missing_dates,
            "duplicate_keys_skipped": duplicate_keys,
            "non_rbi_urls": non_rbi_urls,
        }

    with open(output_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=unified_rbi_headers())
        writer.writeheader()
        writer.writerows(rows)
    return output_path, source_audit


def validate_rbi_unified_export(file_path: Path) -> dict[str, object]:
    rows = load_csv_rows(file_path)
    headers_ok = list(rows[0].keys()) == unified_rbi_headers() if rows else False
    duplicate_count = 0
    missing_dates = 0
    non_rbi_urls = 0
    year_counts: dict[str, int] = {}
    dates: list[str] = []
    seen_keys: set[tuple[str, ...]] = set()
    for row in rows:
        date_value = (row.get("date") or "").strip()
        if date_value:
            dates.append(date_value)
            year_counts[date_value[:4]] = year_counts.get(date_value[:4], 0) + 1
        else:
            missing_dates += 1
        url_candidate = (row.get("canonical_url") or row.get("detail_url") or "").lower()
        if url_candidate and "rbi.org.in" not in url_candidate and "rbidocs.rbi.org.in" not in url_candidate:
            non_rbi_urls += 1
        dedupe_key = (
            (row.get("source_id") or "").lower(),
            (row.get("canonical_url") or row.get("detail_url") or row.get("title_or_subject") or "").lower(),
            (row.get("archive_year") or "").lower(),
            (row.get("archive_month") or "").lower(),
            (row.get("withdrawal_category") or "").lower(),
            (row.get("regulated_entity") or "").lower(),
        )
        if dedupe_key in seen_keys:
            duplicate_count += 1
        else:
            seen_keys.add(dedupe_key)
    report = {
        "file": str(file_path),
        "headers_ok": headers_ok,
        "total_rows": len(rows),
        "rows_per_year": {key: year_counts[key] for key in sorted(year_counts)},
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "missing_dates": missing_dates,
        "duplicate_keys": duplicate_count,
        "non_rbi_urls": non_rbi_urls,
        "quality_gate_passed": headers_ok and duplicate_count == 0,
    }
    report_path = file_path.with_name("rbi_documents_all_validation_report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["quality_gate_passed"]:
        raise RuntimeError("Unified RBI export failed validation")
    return report


def run_rebuild_rbi_official(use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    data_dir = Path("data")
    archive_dir = archive_existing_rbi_outputs(data_dir)
    print(f"Archived previous RBI outputs to: {archive_dir}")
    for source_id in RBI_REBUILD_SOURCE_IDS:
        config = configs[source_id]
        scraper_class = SCRAPER_MAP[source_id]
        out_path = data_dir / f"{source_id.replace('-', '_')}_archive.csv"
        checkpoint_path = data_dir / f"{source_id.replace('-', '_')}_checkpoint.json"
        listing_url = config["listing_url"]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.scrape_listing_url(
                url=listing_url,
                out_path=out_path,
                resume=False,
                checkpoint_path=checkpoint_path,
                max_chunks_this_run=None,
                delay_seconds=0.1,
                retries=5,
                retry_base_delay=3.0,
                retry_max_delay=60.0,
                all_available=True,
            )
            scraper.validate_export(out_path)
    unified_path, source_audit = build_rbi_unified_export(data_dir)
    unified_report = validate_rbi_unified_export(unified_path)
    print(json.dumps({"source_audit": source_audit, "unified_report": unified_report}, ensure_ascii=False, indent=2))
    return 0


def run_backfill(
    source_arg: str,
    from_date: date,
    to_date: date,
    use_playwright_fallback: bool,
    limit: int | None = None,
) -> int:
    configs = load_source_configs()
    exit_code = 0
    for source_name, config in iter_selected_sources(source_arg, configs):
        scraper_class = SCRAPER_MAP[source_name]
        try:
            with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
                scraper.run_backfill(from_date=from_date, to_date=to_date, limit=limit)
        except NotImplementedError as exc:
            exit_code = 1
            logger.bind(source=source_name, error=str(exc)).warning("Source configuration incomplete")
    return exit_code


def run_incremental(source_arg: str, days_back: int, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    exit_code = 0
    for source_name, config in iter_selected_sources(source_arg, configs):
        scraper_class = SCRAPER_MAP[source_name]
        try:
            with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
                scraper.run_incremental(days_back=days_back)
        except NotImplementedError as exc:
            exit_code = 1
            logger.bind(source=source_name, error=str(exc)).warning("Source configuration incomplete")
    return exit_code


def run_inspect(source_arg: str, listing_type: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "sebi":
        raise NotImplementedError("Inspect is currently implemented only for SEBI")

    config = configs[source_arg]
    with SEBIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect(listing_type)
    return 0


def run_inspect_url(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "sebi":
        raise NotImplementedError("inspect-url is currently implemented only for SEBI")

    config = configs[source_arg]
    with SEBIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_listing_url(url, "tests/fixtures/sebi/doListingAll_listing.html")
    return 0


def run_inspect_pagination(source_arg: str, url: str, use_playwright_fallback: bool, headless: bool) -> int:
    configs = load_source_configs()
    if source_arg != "sebi":
        raise NotImplementedError("inspect-pagination is currently implemented only for SEBI")

    config = configs[source_arg]
    with SEBIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_pagination(
            url=url,
            fixture_path="tests/fixtures/sebi/doListingAll_page_1.html",
            network_capture_path="tests/fixtures/sebi/pagination_network_capture.json",
            headless=headless,
        )
    return 0


def run_inspect_nse_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nse":
        raise NotImplementedError("inspect-nse-circulars is currently implemented only for NSE")

    config = configs[source_arg]
    with NSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_bse_notices(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "bse":
        raise NotImplementedError("inspect-bse-notices is currently implemented only for BSE")

    config = configs[source_arg]
    with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_notices(url)
    return 0


def run_inspect_nse_press_releases(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nse-press-releases":
        raise NotImplementedError("inspect-nse-press-releases is currently implemented only for NSE press releases")

    config = configs[source_arg]
    with NSEPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_press_releases(url)
    return 0


def run_inspect_nse_press_release_archives(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nse-press-releases":
        raise NotImplementedError(
            "inspect-nse-press-release-archives is currently implemented only for NSE press releases"
        )

    config = configs[source_arg]
    with NSEPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_press_release_archives(url)
    return 0


def run_inspect_bse_browser_archive(source_arg: str, url: str, use_playwright_fallback: bool, headless: bool) -> int:
    configs = load_source_configs()
    if source_arg != "bse":
        raise NotImplementedError("inspect-bse-browser-archive is currently implemented only for BSE")

    config = configs[source_arg]
    with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_browser_archive(url, headless=headless)
    return 0


def run_inspect_mcx_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "mcx":
        raise NotImplementedError("inspect-mcx-circulars is currently implemented only for MCX")

    config = configs[source_arg]
    with MCXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_cdsl_communiques(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "cdsl":
        raise NotImplementedError("inspect-cdsl-communiques is currently implemented only for CDSL")

    config = configs[source_arg]
    with CDSLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_communiques(url)
    return 0


def run_inspect_ccrl_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ccrl":
        raise NotImplementedError("inspect-ccrl-circulars is currently implemented only for CCRL")

    config = configs[source_arg]
    with CCRLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_cdsl_communique_index(url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    config = configs["cdsl"]
    with CDSLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_communique_index(url)
    return 0


def run_inspect_ncdex_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ncdex":
        raise NotImplementedError("inspect-ncdex-circulars is currently implemented only for NCDEX")

    config = configs[source_arg]
    with NCDEXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_nerl_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nerl":
        raise NotImplementedError("inspect-nerl-circulars is currently implemented only for NERL")

    config = configs[source_arg]
    with NERLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_nsdl_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nsdl":
        raise NotImplementedError("inspect-nsdl-circulars is currently implemented only for NSDL")

    config = configs[source_arg]
    with NSDLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_circulars(url)
    return 0


def run_inspect_ifsca_new_section(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ifsca":
        raise NotImplementedError("inspect-ifsca-new-section is currently implemented only for IFSCA")

    config = configs[source_arg]
    with IFSCAScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_new_section(url)
    return 0


def run_inspect_irdai_whats_new(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "irdai-whats-new":
        raise NotImplementedError("inspect-irdai-whats-new is currently implemented only for IRDAI Whats New")

    config = configs[source_arg]
    with IRDAIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_whats_new(url)
    return 0


def run_inspect_irdai_whats_new_filters(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "irdai-whats-new":
        raise NotImplementedError(
            "inspect-irdai-whats-new-filters is currently implemented only for IRDAI Whats New"
        )

    config = configs[source_arg]
    with IRDAIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_whats_new_filters(url)
    return 0


def run_scout_incometax(url: str, use_playwright_fallback: bool) -> int:
    del url
    configs = load_source_configs()
    config = configs["incometax-circulars"]
    with IncomeTaxCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.scout_site("https://www.incometaxindia.gov.in/")
    return 0


def run_inspect_incometax_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "incometax-circulars":
        raise NotImplementedError("inspect-incometax-circulars is currently implemented only for Income Tax circulars")
    config = configs[source_arg]
    with IncomeTaxCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_listing(url)
    return 0


def run_inspect_incometax_notifications(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "incometax-notifications":
        raise NotImplementedError("inspect-incometax-notifications is currently implemented only for Income Tax notifications")
    config = configs[source_arg]
    with IncomeTaxNotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_listing(url)
    return 0


def run_discover_incometax_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "incometax-circulars":
        raise NotImplementedError("discover-incometax-circular-range is currently implemented only for Income Tax circulars")
    config = configs[source_arg]
    with IncomeTaxCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_range(url)
    return 0


def run_discover_incometax_notification_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "incometax-notifications":
        raise NotImplementedError("discover-incometax-notification-range is currently implemented only for Income Tax notifications")
    config = configs[source_arg]
    with IncomeTaxNotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_range(url)
    return 0


def run_scout_rbi(url: str, use_playwright_fallback: bool) -> int:
    del url
    configs = load_source_configs()
    config = configs["rbi-notifications"]
    with RBINotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.scout_site("https://www.rbi.org.in/home.aspx")
    return 0


def run_inspect_rbi_notifications(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-notifications":
        raise NotImplementedError("inspect-rbi-notifications is currently implemented only for RBI notifications")
    config = configs[source_arg]
    with RBINotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_notifications(url)
    return 0


def run_inspect_rbi_press_releases(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-press-releases":
        raise NotImplementedError("inspect-rbi-press-releases is currently implemented only for RBI press releases")
    config = configs[source_arg]
    with RBIPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_press_releases(url)
    return 0


def run_inspect_rbi_master_directions(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-master-directions":
        raise NotImplementedError("inspect-rbi-master-directions is currently implemented only for RBI master directions")
    config = configs[source_arg]
    with RBIMasterDirectionsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_master_directions(url)
    return 0


def run_inspect_rbi_master_circulars(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-master-circulars":
        raise NotImplementedError("inspect-rbi-master-circulars is currently implemented only for RBI master circulars")
    config = configs[source_arg]
    with RBIMasterCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_master_circulars(url)
    return 0


def run_inspect_rbi_speeches(url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    config = configs["rbi-press-releases"]
    with RBIPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_speeches(url)
    return 0


def run_inspect_rbi_faqs(url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    config = configs["rbi-master-directions"]
    with RBIMasterDirectionsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_faqs(url)
    return 0


def run_discover_rbi_notification_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-notifications":
        raise NotImplementedError("discover-rbi-notification-range is currently implemented only for RBI notifications")
    config = configs[source_arg]
    with RBINotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_notification_range(url)
    return 0


def run_discover_rbi_press_release_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-press-releases":
        raise NotImplementedError("discover-rbi-press-release-range is currently implemented only for RBI press releases")
    config = configs[source_arg]
    with RBIPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_press_release_range(url)
    return 0


def run_discover_rbi_master_direction_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-master-directions":
        raise NotImplementedError("discover-rbi-master-direction-range is currently implemented only for RBI master directions")
    config = configs[source_arg]
    with RBIMasterDirectionsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_master_direction_range(url)
    return 0


def run_discover_rbi_master_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "rbi-master-circulars":
        raise NotImplementedError("discover-rbi-master-circular-range is currently implemented only for RBI master circulars")
    config = configs[source_arg]
    with RBIMasterCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_master_circular_range(url)
    return 0


PFRDA_SCRAPER_CLASS_MAP = {
    "pfrda-recent-updates": PFRDARecentUpdatesScraper,
    "pfrda-circulars-active": PFRDACircularsActiveScraper,
    "pfrda-circulars-inoperative": PFRDACircularsInoperativeScraper,
    "pfrda-master-circulars-active": PFRDAMasterCircularsActiveScraper,
    "pfrda-notifications": PFRDANotificationsScraper,
    "pfrda-regulations": PFRDARegulationsScraper,
    "pfrda-guidelines": PFRDAGuidelinesScraper,
    "pfrda-press-releases": PFRDAPressReleasesScraper,
    "pfrda-tenders": PFRDATendersScraper,
}

AMFI_SCRAPER_CLASS_MAP = {
    "amfi-important-updates": AMFIImportantUpdatesScraper,
    "amfi-mfd-circulars": AMFIMFDCircularsScraper,
}

NISM_SCRAPER_CLASS_MAP = {
    "nism-circulars": NISMScraper,
}

NSEKRA_SCRAPER_CLASS_MAP = {
    "nsekra-circulars": NSEKRAScraper,
}

APMI_SCRAPER_CLASS_MAP = {
    "apmi-documents": APMIDocumentsScraper,
    "apmi-circulars": APMICircularsScraper,
    "apmi-sebi-resources": APMISEBIResourcesScraper,
    "apmi-compliance-sutra": APMIComplianceSutraScraper,
}


def run_scout_amfi(url: str, use_playwright_fallback: bool) -> int:
    del url
    configs = load_source_configs()
    config = configs["amfi-important-updates"]
    with AMFIImportantUpdatesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.scout_site("https://www.amfiindia.com/important-updates")
    return 0


def run_inspect_amfi(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = AMFI_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_discover_amfi(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = AMFI_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_inspect_nism(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = NISM_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_discover_nism(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = NISM_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_inspect_nsekra(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = NSEKRA_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_discover_nsekra(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = NSEKRA_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_inspect_apmi(url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    config = configs["apmi-documents"]
    with APMIDocumentsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.inspect_site(url)
    return 0


def run_scout_pfrda(url: str, use_playwright_fallback: bool) -> int:
    del url
    configs = load_source_configs()
    config = configs["pfrda-recent-updates"]
    with PFRDARecentUpdatesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.scout_site("https://www.pfrda.org.in/web/pfrda/recent-updates")
    return 0


def run_inspect_pfrda(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = PFRDA_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_discover_pfrda(source_arg: str, url: str, use_playwright_fallback: bool, method_name: str) -> int:
    configs = load_source_configs()
    scraper_class = PFRDA_SCRAPER_CLASS_MAP[source_arg]
    config = configs[source_arg]
    with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        getattr(scraper, method_name)(url)
    return 0


def run_discover_nse_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nse":
        raise NotImplementedError("discover-nse-circular-range is currently implemented only for NSE")

    config = configs[source_arg]
    with NSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_bse_notice_range(source_arg: str, url: str, use_playwright_fallback: bool, headless: bool) -> int:
    configs = load_source_configs()
    if source_arg != "bse":
        raise NotImplementedError("discover-bse-notice-range is currently implemented only for BSE")

    config = configs[source_arg]
    with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_notice_range(url, headless=headless)
    return 0


def run_discover_nse_press_release_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nse-press-releases":
        raise NotImplementedError(
            "discover-nse-press-release-range is currently implemented only for NSE press releases"
        )

    config = configs[source_arg]
    with NSEPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_press_release_range(url)
    return 0


def run_discover_mcx_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "mcx":
        raise NotImplementedError("discover-mcx-circular-range is currently implemented only for MCX")

    config = configs[source_arg]
    with MCXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_cdsl_communique_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "cdsl":
        raise NotImplementedError("discover-cdsl-communique-range is currently implemented only for CDSL")

    config = configs[source_arg]
    with CDSLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_communique_range(url)
    return 0


def run_discover_ccrl_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ccrl":
        raise NotImplementedError("discover-ccrl-circular-range is currently implemented only for CCRL")

    config = configs[source_arg]
    with CCRLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_ncdex_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ncdex":
        raise NotImplementedError("discover-ncdex-circular-range is currently implemented only for NCDEX")

    config = configs[source_arg]
    with NCDEXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_nerl_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nerl":
        raise NotImplementedError("discover-nerl-circular-range is currently implemented only for NERL")

    config = configs[source_arg]
    with NERLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_nsdl_circular_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "nsdl":
        raise NotImplementedError("discover-nsdl-circular-range is currently implemented only for NSDL")

    config = configs[source_arg]
    with NSDLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_circular_range(url)
    return 0


def run_discover_ifsca_new_section_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "ifsca":
        raise NotImplementedError("discover-ifsca-new-section-range is currently implemented only for IFSCA")

    config = configs[source_arg]
    with IFSCAScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_new_section_range(url)
    return 0


def run_discover_irdai_whats_new_range(source_arg: str, url: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "irdai-whats-new":
        raise NotImplementedError(
            "discover-irdai-whats-new-range is currently implemented only for IRDAI Whats New"
        )

    config = configs[source_arg]
    with IRDAIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.discover_whats_new_range(url)
    return 0


def run_recover_bse_old_notice_dates(
    input_path: str,
    out_path: str,
    unresolved_out_path: str,
    use_playwright_fallback: bool,
    *,
    delay_seconds: float,
    retries: int,
    retry_base_delay: float,
    retry_max_delay: float,
    headless: bool,
) -> int:
    configs = load_source_configs()
    config = configs["bse"]
    with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.recover_old_notice_dates(
            input_path=input_path,
            out_path=out_path,
            unresolved_out_path=unresolved_out_path,
            delay_seconds=delay_seconds,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            headless=headless,
        )
    return 0


def run_merge_export(source_arg: str, main_path: str, add_path: str, out_path: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    if source_arg != "bse":
        raise NotImplementedError("merge-export is currently implemented only for BSE")
    config = configs[source_arg]
    with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.merge_export(main_path=main_path, add_path=add_path, out_path=out_path)
    return 0


def run_validate_export(source_arg: str, file_path: str, use_playwright_fallback: bool) -> int:
    configs = load_source_configs()
    config = configs[source_arg]
    if source_arg == "nse":
        with NSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "nse-press-releases":
        with NSEPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "bse":
        with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "mcx":
        with MCXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "ncdex":
        with NCDEXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "nerl":
        with NERLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "cdsl":
        with CDSLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "ccrl":
        with CCRLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "nsdl":
        with NSDLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "ifsca":
        with IFSCAScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "irdai-whats-new":
        with IRDAIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "incometax-circulars":
        with IncomeTaxCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg == "incometax-notifications":
        with IncomeTaxNotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in AMFI_SCRAPER_CLASS_MAP:
        scraper_class = AMFI_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in NISM_SCRAPER_CLASS_MAP:
        scraper_class = NISM_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in NSEKRA_SCRAPER_CLASS_MAP:
        scraper_class = NSEKRA_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in PFRDA_SCRAPER_CLASS_MAP:
        scraper_class = PFRDA_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in APMI_SCRAPER_CLASS_MAP:
        scraper_class = APMI_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    if source_arg in RBI_REBUILD_SOURCE_IDS:
        scraper_class = SCRAPER_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.validate_export(file_path)
        return 0
    raise NotImplementedError(
        "validate-export is currently implemented only for AMFI, NISM, NSE KRA, APMI, NSE, NSE press releases, BSE, MCX, NCDEX, NERL, CDSL, CCRL, NSDL, IFSCA, IRDAI Whats New, Income Tax circulars/notifications, PFRDA sources, and RBI sources"
    )


def run_check_links(
    source_arg: str,
    file_path: str,
    out_path: str,
    use_playwright_fallback: bool,
    *,
    delay_seconds: float,
    retries: int,
    retry_base_delay: float,
    retry_max_delay: float,
) -> int:
    configs = load_source_configs()
    if source_arg == "apmi-documents":
        config = configs[source_arg]
        with APMIDocumentsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            scraper.check_links(
                file_path=file_path,
                out_path=out_path,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
        return 0
    if source_arg != "nsdl":
        raise NotImplementedError("check-links is currently implemented only for NSDL and APMI documents")

    config = configs[source_arg]
    with NSDLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
        scraper.check_links(
            file_path=file_path,
            out_path=out_path,
            delay_seconds=delay_seconds,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
    return 0


def run_scrape_url(
    source_arg: str,
    url: str,
    out_path: str,
    use_playwright_fallback: bool,
    *,
    limit: int | None,
    from_date: date | None,
    to_date: date | None,
    doc_type: str | None,
    pages: int | None,
    all_pages: bool,
    store_db: bool,
    resume: bool,
    checkpoint: str | None,
    start_page: int | None,
    end_page: int | None,
    max_pages_this_run: int | None,
    delay_seconds: float,
    max_errors: int,
    retries: int,
    retry_base_delay: float,
    retry_max_delay: float,
    allow_partial: bool,
    headless: bool,
    department: str | None,
    category: str | None,
    all_available: bool,
    older_unresolved: bool,
    include_type: bool,
    include_department: bool,
    include_downloads: bool,
) -> int:
    configs = load_source_configs()
    config = configs[source_arg]
    if source_arg == "sebi":
        with SEBIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                limit=limit,
                from_date=from_date,
                to_date=to_date,
                doc_type=doc_type,
                pages=pages,
                all_pages=all_pages,
                store_db=store_db,
                resume=resume,
                checkpoint_path=checkpoint,
                start_page=start_page,
                end_page=end_page,
                max_pages_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                max_errors=max_errors,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                allow_partial=allow_partial,
                headless=headless,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "nse":
        with NSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                department=department,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "nse-press-releases":
        with NSEPressReleasesScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "bse":
        with BSEScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            if older_unresolved:
                records = scraper.scrape_older_unresolved(
                    url=url,
                    out_path=out_path,
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                    headless=headless,
                )
            else:
                records = scraper.scrape_listing_url(
                    url=url,
                    out_path=out_path,
                    from_date=from_date,
                    to_date=to_date,
                    resume=resume,
                    checkpoint_path=checkpoint,
                    max_chunks_this_run=max_pages_this_run,
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                    all_available=all_available,
                    allow_partial=allow_partial,
                    headless=headless,
                )
            transport = scraper.last_fetch_transport
    elif source_arg == "mcx":
        with MCXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                category=category,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "ncdex":
        with NCDEXScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                department=department,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
                headless=headless,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "nerl":
        with NERLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_department=include_department,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "cdsl":
        with CDSLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "ccrl":
        with CCRLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_department=include_department,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "nsdl":
        with NSDLScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "ifsca":
        with IFSCAScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                type_filter=doc_type,
                include_type=include_type,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "irdai-whats-new":
        with IRDAIScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                type_filter=doc_type,
                include_type=include_type,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "incometax-circulars":
        with IncomeTaxCircularsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg == "incometax-notifications":
        with IncomeTaxNotificationsScraper(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg in PFRDA_SCRAPER_CLASS_MAP:
        scraper_class = PFRDA_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_category=include_type,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg in AMFI_SCRAPER_CLASS_MAP:
        scraper_class = AMFI_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_category=include_type,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg in NISM_SCRAPER_CLASS_MAP:
        scraper_class = NISM_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_downloads=include_downloads,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg in NSEKRA_SCRAPER_CLASS_MAP:
        scraper_class = NSEKRA_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    elif source_arg in APMI_SCRAPER_CLASS_MAP:
        scraper_class = APMI_SCRAPER_CLASS_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_category=include_type,
                all_available=all_available,
                delay_seconds=delay_seconds,
            )
            transport = "httpx"
    elif source_arg in RBI_REBUILD_SOURCE_IDS:
        scraper_class = SCRAPER_MAP[source_arg]
        with scraper_class(config=config, use_playwright_fallback=use_playwright_fallback) as scraper:
            records = scraper.scrape_listing_url(
                url=url,
                out_path=out_path,
                from_date=from_date,
                to_date=to_date,
                include_category=include_type,
                resume=resume,
                checkpoint_path=checkpoint,
                max_chunks_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                all_available=all_available,
            )
            transport = scraper.last_fetch_transport
    else:
        raise NotImplementedError(
            "scrape-url is currently implemented only for SEBI, NSE, NSE press releases, NSE KRA, BSE, MCX, NCDEX, NERL, CDSL, CCRL, NSDL, IFSCA, IRDAI Whats New, Income Tax circulars/notifications, PFRDA sources, and RBI sources"
        )
    print(f"Wrote {len(records)} records to {out_path}")
    print(f"Fetch transport: {transport}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regulatory circular scraper framework")
    parser.add_argument("--use-playwright-fallback", action="store_true", help="Enable optional Playwright fallback")

    subparsers = parser.add_subparsers(dest="command", required=True)

    backfill = subparsers.add_parser("backfill", help="Run a historical backfill")
    backfill.add_argument("--source", required=True, choices=["all", *SCRAPER_MAP.keys()])
    backfill.add_argument("--from", dest="from_date", required=True, type=parse_date)
    backfill.add_argument("--to", dest="to_date", required=True, type=parse_date)
    backfill.add_argument("--limit", type=int, default=None)

    incremental = subparsers.add_parser("incremental", help="Run an incremental scrape")
    incremental.add_argument("--source", required=True, choices=["all", *SCRAPER_MAP.keys()])
    incremental.add_argument("--days-back", type=int, default=7)

    subparsers.add_parser("rebuild-rbi-official", help="Archive old RBI outputs, rebuild all official RBI sources, and validate the unified export")

    inspect = subparsers.add_parser("inspect", help="Inspect a live source listing and capture fixtures")
    inspect.add_argument("--source", required=True, choices=["sebi"])
    inspect.add_argument("--type", dest="listing_type", required=True, choices=sorted(SEBI_LISTING_TYPES))

    inspect_url = subparsers.add_parser("inspect-url", help="Inspect a live listing URL and capture its HTML fixture")
    inspect_url.add_argument("--source", required=True, choices=["sebi"])
    inspect_url.add_argument("--url", required=True)

    inspect_pagination = subparsers.add_parser("inspect-pagination", help="Inspect SEBI pagination and capture request details")
    inspect_pagination.add_argument("--source", required=True, choices=["sebi"])
    inspect_pagination.add_argument("--url", required=True)
    inspect_pagination.add_argument("--headless", type=lambda v: str(v).lower() != "false", default=True)

    inspect_nse = subparsers.add_parser("inspect-nse-circulars", help="Inspect NSE circular page and API")
    inspect_nse.add_argument("--url", required=True)

    inspect_nse_press = subparsers.add_parser("inspect-nse-press-releases", help="Inspect NSE press releases page and API")
    inspect_nse_press.add_argument("--url", required=True)

    inspect_nse_press_archive = subparsers.add_parser(
        "inspect-nse-press-release-archives",
        help="Inspect NSE press release archive route and older visible records",
    )
    inspect_nse_press_archive.add_argument("--url", required=True)

    inspect_bse = subparsers.add_parser("inspect-bse-notices", help="Inspect BSE notices/circulars page and related public flows")
    inspect_bse.add_argument("--url", required=True)

    inspect_bse_browser = subparsers.add_parser("inspect-bse-browser-archive", help="Inspect BSE archive flow with a real browser")
    inspect_bse_browser.add_argument("--url", required=True)
    inspect_bse_browser.add_argument("--headless", type=lambda v: str(v).lower() != "false", default=True)

    inspect_mcx = subparsers.add_parser("inspect-mcx-circulars", help="Inspect MCX circular page and discovered endpoints")
    inspect_mcx.add_argument("--url", required=True)

    inspect_cdsl = subparsers.add_parser("inspect-cdsl-communiques", help="Inspect CDSL communique page and discovered endpoints")
    inspect_cdsl.add_argument("--url", required=True)

    inspect_ccrl = subparsers.add_parser("inspect-ccrl-circulars", help="Inspect CCRL circular page and archive shape")
    inspect_ccrl.add_argument("--url", required=True)

    inspect_cdsl_index = subparsers.add_parser("inspect-cdsl-communique-index", help="Inspect the public CDSL DP communique index")
    inspect_cdsl_index.add_argument("--url", required=True)

    inspect_ncdex = subparsers.add_parser("inspect-ncdex-circulars", help="Inspect NCDEX circular page and discovered endpoints")
    inspect_ncdex.add_argument("--url", required=True)

    inspect_nerl = subparsers.add_parser("inspect-nerl-circulars", help="Inspect NERL circular page and archive shape")
    inspect_nerl.add_argument("--url", required=True)

    inspect_nsdl = subparsers.add_parser("inspect-nsdl-circulars", help="Inspect NSDL circular routes and discovered archive layout")
    inspect_nsdl.add_argument("--url", required=True)

    inspect_ifsca = subparsers.add_parser("inspect-ifsca-new-section", help="Inspect IFSCA New Section listing page and archive shape")
    inspect_ifsca.add_argument("--url", required=True)

    inspect_irdai = subparsers.add_parser("inspect-irdai-whats-new", help="Inspect IRDAI Whats New listing page and archive shape")
    inspect_irdai.add_argument("--url", required=True)

    inspect_irdai_filters = subparsers.add_parser(
        "inspect-irdai-whats-new-filters",
        help="Inspect IRDAI Whats New filter and pagination flow",
    )
    inspect_irdai_filters.add_argument("--url", required=True)

    scout_incometax = subparsers.add_parser("scout-incometax", help="Scout Income Tax India official routes and discover public feeds/endpoints")
    scout_incometax.add_argument("--url", required=True)

    inspect_incometax_circulars = subparsers.add_parser("inspect-incometax-circulars", help="Inspect Income Tax circulars listing flow and APIs")
    inspect_incometax_circulars.add_argument("--url", required=True)

    inspect_incometax_notifications = subparsers.add_parser("inspect-incometax-notifications", help="Inspect Income Tax notifications listing flow and APIs")
    inspect_incometax_notifications.add_argument("--url", required=True)

    scout_rbi = subparsers.add_parser("scout-rbi", help="Scout RBI official routes and discover accessible archive flows")
    scout_rbi.add_argument("--url", required=True)

    inspect_rbi_notifications = subparsers.add_parser("inspect-rbi-notifications", help="Inspect RBI notifications listing flow and detail metadata")
    inspect_rbi_notifications.add_argument("--url", required=True)

    inspect_rbi_press_releases = subparsers.add_parser("inspect-rbi-press-releases", help="Inspect RBI press releases listing flow and archive behavior")
    inspect_rbi_press_releases.add_argument("--url", required=True)

    inspect_rbi_master_directions = subparsers.add_parser("inspect-rbi-master-directions", help="Inspect RBI Master Directions listing flow")
    inspect_rbi_master_directions.add_argument("--url", required=True)

    inspect_rbi_master_circulars = subparsers.add_parser("inspect-rbi-master-circulars", help="Inspect RBI Master Circulars listing flow")
    inspect_rbi_master_circulars.add_argument("--url", required=True)

    inspect_rbi_speeches = subparsers.add_parser("inspect-rbi-speeches", help="Inspect RBI speeches inventory page")
    inspect_rbi_speeches.add_argument("--url", required=True)

    inspect_rbi_faqs = subparsers.add_parser("inspect-rbi-faqs", help="Inspect RBI FAQs inventory page")
    inspect_rbi_faqs.add_argument("--url", required=True)

    inspect_apmi = subparsers.add_parser("inspect-apmi", help="Inspect the APMI welcome/resource page")
    inspect_apmi.add_argument("--url", required=True)

    scout_amfi = subparsers.add_parser("scout-amfi", help="Scout AMFI official routes and discover rendered listing flows")
    scout_amfi.add_argument("--url", required=True)

    inspect_amfi_important_updates = subparsers.add_parser(
        "inspect-amfi-important-updates",
        help="Inspect AMFI Important Updates listing flow",
    )
    inspect_amfi_important_updates.add_argument("--url", required=True)

    inspect_amfi_mfd_circulars = subparsers.add_parser(
        "inspect-amfi-mfd-circulars",
        help="Inspect AMFI MFD circulars listing flow",
    )
    inspect_amfi_mfd_circulars.add_argument("--url", required=True)

    inspect_nsekra_circulars = subparsers.add_parser(
        "inspect-nsekra-circulars",
        help="Inspect NSE KRA circular page, JS shell, and discovered public API flow",
    )
    inspect_nsekra_circulars.add_argument("--url", required=True)

    inspect_nism_circulars = subparsers.add_parser("inspect-nism-circulars", help="Inspect NISM circular pages and archive entry points")
    inspect_nism_circulars.add_argument("--url", required=True)

    inspect_nism_circular_archive = subparsers.add_parser("inspect-nism-circular-archive", help="Inspect NISM circular archive flow and pagination")
    inspect_nism_circular_archive.add_argument("--url", required=True)

    scout_pfrda = subparsers.add_parser("scout-pfrda", help="Scout PFRDA official routes and discover portal listing flows")
    scout_pfrda.add_argument("--url", required=True)

    inspect_pfrda_recent_updates = subparsers.add_parser("inspect-pfrda-recent-updates", help="Inspect PFRDA Recent Updates listing flow")
    inspect_pfrda_recent_updates.add_argument("--url", required=True)

    inspect_pfrda_circulars_active = subparsers.add_parser("inspect-pfrda-circulars-active", help="Inspect PFRDA active circulars listing flow")
    inspect_pfrda_circulars_active.add_argument("--url", required=True)

    inspect_pfrda_circulars_inoperative = subparsers.add_parser("inspect-pfrda-circulars-inoperative", help="Inspect PFRDA archived circulars listing flow")
    inspect_pfrda_circulars_inoperative.add_argument("--url", required=True)

    inspect_pfrda_master_circulars_active = subparsers.add_parser(
        "inspect-pfrda-master-circulars-active",
        help="Inspect PFRDA active master circulars listing flow",
    )
    inspect_pfrda_master_circulars_active.add_argument("--url", required=True)

    inspect_pfrda_notifications = subparsers.add_parser("inspect-pfrda-notifications", help="Inspect PFRDA notifications listing flow")
    inspect_pfrda_notifications.add_argument("--url", required=True)

    inspect_pfrda_regulations = subparsers.add_parser("inspect-pfrda-regulations", help="Inspect PFRDA regulations listing flow")
    inspect_pfrda_regulations.add_argument("--url", required=True)

    inspect_pfrda_guidelines = subparsers.add_parser("inspect-pfrda-guidelines", help="Inspect PFRDA guidelines listing flow")
    inspect_pfrda_guidelines.add_argument("--url", required=True)

    inspect_pfrda_press_releases = subparsers.add_parser("inspect-pfrda-press-releases", help="Inspect PFRDA press releases listing flow")
    inspect_pfrda_press_releases.add_argument("--url", required=True)

    discover_nse = subparsers.add_parser("discover-nse-circular-range", help="Discover the oldest available NSE circular date")
    discover_nse.add_argument("--url", required=True)

    discover_nse_press = subparsers.add_parser(
        "discover-nse-press-release-range",
        help="Discover the oldest available NSE press release date",
    )
    discover_nse_press.add_argument("--url", required=True)

    discover_bse = subparsers.add_parser("discover-bse-notice-range", help="Discover the oldest publicly reachable BSE notice date")
    discover_bse.add_argument("--url", required=True)
    discover_bse.add_argument("--headless", type=lambda v: str(v).lower() != "false", default=True)

    discover_mcx = subparsers.add_parser("discover-mcx-circular-range", help="Discover the oldest available MCX circular date")
    discover_mcx.add_argument("--url", required=True)

    discover_cdsl = subparsers.add_parser("discover-cdsl-communique-range", help="Discover the oldest available CDSL communique date")
    discover_cdsl.add_argument("--url", required=True)

    discover_ccrl = subparsers.add_parser("discover-ccrl-circular-range", help="Discover the oldest available CCRL circular date")
    discover_ccrl.add_argument("--url", required=True)

    discover_ncdex = subparsers.add_parser("discover-ncdex-circular-range", help="Discover the oldest available NCDEX circular date")
    discover_ncdex.add_argument("--url", required=True)

    discover_nerl = subparsers.add_parser("discover-nerl-circular-range", help="Discover the oldest available NERL circular date")
    discover_nerl.add_argument("--url", required=True)

    discover_nsdl = subparsers.add_parser("discover-nsdl-circular-range", help="Discover the oldest available NSDL circular date")
    discover_nsdl.add_argument("--url", required=True)

    discover_ifsca = subparsers.add_parser("discover-ifsca-new-section-range", help="Discover the oldest available IFSCA New Section listing date")
    discover_ifsca.add_argument("--url", required=True)

    discover_irdai = subparsers.add_parser("discover-irdai-whats-new-range", help="Discover the oldest available IRDAI Whats New listing date")
    discover_irdai.add_argument("--url", required=True)

    discover_incometax_circulars = subparsers.add_parser("discover-incometax-circular-range", help="Discover the oldest available Income Tax circular date")
    discover_incometax_circulars.add_argument("--url", required=True)

    discover_incometax_notifications = subparsers.add_parser("discover-incometax-notification-range", help="Discover the oldest available Income Tax notification date")
    discover_incometax_notifications.add_argument("--url", required=True)

    discover_rbi_notifications = subparsers.add_parser("discover-rbi-notification-range", help="Discover the oldest available RBI notification date")
    discover_rbi_notifications.add_argument("--url", required=True)

    discover_rbi_press_releases = subparsers.add_parser("discover-rbi-press-release-range", help="Discover the oldest available RBI press release date")
    discover_rbi_press_releases.add_argument("--url", required=True)

    discover_rbi_master_directions = subparsers.add_parser("discover-rbi-master-direction-range", help="Discover the oldest available RBI Master Direction date")
    discover_rbi_master_directions.add_argument("--url", required=True)

    discover_rbi_master_circulars = subparsers.add_parser("discover-rbi-master-circular-range", help="Discover the oldest available RBI Master Circular date")
    discover_rbi_master_circulars.add_argument("--url", required=True)

    discover_pfrda_recent_updates = subparsers.add_parser("discover-pfrda-recent-updates-range", help="Discover the oldest available PFRDA recent update date")
    discover_pfrda_recent_updates.add_argument("--url", required=True)

    discover_pfrda_circulars_active = subparsers.add_parser("discover-pfrda-circulars-active-range", help="Discover the oldest available PFRDA active circular date")
    discover_pfrda_circulars_active.add_argument("--url", required=True)

    discover_pfrda_circulars_inoperative = subparsers.add_parser("discover-pfrda-circulars-inoperative-range", help="Discover the oldest available PFRDA archived circular date")
    discover_pfrda_circulars_inoperative.add_argument("--url", required=True)

    discover_pfrda_master_circulars_active = subparsers.add_parser(
        "discover-pfrda-master-circulars-active-range",
        help="Discover the oldest available PFRDA active master circular date",
    )
    discover_pfrda_master_circulars_active.add_argument("--url", required=True)

    discover_pfrda_notifications = subparsers.add_parser("discover-pfrda-notifications-range", help="Discover the oldest available PFRDA notification date")
    discover_pfrda_notifications.add_argument("--url", required=True)

    discover_pfrda_regulations = subparsers.add_parser("discover-pfrda-regulations-range", help="Discover the oldest available PFRDA regulation date")
    discover_pfrda_regulations.add_argument("--url", required=True)

    discover_pfrda_guidelines = subparsers.add_parser("discover-pfrda-guidelines-range", help="Discover the oldest available PFRDA guideline date")
    discover_pfrda_guidelines.add_argument("--url", required=True)

    discover_pfrda_press_releases = subparsers.add_parser("discover-pfrda-press-releases-range", help="Discover the oldest available PFRDA press release date")
    discover_pfrda_press_releases.add_argument("--url", required=True)

    discover_amfi_important_updates = subparsers.add_parser(
        "discover-amfi-important-updates-range",
        help="Discover available AMFI Important Updates date coverage",
    )
    discover_amfi_important_updates.add_argument("--url", required=True)

    discover_amfi_mfd_circulars = subparsers.add_parser(
        "discover-amfi-mfd-circular-range",
        help="Discover the oldest available AMFI MFD circular date",
    )
    discover_amfi_mfd_circulars.add_argument("--url", required=True)

    discover_nsekra_circulars = subparsers.add_parser(
        "discover-nsekra-circular-range",
        help="Discover the oldest available NSE KRA circular date",
    )
    discover_nsekra_circulars.add_argument("--url", required=True)

    discover_nism_circulars = subparsers.add_parser("discover-nism-circular-range", help="Discover the oldest available NISM circular date")
    discover_nism_circulars.add_argument("--url", required=True)

    recover_bse = subparsers.add_parser("recover-bse-old-notice-dates", help="Recover exact dates from older unresolved BSE notice detail pages")
    recover_bse.add_argument("--input", required=True)
    recover_bse.add_argument("--out", required=True)
    recover_bse.add_argument("--unresolved-out", required=True)
    recover_bse.add_argument("--delay-seconds", type=float, default=1.5)
    recover_bse.add_argument("--retries", type=int, default=5)
    recover_bse.add_argument("--retry-base-delay", type=float, default=3.0)
    recover_bse.add_argument("--retry-max-delay", type=float, default=60.0)
    recover_bse.add_argument("--headless", type=lambda v: str(v).lower() != "false", default=True)

    merge_export = subparsers.add_parser("merge-export", help="Merge a main export with recovered rows")
    merge_export.add_argument("--source", required=True, choices=["bse"])
    merge_export.add_argument("--main", required=True)
    merge_export.add_argument("--add", required=True)
    merge_export.add_argument("--out", required=True)

    validate_export = subparsers.add_parser("validate-export", help="Validate a completed source export file")
    validate_export.add_argument(
        "--source",
        required=True,
        choices=["amfi-important-updates", "amfi-mfd-circulars", "nism-circulars", "nsekra-circulars", "apmi-documents", "apmi-circulars", "apmi-sebi-resources", "apmi-compliance-sutra", "nse", "nse-press-releases", "bse", "mcx", "ncdex", "nerl", "cdsl", "ccrl", "nsdl", "ifsca", "irdai-whats-new", "incometax-circulars", "incometax-notifications", "pfrda-recent-updates", "pfrda-circulars-active", "pfrda-circulars-inoperative", "pfrda-master-circulars-active", "pfrda-notifications", "pfrda-regulations", "pfrda-guidelines", "pfrda-press-releases", "pfrda-tenders", "rbi-notifications", "rbi-press-releases", "rbi-master-directions", "rbi-master-circulars", "rbi-circular-index", "rbi-standalone-circulars", "rbi-withdrawn-circulars", "rbi-amendment-directions", "rbi-acts", "rbi-rules", "rbi-regulations", "rbi-schemes", "rbi-draft-notifications-guidelines", "rbi-draft-directions-re-wise"],
    )
    validate_export.add_argument("--file", required=True)

    check_links = subparsers.add_parser("check-links", help="Check exported archive links without downloading full files")
    check_links.add_argument("--source", required=True, choices=["nsdl", "apmi-documents"])
    check_links.add_argument("--file", required=True)
    check_links.add_argument("--out", required=True)
    check_links.add_argument("--delay-seconds", type=float, default=1.5)
    check_links.add_argument("--retries", type=int, default=3)
    check_links.add_argument("--retry-base-delay", type=float, default=3.0)
    check_links.add_argument("--retry-max-delay", type=float, default=60.0)

    scrape_url = subparsers.add_parser("scrape-url", help="Scrape supported source listing rows from a listing URL")
    scrape_url.add_argument(
        "--source",
        required=True,
        choices=["amfi-important-updates", "amfi-mfd-circulars", "nism-circulars", "nsekra-circulars", "apmi-documents", "apmi-circulars", "apmi-sebi-resources", "apmi-compliance-sutra", "sebi", "nse", "nse-press-releases", "bse", "mcx", "ncdex", "nerl", "cdsl", "ccrl", "nsdl", "ifsca", "irdai-whats-new", "incometax-circulars", "incometax-notifications", "pfrda-recent-updates", "pfrda-circulars-active", "pfrda-circulars-inoperative", "pfrda-master-circulars-active", "pfrda-notifications", "pfrda-regulations", "pfrda-guidelines", "pfrda-press-releases", "pfrda-tenders", "rbi-notifications", "rbi-press-releases", "rbi-master-directions", "rbi-master-circulars", "rbi-circular-index", "rbi-standalone-circulars", "rbi-withdrawn-circulars", "rbi-amendment-directions", "rbi-acts", "rbi-rules", "rbi-regulations", "rbi-schemes", "rbi-draft-notifications-guidelines", "rbi-draft-directions-re-wise"],
    )
    scrape_url.add_argument("--url", required=True)
    scrape_url.add_argument("--out", required=True)
    scrape_url.add_argument("--limit", type=int, default=None)
    scrape_url.add_argument("--from", dest="from_date", type=parse_date, default=None)
    scrape_url.add_argument("--to", dest="to_date", type=parse_date, default=None)
    scrape_url.add_argument("--type", dest="doc_type", default=None)
    scrape_url.add_argument("--pages", type=int, default=None)
    scrape_url.add_argument("--all-pages", action="store_true")
    scrape_url.add_argument("--store-db", action="store_true")
    scrape_url.add_argument("--resume", action="store_true")
    scrape_url.add_argument("--checkpoint", default=None)
    scrape_url.add_argument("--start-page", type=int, default=None)
    scrape_url.add_argument("--end-page", type=int, default=None)
    scrape_url.add_argument("--max-pages-this-run", type=int, default=None)
    scrape_url.add_argument("--max-chunks-this-run", dest="max_pages_this_run", type=int, default=None)
    scrape_url.add_argument("--delay-seconds", type=float, default=1.5)
    scrape_url.add_argument("--max-errors", type=int, default=10)
    scrape_url.add_argument("--retries", type=int, default=5)
    scrape_url.add_argument("--retry-base-delay", type=float, default=3.0)
    scrape_url.add_argument("--retry-max-delay", type=float, default=60.0)
    scrape_url.add_argument("--allow-partial", action="store_true")
    scrape_url.add_argument("--headless", type=lambda v: str(v).lower() != "false", default=True)
    scrape_url.add_argument("--department", default=None)
    scrape_url.add_argument("--category", default=None)
    scrape_url.add_argument("--all-available", action="store_true")
    scrape_url.add_argument("--older-unresolved", action="store_true")
    scrape_url.add_argument("--include-type", action="store_true")
    scrape_url.add_argument("--include-category", action="store_true")
    scrape_url.add_argument("--include-department", action="store_true")
    scrape_url.add_argument("--include-downloads", action="store_true")

    return parser


def main() -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "backfill":
        return run_backfill(args.source, args.from_date, args.to_date, args.use_playwright_fallback, limit=args.limit)
    if args.command == "incremental":
        return run_incremental(args.source, args.days_back, args.use_playwright_fallback)
    if args.command == "rebuild-rbi-official":
        return run_rebuild_rbi_official(args.use_playwright_fallback)
    if args.command == "inspect":
        return run_inspect(args.source, args.listing_type, args.use_playwright_fallback)
    if args.command == "inspect-url":
        return run_inspect_url(args.source, args.url, args.use_playwright_fallback)
    if args.command == "inspect-pagination":
        return run_inspect_pagination(args.source, args.url, args.use_playwright_fallback, args.headless)
    if args.command == "inspect-nse-circulars":
        return run_inspect_nse_circulars("nse", args.url, args.use_playwright_fallback)
    if args.command == "inspect-nse-press-releases":
        return run_inspect_nse_press_releases("nse-press-releases", args.url, args.use_playwright_fallback)
    if args.command == "inspect-nse-press-release-archives":
        return run_inspect_nse_press_release_archives("nse-press-releases", args.url, args.use_playwright_fallback)
    if args.command == "inspect-bse-notices":
        return run_inspect_bse_notices("bse", args.url, args.use_playwright_fallback)
    if args.command == "inspect-bse-browser-archive":
        return run_inspect_bse_browser_archive("bse", args.url, args.use_playwright_fallback, args.headless)
    if args.command == "inspect-mcx-circulars":
        return run_inspect_mcx_circulars("mcx", args.url, args.use_playwright_fallback)
    if args.command == "inspect-cdsl-communiques":
        return run_inspect_cdsl_communiques("cdsl", args.url, args.use_playwright_fallback)
    if args.command == "inspect-ccrl-circulars":
        return run_inspect_ccrl_circulars("ccrl", args.url, args.use_playwright_fallback)
    if args.command == "inspect-cdsl-communique-index":
        return run_inspect_cdsl_communique_index(args.url, args.use_playwright_fallback)
    if args.command == "inspect-ncdex-circulars":
        return run_inspect_ncdex_circulars("ncdex", args.url, args.use_playwright_fallback)
    if args.command == "inspect-nerl-circulars":
        return run_inspect_nerl_circulars("nerl", args.url, args.use_playwright_fallback)
    if args.command == "inspect-nsdl-circulars":
        return run_inspect_nsdl_circulars("nsdl", args.url, args.use_playwright_fallback)
    if args.command == "inspect-ifsca-new-section":
        return run_inspect_ifsca_new_section("ifsca", args.url, args.use_playwright_fallback)
    if args.command == "inspect-irdai-whats-new":
        return run_inspect_irdai_whats_new("irdai-whats-new", args.url, args.use_playwright_fallback)
    if args.command == "inspect-irdai-whats-new-filters":
        return run_inspect_irdai_whats_new_filters("irdai-whats-new", args.url, args.use_playwright_fallback)
    if args.command == "scout-incometax":
        return run_scout_incometax(args.url, args.use_playwright_fallback)
    if args.command == "inspect-incometax-circulars":
        return run_inspect_incometax_circulars("incometax-circulars", args.url, args.use_playwright_fallback)
    if args.command == "inspect-incometax-notifications":
        return run_inspect_incometax_notifications("incometax-notifications", args.url, args.use_playwright_fallback)
    if args.command == "scout-rbi":
        return run_scout_rbi(args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-notifications":
        return run_inspect_rbi_notifications("rbi-notifications", args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-press-releases":
        return run_inspect_rbi_press_releases("rbi-press-releases", args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-master-directions":
        return run_inspect_rbi_master_directions("rbi-master-directions", args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-master-circulars":
        return run_inspect_rbi_master_circulars("rbi-master-circulars", args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-speeches":
        return run_inspect_rbi_speeches(args.url, args.use_playwright_fallback)
    if args.command == "inspect-rbi-faqs":
        return run_inspect_rbi_faqs(args.url, args.use_playwright_fallback)
    if args.command == "inspect-apmi":
        return run_inspect_apmi(args.url, args.use_playwright_fallback)
    if args.command == "scout-amfi":
        return run_scout_amfi(args.url, args.use_playwright_fallback)
    if args.command == "inspect-amfi-important-updates":
        return run_inspect_amfi("amfi-important-updates", args.url, args.use_playwright_fallback, "inspect_important_updates")
    if args.command == "inspect-amfi-mfd-circulars":
        return run_inspect_amfi("amfi-mfd-circulars", args.url, args.use_playwright_fallback, "inspect_mfd_circulars")
    if args.command == "inspect-nsekra-circulars":
        return run_inspect_nsekra("nsekra-circulars", args.url, args.use_playwright_fallback, "inspect_circulars")
    if args.command == "inspect-nism-circulars":
        return run_inspect_nism("nism-circulars", args.url, args.use_playwright_fallback, "inspect_circulars")
    if args.command == "inspect-nism-circular-archive":
        return run_inspect_nism("nism-circulars", args.url, args.use_playwright_fallback, "inspect_circular_archive")
    if args.command == "scout-pfrda":
        return run_scout_pfrda(args.url, args.use_playwright_fallback)
    if args.command == "inspect-pfrda-recent-updates":
        return run_inspect_pfrda("pfrda-recent-updates", args.url, args.use_playwright_fallback, "inspect_recent_updates")
    if args.command == "inspect-pfrda-circulars-active":
        return run_inspect_pfrda("pfrda-circulars-active", args.url, args.use_playwright_fallback, "inspect_circulars_active")
    if args.command == "inspect-pfrda-circulars-inoperative":
        return run_inspect_pfrda("pfrda-circulars-inoperative", args.url, args.use_playwright_fallback, "inspect_circulars_inoperative")
    if args.command == "inspect-pfrda-master-circulars-active":
        return run_inspect_pfrda("pfrda-master-circulars-active", args.url, args.use_playwright_fallback, "inspect_master_circulars_active")
    if args.command == "inspect-pfrda-notifications":
        return run_inspect_pfrda("pfrda-notifications", args.url, args.use_playwright_fallback, "inspect_notifications")
    if args.command == "inspect-pfrda-regulations":
        return run_inspect_pfrda("pfrda-regulations", args.url, args.use_playwright_fallback, "inspect_regulations")
    if args.command == "inspect-pfrda-guidelines":
        return run_inspect_pfrda("pfrda-guidelines", args.url, args.use_playwright_fallback, "inspect_guidelines")
    if args.command == "inspect-pfrda-press-releases":
        return run_inspect_pfrda("pfrda-press-releases", args.url, args.use_playwright_fallback, "inspect_press_releases")
    if args.command == "discover-nse-circular-range":
        return run_discover_nse_circular_range("nse", args.url, args.use_playwright_fallback)
    if args.command == "discover-nse-press-release-range":
        return run_discover_nse_press_release_range("nse-press-releases", args.url, args.use_playwright_fallback)
    if args.command == "discover-bse-notice-range":
        return run_discover_bse_notice_range("bse", args.url, args.use_playwright_fallback, args.headless)
    if args.command == "discover-mcx-circular-range":
        return run_discover_mcx_circular_range("mcx", args.url, args.use_playwright_fallback)
    if args.command == "discover-cdsl-communique-range":
        return run_discover_cdsl_communique_range("cdsl", args.url, args.use_playwright_fallback)
    if args.command == "discover-ccrl-circular-range":
        return run_discover_ccrl_circular_range("ccrl", args.url, args.use_playwright_fallback)
    if args.command == "discover-ncdex-circular-range":
        return run_discover_ncdex_circular_range("ncdex", args.url, args.use_playwright_fallback)
    if args.command == "discover-nerl-circular-range":
        return run_discover_nerl_circular_range("nerl", args.url, args.use_playwright_fallback)
    if args.command == "discover-nsdl-circular-range":
        return run_discover_nsdl_circular_range("nsdl", args.url, args.use_playwright_fallback)
    if args.command == "discover-ifsca-new-section-range":
        return run_discover_ifsca_new_section_range("ifsca", args.url, args.use_playwright_fallback)
    if args.command == "discover-irdai-whats-new-range":
        return run_discover_irdai_whats_new_range("irdai-whats-new", args.url, args.use_playwright_fallback)
    if args.command == "discover-incometax-circular-range":
        return run_discover_incometax_circular_range("incometax-circulars", args.url, args.use_playwright_fallback)
    if args.command == "discover-incometax-notification-range":
        return run_discover_incometax_notification_range("incometax-notifications", args.url, args.use_playwright_fallback)
    if args.command == "discover-rbi-notification-range":
        return run_discover_rbi_notification_range("rbi-notifications", args.url, args.use_playwright_fallback)
    if args.command == "discover-rbi-press-release-range":
        return run_discover_rbi_press_release_range("rbi-press-releases", args.url, args.use_playwright_fallback)
    if args.command == "discover-rbi-master-direction-range":
        return run_discover_rbi_master_direction_range("rbi-master-directions", args.url, args.use_playwright_fallback)
    if args.command == "discover-rbi-master-circular-range":
        return run_discover_rbi_master_circular_range("rbi-master-circulars", args.url, args.use_playwright_fallback)
    if args.command == "discover-pfrda-recent-updates-range":
        return run_discover_pfrda("pfrda-recent-updates", args.url, args.use_playwright_fallback, "discover_recent_updates_range")
    if args.command == "discover-pfrda-circulars-active-range":
        return run_discover_pfrda("pfrda-circulars-active", args.url, args.use_playwright_fallback, "discover_circulars_active_range")
    if args.command == "discover-pfrda-circulars-inoperative-range":
        return run_discover_pfrda("pfrda-circulars-inoperative", args.url, args.use_playwright_fallback, "discover_circulars_inoperative_range")
    if args.command == "discover-pfrda-master-circulars-active-range":
        return run_discover_pfrda("pfrda-master-circulars-active", args.url, args.use_playwright_fallback, "discover_master_circulars_active_range")
    if args.command == "discover-pfrda-notifications-range":
        return run_discover_pfrda("pfrda-notifications", args.url, args.use_playwright_fallback, "discover_notifications_range")
    if args.command == "discover-pfrda-regulations-range":
        return run_discover_pfrda("pfrda-regulations", args.url, args.use_playwright_fallback, "discover_regulations_range")
    if args.command == "discover-pfrda-guidelines-range":
        return run_discover_pfrda("pfrda-guidelines", args.url, args.use_playwright_fallback, "discover_guidelines_range")
    if args.command == "discover-pfrda-press-releases-range":
        return run_discover_pfrda("pfrda-press-releases", args.url, args.use_playwright_fallback, "discover_press_releases_range")
    if args.command == "discover-amfi-important-updates-range":
        return run_discover_amfi("amfi-important-updates", args.url, args.use_playwright_fallback, "discover_important_updates_range")
    if args.command == "discover-amfi-mfd-circular-range":
        return run_discover_amfi("amfi-mfd-circulars", args.url, args.use_playwright_fallback, "discover_mfd_circular_range")
    if args.command == "discover-nsekra-circular-range":
        return run_discover_nsekra("nsekra-circulars", args.url, args.use_playwright_fallback, "discover_circular_range")
    if args.command == "discover-nism-circular-range":
        return run_discover_nism("nism-circulars", args.url, args.use_playwright_fallback, "discover_circular_range")
    if args.command == "recover-bse-old-notice-dates":
        return run_recover_bse_old_notice_dates(
            args.input,
            args.out,
            args.unresolved_out,
            args.use_playwright_fallback,
            delay_seconds=args.delay_seconds,
            retries=args.retries,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
            headless=args.headless,
        )
    if args.command == "merge-export":
        return run_merge_export(args.source, args.main, args.add, args.out, args.use_playwright_fallback)
    if args.command == "validate-export":
        return run_validate_export(args.source, args.file, args.use_playwright_fallback)
    if args.command == "check-links":
        return run_check_links(
            args.source,
            args.file,
            args.out,
            args.use_playwright_fallback,
            delay_seconds=args.delay_seconds,
            retries=args.retries,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
        )
    if args.command == "scrape-url":
        return run_scrape_url(
            args.source,
            args.url,
            args.out,
            args.use_playwright_fallback,
            limit=args.limit,
            from_date=args.from_date,
            to_date=args.to_date,
            doc_type=args.doc_type,
            pages=args.pages,
            all_pages=args.all_pages,
            store_db=args.store_db,
            resume=args.resume,
            checkpoint=args.checkpoint,
            start_page=args.start_page,
            end_page=args.end_page,
            max_pages_this_run=args.max_pages_this_run,
            delay_seconds=args.delay_seconds,
            max_errors=args.max_errors,
            retries=args.retries,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
            allow_partial=args.allow_partial,
            headless=args.headless,
            department=args.department,
            category=args.category,
            all_available=args.all_available,
            older_unresolved=args.older_unresolved,
            include_type=(args.include_type or args.include_department or args.include_category),
            include_department=args.include_department,
            include_downloads=args.include_downloads,
        )

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
