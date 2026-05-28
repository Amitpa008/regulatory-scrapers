import json
import csv
from pathlib import Path
from datetime import date

import httpx
import pytest

from scrapers.sebi import ArchiveCheckpoint, SEBIScraper, SebiListingRecord


FIXTURE_PATH = Path("tests/fixtures/sebi/doListingAll_listing.html")
PAGE_2_FIXTURE_PATH = Path("tests/fixtures/sebi/doListingAll_page_2_fragment.html")
NETWORK_CAPTURE_PATH = Path("tests/fixtures/sebi/pagination_network_capture.json")


def make_scraper() -> SEBIScraper:
    return SEBIScraper(config={}, rate_limit_seconds=0)


def load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def load_page_2_fixture() -> str:
    return PAGE_2_FIXTURE_PATH.read_text(encoding="utf-8")


def write_csv_records(path: Path, records: list[SebiListingRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(
            file_obj,
            fieldnames=["date", "type", "title", "link", "source_url", "scraped_at"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "date": record.date,
                    "type": record.type,
                    "title": record.title,
                    "link": record.link,
                    "source_url": record.source_url,
                    "scraped_at": record.scraped_at,
                }
            )


def make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistallinfo.jsp")
    response = httpx.Response(status_code=status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def test_parse_do_listing_all_fixture_extracts_at_least_ten_rows() -> None:
    scraper = make_scraper()
    records, total_records = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )

    assert len(records) >= 10
    assert total_records == 82678
    assert records[0].title == "Adjudication order in the matter of Darshan Orna Limited"


def test_date_normalization_and_absolute_url_conversion() -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )

    assert records[0].date == "2026-05-15"
    assert records[0].link.startswith("https://www.sebi.gov.in/")


def test_parse_simulated_page_2_fragment() -> None:
    scraper = make_scraper()
    state = scraper.build_archive_state(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    page2_state = scraper.build_archive_state_from_fragment(load_page_2_fixture(), state)

    records, total_records = scraper.parse_listing_url_html(
        f"<html><body>{load_page_2_fixture()}</body></html>",
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    assert page2_state.current_page == 2
    assert page2_state.next_value == "2"
    assert total_records == 82678
    assert records[0].date == "2026-05-14"


def test_pagination_network_capture_fixture_parses() -> None:
    payload = json.loads(NETWORK_CAPTURE_PATH.read_text(encoding="utf-8"))
    assert payload["pagination_requests"][0]["url"].endswith("getnewslistallinfo.jsp")
    assert payload["pagination_requests"][0]["form_payload"]["doDirect"] == "1"


def test_csv_writing(tmp_path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    output_path = tmp_path / "output.csv"
    scraper.write_listing_output(records[:2], output_path)

    content = output_path.read_text(encoding="utf-8")
    assert content.splitlines()[0] == "date,type,title,link,source_url,scraped_at"
    assert "Adjudication order in the matter of Darshan Orna Limited" in content


def test_json_writing(tmp_path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    output_path = tmp_path / "output.json"
    scraper.write_listing_output(records[:2], output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert payload[0]["type"] == "Orders"


def test_deduplication() -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    deduped = scraper.deduplicate_listing_records(records + [records[0]])
    assert len(deduped) == len(records)


def test_csv_append_mode(tmp_path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    output_path = tmp_path / "append.csv"
    scraper.append_listing_output(records[:1], output_path)
    scraper.append_listing_output(records[1:2], output_path)

    content = output_path.read_text(encoding="utf-8").splitlines()
    assert len(content) == 3
    assert content.count("date,type,title,link,source_url,scraped_at") == 1


def test_type_filtering() -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    filtered = scraper.filter_listing_records(records, doc_type="Circulars")
    assert filtered
    assert all(record.type == "Circulars" for record in filtered)


def test_date_filtering() -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    filtered = scraper.filter_listing_records(
        records,
        from_date=date.fromisoformat("2026-05-15"),
        to_date=date.fromisoformat("2026-05-15"),
    )
    assert filtered
    assert all(record.date == "2026-05-15" for record in filtered)


def test_resume_from_checkpoint(tmp_path) -> None:
    scraper = make_scraper()
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = ArchiveCheckpoint(
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
        output_path=str(tmp_path / "out.csv"),
        total_records_detected=82678,
        page_size=25,
        last_completed_page=2,
        records_written=50,
        unique_records_written=50,
        started_at="2026-05-16T00:00:00+00:00",
        updated_at="2026-05-16T00:10:00+00:00",
        completed=False,
        errors=[],
    )
    scraper.save_checkpoint(checkpoint_path, checkpoint)
    loaded = scraper.load_checkpoint(checkpoint_path)
    assert loaded.last_completed_page == 2


def test_resume_page_derived_from_10125_csv_rows() -> None:
    scraper = make_scraper()
    base_record = SebiListingRecord(
        date="2026-05-15",
        type="Circulars",
        title="Example title",
        link="https://www.sebi.gov.in/example",
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
        scraped_at="2026-05-16T00:00:00+00:00",
    )
    records = [
        SebiListingRecord(
            date=base_record.date,
            type=base_record.type,
            title=f"Example title {index}",
            link=f"{base_record.link}/{index}",
            source_url=base_record.source_url,
            scraped_at=base_record.scraped_at,
        )
        for index in range(10125)
    ]
    checkpoint = ArchiveCheckpoint(
        source_url=base_record.source_url,
        output_path="data/sebi_all_archive.csv",
        total_records_detected=82678,
        page_size=25,
        last_completed_page=109,
        records_written=2725,
        unique_records_written=2725,
        started_at="2026-05-16T00:00:00+00:00",
        updated_at="2026-05-16T00:00:00+00:00",
        completed=False,
        errors=[],
    )

    resume_state = scraper.reconcile_archive_resume_state(
        output_records=records,
        checkpoint=checkpoint,
        source_url=base_record.source_url,
        out_path="data/sebi_all_archive.csv",
        total_records_detected=82678,
        page_size=25,
    )

    assert resume_state.csv_rows_detected == 10125
    assert resume_state.checkpoint_last_completed_page == 109
    assert resume_state.reconciled_last_completed_page == 405
    assert resume_state.resume_from_page == 406
    assert checkpoint.last_completed_page == 405
    assert checkpoint.records_written == 10125


def test_existing_dedupe_keys_loaded_from_csv(tmp_path) -> None:
    scraper = make_scraper()
    records, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    output_path = tmp_path / "existing.csv"
    write_csv_records(output_path, records[:3])

    loaded_records = scraper.load_existing_output_records(output_path)
    resume_state = scraper.reconcile_archive_resume_state(
        output_records=loaded_records,
        checkpoint=ArchiveCheckpoint(
            source_url=records[0].source_url,
            output_path=str(output_path),
            total_records_detected=82678,
            page_size=25,
            last_completed_page=0,
            records_written=0,
            unique_records_written=0,
            started_at="2026-05-16T00:00:00+00:00",
            updated_at="2026-05-16T00:00:00+00:00",
            completed=False,
            errors=[],
        ),
        source_url=records[0].source_url,
        out_path=output_path,
        total_records_detected=82678,
        page_size=25,
    )

    assert len(loaded_records) == 3
    assert resume_state.existing_dedupe_keys_loaded == 3
    assert all(record.date == "2026-05-15" for record in loaded_records)


def test_partial_final_page_resume_is_safe() -> None:
    scraper = make_scraper()
    records = [
        SebiListingRecord(
            date="2026-05-15",
            type="Circulars",
            title=f"Row {index}",
            link=f"https://www.sebi.gov.in/example/{index}",
            source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
            scraped_at="2026-05-16T00:00:00+00:00",
        )
        for index in range(10130)
    ]
    checkpoint = ArchiveCheckpoint(
        source_url=records[0].source_url,
        output_path="data/sebi_all_archive.csv",
        total_records_detected=82678,
        page_size=25,
        last_completed_page=500,
        records_written=0,
        unique_records_written=0,
        started_at="2026-05-16T00:00:00+00:00",
        updated_at="2026-05-16T00:00:00+00:00",
        completed=False,
        errors=[],
    )

    resume_state = scraper.reconcile_archive_resume_state(
        output_records=records,
        checkpoint=checkpoint,
        source_url=records[0].source_url,
        out_path="data/sebi_all_archive.csv",
        total_records_detected=82678,
        page_size=25,
    )

    assert resume_state.partial_page_rows == 5
    assert resume_state.reconciled_last_completed_page == 405
    assert resume_state.resume_from_page == 406


def test_resolve_archive_end_page_for_resume_window() -> None:
    scraper = make_scraper()
    end_page = scraper.resolve_archive_end_page(
        effective_start_page=406,
        total_pages=3308,
        pages=None,
        all_pages=False,
        end_page=None,
        max_pages_this_run=5,
    )
    assert end_page == 410


def test_dedupe_across_pages() -> None:
    scraper = make_scraper()
    page1, _ = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    duplicate = SebiListingRecord(**page1[0].__dict__)
    deduped = scraper.deduplicate_listing_records(page1 + [duplicate])
    assert len(deduped) == len(page1)


def test_total_count_extraction() -> None:
    scraper = make_scraper()
    _, total_records = scraper.parse_listing_url_html(
        load_fixture(),
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
    )
    assert total_records == 82678


def test_malformed_row_skip() -> None:
    scraper = make_scraper()
    html = """
    <html><body>
      <table id='sample_1'>
        <tbody>
          <tr><td>May 15, 2026</td><td>Orders</td><td><a href='/a' class='points'>Valid Row</a></td></tr>
          <tr><td>May 15, 2026</td><td>Orders</td><td></td></tr>
        </tbody>
      </table>
      <div class='pagination'><div class='pagination_inner'><p>1 to 2 of 2 records</p></div></div>
    </body></html>
    """
    records, total = scraper.parse_listing_url_html(html, "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes")
    assert len(records) == 1
    assert total == 2


def test_completion_validation_partial_case() -> None:
    scraper = make_scraper()
    with pytest.raises(NotImplementedError):
        raise scraper._pagination_not_implemented("https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes")


def test_zero_row_failure() -> None:
    scraper = make_scraper()
    html = "<html><head><title>Empty</title></head><body><table id='sample_1'><tbody></tbody></table></body></html>"
    with pytest.raises(RuntimeError, match="zero rows"):
        scraper.parse_listing_url_html(html, "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes")


def test_remote_protocol_retry_succeeds_on_second_attempt(monkeypatch) -> None:
    scraper = make_scraper()
    monkeypatch.setattr("scrapers.sebi.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def operation() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected")
        return "ok"

    result = scraper.execute_page_request_with_retry(
        page_number=812,
        operation=operation,
        retries=5,
        base_delay=3,
        max_delay=60,
    )

    assert result == "ok"
    assert attempts["count"] == 2


@pytest.mark.parametrize("status_code", [429, 503])
def test_retryable_http_status_retries(status_code: int, monkeypatch) -> None:
    scraper = make_scraper()
    monkeypatch.setattr("scrapers.sebi.time.sleep", lambda _: None)
    attempts = {"count": 0}

    def operation() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise make_http_status_error(status_code)
        return "ok"

    result = scraper.execute_page_request_with_retry(
        page_number=500,
        operation=operation,
        retries=5,
        base_delay=3,
        max_delay=60,
    )

    assert result == "ok"
    assert attempts["count"] == 2


def test_failed_page_does_not_advance_checkpoint_and_resume_starts_after_last_success(tmp_path, monkeypatch) -> None:
    scraper = make_scraper()
    fixture_html = load_fixture()
    checkpoint_path = tmp_path / "checkpoint.json"
    output_path = tmp_path / "archive.csv"
    page2_attempts = {"count": 0}

    monkeypatch.setattr("scrapers.sebi.time.sleep", lambda _: None)

    def fake_fetch_listing_url(url: str):
        return type(
            "Fetched",
            (),
            {
                "url": url,
                "html": fixture_html,
                "transport": "httpx",
            },
        )()

    def fake_fetch_archive_page_fragment(state, target_page: int):
        page2_attempts["count"] += 1
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    monkeypatch.setattr(scraper, "fetch_listing_url", fake_fetch_listing_url)
    monkeypatch.setattr(scraper, "fetch_archive_page_fragment", fake_fetch_archive_page_fragment)

    with pytest.raises(RuntimeError, match="Resume with:"):
        scraper.scrape_archive_all_pages(
            url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
            out_path=output_path,
            limit=None,
            from_date=None,
            to_date=None,
            doc_type=None,
            pages=2,
            all_pages=False,
            resume=False,
            checkpoint_path=checkpoint_path,
            start_page=None,
            end_page=None,
            max_pages_this_run=None,
            delay_seconds=0,
            max_errors=10,
            retries=2,
            retry_base_delay=3,
            retry_max_delay=60,
            allow_partial=False,
            headless=True,
        )

    checkpoint = scraper.load_checkpoint(checkpoint_path)
    existing_records = scraper.load_existing_output_records(output_path)
    resume_state = scraper.reconcile_archive_resume_state(
        output_records=existing_records,
        checkpoint=checkpoint,
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes",
        out_path=output_path,
        total_records_detected=82678,
        page_size=25,
    )

    assert checkpoint.last_completed_page == 1
    assert checkpoint.records_written == 25
    assert len(existing_records) == 25
    assert resume_state.resume_from_page == 2
    assert page2_attempts["count"] == 2
