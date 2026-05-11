#!/usr/bin/env python3
"""LSTEP friend-list and chat-history scraper.

This script opens LSTEP with Chrome/Selenium, lets the operator log in manually,
then saves friend links and chat messages into a local SQLite database.

The LSTEP DOM can change, so most CSS selectors are configurable by CLI options.
Start with the defaults, and narrow selectors if unrelated links/messages are
captured on your account screen.
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
DEFAULT_FRIEND_LINK_SELECTOR = "a[href]"
DEFAULT_NEXT_SELECTOR = (
    "a[rel='next'], button[rel='next'], .pagination a[aria-label*='次'], "
    ".pagination button[aria-label*='次'], a[aria-label='Next'], button[aria-label='Next']"
)
DEFAULT_CHAT_MESSAGE_SELECTOR = (
    "[class*='message'], [class*='Message'], [class*='chat'], [class*='Chat']"
)
DISABLED_CLASSES = ("disabled", "is-disabled", "is_disabled", "pager-disabled")


@dataclass(frozen=True)
class Friend:
    """Friend row scraped from the friend-list page."""

    name: str
    href: str


@dataclass(frozen=True)
class ChatMessage:
    """Chat message row scraped from a friend detail/chat page."""

    text: str
    sender: str | None = None
    sent_at: str | None = None


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            sender TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, message_text, sender, sent_at),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
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
            name = link.get_attribute("title") or link.get_attribute("aria-label") or href
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


def find_next_button(driver: WebDriver, next_selector: str) -> WebElement | None:
    """Find an enabled next-page control using CSS selector and text fallback."""

    candidates = driver.find_elements(By.CSS_SELECTOR, next_selector)
    candidates.extend(
        driver.find_elements(
            By.XPATH,
            "//a[contains(normalize-space(.), '次') or contains(normalize-space(.), 'Next')]"
            " | //button[contains(normalize-space(.), '次') or contains(normalize-space(.), 'Next')]",
        )
    )
    for candidate in candidates:
        try:
            if candidate.is_displayed() and candidate.is_enabled() and not element_is_disabled(candidate):
                return candidate
        except StaleElementReferenceException:
            continue
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

        next_button = find_next_button(driver, next_selector)
        if next_button is None:
            print("[friends] next page button was not found; finished friend-list collection.")
            break

        previous_url = driver.current_url
        next_button.click()
        time.sleep(wait_seconds)
        try:
            WebDriverWait(driver, max(3, int(wait_seconds * 4))).until(
                lambda d: d.current_url != previous_url or scrape_friends_on_current_page(d, link_selector, include_keywords)
            )
        except TimeoutException:
            print("[friends] page transition wait timed out; continuing with current page.")
        page += 1
    return total_seen


def get_users(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """Return saved users as (id, name, href)."""

    return list(conn.execute("SELECT id, name, href FROM users ORDER BY id"))


def extract_chat_messages(driver: WebDriver, message_selector: str) -> list[ChatMessage]:
    """Extract chat messages from the current user detail/chat page."""

    messages: list[ChatMessage] = []
    seen: set[str] = set()
    for node in driver.find_elements(By.CSS_SELECTOR, message_selector):
        text = safe_text(node)
        if not text or text in seen:
            continue
        sender = node.get_attribute("data-sender") or None
        sent_at = node.get_attribute("datetime") or node.get_attribute("data-time") or None
        messages.append(ChatMessage(text=text, sender=sender, sent_at=sent_at))
        seen.add(text)
    return messages


def insert_chat_messages(
    conn: sqlite3.Connection,
    user_id: int,
    messages: Iterable[ChatMessage],
) -> int:
    """Insert chat messages for one user and return inserted/ignored count."""

    now = utc_now_iso()
    count = 0
    for message in messages:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_messages
                (user_id, message_text, sender, sent_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, message.text, message.sender, message.sent_at, now),
        )
        count += 1
    conn.commit()
    return count


def collect_chat_histories(
    driver: WebDriver,
    conn: sqlite3.Connection,
    message_selector: str,
    wait_seconds: float,
    max_users: int | None,
) -> int:
    """Open each saved friend URL and persist visible chat messages."""

    total = 0
    users = get_users(conn)
    if max_users is not None:
        users = users[:max_users]

    for index, (user_id, name, href) in enumerate(users, start=1):
        print(f"[chat] ({index}/{len(users)}) open user_id={user_id} name={name} href={href}")
        driver.get(href)
        time.sleep(wait_seconds)
        messages = extract_chat_messages(driver, message_selector)
        saved = insert_chat_messages(conn, user_id, messages)
        total += saved
        print(f"[chat] user_id={user_id} saved_or_ignored={saved}")
    return total


def parse_keywords(raw_keywords: str) -> tuple[str, ...]:
    """Parse comma-separated href include keywords."""

    return tuple(keyword.strip() for keyword in raw_keywords.split(",") if keyword.strip())


def parse_args() -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(
        description="Scrape LSTEP friend links and visible chat histories into SQLite."
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
        default="friend,chat,talk,user,customer",
        help="Comma-separated keywords that must appear in friend hrefs. Empty means all links.",
    )
    parser.add_argument(
        "--next-selector",
        default=DEFAULT_NEXT_SELECTOR,
        help="CSS selector for the next-page control.",
    )
    parser.add_argument(
        "--chat-message-selector",
        default=DEFAULT_CHAT_MESSAGE_SELECTOR,
        help="CSS selector for chat message containers on a user detail/chat page.",
    )
    parser.add_argument("--wait-seconds", type=float, default=1.5, help="Wait after page changes.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit friend-list pages for testing.")
    parser.add_argument("--max-users", type=int, default=None, help="Limit chat-history users for testing.")
    parser.add_argument("--skip-chat", action="store_true", help="Collect only users, not chat histories.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless after login if possible.")
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Chrome user-data-dir path for keeping a persistent login session.",
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
            input(
                "LSTEPにログインし、友だちリスト画面を開いたらEnterを押してください..."
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

            if not args.skip_chat:
                input(
                    "チャット履歴の取得を開始します。必要に応じて画面状態を整えてEnterを押してください..."
                )
                total_messages = collect_chat_histories(
                    driver=driver,
                    conn=conn,
                    message_selector=args.chat_message_selector,
                    wait_seconds=args.wait_seconds,
                    max_users=args.max_users,
                )
                print(f"[chat] total saved_or_ignored rows: {total_messages}")
        finally:
            driver.quit()
    print(f"Done. DB: {db_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
