from __future__ import annotations

from bs4 import BeautifulSoup
import httpx


URL = "https://beta.bseindia.com/markets/MarketInfo/NoticesCircularsArchive.aspx?id=0&pagecont=&subject=&txtscripcd="
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": URL,
    "Origin": "https://beta.bseindia.com",
}


def main() -> None:
    with httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        for start, end in [
            ("01/03/2026", "31/03/2026"),
            ("01/01/2023", "31/12/2025"),
            ("01/01/2020", "31/12/2022"),
            ("01/01/2017", "31/12/2019"),
            ("01/01/2014", "31/12/2016"),
            ("01/01/2011", "31/12/2013"),
            ("01/01/2008", "31/12/2010"),
            ("01/01/2005", "31/12/2007"),
            ("01/01/2002", "31/12/2004"),
            ("01/01/1999", "31/12/2001"),
            ("01/01/1996", "31/12/1998"),
            ("01/01/1993", "31/12/1995"),
        ]:
            response = client.get(URL)
            print("GET", response.status_code, len(response.text), start, end)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            payload: dict[str, str] = {}
            for field in soup.select("form input[name], form select[name], form textarea[name]"):
                name = field.get("name")
                if not name:
                    continue
                if field.name == "select":
                    option = field.select_one("option[selected]") or field.select_one("option")
                    payload[name] = option.get("value", "") if option else ""
                    continue
                field_type = (field.get("type") or "").lower()
                if field_type in {"checkbox", "radio"}:
                    if field.has_attr("checked"):
                        payload[name] = field.get("value", "on")
                    continue
                if field_type == "image":
                    continue
                payload[name] = field.get("value", "")

            payload["ctl00$ContentPlaceHolder1$txtDate"] = start
            payload["ctl00$ContentPlaceHolder1$txtTodate"] = end
            payload["ctl00$ContentPlaceHolder1$btnSubmit"] = "Submit"

            post_response = client.post(URL, data=payload)
            print("POST", post_response.status_code, len(post_response.text), str(post_response.url))
            soup = BeautifulSoup(post_response.text, "html.parser")
            for grid_id in ("ContentPlaceHolder1_GridView1", "ContentPlaceHolder1_GridView2"):
                if soup.select(f"#{grid_id}"):
                    rows = []
                    for tr in soup.select(f"#{grid_id} tr")[1:4]:
                        rows.append([td.get_text(" ", strip=True) for td in tr.find_all("td")[:2]])
                    print("GRID", grid_id, "ROWS", len(soup.select(f"#{grid_id} tr")) - 1, rows)
            error_cell = soup.select_one("td.ErrorRow")
            if error_cell:
                print("ERROR", error_cell.get_text(" ", strip=True))


if __name__ == "__main__":
    main()
