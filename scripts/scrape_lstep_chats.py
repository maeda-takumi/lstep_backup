#!/usr/bin/env python3
"""LSTEP chat-history scraper for users already saved in SQLite.

This script reads friend detail URLs from the ``users`` table, converts the last
path segment of each ``/line/detail/<member>`` URL to a LSTEP talk URL, scrolls
that talk view back to the oldest loaded message, and saves text messages into
``chat_messages``.

The LSTEP DOM can change, so selectors and timing values are configurable by CLI
options. Message attachments are ignored; only visible text content is saved.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from scrape_lstep import DEFAULT_DB_PATH, LOGIN_URL, create_driver, utc_now_iso

DEFAULT_TALK_URL_TEMPLATE = "https://manager.linestep.net/line/visual?member={member}"
DEFAULT_CHAT_SCROLL_SELECTOR = "div.tw-min-h-0.tw-flex-1.tw-overflow-y-scroll"
DEFAULT_MESSAGE_SELECTOR = "div[data-message-id]"
DEFAULT_MESSAGE_TEXT_SELECTOR = "p.text-content"
DEFAULT_MESSAGE_TIME_SELECTOR = ".tw-text-xs.tw-text-n-soft"
DEFAULT_DATE_SEPARATOR_SELECTOR = "p.tw-text-center"
LOGIN_PATH_KEYWORD = "/account/login"


@dataclass(frozen=True)
class UserRecord:
    """User row to scrape chat messages for."""

    id: int
    href: str


@dataclass(frozen=True)
class ChatMessage:
    """Text chat message parsed from the talk page."""

    user_id: int
    message_text: str
    sender: str
    sent_at: str | None


def normalize_text(value: str) -> str:
    """Normalize text while preserving message line breaks."""

    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def init_chat_db(conn: sqlite3.Connection) -> None:
    """Create the chat message table used by this scraper."""

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            sent_at TEXT,
            sender TEXT NOT NULL CHECK(sender IN ('me', 'you')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, message_text, sent_at, sender)
        )
        """
    )
    conn.commit()


def fetch_users(conn: sqlite3.Connection, limit: int | None = None) -> list[UserRecord]:
    """Return saved users in stable ID order."""

    sql = "SELECT id, href FROM users ORDER BY id"
    params: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    return [UserRecord(id=int(row[0]), href=str(row[1])) for row in rows]


def member_id_from_href(href: str) -> str:
    """Extract the member ID from the final path segment of a user detail href."""

    parsed = urlparse(href)
    path = parsed.path.rstrip("/")
    member_id = path.rsplit("/", 1)[-1] if path else ""
    if not re.fullmatch(r"\d+", member_id):
        raise ValueError(f"Could not extract numeric member ID from href: {href}")
    return member_id


def talk_url_for_href(href: str, talk_url_template: str) -> str:
    """Build the LSTEP talk URL for a saved user href."""

    return talk_url_template.format(member=member_id_from_href(href))


def wait_for_login(driver: WebDriver, login_url: str, timeout_seconds: int) -> None:
    """Open the login page and wait until the user is authenticated."""

    driver.get(login_url)
    print("LSTEPにログインしてください。ログイン完了後、自動でチャット取得を開始します。")
    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda d: LOGIN_PATH_KEYWORD not in urlparse(d.current_url).path
        )
    except TimeoutException as exc:
        raise TimeoutException(
            f"Login was not completed within {timeout_seconds} seconds."
        ) from exc


def find_chat_scroll_container(driver: WebDriver, selector: str) -> WebElement:
    """Find the chat scroll container, falling back to the document element."""

    if selector:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements[0]
    return driver.execute_script(
        "return document.scrollingElement || document.documentElement;"
    )


def scroll_chat_to_oldest(
    driver: WebDriver,
    scroll_selector: str,
    wait_seconds: float,
    max_scrolls: int,
) -> None:
    """Scroll the chat container upward until older messages stop loading."""

    container = find_chat_scroll_container(driver, scroll_selector)
    stable_rounds = 0
    previous_state: tuple[int, str] | None = None

    for _ in range(max_scrolls):
        state = driver.execute_script(
            """
            const el = arguments[0];
            const firstMessage = document.querySelector('div[data-message-id]');
            return [
                el.scrollHeight || 0,
                firstMessage ? firstMessage.getAttribute('data-message-id') || '' : '',
            ];
            """,
            container,
        )
        current_state = (int(state[0] or 0), str(state[1] or ""))
        if current_state == previous_state:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break

        previous_state = current_state
        driver.execute_script("arguments[0].scrollTop = 0;", container)
        time.sleep(wait_seconds)


def element_text(element: WebElement) -> str:
    """Read text from a WebElement while tolerating stale DOM updates."""

    try:
        return normalize_text(element.text)
    except StaleElementReferenceException:
        return ""


def is_date_separator(message_element: WebElement, date_selector: str) -> bool:
    """Return True when the data-message-id row is a date separator."""

    message_id = message_element.get_attribute("data-message-id") or ""
    if message_id.startswith("-"):
        return True
    return bool(message_element.find_elements(By.CSS_SELECTOR, date_selector))


def message_sender(message_element: WebElement) -> str:
    """Infer sender from the message alignment classes."""

    class_name = message_element.get_attribute("class") or ""
    if "self-end" in class_name.split():
        return "me"
    if message_element.find_elements(By.CSS_SELECTOR, ".tw-self-end, .self-end"):
        return "me"
    return "you"


def message_time(message_element: WebElement, time_selector: str) -> str | None:
    """Return the visible time text for a message row."""

    for element in message_element.find_elements(By.CSS_SELECTOR, time_selector):
        text = element_text(element)
        if text:
            return text
    return None


def combine_date_and_time(date_label: str | None, time_label: str | None) -> str | None:
    """Combine the current date separator and message time into one DB value."""

    if date_label and time_label:
        return f"{date_label} {time_label}"
    return time_label or date_label


def extract_chat_messages(
    driver: WebDriver,
    user_id: int,
    message_selector: str,
    text_selector: str,
    time_selector: str,
    date_selector: str,
) -> list[ChatMessage]:
    """Extract text messages currently loaded in the talk page DOM."""

    messages: list[ChatMessage] = []
    current_date: str | None = None
    for row in driver.find_elements(By.CSS_SELECTOR, message_selector):
        try:
            if is_date_separator(row, date_selector):
                date_nodes = row.find_elements(By.CSS_SELECTOR, date_selector)
                if date_nodes:
                    date_text = element_text(date_nodes[0])
                    if date_text:
                        current_date = date_text
                continue

            text_nodes = row.find_elements(By.CSS_SELECTOR, text_selector)
            text_parts = [element_text(node) for node in text_nodes]
            message_text = "\n".join(part for part in text_parts if part)
            if not message_text:
                continue

            sent_at = combine_date_and_time(current_date, message_time(row, time_selector))
            messages.append(
                ChatMessage(
                    user_id=user_id,
                    message_text=message_text,
                    sender=message_sender(row),
                    sent_at=sent_at,
                )
            )
        except StaleElementReferenceException:
            continue
    return messages


def insert_chat_messages(conn: sqlite3.Connection, messages: list[ChatMessage]) -> int:
    """Insert chat messages and return the number of newly saved rows."""

    now = utc_now_iso()
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO chat_messages
            (user_id, message_text, sent_at, sender, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (message.user_id, message.message_text, message.sent_at, message.sender, now)
            for message in messages
        ],
    )
    conn.commit()
    return conn.total_changes - before


def scrape_chats_for_users(
    driver: WebDriver,
    conn: sqlite3.Connection,
    users: list[UserRecord],
    talk_url_template: str,
    message_selector: str,
    text_selector: str,
    time_selector: str,
    date_selector: str,
    scroll_selector: str,
    page_wait_seconds: float,
    scroll_wait_seconds: float,
    max_scrolls: int,
) -> int:
    """Open each user's talk page, scrape loaded text chats, and save them."""

    total_saved = 0
    for index, user in enumerate(users, start=1):
        try:
            talk_url = talk_url_for_href(user.href, talk_url_template)
        except ValueError as exc:
            print(f"[chats] skipped user_id={user.id}: {exc}")
            continue

        driver.get(talk_url)
        time.sleep(page_wait_seconds)
        scroll_chat_to_oldest(driver, scroll_selector, scroll_wait_seconds, max_scrolls)
        messages = extract_chat_messages(
            driver=driver,
            user_id=user.id,
            message_selector=message_selector,
            text_selector=text_selector,
            time_selector=time_selector,
            date_selector=date_selector,
        )
        saved = insert_chat_messages(conn, messages)
        total_saved += saved
        print(
            f"[chats] {index}/{len(users)} user_id={user.id} "
            f"messages_found={len(messages)} saved_new={saved} url={talk_url}"
        )
    return total_saved


def parse_args() -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(
        description="Scrape LSTEP talk text messages for users saved in SQLite."
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB file path.")
    parser.add_argument("--login-url", default=LOGIN_URL, help="LSTEP login URL.")
    parser.add_argument(
        "--talk-url-template",
        default=DEFAULT_TALK_URL_TEMPLATE,
        help="Talk URL template. Use {member} where the final detail href path segment goes.",
    )
    parser.add_argument(
        "--chat-scroll-selector",
        default=DEFAULT_CHAT_SCROLL_SELECTOR,
        help="CSS selector for the scrollable talk-history container.",
    )
    parser.add_argument(
        "--message-selector",
        default=DEFAULT_MESSAGE_SELECTOR,
        help="CSS selector for message/date rows.",
    )
    parser.add_argument(
        "--message-text-selector",
        default=DEFAULT_MESSAGE_TEXT_SELECTOR,
        help="CSS selector for message text nodes inside a message row.",
    )
    parser.add_argument(
        "--message-time-selector",
        default=DEFAULT_MESSAGE_TIME_SELECTOR,
        help="CSS selector for sent-time text inside a message row.",
    )
    parser.add_argument(
        "--date-separator-selector",
        default=DEFAULT_DATE_SEPARATOR_SELECTOR,
        help="CSS selector for date separator text inside date rows.",
    )
    parser.add_argument(
        "--page-wait-seconds",
        type=float,
        default=1.5,
        help="Wait after opening each talk page.",
    )
    parser.add_argument(
        "--scroll-wait-seconds",
        type=float,
        default=1.0,
        help="Wait after each upward scroll so older chats can load.",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=80,
        help="Safety limit for upward chat scrolls.",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=900,
        help="Seconds to wait for manual login.",
    )
    parser.add_argument("--max-users", type=int, default=None, help="Limit users for testing.")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless if already logged in.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help="Chrome user-data-dir path for keeping a persistent login session.",
    )
    parser.add_argument(
        "--skip-login-wait",
        action="store_true",
        help="Do not open the login page first; immediately start opening talk URLs.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the chat scraper."""

    args = parse_args()
    db_path = Path(args.db)
    with closing(sqlite3.connect(db_path)) as conn:
        init_chat_db(conn)
        users = fetch_users(conn, limit=args.max_users)
        if not users:
            print(f"[chats] no users found in DB: {db_path.resolve()}")
            return 0

        driver = create_driver(headless=args.headless, user_data_dir=args.user_data_dir)
        try:
            if not args.skip_login_wait:
                wait_for_login(driver, args.login_url, args.login_timeout)
            total_saved = scrape_chats_for_users(
                driver=driver,
                conn=conn,
                users=users,
                talk_url_template=args.talk_url_template,
                message_selector=args.message_selector,
                text_selector=args.message_text_selector,
                time_selector=args.message_time_selector,
                date_selector=args.date_separator_selector,
                scroll_selector=args.chat_scroll_selector,
                page_wait_seconds=args.page_wait_seconds,
                scroll_wait_seconds=args.scroll_wait_seconds,
                max_scrolls=args.max_scrolls,
            )
            print(f"[chats] total newly saved rows: {total_saved}")
        finally:
            driver.quit()
    print(f"Done. DB: {db_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
