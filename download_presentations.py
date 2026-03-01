#!/usr/bin/env python3
"""
Download Delta earnings presentations from Delta's IR RSS feed (q4cdn.com CDN).
These do not require EDGAR access and work immediately.
"""

import re
import html
import time
import requests
from pathlib import Path

OUTPUT_DIR = Path("delta_financial_reports/Earnings_Presentations")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

IR_PRESENTATIONS_RSS = "https://ir.delta.com/rss/Presentation.aspx"


def extract_year_quarter(text, url=""):
    combined = f"{text} {url}".lower()
    ym = re.search(r"\b(20\d{2})\b", combined)
    year = int(ym.group(1)) if ym else None
    patterns = [
        (r"\bq1\b|first.quarter|1q\b|\b1st|march.quarter", "Q1"),
        (r"\bq2\b|second.quarter|2q\b|\b2nd|june.quarter", "Q2"),
        (r"\bq3\b|third.quarter|3q\b|\b3rd|sept|september.quarter", "Q3"),
        (r"\bq4\b|fourth.quarter|4q\b|\b4th|december.quarter|full.year|annual", "Q4"),
    ]
    quarter = None
    for pat, label in patterns:
        if re.search(pat, combined, re.I):
            quarter = label
            break
    return year, quarter


def download(url, filepath):
    for attempt in range(4):
        try:
            r = requests.get(url, headers=WEB_HEADERS, stream=True, timeout=120)
            r.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in r.iter_content(32768):
                    fh.write(chunk)
            size = filepath.stat().st_size // 1024
            print(f"  ✓ {filepath.name}  ({size} KB)")
            return True
        except Exception as e:
            wait = 10 * (2 ** attempt)
            print(f"  Attempt {attempt+1} failed: {e}  (retry in {wait}s)")
            time.sleep(wait)
    print(f"  ✗ Failed: {filepath.name}")
    return False


def main():
    print("Fetching Delta IR Presentations RSS feed …")
    resp = requests.get(IR_PRESENTATIONS_RSS, headers=WEB_HEADERS, timeout=30)
    resp.raise_for_status()

    content = resp.text
    items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
    print(f"Found {len(items)} presentations in RSS\n")

    ok = fail = skip = 0
    for item in items:
        title_m = re.search(r"<title>(.*?)</title>", item)
        url_m   = re.search(r'href=["\']?(https?://[^"\'>\s]+\.pdf)', html.unescape(item), re.I)
        date_m  = re.search(r"<pubDate>(.*?)</pubDate>", item)

        if not title_m or not url_m:
            continue

        title   = html.unescape(title_m.group(1)).strip()
        pdf_url = url_m.group(1).replace("http://", "https://")  # force HTTPS
        date    = date_m.group(1)[:11] if date_m else ""

        year, quarter = extract_year_quarter(title, pdf_url)

        if year and quarter and 2016 <= year <= 2025:
            base_name = f"Delta {year} {quarter} Earnings Presentation.pdf"
        else:
            # Save non-quarterly presentations with their original title (sanitized)
            safe_title = re.sub(r'[<>:"/\\|?*]', '', title)[:80]
            base_name  = f"Delta IR Presentation - {date} - {safe_title}.pdf"

        filepath = OUTPUT_DIR / base_name
        if filepath.exists():
            print(f"  Already have: {base_name}")
            skip += 1
            continue

        print(f"  Downloading: {base_name}")
        print(f"    {pdf_url}")
        time.sleep(1)
        if download(pdf_url, filepath):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} downloaded, {skip} skipped, {fail} failed")


if __name__ == "__main__":
    main()
