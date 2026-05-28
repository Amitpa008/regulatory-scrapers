from __future__ import annotations

from datetime import date, timedelta

from scrapers.bse import BSEScraper, BSE_ARCHIVE_BETA_URL


def main() -> None:
    scraper = BSEScraper(config={}, rate_limit_seconds=0)
    try:
        year = 2002
        for month in range(1, 13):
            start = date(year, month, 1)
            if month == 12:
                end = date(year, 12, 31)
            else:
                end = date(year, month + 1, 1) - timedelta(days=1)
            html = scraper.execute_archive_search(start, end, retries=5, retry_base_delay=3.0, retry_max_delay=60.0)
            records = scraper.parse_notice_records(html, BSE_ARCHIVE_BETA_URL, source_url=BSE_ARCHIVE_BETA_URL)
            print(start.isoformat(), end.isoformat(), len(records))
            if records:
                print("sample", records[0].date, records[0].circular_no, records[0].subject)
                break
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
