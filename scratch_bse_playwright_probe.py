from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


URL = "https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject="
ARCHIVE = "https://beta.bseindia.com/markets/MarketInfo/NoticesCircularsArchive.aspx?id=0&pagecont=&subject=&txtscripcd="


def rows_from_html(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("#ContentPlaceHolder1_GridView1 tr")[1:11]:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        rows.append(cells[:2])
    return rows


def main() -> None:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        page = context.new_page()
        requests = []

        def on_request(req):
            post = req.post_data
            requests.append(
                {
                    "method": req.method,
                    "url": req.url,
                    "resource_type": req.resource_type,
                    "post_data": post[:3000] if post else None,
                }
            )

        page.on("request", on_request)
        page.goto("https://www.bseindia.com/", wait_until="domcontentloaded", timeout=120000)
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        out.append({"current_title": page.title(), "current_url": page.url})
        page.goto(ARCHIVE, wait_until="domcontentloaded", timeout=120000)
        html = page.content()
        Path("scratch_bse_archive_rendered.html").write_text(html, encoding="utf-8")
        out.append(
            {
                "archive_title": page.title(),
                "archive_url": page.url,
                "frame_urls": [frame.url for frame in page.frames],
                "has_top_txtDate": page.locator("#ContentPlaceHolder1_txtDate").count(),
                "forms": page.locator("form").count(),
                "inputs": page.locator("input").count(),
                "html_path": "scratch_bse_archive_rendered.html",
            }
        )
        html = page.content()
        out.append({"initial_rows": rows_from_html(html)})
        if page.locator("#ContentPlaceHolder1_lnkPreviousDay").count():
            before = len(requests)
            page.click("#ContentPlaceHolder1_lnkPreviousDay")
            page.wait_for_load_state("networkidle", timeout=120000)
            prev_html = page.content()
            Path("scratch_bse_previous_day.html").write_text(prev_html, encoding="utf-8")
            prev_soup = BeautifulSoup(prev_html, "html.parser")
            out.append(
                {
                    "previous_day_row_count": len(prev_soup.select("#ContentPlaceHolder1_GridView1 tr")) - 1,
                    "previous_day_rows": rows_from_html(prev_html),
                    "previous_day_url": page.url,
                    "previous_day_requests": requests[before:],
                    "previous_day_html_path": "scratch_bse_previous_day.html",
                }
            )
            page.goto(ARCHIVE, wait_until="domcontentloaded", timeout=120000)
        if not page.locator("#ContentPlaceHolder1_txtDate").count():
            Path("scratch_bse_playwright_probe.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            browser.close()
            return
        today = date(2026, 5, 17)
        ranges = [
            ("current_month", date(today.year, today.month, 1), date(today.year, today.month, 16)),
            ("previous_month", date(2026, 4, 1), date(2026, 4, 30)),
            ("older_month", date(2026, 3, 1), date(2026, 3, 31)),
        ]
        for label, start, end in ranges:
            page.fill("#ContentPlaceHolder1_txtDate", start.strftime("%d/%m/%Y"))
            page.fill("#ContentPlaceHolder1_txtTodate", end.strftime("%d/%m/%Y"))
            before = len(requests)
            page.click("#ContentPlaceHolder1_btnSubmit")
            page.wait_for_load_state("networkidle", timeout=120000)
            result_html = page.content()
            Path(f"scratch_bse_{label}.html").write_text(result_html, encoding="utf-8")
            result_rows = rows_from_html(result_html)
            out.append(
                {
                    "label": label,
                    "range": [start.isoformat(), end.isoformat()],
                    "row_count": len(BeautifulSoup(result_html, "html.parser").select("#ContentPlaceHolder1_GridView1 tr")) - 1,
                    "rows": result_rows[:5],
                    "requests": requests[before:],
                    "url_after": page.url,
                    "html_path": f"scratch_bse_{label}.html",
                }
            )
        Path("scratch_bse_playwright_probe.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        browser.close()


if __name__ == "__main__":
    main()
