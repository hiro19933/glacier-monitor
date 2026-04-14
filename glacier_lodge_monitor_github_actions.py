#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://secure.glaciernationalparklodges.com/booking/lodging-flex-search"
ADULTS = int(os.getenv("ADULTS", "2"))
CHILDREN = int(os.getenv("CHILDREN", "0"))
NIGHTS = int(os.getenv("NIGHTS", "1"))
RATE_CODE = os.getenv("RATE_CODE", "")
RATE_TYPE = os.getenv("RATE_TYPE", "")
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "90000"))
HEADLESS = True
STATE_FILE = Path(os.getenv("STATE_FILE", ".github/state/glacier_monitor_line_state.json"))
ENABLE_SCREENSHOTS = os.getenv("ENABLE_SCREENSHOTS", "false").lower() == "true"
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", ".github/state/screenshots"))

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO_USER_ID = os.getenv("LINE_TO_USER_ID", "")

DEFAULT_TARGET_DATES = [
    "08-08-2026",
    "08-09-2026",
    "08-10-2026",
    "08-11-2026",
    "08-12-2026",
    "08-18-2026",
]
DEFAULT_TARGET_HOTELS = [
    "Many Glacier Hotel",
    "Lake McDonald",
    "Village Inn at Apgar",
]


def parse_list_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if "\n" in raw:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


TARGET_DATES = parse_list_env("TARGET_DATES", DEFAULT_TARGET_DATES)
TARGET_HOTELS = parse_list_env("TARGET_HOTELS", DEFAULT_TARGET_HOTELS)

POSITIVE_KEYWORDS = [
    "book now",
    "select",
    "available",
    "rooms available",
    "reserve",
    "book",
    "lodging options",
]
NEGATIVE_KEYWORDS = [
    "no availability",
    "not available",
    "sold out",
    "no rooms available",
    "there are no available",
    "no results",
    "unavailable",
    "call to book",
]
HOTEL_ORDER = [
    "Many Glacier Hotel",
    "Lake McDonald Lodge",
    "Cedar Creek Lodge",
    "Swiftcurrent Motor Inn & Cabins",
    "Rising Sun Motor Inn & Cabins",
    "Village Inn at Apgar",
]
HOTEL_ALIASES = {
    "many glacier hotel": "Many Glacier Hotel",
    "many glacier": "Many Glacier Hotel",
    "lake mcdonald": "Lake McDonald Lodge",
    "lake mcdonald lodge": "Lake McDonald Lodge",
    "village inn at apgar": "Village Inn at Apgar",
    "apgar": "Village Inn at Apgar",
}


@dataclass
class MonitorState:
    availability: dict[str, bool] = field(default_factory=dict)
    last_page_hash_by_date: dict[str, int] = field(default_factory=dict)
    last_alerted_at: str | None = None


@dataclass
class HotelResult:
    date: str
    hotel: str
    available: bool | None
    reason: str


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_state() -> MonitorState:
    if not STATE_FILE.exists():
        return MonitorState()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return MonitorState(**data)
    except Exception:
        return MonitorState()


def save_state(state: MonitorState) -> None:
    ensure_parent_dir(STATE_FILE)
    STATE_FILE.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_compare(text: str) -> str:
    return normalize_space(text).lower()


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(k.lower() in text for k in keywords)


def canonical_hotel_name(name: str) -> str:
    return HOTEL_ALIASES.get(normalize_compare(name), name.strip())


def build_url(date_mmddyyyy: str) -> str:
    return (
        f"{BASE_URL}?destination=ALL"
        f"&adults={ADULTS}"
        f"&children={CHILDREN}"
        f"&rateCode={RATE_CODE}"
        f"&rateType={RATE_TYPE}"
        f"&dateFrom={date_mmddyyyy}"
        f"&nights={NIGHTS}"
    )


def to_jp_date(mmddyyyy: str) -> str:
    month, day, year = mmddyyyy.split("-")
    return f"{year}年{int(month)}月{int(day)}日"


def block_for_hotel(full_text: str, hotel_name: str) -> str:
    lower = full_text.lower()
    target = hotel_name.lower()
    start = lower.find(target)
    if start == -1:
        return ""
    end = len(full_text)
    for other in HOTEL_ORDER:
        other_lower = other.lower()
        if other_lower == target:
            continue
        idx = lower.find(other_lower, start + len(target))
        if idx != -1:
            end = min(end, idx)
    return full_text[start:end]


def classify_hotel_block(block: str) -> tuple[bool | None, str]:
    if not block:
        return None, "hotel_block_not_found"
    normalized = normalize_compare(block)
    has_positive = contains_any(normalized, POSITIVE_KEYWORDS)
    has_negative = contains_any(normalized, NEGATIVE_KEYWORDS)
    if has_positive and not has_negative:
        return True, "positive_keywords"
    if has_negative and not has_positive:
        return False, "negative_keywords"
    if has_positive and has_negative:
        return None, "mixed_keywords"
    return None, "no_keywords"


def fetch_page_text(url: str, screenshot_file: Path | None) -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=180_000)
            page.wait_for_timeout(60000)

            if screenshot_file is not None:
                ensure_parent_dir(screenshot_file)
                page.screenshot(path=str(screenshot_file), full_page=True)

            title = page.title()
            body_text = page.locator("body").inner_text(timeout=60000)
            return title, body_text
        finally:
            browser.close()


def send_line_message(text: str) -> None:
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Line-Retry-Key": str(uuid.uuid4()),
    }
    payload = {
        "to": LINE_TO_USER_ID,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=headers,
        json=payload,
        timeout=30,
    )
    print("LINE API status:", response.status_code)
    print("LINE API body:", response.text)
    response.raise_for_status()


def inspect_date(date_mmddyyyy: str, target_hotels: list[str]) -> tuple[list[HotelResult], int, str]:
    url = build_url(date_mmddyyyy)
    screenshot_file = SCREENSHOT_DIR / f"{date_mmddyyyy}.png" if ENABLE_SCREENSHOTS else None
    title, page_text = fetch_page_text(url, screenshot_file)
    page_hash = hash(normalize_compare(page_text))

    results: list[HotelResult] = []
    for hotel in target_hotels:
        canonical = canonical_hotel_name(hotel)
        block = block_for_hotel(page_text, canonical)
        available, reason = classify_hotel_block(block)
        results.append(HotelResult(date=date_mmddyyyy, hotel=canonical, available=available, reason=reason))

    print(f"[{datetime.now().isoformat(timespec='seconds')}] {date_mmddyyyy} title={title!r} hash={page_hash}")
    for r in results:
        print(f"  - {r.hotel}: available={r.available} reason={r.reason}")
    return results, page_hash, url


def make_alert_text(newly_available: list[HotelResult], urls_by_date: dict[str, str]) -> str:
    lines = ["Glacier National Park Lodges で空室の可能性があります。", ""]
    for item in newly_available:
        lines.append(f"・{to_jp_date(item.date)} / {item.hotel}")
    lines.append("")
    lines.append("確認用URL")
    used_dates: list[str] = []
    for item in newly_available:
        if item.date not in used_dates:
            used_dates.append(item.date)
            lines.append(f"・{to_jp_date(item.date)}: {urls_by_date[item.date]}")
    lines.append("")
    lines.append("サイト上の表示は変わることがあるため、必ず予約画面で最終確認してください。")
    return "\n".join(lines)


def monitor_once(state: MonitorState) -> tuple[MonitorState, bool]:
    found_new_availability = False
    for date_mmddyyyy in TARGET_DATES:
        results, page_hash, url = inspect_date(date_mmddyyyy, TARGET_HOTELS)
        state.last_page_hash_by_date[date_mmddyyyy] = page_hash
        newly_available_for_date: list[HotelResult] = []
        urls_by_date = {date_mmddyyyy: url}
        for result in results:
            key = f"{result.date}|{result.hotel}"
            previous = state.availability.get(key)
            if result.available is True and previous is not True:
                newly_available_for_date.append(result)
                state.availability[key] = True
            elif result.available is False:
                state.availability[key] = False
            else:
                state.availability.setdefault(key, False)
        if newly_available_for_date:
            send_line_message(make_alert_text(newly_available_for_date, urls_by_date))
            state.last_alerted_at = datetime.now().isoformat(timespec="seconds")
            found_new_availability = True
            print(f"LINE notification sent immediately for {date_mmddyyyy}")
    if not found_new_availability:
        print("No new availability detected")
    return state, found_new_availability


def validate_config() -> list[str]:
    problems: list[str] = []
    if not LINE_CHANNEL_ACCESS_TOKEN:
        problems.append("LINE_CHANNEL_ACCESS_TOKEN")
    if not LINE_TO_USER_ID:
        problems.append("LINE_TO_USER_ID")
    if not TARGET_DATES:
        problems.append("TARGET_DATES")
    if not TARGET_HOTELS:
        problems.append("TARGET_HOTELS")
    return problems


def main() -> int:
    problems = validate_config()
    if problems:
        print("Missing config:", ", ".join(problems), file=sys.stderr)
        return 1

    print("Starting single GitHub Actions check...")
    print(f"Dates: {', '.join(TARGET_DATES)}")
    print(f"Hotels: {', '.join(canonical_hotel_name(h) for h in TARGET_HOTELS)}")
    print(f"State file: {STATE_FILE}")

    try:
        state = load_state()
        state, _ = monitor_once(state)
        save_state(state)
        return 0
    except PlaywrightTimeoutError as e:
        print(f"Page timeout: {e}", file=sys.stderr)
        return 2
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        print(f"LINE API error: {detail}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 4

if __name__ == "__main__":
    raise SystemExit(main())
