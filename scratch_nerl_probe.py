from __future__ import annotations

import json
from collections import Counter
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


def main() -> None:
    url = "https://www.nerlindia.com/circulars/"
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
    }
    response = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")
    rows = []
    departments = Counter()
    links = Counter()
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        row_date = tds[0].get_text(" ", strip=True)
        title = tds[1].get_text(" ", strip=True)
        department = tds[2].get_text(" ", strip=True)
        anchor = tds[3].find("a", href=True)
        href = urljoin(str(response.url), anchor.get("href")) if anchor else ""
        rows.append((row_date, title, department, href, str(tds[3])[:300]))
        departments[department] += 1
        lowered = href.lower()
        if not href:
            links["empty"] += 1
        elif lowered.endswith(".pdf"):
            links["pdf"] += 1
        elif lowered.endswith(".doc") or lowered.endswith(".docx"):
            links["doc"] += 1
        elif lowered.endswith(".zip"):
            links["zip"] += 1
        elif lowered.endswith(".htm") or lowered.endswith(".html"):
            links["html"] += 1
        else:
            links["other"] += 1
        if anchor and anchor.find("img"):
            links["image_icon_anchor"] += 1

    print("ROW_COUNT", len(rows))
    print("FIRST10", json.dumps(rows[:10], indent=2)[:5000])
    print("LAST10", json.dumps(rows[-10:], indent=2)[:5000])
    print("DEPARTMENTS", json.dumps(dict(departments), indent=2))
    print("LINKS", json.dumps(dict(links), indent=2))


if __name__ == "__main__":
    main()
