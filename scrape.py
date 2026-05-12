"""
TwitchTracker top streamers scraper — fetches /channels/ranking page.

Page shows the top streamers ranked by Hours Watched in the last 30 days.
Each TwitchTracker page shows 50 channels. We try pages 1 and 2 to get 100.

Columns (11 per row):
  [0] rank          ("#1")
  [1] avatar        (<a href="/slug"><img src="..."></a>)
  [2] name+link     (<a href="/slug">Display Name</a>)
  [3] avg_viewers   (last 30d)
  [4] time_streamed (last 30d, in hours)
  [5] peak_viewers  (all-time)
  [6] hours_watched (last 30d, compact format like "10.2M")
  [7] rank_value    (duplicate of #0)
  [8] followers_gained (last 30d, with +)
  [9] total_followers
  [10] total_views

Defensive features:
  - Filters ad rows (<tr><td colspan="11">)
  - Handles "--" as null
  - Expands compact numbers ("10.2M" -> 10_200_000)
  - Stops early if pagination returns empty
  - Validates >= 30 rows before overwriting JSON
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

PAGES_PER_LIST = 2  # 50 channels per page
DELAY_BETWEEN_PAGES_MS = 3000
BASE_URL = "https://twitchtracker.com/channels/ranking"

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "top-streamers.json"

MIN_ROWS_FOR_VALID = 30  # Lower than subs scraper since first page already has 50
MIN_TOP_HOURS_FOR_VALID = 1_000_000  # Top channel should have >= 1M hours watched


# ---------- number parsing ----------

def parse_int(text):
    """Parse a plain integer like '40,068'. Returns None for '--', '?', empty."""
    if text is None:
        return None
    text = text.strip().replace(",", "").replace("\u00a0", "")
    if not text or text in ("?", "-", "--", "—", "N/A"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_compact(text):
    """
    Parse compact numbers like '10.2M', '109K', '2.25M' into integers.
    Returns None for '--' or unparseable values.
    """
    if text is None:
        return None
    text = text.strip().replace(",", "").replace("\u00a0", "").replace("+", "")
    if not text or text in ("?", "-", "--", "—", "N/A"):
        return None

    # Match a number (with optional decimal) followed by optional K/M/B suffix
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMB]?)$", text, re.IGNORECASE)
    if not m:
        # Fall back to plain int parse (no suffix, may have decimals — treat as int)
        try:
            return int(float(text))
        except ValueError:
            return None

    num = float(m.group(1))
    suffix = m.group(2).upper()
    multipliers = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(num * multipliers.get(suffix, 1))


def parse_float(text):
    """Parse '253.5' into 253.5. Returns None on failure."""
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if not text or text in ("--", "—", "?", "-", "N/A"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_rank(text):
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def slug_from_href(href):
    if not href:
        return None
    parts = [p for p in href.strip("/").split("/") if p]
    return parts[0] if parts else None


def page_url(page_num):
    return BASE_URL if page_num <= 1 else f"{BASE_URL}?page={page_num}"


# ---------- row parsing ----------

def parse_row(cells):
    """Parse one row of the channels table."""
    # Skip ad rows
    if len(cells) <= 1:
        return None
    first_colspan = cells[0].get_attribute("colspan")
    if first_colspan and int(first_colspan) > 1:
        return None
    if len(cells) < 7:  # Need at least rank, avatar, name, viewers, streamed, peak, hours
        return None

    rank = parse_rank(cells[0].inner_text())
    if not rank:
        return None

    # Avatar (cell 1)
    avatar_url = None
    slug = None
    avatar_link = cells[1].query_selector("a")
    if avatar_link:
        slug = slug_from_href(avatar_link.get_attribute("href"))
        img = avatar_link.query_selector("img")
        if img:
            avatar_url = img.get_attribute("src") or img.get_attribute("data-src")

    # Name (cell 2)
    name_link = cells[2].query_selector("a")
    name = name_link.inner_text().strip() if name_link else cells[2].inner_text().strip()
    if not slug and name_link:
        slug = slug_from_href(name_link.get_attribute("href"))
    if not name:
        return None

    # Time Streamed: pull just the number part (before the <br>hours)
    time_streamed_text = cells[4].inner_text() if len(cells) >= 5 else ""
    # The cell looks like "253.5\nhours" — first line is the number
    time_streamed_first_line = time_streamed_text.split("\n")[0].strip()

    return {
        "rank":             rank,
        "name":             name,
        "slug":             slug,
        "url":              f"https://twitchtracker.com/{slug}" if slug else None,
        "avatar":           avatar_url,
        "avg_viewers":      parse_int(cells[3].inner_text()) if len(cells) >= 4 else None,
        "time_streamed":    parse_float(time_streamed_first_line),
        "peak_viewers":     parse_int(cells[5].inner_text()) if len(cells) >= 6 else None,
        "hours_watched":    parse_compact(cells[6].inner_text()) if len(cells) >= 7 else None,
        "followers_gained": parse_compact(cells[8].inner_text()) if len(cells) >= 9 else None,
        "total_followers":  parse_compact(cells[9].inner_text()) if len(cells) >= 10 else None,
        "total_views":      parse_compact(cells[10].inner_text()) if len(cells) >= 11 else None,
    }


# ---------- page-level ----------

def diagnose_table(page: Page, page_num: int):
    try:
        rows = page.query_selector_all("table#channels tbody tr")
        print(f"   [diag] total tbody rows: {len(rows)}", flush=True)
        for i, row in enumerate(rows[:2]):
            cells = row.query_selector_all("td")
            print(f"   [diag] row {i} cell count: {len(cells)}", flush=True)
            for j, cell in enumerate(cells[:11]):
                txt = (cell.inner_text() or '').strip()[:40].replace("\n", " | ")
                print(f"     td[{j}] {txt!r}", flush=True)
        debug = Path(__file__).resolve().parent / f"debug_streamers_p{page_num}.png"
        page.screenshot(path=str(debug), full_page=True)
        print(f"   [diag] screenshot: {debug}", flush=True)
    except Exception as e:
        print(f"   [diag] failed: {e}", flush=True)


def scrape_one_page(page: Page, page_num: int) -> list[dict]:
    url = page_url(page_num)
    print(f"[streamers] page {page_num}: {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("table#channels tbody tr", timeout=30_000)
    except Exception:
        print(f"[streamers] page {page_num}: ! No #channels table found.", flush=True)
        diagnose_table(page, page_num)
        return []

    rows_el = page.query_selector_all("table#channels tbody tr")
    parsed = []
    for row in rows_el:
        cells = row.query_selector_all("td")
        result = parse_row(cells)
        if result is not None:
            parsed.append(result)

    print(f"[streamers] page {page_num}: parsed {len(parsed)} rows", flush=True)
    if not parsed:
        diagnose_table(page, page_num)
    return parsed


def scrape_all() -> list[dict]:
    all_rows = []
    seen_ranks = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()

        for page_num in range(1, PAGES_PER_LIST + 1):
            try:
                rows = scrape_one_page(page, page_num)
            except Exception as e:
                print(f"[streamers] page {page_num}: ! exception: {e}", flush=True)
                rows = []

            if not rows:
                print(f"[streamers] page {page_num} empty; stopping.", flush=True)
                break

            for r in rows:
                if r["rank"] in seen_ranks:
                    continue
                seen_ranks.add(r["rank"])
                all_rows.append(r)

            if page_num < PAGES_PER_LIST:
                page.wait_for_timeout(DELAY_BETWEEN_PAGES_MS)

        browser.close()

    print(f"[streamers] TOTAL: {len(all_rows)} unique channels", flush=True)
    return all_rows


def is_valid(channels):
    if len(channels) < MIN_ROWS_FOR_VALID:
        return False, f"only {len(channels)} rows (need >= {MIN_ROWS_FOR_VALID})"
    top_hours = channels[0].get("hours_watched") or 0
    if top_hours < MIN_TOP_HOURS_FOR_VALID:
        return False, f"top channel has {top_hours} hours_watched (need >= {MIN_TOP_HOURS_FOR_VALID})"
    return True, "ok"


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    fresh = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": "twitchtracker.com",
        "channels": scrape_all(),
    }

    ok, reason = is_valid(fresh["channels"])
    if not ok:
        print(f"\n! Validation failed: {reason}", flush=True)
        if OUTPUT_PATH.exists():
            print("Keeping previous top-streamers.json untouched.", flush=True)
            return 1
        else:
            print("No previous file exists; writing what we have anyway.", flush=True)

    OUTPUT_PATH.write_text(json.dumps(fresh, indent=2, ensure_ascii=False))
    print(f"\nDone. Wrote {len(fresh['channels'])} channels to {OUTPUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
