#!/usr/bin/env python3
"""LSTEP friend-list href scraper.

This script opens LSTEP with Chrome/Selenium, lets the operator log in manually,
then saves friend hrefs from every paginated friend-list page into a local
SQLite database. It does not open each friend detail page while collecting hrefs.

The LSTEP DOM can change, so most CSS selectors are configurable by CLI options.
Start with the defaults, and narrow selectors if unrelated links are captured
on your account screen.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

LOGIN_URL = "https://manager.linestep.net/account/login"
DEFAULT_DB_PATH = "lstep_chat_history.db"
DEFAULT_FRIEND_LINK_SELECTOR = "a[href*='/line/detail/']"
DEFAULT_NEXT_SELECTOR = (
    "nav[aria-label='Pagination'] a, nav[aria-label='Pagination'] button, "
    "a[rel='next'], button[rel='next'], .pagination a[aria-label*='次'], "
    ".pagination button[aria-label*='次'], a[aria-label='Next'], button[aria-label='Next']"
)
DISABLED_CLASSES = ("disabled", "is-disabled", "is_disabled", "pager-disabled")


@dataclass(frozen=True)
class Friend:
    """Friend row scraped from the friend-list page."""

    name: str
    href: str



def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for DB audit columns."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_driver(headless: bool = False, user_data_dir: str | None = None) -> WebDriver:
    """Create a Chrome WebDriver using Selenium Manager for driver discovery."""

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1440,1100")
    return webdriver.Chrome(options=options)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite DB and create required tables."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            href TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def normalize_space(value: str) -> str:
    """Collapse repeated whitespace in scraped text."""

    return " ".join(value.split())


def safe_text(element: WebElement) -> str:
    """Read visible text from an element while tolerating stale nodes."""

    try:
        return normalize_space(element.text)
    except StaleElementReferenceException:
        return ""

def closest_text(driver: WebDriver, element: WebElement, selector: str) -> str:
    """Read visible text from the closest ancestor matching selector."""

    try:
        ancestor = driver.execute_script("return arguments[0].closest(arguments[1]);", element, selector)
    except StaleElementReferenceException:
        return ""
    if not ancestor:
        return ""
    return safe_text(ancestor)


def friend_page_fingerprint(driver: WebDriver, link_selector: str) -> tuple[str, ...]:
    """Return the ordered friend hrefs currently visible on the page."""

    hrefs: list[str] = []
    for link in driver.find_elements(By.CSS_SELECTOR, link_selector):
        href = link.get_attribute("href") or ""
        if href:
            hrefs.append(absolute_href(driver, href.strip()))
    return tuple(hrefs)

def absolute_href(driver: WebDriver, href: str) -> str:
    """Convert a possibly relative href into an absolute URL."""

    return urljoin(driver.current_url, href)


def looks_like_friend_href(href: str, include_keywords: tuple[str, ...]) -> bool:
    """Return True when a link should be treated as a friend-detail link."""

    if href.startswith("javascript:") or href.startswith("#"):
        return False
    if not include_keywords:
        return True
    lower_href = href.lower()
    return any(keyword.lower() in lower_href for keyword in include_keywords)


def scrape_friends_on_current_page(
    driver: WebDriver,
    link_selector: str,
    include_keywords: tuple[str, ...],
) -> list[Friend]:
    """Collect friend href/name pairs from the current friend-list page."""

    friends: list[Friend] = []
    seen_hrefs: set[str] = set()
    for link in driver.find_elements(By.CSS_SELECTOR, link_selector):
        href = link.get_attribute("href") or ""
        href = absolute_href(driver, href.strip()) if href else ""
        if not href or href in seen_hrefs or not looks_like_friend_href(href, include_keywords):
            continue

        name = safe_text(link)
        if not name:
            name = link.get_attribute("title") or link.get_attribute("aria-label") or ""
        if not name:
            name = closest_text(driver, link, "tr")
        if not name:
            name = href
        friends.append(Friend(name=normalize_space(name), href=href))
        seen_hrefs.add(href)
    return friends


def upsert_friends(conn: sqlite3.Connection, friends: Iterable[Friend]) -> int:
    """Insert or update friends and return affected friend count."""

    count = 0
    now = utc_now_iso()
    for friend in friends:
        conn.execute(
            """
            INSERT INTO users (name, href, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(href) DO UPDATE SET
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (friend.name, friend.href, now, now),
        )
        count += 1
    conn.commit()
    return count


def element_is_disabled(element: WebElement) -> bool:
    """Detect disabled pager controls."""

    disabled_attr = (element.get_attribute("disabled") or "").lower()
    aria_disabled = (element.get_attribute("aria-disabled") or "").lower()
    classes = (element.get_attribute("class") or "").lower().split()
    href = element.get_attribute("href") or ""
    return (
        disabled_attr in {"true", "disabled"}
        or aria_disabled == "true"
        or any(disabled_class in classes for disabled_class in DISABLED_CLASSES)
        or href.startswith("javascript:void")
    )


def usable_pager_control(element: WebElement) -> bool:
    """Return True when a pager control can be clicked."""
    try:
        return element.is_displayed() and element.is_enabled() and not element_is_disabled(element)
    except StaleElementReferenceException:
        return False


def find_first_usable(elements: Iterable[WebElement]) -> WebElement | None:
    """Return the first displayed, enabled, non-disabled element."""

    for element in elements:
        if usable_pager_control(element):
            return element
    return None


def find_next_button(driver: WebDriver, next_selector: str, current_page: int) -> WebElement | None:
    """Find the pager control for the next page.

    LSTEP's current pager may render page numbers as ``nav[aria-label=Pagination]``
    buttons rather than a dedicated ``rel=next`` link. Prefer explicit next controls,
    then click the button/link whose visible number is ``current_page + 1``.
    """

    explicit_next = driver.find_elements(
        By.XPATH,
        "//a[(contains(@aria-label, '次') or contains(@aria-label, 'Next') "
        "or contains(normalize-space(.), '次') or contains(normalize-space(.), 'Next')) "
        "and not(contains(@aria-label, '前')) and not(contains(normalize-space(.), '前'))]"
        " | //button[(contains(@aria-label, '次') or contains(@aria-label, 'Next') "
        "or contains(normalize-space(.), '次') or contains(normalize-space(.), 'Next')) "
        "and not(contains(@aria-label, '前')) and not(contains(normalize-space(.), '前'))]",
    )
    explicit = find_first_usable(explicit_next)
    if explicit is not None:
        return explicit

    next_page_label = str(current_page + 1)
    numbered_next = driver.find_elements(
        By.XPATH,
        f"//nav[@aria-label='Pagination']//a[normalize-space(.)='{next_page_label}']"
        f" | //nav[@aria-label='Pagination']//button[normalize-space(.)='{next_page_label}']",
    )
    numbered = find_first_usable(numbered_next)
    if numbered is not None:
        return numbered

    if next_selector != DEFAULT_NEXT_SELECTOR:
        return find_first_usable(driver.find_elements(By.CSS_SELECTOR, next_selector))
    return None


def paginate_and_collect_friends(
    driver: WebDriver,
    conn: sqlite3.Connection,
    link_selector: str,
    next_selector: str,
    include_keywords: tuple[str, ...],
    wait_seconds: float,
    max_pages: int | None,
) -> int:
    """Scrape friend links on each paginated friend-list page."""

    total_seen = 0
    page = 1
    while True:
        friends = scrape_friends_on_current_page(driver, link_selector, include_keywords)
        saved = upsert_friends(conn, friends)
        total_seen += saved
        print(f"[friends] page={page} saved_or_updated={saved} url={driver.current_url}")

        if max_pages is not None and page >= max_pages:
            print(f"[friends] max pages reached: {max_pages}")
            break

        next_button = find_next_button(driver, next_selector, page)
        if next_button is None:
            print("[friends] next page button was not found; finished friend-list collection.")
            break

        previous_url = driver.current_url
        previous_fingerprint = friend_page_fingerprint(driver, link_selector)
        next_button.click()
        time.sleep(wait_seconds)
        try:
            WebDriverWait(driver, max(3, int(wait_seconds * 4))).until(
                lambda d: d.current_url != previous_url
                or friend_page_fingerprint(d, link_selector) != previous_fingerprint
            )
        except TimeoutException:
            print("[friends] page transition wait timed out; stopping to avoid duplicate collection.")
            break
        page += 1
    return total_seen

def wait_for_friend_list(driver: WebDriver, link_selector: str, timeout_seconds: int) -> None:
    """Wait until the operator reaches a page containing friend hrefs."""

    print(
        "LSTEPにログインし、友だちリスト画面を開いてください。"
        "リンクを検出したら自動で取得を開始します。"
    )
    WebDriverWait(driver, timeout_seconds).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, link_selector)) > 0
    )



def parse_keywords(raw_keywords: str) -> tuple[str, ...]:
    """Parse comma-separated href include keywords."""

    return tuple(keyword.strip() for keyword in raw_keywords.split(",") if keyword.strip())


def parse_args() -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(
        description="Scrape LSTEP friend hrefs from all paginated friend-list pages into SQLite."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB file path.")
    parser.add_argument("--login-url", default=LOGIN_URL, help="LSTEP login URL.")
    parser.add_argument(
        "--friend-link-selector",
        default=DEFAULT_FRIEND_LINK_SELECTOR,
        help="CSS selector for friend links on the friend-list page.",
    )
    parser.add_argument(
        "--friend-href-keywords",
        default="/line/detail/",
        help="Comma-separated keywords that must appear in friend hrefs. Empty means all links.",
    )
    parser.add_argument(
        "--next-selector",
        default=DEFAULT_NEXT_SELECTOR,
        help="CSS selector for the next-page control.",
    )
    parser.add_argument("--wait-seconds", type=float, default=1.5, help="Wait after page changes.")
    parser.add_argument(
        "--friend-list-timeout",
        type=int,
        default=900,
        help="Seconds to wait for the friend-list page after opening the login URL.",
    )
    parser.add_argument("--max-pages", type=int, default=None, help="Limit friend-list pages for testing.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless after login if possible.")
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Chrome user-data-dir path for keeping a persistent login session.",
    )
    parser.add_argument(
        "--confirm-before-friends",
        action="store_true",
        help="Require Enter before collecting friend links (disabled by default).",
    )
    return parser.parse_args()


def main() -> int:
    """Run the scraper."""

    args = parse_args()
    include_keywords = parse_keywords(args.friend_href_keywords)
    db_path = Path(args.db)

    with closing(init_db(db_path)) as conn:
        driver = create_driver(headless=args.headless, user_data_dir=args.user_data_dir)
        try:
            driver.get(args.login_url)
            if args.confirm_before_friends:
                input(
                    "LSTEPにログインし、友だちリスト画面を開いたらEnterを押してください..."
                )
            else:
                wait_for_friend_list(
                    driver=driver,
                    link_selector=args.friend_link_selector,
                    timeout_seconds=args.friend_list_timeout,
                )
            total_friends = paginate_and_collect_friends(
                driver=driver,
                conn=conn,
                link_selector=args.friend_link_selector,
                next_selector=args.next_selector,
                include_keywords=include_keywords,
                wait_seconds=args.wait_seconds,
                max_pages=args.max_pages,
            )
            print(f"[friends] total saved_or_updated rows: {total_friends}")

            print("[friends] detail pages were not opened; href collection is complete.")
        finally:
            driver.quit()
    print(f"Done. DB: {db_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
