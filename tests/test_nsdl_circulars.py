import csv
import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from scrapers.nsdl import NSDLCheckpoint, NSDLCircularRecord, NSDLScraper


FIXTURE_DIR = Path("tests/fixtures/nsdl")
MAIN_HTML_FIXTURE = FIXTURE_DIR / "circular_main.html"
YEAR_2018_HTML_FIXTURE = FIXTURE_DIR / "circular_2018.html"
STATUS_HTML_FIXTURE = FIXTURE_DIR / "circular_stat.html"


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_scraper() -> NSDLScraper:
    return NSDLScraper(config={}, rate_limit_seconds=0)


def test_parse_year_selector_fixture() -> None:
    scraper = make_scraper()
    years = scraper.extract_year_options(BeautifulSoup(load_text(MAIN_HTML_FIXTURE), "html.parser"))
    assert years[0] == 2026
    assert years[-1] == 1997
    assert 2018 in years


def test_parse_year_page_records_mapping() -> None:
    scraper = make_scraper()
    records = scraper.parse_year_page_records(load_text(YEAR_2018_HTML_FIXTURE), "https://nsdl.co.in/business/circular.php?yr=2018")
    assert len(records) >= 50
    assert records[0].date == "2018-12-31"
    assert records[0].circular_no == "2018-0074-Policy-Amendments"
    assert records[0].link.startswith("https://nsdl.co.in/downloadables/pdf/")


def test_parse_status_page_records_mapping() -> None:
    scraper = make_scraper()
    records = scraper.parse_status_page_records(load_text(STATUS_HTML_FIXTURE), "https://nsdl.co.in/business/circular_stat.php")
    assert len(records) >= 50
    first = records[0]
    assert first.date == "2026-05-14"
    assert first.status == "ACTIVE"
    assert "Circular nos 2026-1146 to 2026-1159" in first.subject
    assert first.link.endswith(".zip")


def test_absolute_url_conversion_and_link_type_count() -> None:
    scraper = make_scraper()
    assert scraper.normalize_nsdl_link("/business/cr1997dec28_003.php", "https://nsdl.co.in/business/circular.php?yr=1997") == "https://nsdl.co.in/business/cr1997dec28_003.php"
    counts = scraper.count_link_types(
        [
            NSDLCircularRecord("2026-05-14", "A", "1", "https://nsdl.co.in/a.pdf", "u", "s"),
            NSDLCircularRecord("2026-05-14", "B", "2", "https://nsdl.co.in/b.php", "u", "s"),
            NSDLCircularRecord("2026-05-14", "C", "3", "https://nsdl.co.in/c.doc", "u", "s"),
            NSDLCircularRecord("2026-05-14", "D", "4", "https://nsdl.co.in/d.zip", "u", "s"),
            NSDLCircularRecord("2026-05-14", "E", "5", "", "u", "s"),
        ]
    )
    assert counts == {"pdf": 1, "html": 1, "doc": 1, "zip": 1, "other": 0, "empty": 1}


def test_request_rejected_detection() -> None:
    scraper = make_scraper()
    assert scraper.is_request_rejected("<html><body>Request Rejected</body></html>") is True
    assert scraper.is_request_rejected("<html><body>DP Circulars - NSDL</body></html>") is False


def test_csv_and_json_writing(tmp_path: Path) -> None:
    scraper = make_scraper()
    records = scraper.parse_year_page_records(load_text(YEAR_2018_HTML_FIXTURE), "https://nsdl.co.in/business/circular.php?yr=2018")[:2]
    csv_path = tmp_path / "nsdl.csv"
    json_path = tmp_path / "nsdl.json"
    scraper.append_output(records, csv_path)
    scraper.append_output(records, json_path)
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "date,subject,circular_no,link,source_url,scraped_at"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 2


def test_dedupe_fallback_without_circular_number() -> None:
    scraper = make_scraper()
    record = NSDLCircularRecord(
        date="2026-05-12",
        subject="Same subject",
        circular_no="",
        link="https://nsdl.co.in/business/x.php",
        source_url="https://nsdl.co.in/business/circular_stat.php",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    assert scraper.record_dedup_key(record) == ("2026-05-12", "same subject", "https://nsdl.co.in/business/x.php")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    scraper = make_scraper()
    checkpoint = NSDLCheckpoint(
        source_url="https://nsdl.co.in/business/circular_stat.php",
        output_path=str(tmp_path / "out.csv"),
        newest_available_date="2026-05-14",
        oldest_available_date="1997-12-24",
        years_discovered=[2026, 2025, 2018, 1997],
        total_records_detected=100,
        chunk_strategy="year_pages_via_browser",
        last_completed_chunk=2,
        records_written=10,
        unique_records_written=10,
        started_at="2026-05-18T00:00:00+00:00",
        updated_at="2026-05-18T00:00:00+00:00",
        completed=False,
        errors=[],
    )
    path = tmp_path / "checkpoint.json"
    scraper.save_checkpoint(path, checkpoint)
    loaded = scraper.load_checkpoint(path)
    assert loaded.years_discovered[-1] == 1997
    assert loaded.last_completed_chunk == 2


def test_yearly_discovery_uses_1997_and_rejects_1996(monkeypatch) -> None:
    scraper = make_scraper()
    main_html = """
    <html><body><select id="yr">
      <option value="2026">2026</option>
      <option value="1997">1997</option>
    </select></body></html>
    """
    year_1997_html = """
    <html><body><table>
      <tr><td>28 December 1997</td><td><a href="/business/cr1997dec28_003.php">3 General Operating Instructions</a></td></tr>
    </table></body></html>
    """
    year_1996_html = '<html><body><div class="alerttext">No Record Found</div></body></html>'
    status_html = """
    <html><body><table>
      <tr><td>14 May , 2026</td><td>ACTIVE</td><td><a href="/downloadables/pdf/x.zip">2026-0076-Policy-SEBI Circular</a></td></tr>
    </table></body></html>
    """

    @contextmanager
    def fake_browser_session(*, headless: bool):
        del headless
        yield {"page": object()}

    def fake_fetch_route_html(
        url: str,
        *,
        session: dict[str, object] | None = None,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> str:
        del session, retries, retry_base_delay, retry_max_delay
        if url.endswith("circular.php"):
            return main_html
        if url.endswith("circular_stat.php"):
            return status_html
        raise AssertionError(url)

    def fake_fetch_year_page_html(year: int, *, session: dict[str, object], retries: int = 5, retry_base_delay: float = 3.0, retry_max_delay: float = 60.0) -> str:
        del session, retries, retry_base_delay, retry_max_delay
        return year_1997_html if year == 1997 else year_1996_html

    monkeypatch.setattr(scraper, "browser_session", fake_browser_session)
    monkeypatch.setattr(scraper, "fetch_route_html", fake_fetch_route_html)
    monkeypatch.setattr(scraper, "fetch_year_page_html", fake_fetch_year_page_html)

    result = scraper.discover_circular_range("https://nsdl.co.in/business/circular_stat.php")
    assert result["oldest_year_tested"] == 1996
    assert result["oldest_year_with_actual_rows"] == 1997
    assert result["oldest_circular_date_found"] == "1997-12-28"


def test_retry_handling_for_link_check(monkeypatch) -> None:
    scraper = make_scraper()
    attempts = {"count": 0}
    monkeypatch.setattr("scrapers.nsdl.time.sleep", lambda _: None)

    def fake_head(url: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("disconnect")
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(scraper.client, "head", fake_head)
    response = scraper.request_link_metadata(
        "https://nsdl.co.in/business/x.php",
        method="HEAD",
        retries=3,
        retry_base_delay=0.1,
        retry_max_delay=0.2,
    )
    assert response.status_code == 200
    assert attempts["count"] == 2


def test_zero_record_year_page() -> None:
    scraper = make_scraper()
    html = '<html><body><div class="alerttext">No Record Found</div></body></html>'
    assert scraper.parse_year_page_records(html, "https://nsdl.co.in/business/circular.php?yr=1996") == []


def test_file_lock_preflight(monkeypatch, tmp_path: Path) -> None:
    scraper = make_scraper()
    locked_path = tmp_path / "locked.csv"
    locked_path.write_text("", encoding="utf-8")

    def fake_open(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(RuntimeError, match="Output file is locked"):
        scraper.ensure_output_writable(locked_path, resume=True)


def test_validation_and_append_mode(tmp_path: Path) -> None:
    scraper = make_scraper()
    record = NSDLCircularRecord(
        date="2026-05-12",
        subject="2026-0076-Policy-SEBI Circular on Norms for sharing and usage of price data for educational purposes",
        circular_no="2026-0076-Policy-SEBI",
        link="https://nsdl.co.in/downloadables/pdf/example.pdf",
        source_url="https://nsdl.co.in/business/circular_stat.php",
        scraped_at="2026-05-18T00:00:00+00:00",
    )
    export_path = tmp_path / "nsdl.csv"
    scraper.append_output([record], export_path)
    scraper.append_output([record], export_path)

    rows = list(csv.reader(export_path.open("r", encoding="utf-8")))
    assert rows[0] == ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
    assert rows.count(rows[0]) == 1

    report = scraper.validate_export(export_path)
    assert report["total_rows"] == 2
    assert report["duplicate_key_count"] == 1
    assert (tmp_path / "nsdl_circulars_validation_report.json").exists()
    assert (tmp_path / "nsdl_circulars_year_counts.csv").exists()
