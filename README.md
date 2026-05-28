# regulatory-scrapers

Scraper framework for Indian regulatory circulars and notifications with resilient HTTP fetching, PDF extraction, normalization, and SQLite-based deduplication.

## Usage

```bash
python main.py backfill --source sebi --from 2015-01-01 --to 2026-05-16
python main.py incremental --source all --days-back 7
```

## Notes

- Playwright is optional and intended only for source-specific fallback flows.
- Website-specific endpoints and selectors are intentionally marked as `TODO` where live inspection is required.

