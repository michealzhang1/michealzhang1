#!/usr/bin/env python3
"""
Delta Airlines Financial Reports Downloader
==========================================
Downloads all available financial reports for Delta Air Lines (DAL)
for the 10-year period 2016-2025:

  1. 10-Q Filings         — from SEC EDGAR Archives
  2. Earnings Presentations— from Delta's IR CDN (q4cdn.com) and EDGAR 8-K exhibits
  3. Earnings Transcripts  — from EDGAR 8-K exhibits (EX-99.1 press releases)

Note on transcripts:
  Verbatim call transcripts require a paid service (Seeking Alpha, Bloomberg, S&P).
  This script downloads the closest public-domain substitute: the quarterly earnings
  press releases filed as EX-99.1 in 8-K submissions.  These contain all financial
  tables and much of management's prepared commentary.

Note on EDGAR rate-limiting:
  SEC EDGAR enforces a 10-request/second limit plus additional IP-level throttling.
  If you receive 503/403 errors, wait 10-60 minutes and re-run — the block is temporary.
  The script automatically skips files already present so it is safe to re-run.

Usage:
  pip install requests beautifulsoup4
  python download_delta_financials.py
"""

import html
import json
import os
import re
import sys
import time
import requests
from datetime import datetime, date
from pathlib import Path
from bs4 import BeautifulSoup
import urllib.parse

# ─── Configuration ────────────────────────────────────────────────────────────
EDGAR_BASE_URL   = "https://data.sec.gov"
EDGAR_ARCHIVES   = "https://www.sec.gov/Archives/edgar/data"
DELTA_CIK        = "27904"
DELTA_CIK_PADDED = "0000027904"
OUTPUT_DIR       = Path("delta_financial_reports")
START_YEAR       = 2016
END_YEAR         = 2025

EDGAR_HEADERS = {
    "User-Agent": "Delta Financial Research downloader@financialresearch.example.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "*/*",
}
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# Inter-request delay for EDGAR Archives (conservative to avoid rate limiting)
EDGAR_DELAY_SECONDS = 35

DIRS = {
    "10q":           OUTPUT_DIR / "10Q_Filings",
    "transcripts":   OUTPUT_DIR / "Earnings_Transcripts",
    "presentations": OUTPUT_DIR / "Earnings_Presentations",
}

LOG_FILE  = OUTPUT_DIR / "download_log.txt"
log_lines: list = []

# ─── Pre-computed 10-Q manifest (from EDGAR submissions API) ──────────────────
# All Delta 10-Q filings 2016-2025, sorted chronologically.
# Format: (report_date, filing_date, accession_number, primary_document)
DELTA_10Q_MANIFEST = [
    ("2016-03-31", "2016-04-14", "0000027904-16-000015", "dal331201610q.htm"),
    ("2016-06-30", "2016-07-15", "0000027904-16-000026", "dal630201610q.htm"),
    ("2016-09-30", "2016-10-13", "0000027904-16-000029", "dal930201610q.htm"),
    ("2017-03-31", "2017-04-12", "0000027904-17-000008", "dal331201710q.htm"),
    ("2017-06-30", "2017-07-13", "0000027904-17-000013", "dal630201710q.htm"),
    ("2017-09-30", "2017-10-11", "0000027904-17-000017", "dal930201710q.htm"),
    ("2018-03-31", "2018-04-12", "0000027904-18-000013", "dal331201810q.htm"),
    ("2018-06-30", "2018-07-12", "0000027904-18-000016", "dal630201810q.htm"),
    ("2018-09-30", "2018-10-11", "0000027904-18-000020", "dal930201810q.htm"),
    ("2019-03-31", "2019-04-10", "0000027904-19-000005", "dal331201910q.htm"),
    ("2019-06-30", "2019-07-11", "0000027904-19-000008", "dal630201910q.htm"),
    ("2019-09-30", "2019-10-10", "0000027904-19-000012", "dal930201910q.htm"),
    ("2020-03-31", "2020-04-22", "0000027904-20-000007", "dal-20200331.htm"),
    ("2020-06-30", "2020-07-15", "0000027904-20-000010", "dal-20200630.htm"),
    ("2020-09-30", "2020-10-14", "0000027904-20-000013", "dal-20200930.htm"),
    ("2021-03-31", "2021-04-15", "0000027904-21-000006", "dal-20210331.htm"),
    ("2021-06-30", "2021-07-14", "0000027904-21-000009", "dal-20210630.htm"),
    ("2021-09-30", "2021-10-13", "0000027904-21-000012", "dal-20210930.htm"),
    ("2022-03-31", "2022-04-13", "0000027904-22-000006", "dal-20220331.htm"),
    ("2022-06-30", "2022-07-13", "0000027904-22-000010", "dal-20220630.htm"),
    ("2022-09-30", "2022-10-13", "0000027904-22-000013", "dal-20220930.htm"),
    ("2023-03-31", "2023-04-13", "0000027904-23-000006", "dal-20230331.htm"),
    ("2023-06-30", "2023-07-13", "0000027904-23-000009", "dal-20230630.htm"),
    ("2023-09-30", "2023-10-12", "0000027904-23-000013", "dal-20230930.htm"),
    ("2024-03-31", "2024-04-10", "0000027904-24-000006", "dal-20240331.htm"),
    ("2024-06-30", "2024-07-11", "0000027904-24-000009", "dal-20240630.htm"),
    ("2024-09-30", "2024-10-10", "0000027904-24-000012", "dal-20240930.htm"),
    ("2025-03-31", "2025-04-09", "0000027904-25-000007", "dal-20250331.htm"),
    ("2025-06-30", "2025-07-10", "0000027904-25-000012", "dal-20250630.htm"),
    ("2025-09-30", "2025-10-09", "0000027904-25-000020", "dal-20250930.htm"),
]

# Pre-computed earnings 8-K manifest (item 2.02 = Results of Operations)
# Format: (report_year, report_quarter, filing_date, accession_number)
# Quarter is the quarter being REPORTED (e.g., Q4 = results for Oct-Dec, filed in Jan)
DELTA_8K_EARNINGS_MANIFEST = [
    (2016, "Q1", "2016-04-15", "0001683168-16-011175"),
    (2016, "Q2", "2016-07-15", "0001683168-16-017742"),
    (2016, "Q3", "2016-10-12", "0001683168-16-022777"),
    (2016, "Q4", "2017-01-11", "0001683168-17-000596"),
    (2017, "Q1", "2017-04-12", "0001683168-17-009032"),
    (2017, "Q2", "2017-07-12", "0001683168-17-016850"),
    (2017, "Q3", "2017-10-10", "0001683168-17-022961"),
    (2017, "Q4", "2018-01-11", "0001683168-18-000493"),
    (2018, "Q1", "2018-04-12", "0001683168-18-008951"),
    (2018, "Q2", "2018-07-12", "0001683168-18-017044"),
    (2018, "Q3", "2018-10-11", "0001683168-18-023568"),
    (2018, "Q4", "2019-01-14", "0001683168-19-000491"),
    (2019, "Q1", "2019-04-10", "0001683168-19-008416"),
    (2019, "Q2", "2019-07-11", "0001683168-19-016561"),
    (2019, "Q3", "2019-10-10", "0001683168-19-022696"),
    (2019, "Q4", "2020-01-14", "0001683168-20-000590"),
    (2020, "Q1", "2020-04-22", "0001683168-20-004964"),
    (2020, "Q2", "2020-07-14", "0001683168-20-007881"),
    (2020, "Q3", "2020-10-13", "0001683168-20-012226"),
    (2020, "Q4", "2021-01-13", "0001683168-21-000297"),
    (2021, "Q1", "2021-04-15", "0001683168-21-001429"),
    (2021, "Q2", "2021-07-14", "0001683168-21-002954"),
    (2021, "Q3", "2021-10-13", "0001683168-21-004801"),
    (2021, "Q4", "2022-01-13", "0001683168-22-000238"),
    (2022, "Q1", "2022-04-13", "0001683168-22-002578"),
    (2022, "Q2", "2022-07-13", "0001683168-22-004923"),
    (2022, "Q3", "2022-10-13", "0001683168-22-006867"),
    (2022, "Q4", "2023-01-13", "0001683168-23-000172"),
    (2023, "Q1", "2023-04-13", "0001683168-23-002326"),
    (2023, "Q2", "2023-07-13", "0001683168-23-004828"),
    (2023, "Q3", "2023-10-12", "0001683168-23-007072"),
    (2023, "Q4", "2024-01-12", "0001683168-24-000244"),
    (2024, "Q1", "2024-04-10", "0001683168-24-002249"),
    (2024, "Q2", "2024-07-11", "0001683168-24-004743"),
    (2024, "Q3", "2024-10-10", "0001683168-24-007033"),
    (2024, "Q4", "2025-01-10", "0001683168-25-000182"),
    (2025, "Q1", "2025-04-09", "0001683168-25-002353"),
    (2025, "Q2", "2025-07-10", "0001683168-25-005002"),
    (2025, "Q3", "2025-10-09", "0000027904-25-000018"),
]

# Presentations available directly from Delta's IR CDN (from RSS feed)
# Format: (year, quarter_or_None, title, pdf_url)
DELTA_CDN_PRESENTATIONS = [
    (2023, "Q1", "Investor Presentation Q1 2023",
     "https://s2.q4cdn.com/181345880/files/doc_presentations/2023/02/1Q-2023-Delta-Standing-Presentation_vF_.pdf"),
    (2022, "Q2", "Investor Update Q2 2022",
     "https://s2.q4cdn.com/181345880/files/doc_downloads/2022/05/Q2-Investor-Update_4pm-5.31.pdf"),
    (2021, "Q2", "Investor Presentation Q2 2021",
     "https://s2.q4cdn.com/181345880/files/doc_presentations/2021/07/DAL-2Q-Presentation-Final.pdf"),
]


# ─── Utilities ────────────────────────────────────────────────────────────────

def log(msg):
    log_lines.append(str(msg))
    print(msg)


def save_log():
    LOG_FILE.write_text("\n".join(log_lines), encoding="utf-8")


def create_directories():
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)


def quarter_label(report_date_str: str) -> str:
    """Map report period end date → Q1/Q2/Q3/Q4."""
    m = datetime.strptime(report_date_str, "%Y-%m-%d").month
    return {1: "Q1", 2: "Q1", 3: "Q1",
            4: "Q2", 5: "Q2", 6: "Q2",
            7: "Q3", 8: "Q3", 9: "Q3",
            10: "Q4", 11: "Q4", 12: "Q4"}[m]


def year_label(report_date_str: str) -> int:
    return datetime.strptime(report_date_str, "%Y-%m-%d").year


def already_have(directory: Path, base_name: str) -> bool:
    return bool(list(directory.glob(f"{base_name}.*")))


def download_file(url: str, filepath: Path, description: str = "",
                  headers: dict = None, delay_after: float = 0) -> bool:
    """Download url → filepath with retry.  Returns True on success."""
    if headers is None:
        headers = EDGAR_HEADERS
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=120)
            if resp.status_code in (429, 503, 403):
                wait = max(60, 30 * (2 ** attempt))
                log(f"    Rate limited ({resp.status_code}), waiting {wait}s …")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    fh.write(chunk)
            size_kb = filepath.stat().st_size // 1024
            log(f"    ✓ {filepath.name}  ({size_kb} KB)")
            if delay_after:
                time.sleep(delay_after)
            return True
        except Exception as exc:
            wait = 15 * (2 ** attempt)
            log(f"    Attempt {attempt+1} failed ({exc}), wait {wait}s …")
            time.sleep(wait)
    log(f"    ✗ Giving up: {description}")
    return False


# ─── 1. 10-Q Filings ─────────────────────────────────────────────────────────

def download_10q_filings():
    log("\n" + "=" * 60)
    log("=== 1. Downloading 10-Q Filings from SEC EDGAR ===")
    log("=" * 60)

    ok = fail = skip = 0
    for report_date, filing_date, accession, primary_doc in DELTA_10Q_MANIFEST:
        q    = quarter_label(report_date)
        yr   = year_label(report_date)
        base = f"Delta {yr} {q} 10Q"

        if already_have(DIRS["10q"], base):
            log(f"  Already have: {base}")
            skip += 1
            continue

        acc_clean = accession.replace("-", "")
        url       = f"{EDGAR_ARCHIVES}/{DELTA_CIK}/{acc_clean}/{primary_doc}"
        ext       = Path(primary_doc).suffix or ".htm"
        filepath  = DIRS["10q"] / f"{base}{ext}"

        log(f"  Downloading: {base}  (filed {filing_date})")
        log(f"    {url}")
        time.sleep(EDGAR_DELAY_SECONDS)
        if download_file(url, filepath, base, headers=EDGAR_HEADERS):
            ok += 1
        else:
            fail += 1

    log(f"\n10-Q summary: {ok} downloaded, {skip} already existed, {fail} failed")
    return ok, fail


# ─── 2. Earnings Presentations ────────────────────────────────────────────────

IR_PRESENTATIONS_RSS = "https://ir.delta.com/rss/Presentation.aspx"


def parse_presentations_rss():
    try:
        resp = requests.get(IR_PRESENTATIONS_RSS, headers=WEB_HEADERS, timeout=30)
        resp.raise_for_status()
        content = resp.text
    except Exception as exc:
        log(f"  Could not fetch presentations RSS: {exc}")
        return []

    items   = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
    results = []
    for item in items:
        title_m = re.search(r"<title>(.*?)</title>", item)
        date_m  = re.search(r"<pubDate>(.*?)</pubDate>", item)
        url_m   = re.search(
            r'href=["\']?(https?://[^"\'>\s]+\.pdf)',
            html.unescape(item), re.I
        )
        if title_m and url_m:
            pdf_url = url_m.group(1).replace("http://", "https://")
            results.append((
                html.unescape(title_m.group(1)).strip(),
                date_m.group(1).strip() if date_m else "",
                pdf_url,
            ))
    return results


def extract_year_quarter_from_text(text: str, url: str = ""):
    combined = f"{text} {url}"
    ym = re.search(r"\b(20\d{2})\b", combined)
    year = int(ym.group(1)) if ym else None
    patterns = [
        (r"\bQ1\b|first.quarter|1Q\b|march.quarter", "Q1"),
        (r"\bQ2\b|second.quarter|2Q\b|june.quarter",  "Q2"),
        (r"\bQ3\b|third.quarter|3Q\b|sept",            "Q3"),
        (r"\bQ4\b|fourth.quarter|4Q\b|december.quarter|full.year|annual", "Q4"),
    ]
    quarter = None
    for pat, label in patterns:
        if re.search(pat, combined, re.I):
            quarter = label
            break
    return year, quarter


def download_presentations():
    log("\n" + "=" * 60)
    log("=== 2. Downloading Earnings Presentations ===")
    log("=" * 60)

    ok = fail = skip = 0
    seen = set()

    # ── Source A: Pre-computed CDN URLs ───────────────────────────────────────
    log("\n  Source A: Pre-known CDN presentations …")
    for yr, q, title, pdf_url in DELTA_CDN_PRESENTATIONS:
        if yr < START_YEAR or yr > END_YEAR:
            continue
        base = f"Delta {yr} {q} Earnings Presentation"
        if already_have(DIRS["presentations"], base):
            log(f"  Already have: {base}")
            skip += 1
            seen.add(pdf_url)
            continue
        filepath = DIRS["presentations"] / f"{base}.pdf"
        log(f"  Downloading: {base}")
        time.sleep(1)
        if download_file(pdf_url, filepath, base, headers=WEB_HEADERS):
            ok += 1
        else:
            fail += 1
        seen.add(pdf_url)

    # ── Source B: IR RSS feed ─────────────────────────────────────────────────
    log("\n  Source B: IR Presentations RSS feed …")
    rss_items = parse_presentations_rss()
    for title, pub_date, pdf_url in rss_items:
        if pdf_url in seen:
            continue
        seen.add(pdf_url)

        yr, q = extract_year_quarter_from_text(title, pdf_url)
        if yr and q and START_YEAR <= yr <= END_YEAR:
            base = f"Delta {yr} {q} Earnings Presentation"
        else:
            safe = re.sub(r'[<>:"/\\|?*]', '', title)[:70]
            base = f"Delta IR Presentation - {pub_date[:11]} - {safe}"

        if already_have(DIRS["presentations"], base):
            log(f"  Already have: {base}")
            skip += 1
            continue

        ext      = Path(urllib.parse.urlparse(pdf_url).path).suffix or ".pdf"
        filepath = DIRS["presentations"] / f"{base}{ext}"
        log(f"  Downloading: {base}")
        time.sleep(1)
        if download_file(pdf_url, filepath, base, headers=WEB_HEADERS):
            ok += 1
        else:
            fail += 1

    # ── Source C: EDGAR 8-K exhibit folders ───────────────────────────────────
    log("\n  Source C: EDGAR 8-K exhibit folders (EX-99.2 presentations) …")
    for yr, q, filing_date, accession in DELTA_8K_EARNINGS_MANIFEST:
        if yr < START_YEAR or yr > END_YEAR:
            continue
        base = f"Delta {yr} {q} Earnings Presentation"
        if already_have(DIRS["presentations"], base):
            log(f"  Already have: {base}")
            skip += 1
            continue

        acc_clean  = accession.replace("-", "")
        folder_url = f"{EDGAR_ARCHIVES}/{DELTA_CIK}/{acc_clean}/"
        log(f"  Checking 8-K folder: {yr} {q} ({filing_date})")
        time.sleep(EDGAR_DELAY_SECONDS)

        resp = None
        for attempt in range(3):
            try:
                r = requests.get(folder_url, headers=EDGAR_HEADERS, timeout=60)
                if r.status_code in (429, 503, 403):
                    wait = 60 * (2 ** attempt)
                    log(f"    Rate limited ({r.status_code}), waiting {wait}s …")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                resp = r
                break
            except Exception as exc:
                wait = 30 * (2 ** attempt)
                log(f"    Folder request failed ({exc}), wait {wait}s …")
                time.sleep(wait)

        if not resp:
            log(f"    Could not access folder for {yr} {q}")
            fail += 1
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        presentation_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            fn   = Path(href).name.lower()
            if re.search(r"presentation|ex.?99.?2|slides|investor", fn, re.I):
                presentation_link = href
                break

        if not presentation_link:
            log(f"    No presentation exhibit found in 8-K folder for {yr} {q}")
            continue

        full_url = presentation_link if presentation_link.startswith("http") \
                   else f"https://www.sec.gov{presentation_link}"
        ext      = Path(urllib.parse.urlparse(full_url).path).suffix or ".pdf"
        filepath = DIRS["presentations"] / f"{base}{ext}"
        if download_file(full_url, filepath, base):
            ok += 1
        else:
            fail += 1

    log(f"\nPresentations summary: {ok} downloaded, {skip} already existed, {fail} failed")
    return ok, fail


# ─── 3. Earnings Transcripts (via 8-K EX-99.1 press releases) ────────────────

def download_transcripts():
    log("\n" + "=" * 60)
    log("=== 3. Downloading Earnings Transcripts ===")
    log("=== (EDGAR 8-K EX-99.1 quarterly earnings press releases) ===")
    log("=" * 60)
    log("  Note: Verbatim call transcripts require a paid service.")
    log("  Downloading the closest public equivalent: the earnings press release,")
    log("  which contains full financial tables and management commentary.\n")

    ok = fail = skip = 0

    for yr, q, filing_date, accession in DELTA_8K_EARNINGS_MANIFEST:
        if yr < START_YEAR or yr > END_YEAR:
            continue
        base = f"Delta {yr} {q} Earnings Transcript"
        if already_have(DIRS["transcripts"], base):
            log(f"  Already have: {base}")
            skip += 1
            continue

        acc_clean  = accession.replace("-", "")
        folder_url = f"{EDGAR_ARCHIVES}/{DELTA_CIK}/{acc_clean}/"
        log(f"  Fetching 8-K folder: {yr} {q} ({filing_date})")
        time.sleep(EDGAR_DELAY_SECONDS)

        resp = None
        for attempt in range(3):
            try:
                r = requests.get(folder_url, headers=EDGAR_HEADERS, timeout=60)
                if r.status_code in (429, 503, 403):
                    wait = 60 * (2 ** attempt)
                    log(f"    Rate limited ({r.status_code}), waiting {wait}s …")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                resp = r
                break
            except Exception as exc:
                wait = 30 * (2 ** attempt)
                log(f"    Folder request failed ({exc}), wait {wait}s …")
                time.sleep(wait)

        if not resp:
            log(f"    Could not access folder for {yr} {q}")
            fail += 1
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for EX-99.1 (press release) or primary 8-K document
        target_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            fn   = href.lower()
            # Prefer: ex-99.1, press release, earnings release
            if re.search(r"ex.?99.?1|press|earnings", fn, re.I) and \
               re.search(r"\.(htm|pdf|txt)$", fn, re.I):
                target_link = href
                break

        # Fallback to the 8-K primary document
        if not target_link:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r"8k|delta.*\.htm", href.lower()) and \
                   re.search(r"\.(htm|pdf)$", href.lower()):
                    target_link = href
                    break

        if not target_link:
            log(f"    No suitable document found for {yr} {q}")
            fail += 1
            continue

        full_url = target_link if target_link.startswith("http") \
                   else f"https://www.sec.gov{target_link}"
        ext      = Path(urllib.parse.urlparse(full_url).path).suffix or ".htm"
        filepath = DIRS["transcripts"] / f"{base}{ext}"
        if download_file(full_url, filepath, base):
            ok += 1
        else:
            fail += 1

    log(f"\nTranscripts summary: {ok} downloaded, {skip} already existed, {fail} failed")
    return ok, fail


# ─── Summary & manifest ───────────────────────────────────────────────────────

def write_manifest():
    """Write a CSV manifest of all expected files, their status, and download URLs."""
    manifest_path = OUTPUT_DIR / "download_manifest.csv"
    lines = ["category,year,quarter,filename,status,source_url"]

    # 10-Qs
    for report_date, filing_date, accession, primary_doc in DELTA_10Q_MANIFEST:
        q    = quarter_label(report_date)
        yr   = year_label(report_date)
        base = f"Delta {yr} {q} 10Q"
        acc_clean = accession.replace("-", "")
        url  = f"{EDGAR_ARCHIVES}/{DELTA_CIK}/{acc_clean}/{primary_doc}"
        existing = list(DIRS["10q"].glob(f"{base}.*"))
        status = "downloaded" if existing else "pending"
        lines.append(f'10-Q,{yr},{q},"{base}",{status},{url}')

    # 8-K earnings (for presentations + transcripts)
    for yr, q, filing_date, accession in DELTA_8K_EARNINGS_MANIFEST:
        acc_clean = accession.replace("-", "")
        base_pres = f"Delta {yr} {q} Earnings Presentation"
        base_trans = f"Delta {yr} {q} Earnings Transcript"
        folder_url = f"{EDGAR_ARCHIVES}/{DELTA_CIK}/{acc_clean}/"

        pres_exists  = bool(list(DIRS["presentations"].glob(f"{base_pres}.*")))
        trans_exists = bool(list(DIRS["transcripts"].glob(f"{base_trans}.*")))

        pres_status  = "downloaded" if pres_exists  else "pending"
        trans_status = "downloaded" if trans_exists else "pending"

        lines.append(f'Presentation,{yr},{q},"{base_pres}",{pres_status},{folder_url}')
        lines.append(f'Transcript,{yr},{q},"{base_trans}",{trans_status},{folder_url}')

    manifest_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"\nManifest written to: {manifest_path}")


def print_summary():
    log("\n" + "=" * 60)
    log("=== DOWNLOAD SUMMARY ===")
    log("=" * 60)
    total = 0
    for cat, d in DIRS.items():
        files = sorted(d.glob("*"))
        total += len(files)
        log(f"\n{cat.upper()} ({len(files)} files):")
        for f in files:
            log(f"  {f.name}")
    log(f"\nTotal files: {total}")
    log(f"All files stored in: {OUTPUT_DIR.resolve()}")
    log("\nTo retry failed downloads, re-run this script after ~1 hour.")
    log("EDGAR Archives blocks temporary IP-level throttling — the script")
    log("is safe to re-run and will skip files already downloaded.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("Delta Airlines Financial Reports Downloader")
    log(f"Scope: {START_YEAR}–{END_YEAR}  |  3 categories")
    log(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log("=" * 60)

    create_directories()

    # Run all three download sections
    download_10q_filings()
    download_presentations()
    download_transcripts()

    write_manifest()
    print_summary()
    save_log()
    log(f"\nLog saved to: {LOG_FILE}")


if __name__ == "__main__":
    main()
