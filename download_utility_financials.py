#!/usr/bin/env python3
"""
Utility Companies Financial Reports Downloader
===============================================
Downloads financial reports for the last 3 years (2023–2025) for:

  Duke Energy, PG&E, AEP, Exelon, Southern Company,
  Edison International, Entergy, Sempra, CenterPoint Energy

Document types collected:
  1. 10-K Annual Filings        — from SEC EDGAR
  2. 10-Q Quarterly Filings     — from SEC EDGAR
  3. Earnings Presentations      — from EDGAR 8-K EX-99.2 exhibits
  4. Earnings Press Releases     — from EDGAR 8-K EX-99.1 exhibits
     (NOTE: Verbatim call transcripts require a paid service such as
      Capital IQ, Bloomberg, or Seeking Alpha Premium and cannot be
      downloaded automatically.  The earnings press release is the
      closest publicly-available substitute — it contains full financial
      tables and management's prepared commentary.)

EDGAR rate-limiting:
  SEC EDGAR enforces ~10 req/s with additional IP-level throttling.
  The script uses conservative delays and retries automatically.
  If you receive persistent 503/403 errors, wait 10–60 minutes and
  re-run.  Already-downloaded files are skipped automatically.

Usage:
  pip install requests beautifulsoup4
  python download_utility_financials.py [--company TICKER] [--type 10k|10q|press|presentation]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─── Configuration ─────────────────────────────────────────────────────────────

START_YEAR = 2023
END_YEAR   = 2025

OUTPUT_DIR = Path("utility_financial_reports")

EDGAR_BASE      = "https://data.sec.gov"
EDGAR_ARCHIVES  = "https://www.sec.gov/Archives/edgar/data"
EDGAR_SEARCH    = "https://efts.sec.gov/LATEST/search-index"

EDGAR_HEADERS = {
    "User-Agent": "Utility Financial Research downloader@financialresearch.example.com",
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

# Delay between EDGAR Archives requests (conservative to avoid throttling)
EDGAR_DELAY = 12   # seconds between filing downloads
FOLDER_DELAY = 8   # seconds between folder-listing requests

# ─── Company Registry ──────────────────────────────────────────────────────────
# CIK numbers sourced from SEC EDGAR company search.
# Each entry: ticker → (display_name, cik_string_no_padding)

COMPANIES = {
    "DUK":  ("Duke Energy",             "1326160"),
    "PCG":  ("PG&E",                    "1004980"),   # PG&E Corp
    "AEP":  ("American Electric Power", "4904"),
    "EXC":  ("Exelon",                  "1109357"),
    "SO":   ("Southern Company",        "92122"),
    "EIX":  ("Edison International",    "827052"),
    "ETR":  ("Entergy",                 "65984"),
    "SRE":  ("Sempra",                  "1032208"),
    "CNP":  ("CenterPoint Energy",      "1130310"),
}

# ─── Logging ───────────────────────────────────────────────────────────────────

log_lines: list = []


def log(msg: str):
    log_lines.append(str(msg))
    print(msg)


def save_log(path: Path):
    path.write_text("\n".join(log_lines), encoding="utf-8")


# ─── Filesystem helpers ────────────────────────────────────────────────────────

def company_dirs(ticker: str) -> dict:
    base = OUTPUT_DIR / ticker
    dirs = {
        "10k":          base / "10K_Filings",
        "10q":          base / "10Q_Filings",
        "presentations": base / "Earnings_Presentations",
        "press":        base / "Earnings_Press_Releases",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def already_have(directory: Path, base_name: str) -> bool:
    return bool(list(directory.glob(f"{base_name}.*")))


# ─── HTTP helpers ──────────────────────────────────────────────────────────────

def get_json(url: str, retries: int = 5) -> dict | None:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
            if r.status_code in (429, 503):
                wait = 60 * (2 ** attempt)
                log(f"    Rate limited ({r.status_code}), waiting {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            wait = 15 * (2 ** attempt)
            log(f"    get_json attempt {attempt+1} failed ({exc}), wait {wait}s …")
            time.sleep(wait)
    return None


def get_html(url: str, headers: dict = None, retries: int = 4) -> str | None:
    if headers is None:
        headers = EDGAR_HEADERS
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code in (429, 503, 403):
                wait = 60 * (2 ** attempt)
                log(f"    Rate limited ({r.status_code}), waiting {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except Exception as exc:
            wait = 15 * (2 ** attempt)
            log(f"    get_html attempt {attempt+1} failed ({exc}), wait {wait}s …")
            time.sleep(wait)
    return None


def download_file(url: str, filepath: Path, description: str = "",
                  headers: dict = None, delay_after: float = 0) -> bool:
    if headers is None:
        headers = EDGAR_HEADERS
    for attempt in range(5):
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=120)
            if r.status_code in (429, 503, 403):
                wait = max(60, 30 * (2 ** attempt))
                log(f"    Rate limited ({r.status_code}), waiting {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in r.iter_content(chunk_size=32768):
                    fh.write(chunk)
            size_kb = filepath.stat().st_size // 1024
            log(f"    ✓  {filepath.name}  ({size_kb} KB)")
            if delay_after:
                time.sleep(delay_after)
            return True
        except Exception as exc:
            wait = 15 * (2 ** attempt)
            log(f"    Attempt {attempt+1} failed ({exc}), wait {wait}s …")
            time.sleep(wait)
    log(f"    ✗  Giving up: {description}")
    return False


# ─── EDGAR submissions helper ──────────────────────────────────────────────────

def fetch_submissions(cik: str) -> dict | None:
    """
    Fetch the full filing history for a company from EDGAR submissions API.
    Handles the 'older-filings' pagination automatically.
    """
    cik_padded = cik.zfill(10)
    url = f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json"
    data = get_json(url)
    if not data:
        return None

    # The filings dict may be split into additional pages
    recent = data.get("filings", {}).get("recent", {})
    all_forms        = list(recent.get("form",            []))
    all_dates        = list(recent.get("filingDate",      []))
    all_accessions   = list(recent.get("accessionNumber", []))
    all_primary_docs = list(recent.get("primaryDocument", []))
    all_report_dates = list(recent.get("reportDate",      []))

    for extra in data.get("filings", {}).get("files", []):
        extra_url = f"{EDGAR_BASE}/submissions/{extra['name']}"
        time.sleep(1)
        extra_data = get_json(extra_url)
        if not extra_data:
            continue
        all_forms        += extra_data.get("form",            [])
        all_dates        += extra_data.get("filingDate",      [])
        all_accessions   += extra_data.get("accessionNumber", [])
        all_primary_docs += extra_data.get("primaryDocument", [])
        all_report_dates += extra_data.get("reportDate",      [])

    return {
        "form":            all_forms,
        "filingDate":      all_dates,
        "accessionNumber": all_accessions,
        "primaryDocument": all_primary_docs,
        "reportDate":      all_report_dates,
    }


def filter_filings(submissions: dict, form_type: str,
                   start_year: int, end_year: int) -> list[dict]:
    """Return list of filing dicts matching form_type within year range."""
    results = []
    forms        = submissions["form"]
    dates        = submissions["filingDate"]
    accessions   = submissions["accessionNumber"]
    primary_docs = submissions["primaryDocument"]
    report_dates = submissions["reportDate"]

    for i, form in enumerate(forms):
        if form.upper() != form_type.upper():
            continue
        filing_date = dates[i] if i < len(dates) else ""
        try:
            yr = int(filing_date[:4])
        except (ValueError, TypeError):
            continue
        if not (start_year <= yr <= end_year):
            continue
        results.append({
            "form":           form,
            "filingDate":     filing_date,
            "accessionNumber": accessions[i] if i < len(accessions) else "",
            "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
            "reportDate":     report_dates[i] if i < len(report_dates) else "",
        })
    return results


# ─── Date / label helpers ──────────────────────────────────────────────────────

def period_label(report_date: str, form_type: str) -> tuple[int, str]:
    """
    Return (year, label) where label is 'Q1'/'Q2'/'Q3'/'Annual' etc.
    Uses reportDate if available, else filingDate.
    """
    try:
        dt = datetime.strptime(report_date[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0, "Unknown"
    yr = dt.year
    mo = dt.month
    if "10-K" in form_type.upper():
        return yr, "Annual"
    # 10-Q: period end month → quarter
    q = {1: "Q1", 2: "Q1", 3: "Q1",
         4: "Q2", 5: "Q2", 6: "Q2",
         7: "Q3", 8: "Q3", 9: "Q3",
         10: "Q4", 11: "Q4", 12: "Q4"}.get(mo, "Q?")
    return yr, q


# ─── 1. 10-K and 10-Q Downloader ──────────────────────────────────────────────

def download_annual_and_quarterly(ticker: str, name: str, cik: str, dirs: dict):
    log(f"\n{'='*60}")
    log(f"  {name} ({ticker})  —  10-K + 10-Q Filings")
    log(f"{'='*60}")

    submissions = fetch_submissions(cik)
    if not submissions:
        log(f"  ERROR: Could not fetch EDGAR submissions for {name}")
        return

    ok = fail = skip = 0

    for form_type, target_dir in [("10-K", dirs["10k"]), ("10-Q", dirs["10q"])]:
        filings = filter_filings(submissions, form_type, START_YEAR, END_YEAR)
        log(f"\n  Found {len(filings)} {form_type} filings in {START_YEAR}–{END_YEAR}")

        for f in filings:
            date_key = f["reportDate"] or f["filingDate"]
            yr, label = period_label(date_key, form_type)
            base      = f"{name} {yr} {label} {form_type.replace('-', '')}"

            if already_have(target_dir, base):
                log(f"  Already have: {base}")
                skip += 1
                continue

            acc_clean = f["accessionNumber"].replace("-", "")
            primary   = f["primaryDocument"]
            url       = f"{EDGAR_ARCHIVES}/{cik}/{acc_clean}/{primary}"
            ext       = Path(primary).suffix or ".htm"
            filepath  = target_dir / f"{base}{ext}"

            log(f"  Downloading: {base}  (filed {f['filingDate']})")
            log(f"    {url}")
            time.sleep(EDGAR_DELAY)
            if download_file(url, filepath, base):
                ok += 1
            else:
                fail += 1

    log(f"\n  10-K/10-Q summary for {name}: {ok} downloaded, {skip} skipped, {fail} failed")


# ─── 8-K folder inspector ─────────────────────────────────────────────────────

def list_8k_exhibits(cik: str, accession: str) -> list[dict]:
    """
    Return list of {name, url} for all documents in an 8-K filing folder.
    """
    acc_clean  = accession.replace("-", "")
    folder_url = f"{EDGAR_ARCHIVES}/{cik}/{acc_clean}/"
    time.sleep(FOLDER_DELAY)
    html_text  = get_html(folder_url)
    if not html_text:
        return []

    soup    = BeautifulSoup(html_text, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"\.(htm|html|pdf|txt)$", href, re.I):
            continue
        full_url = href if href.startswith("http") else f"https://www.sec.gov{href}"
        results.append({"name": Path(href).name, "url": full_url})
    return results


def find_exhibit_url(exhibits: list[dict], patterns: list[str]) -> str | None:
    for pat in patterns:
        for ex in exhibits:
            if re.search(pat, ex["name"], re.I):
                return ex["url"]
    return None


# ─── 2. Earnings 8-K Press Releases + Presentations ──────────────────────────

def is_earnings_8k(filing: dict) -> bool:
    """
    Best-effort check: earnings 8-Ks are typically filed within 4 days of
    quarter-end and contain 'Results of Operations' (item 2.02).
    We rely on the filing index to confirm.  Here we accept all 8-Ks filed
    in the target year range and let the exhibit-finder weed out empties.
    """
    return True   # filter by year range is done in filter_filings()


def download_earnings_8ks(ticker: str, name: str, cik: str, dirs: dict):
    log(f"\n{'='*60}")
    log(f"  {name} ({ticker})  —  Earnings 8-K Press Releases + Presentations")
    log(f"{'='*60}")
    log("  NOTE: Verbatim call transcripts require Capital IQ / Bloomberg.")
    log("  Downloading EDGAR 8-K EX-99.1 press releases as public substitute.\n")

    submissions = fetch_submissions(cik)
    if not submissions:
        log(f"  ERROR: Could not fetch submissions for {name}")
        return

    filings = filter_filings(submissions, "8-K", START_YEAR, END_YEAR)
    log(f"  Found {len(filings)} 8-K filings in {START_YEAR}–{END_YEAR}")

    ok_press = fail_press = skip_press = 0
    ok_pres  = fail_pres  = skip_pres  = 0

    for f in filings:
        date_key = f["reportDate"] or f["filingDate"]
        yr, _    = period_label(date_key, "8-K")

        # We'll derive a quarter label from the filing date (report date often blank for 8-Ks)
        try:
            filing_dt = datetime.strptime(f["filingDate"][:10], "%Y-%m-%d")
        except ValueError:
            continue
        mo    = filing_dt.month
        q_map = {1: "Q4", 2: "Q4", 3: "Q4",   # Q4 results typically filed Jan–Mar
                 4: "Q1", 5: "Q1", 6: "Q1",
                 7: "Q2", 8: "Q2", 9: "Q2",
                 10: "Q3", 11: "Q3", 12: "Q3"}
        # Year adjustment: Q4 results filed in Jan/Feb belong to prior year
        report_yr = filing_dt.year
        if mo <= 3:
            report_yr -= 1

        q_label    = q_map[mo]
        base_press = f"{name} {report_yr} {q_label} Earnings Press Release"
        base_pres  = f"{name} {report_yr} {q_label} Earnings Presentation"

        press_exists = already_have(dirs["press"], base_press)
        pres_exists  = already_have(dirs["presentations"], base_pres)

        if press_exists and pres_exists:
            skip_press += 1
            skip_pres  += 1
            continue

        log(f"  Checking 8-K folder: {name} {report_yr} {q_label} (filed {f['filingDate']})")
        exhibits = list_8k_exhibits(cik, f["accessionNumber"])
        if not exhibits:
            log(f"    No exhibits found")
            continue

        # ── Press Release (EX-99.1) ──────────────────────────────────────────
        if not press_exists:
            press_url = find_exhibit_url(exhibits, [
                r"ex.?99.?1", r"press", r"earnings.*release", r"release.*earnings",
                r"ex991", r"er\d", r"earningsrelease",
            ])
            if press_url:
                ext      = Path(urllib.parse.urlparse(press_url).path).suffix or ".htm"
                filepath = dirs["press"] / f"{base_press}{ext}"
                log(f"  Downloading press release: {base_press}")
                if download_file(press_url, filepath, base_press, delay_after=2):
                    ok_press += 1
                else:
                    fail_press += 1
            else:
                log(f"    No press release exhibit found for {name} {report_yr} {q_label}")
                fail_press += 1

        # ── Earnings Presentation (EX-99.2 / slides) ──────────────────────────
        if not pres_exists:
            pres_url = find_exhibit_url(exhibits, [
                r"ex.?99.?2", r"ex992", r"presentation", r"slides", r"investor.*update",
                r"earnings.*pres", r"pres.*earnings",
            ])
            if pres_url:
                ext      = Path(urllib.parse.urlparse(pres_url).path).suffix or ".pdf"
                filepath = dirs["presentations"] / f"{base_pres}{ext}"
                log(f"  Downloading presentation: {base_pres}")
                if download_file(pres_url, filepath, base_pres, delay_after=2):
                    ok_pres += 1
                else:
                    fail_pres += 1
            else:
                log(f"    No presentation exhibit found for {name} {report_yr} {q_label}")
                # Not all companies file presentations as 8-K exhibits
                skip_pres += 1

    log(f"\n  Press releases: {ok_press} downloaded, {skip_press} skipped, {fail_press} failed")
    log(f"  Presentations:  {ok_pres} downloaded, {skip_pres} skipped, {fail_pres} failed")


# ─── Manifest writer ───────────────────────────────────────────────────────────

def write_manifest():
    manifest_path = OUTPUT_DIR / "download_manifest.csv"
    lines = ["ticker,company,category,year,label,filename,status"]

    for ticker, (name, _cik) in COMPANIES.items():
        base = OUTPUT_DIR / ticker
        for cat_key, cat_label in [
            ("10K_Filings", "10-K"),
            ("10Q_Filings", "10-Q"),
            ("Earnings_Presentations", "Presentation"),
            ("Earnings_Press_Releases", "Press Release"),
        ]:
            cat_dir = base / cat_key
            if not cat_dir.exists():
                continue
            for f in sorted(cat_dir.glob("*")):
                lines.append(f'{ticker},"{name}",{cat_label},,, "{f.name}",downloaded')

    manifest_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"\nManifest written → {manifest_path}")


# ─── Summary printer ───────────────────────────────────────────────────────────

def print_summary():
    log("\n" + "=" * 60)
    log("=== DOWNLOAD SUMMARY ===")
    log("=" * 60)
    total = 0
    for ticker, (name, _) in COMPANIES.items():
        base = OUTPUT_DIR / ticker
        if not base.exists():
            continue
        company_files = list(base.rglob("*"))
        company_files = [f for f in company_files if f.is_file()]
        total += len(company_files)
        log(f"\n  {name} ({ticker}): {len(company_files)} files")
        for cat_dir in sorted(base.iterdir()):
            if cat_dir.is_dir():
                files = list(cat_dir.glob("*"))
                log(f"    {cat_dir.name}: {len(files)} files")

    log(f"\nTotal files downloaded: {total}")
    log(f"Output directory: {OUTPUT_DIR.resolve()}")
    log("\n─── Capital IQ Transcript Note ───────────────────────────────")
    log("  Verbatim earnings call transcripts are NOT publicly available.")
    log("  This script downloaded the EDGAR 8-K EX-99.1 earnings press")
    log("  releases as the closest public substitute.  To get verbatim")
    log("  transcripts, download them manually from Capital IQ:")
    log("    Transcripts → Company → Search → filter by date & event type")
    log("──────────────────────────────────────────────────────────────")


# ─── Argument parsing ─────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Download utility company financial reports from SEC EDGAR"
    )
    p.add_argument(
        "--company", metavar="TICKER",
        help="Run for a single company ticker (e.g. DUK). Omit for all companies."
    )
    p.add_argument(
        "--type", dest="doc_type",
        choices=["10k", "10q", "press", "presentation", "all"],
        default="all",
        help="Document type to download (default: all)"
    )
    return p.parse_args()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTPUT_DIR / "download_log.txt"

    log("=" * 60)
    log("Utility Companies Financial Reports Downloader")
    log(f"Scope: {START_YEAR}–{END_YEAR}  |  9 companies  |  4 document types")
    log(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    log("=" * 60)

    companies_to_run = COMPANIES.items()
    if args.company:
        ticker_upper = args.company.upper()
        if ticker_upper not in COMPANIES:
            log(f"ERROR: Unknown ticker '{args.company}'. "
                f"Valid tickers: {', '.join(COMPANIES)}")
            sys.exit(1)
        companies_to_run = [(ticker_upper, COMPANIES[ticker_upper])]

    for ticker, (name, cik) in companies_to_run:
        log(f"\n{'#'*60}")
        log(f"# Processing: {name} ({ticker})  CIK={cik}")
        log(f"{'#'*60}")

        dirs = company_dirs(ticker)

        if args.doc_type in ("10k", "10q", "all"):
            download_annual_and_quarterly(ticker, name, cik, dirs)
            time.sleep(3)

        if args.doc_type in ("press", "presentation", "all"):
            download_earnings_8ks(ticker, name, cik, dirs)
            time.sleep(3)

    write_manifest()
    print_summary()
    save_log(log_path)
    log(f"\nLog saved → {log_path}")
    log("Re-running the script is safe — already-downloaded files are skipped.")


if __name__ == "__main__":
    main()
