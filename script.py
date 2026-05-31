import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Conversation states ──────────────────────────────────────────────────────
ADD_ACCOUNT_ID, ADD_NAME, ADD_GMAIL, ADD_PLATFORM, ADD_SELLER, ADD_COUNTRY, ADD_DATE, ADD_REMINDER = range(8)
EDIT_SELECT, EDIT_FIELD, EDIT_VALUE = range(8, 11)
DELETE_SELECT, DELETE_CONFIRM = range(11, 13)
SET_DEFAULT_REMINDER = 13
SEARCH_VALUE = 14
QUICK_ADD = 15
IMPORT_CSV = 16
FILTER_VIEW = 17
DUPLICATE_SELECT = 18

# ── Constants ────────────────────────────────────────────────────────────────
GMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@gmail\.com$", re.IGNORECASE)
SUPPORTED_PLATFORMS = ["Bybit", "Bitget", "KuCoin", "MEX", "MEXC", "Other"]

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "accounts.sqlite3"))
BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    or os.getenv("BOT_TOKEN", "").strip()
)
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
DEFAULT_REMINDER_AFTER_DAYS = int(os.getenv("REMINDER_AFTER_DAYS", "4"))
AUTO_DELETE_AFTER_SECONDS = int(os.getenv("AUTO_DELETE_AFTER_SECONDS", "30"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Account:
    user_id: str
    chat_id: str
    id: str
    name: str
    gmail: str
    platform: str
    seller_name: str
    country: str
    creation_at: datetime
    reminder_amount: int
    reminder_unit: str
    reminded_at: str | None


# ── Database ─────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                db_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                gmail TEXT NOT NULL,
                platform TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                creation_date TEXT NOT NULL,
                reminder_days INTEGER NOT NULL DEFAULT 4,
                reminder_amount INTEGER NOT NULL DEFAULT 4,
                reminder_unit TEXT NOT NULL DEFAULT 'days',
                reminded_at TEXT,
                UNIQUE(user_id, id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        migrate_accounts(conn)
        conn.commit()


def migrate_accounts(conn: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "db_id" in columns and "name" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        columns["name"] = {"name": "name"}
    if "db_id" not in columns:
        conn.execute("ALTER TABLE accounts RENAME TO accounts_old")
        conn.execute(
            """
            CREATE TABLE accounts (
                db_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                gmail TEXT NOT NULL,
                platform TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                creation_date TEXT NOT NULL,
                reminder_days INTEGER NOT NULL DEFAULT 4,
                reminder_amount INTEGER NOT NULL DEFAULT 4,
                reminder_unit TEXT NOT NULL DEFAULT 'days',
                reminded_at TEXT,
                UNIQUE(user_id, id)
            )
            """
        )
        old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts_old)").fetchall()}
        fallback_user = ALLOWED_USER_ID or "owner"
        fallback_chat = ALLOWED_USER_ID or ""
        rows = conn.execute("SELECT * FROM accounts_old").fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO accounts (
                    id, name, user_id, chat_id, gmail, platform, seller_name, country,
                    creation_date, reminder_days, reminder_amount, reminder_unit, reminded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["name"] if "name" in old_columns else "",
                    row["user_id"] if "user_id" in old_columns and row["user_id"] else fallback_user,
                    row["chat_id"] if "chat_id" in old_columns and row["chat_id"] else fallback_chat,
                    row["gmail"],
                    row["platform"],
                    row["seller_name"] if "seller_name" in old_columns else "",
                    row["country"] if "country" in old_columns else "",
                    row["creation_date"],
                    row["reminder_days"] if "reminder_days" in old_columns else DEFAULT_REMINDER_AFTER_DAYS,
                    row["reminder_amount"] if "reminder_amount" in old_columns else row["reminder_days"] if "reminder_days" in old_columns else DEFAULT_REMINDER_AFTER_DAYS,
                    row["reminder_unit"] if "reminder_unit" in old_columns else "days",
                    row["reminded_at"] if "reminded_at" in old_columns else None,
                ),
            )
        conn.execute("DROP TABLE accounts_old")


def row_to_account(row: sqlite3.Row) -> Account:
    creation_text = row["creation_date"]
    try:
        creation_at = datetime.fromisoformat(creation_text)
    except ValueError:
        creation_at = datetime.combine(date.fromisoformat(creation_text), time.min)
    return Account(
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        id=row["id"],
        name=row["name"],
        gmail=row["gmail"],
        platform=row["platform"],
        seller_name=row["seller_name"],
        country=row["country"],
        creation_at=creation_at,
        reminder_amount=int(row["reminder_amount"]),
        reminder_unit=row["reminder_unit"],
        reminded_at=row["reminded_at"],
    )


def create_account(
    user_id: str,
    chat_id: str,
    account_id: str,
    name: str,
    gmail: str,
    platform: str,
    seller_name: str,
    country: str,
    creation_at: datetime,
    reminder_amount: int,
    reminder_unit: str,
) -> Account:
    account = Account(
        user_id=user_id,
        chat_id=chat_id,
        id=account_id.strip(),
        name=name.strip(),
        gmail=gmail.strip(),
        platform=platform.strip(),
        seller_name=seller_name.strip(),
        country=country.strip(),
        creation_at=creation_at,
        reminder_amount=reminder_amount,
        reminder_unit=reminder_unit,
        reminded_at=None,
    )
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO accounts (
                id, name, user_id, chat_id, gmail, platform, seller_name, country,
                creation_date, reminder_days, reminder_amount, reminder_unit, reminded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.id,
                account.name,
                account.user_id,
                account.chat_id,
                account.gmail,
                account.platform,
                account.seller_name,
                account.country,
                account.creation_at.isoformat(timespec="minutes"),
                account.reminder_amount if account.reminder_unit == "days" else DEFAULT_REMINDER_AFTER_DAYS,
                account.reminder_amount,
                account.reminder_unit,
                account.reminded_at,
            ),
        )
        conn.commit()
    return account


def get_accounts(user_id: str) -> list[Account]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM accounts
            WHERE user_id = ?
            ORDER BY creation_date DESC, name, platform, gmail
            """,
            (user_id,),
        ).fetchall()
    return [row_to_account(row) for row in rows]


def get_accounts_by_filter(user_id: str, field: str, value: str) -> list[Account]:
    with closing(db()) as conn:
        rows = conn.execute(
            f"SELECT * FROM accounts WHERE user_id = ? AND lower({field}) = ? ORDER BY creation_date DESC",
            (user_id, value.lower()),
        ).fetchall()
    return [row_to_account(row) for row in rows]


def get_account(user_id: str, account_id: str) -> Account | None:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE user_id = ? AND id = ?",
            (user_id, account_id),
        ).fetchone()
    return row_to_account(row) if row else None


def search_accounts(user_id: str, query: str) -> list[Account]:
    needle = f"%{query.strip().lower()}%"
    if not query.strip():
        return []
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM accounts
            WHERE user_id = ?
              AND (
                lower(id) LIKE ?
                OR lower(name) LIKE ?
                OR lower(gmail) LIKE ?
                OR lower(platform) LIKE ?
                OR lower(seller_name) LIKE ?
                OR lower(country) LIKE ?
              )
            ORDER BY creation_date DESC, name, platform, gmail
            LIMIT 25
            """,
            (user_id, needle, needle, needle, needle, needle, needle),
        ).fetchall()
    return [row_to_account(row) for row in rows]


def update_account(user_id: str, account_id: str, field: str, value: str) -> None:
    allowed_fields = {"name", "gmail", "platform", "seller_name", "country", "creation_date"}
    if field not in allowed_fields:
        raise ValueError("Unsupported field")
    reset = ", reminded_at = NULL" if field == "creation_date" else ""
    with closing(db()) as conn:
        conn.execute(
            f"UPDATE accounts SET {field} = ?{reset} WHERE user_id = ? AND id = ?",
            (value, user_id, account_id),
        )
        conn.commit()


def update_reminder(user_id: str, account_id: str, amount: int, unit: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            UPDATE accounts
            SET reminder_amount = ?, reminder_unit = ?, reminder_days = ?, reminded_at = NULL
            WHERE user_id = ? AND id = ?
            """,
            (amount, unit, amount if unit == "days" else DEFAULT_REMINDER_AFTER_DAYS, user_id, account_id),
        )
        conn.commit()


def delete_account(user_id: str, account_id: str) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM accounts WHERE user_id = ? AND id = ?", (user_id, account_id))
        conn.commit()


def reset_reminded(user_id: str, account_id: str) -> None:
    """Reset reminded_at so the account will be reminded again."""
    with closing(db()) as conn:
        conn.execute(
            "UPDATE accounts SET reminded_at = NULL WHERE user_id = ? AND id = ?",
            (user_id, account_id),
        )
        conn.commit()


def snooze_account(user_id: str, account_id: str, snooze_minutes: int) -> None:
    """Snooze by setting creation_at forward so reminder fires later."""
    account = get_account(user_id, account_id)
    if not account:
        return
    new_creation = datetime.now() - reminder_delta(account.reminder_amount, account.reminder_unit) + timedelta(minutes=snooze_minutes)
    with closing(db()) as conn:
        conn.execute(
            "UPDATE accounts SET creation_date = ?, reminded_at = NULL WHERE user_id = ? AND id = ?",
            (new_creation.isoformat(timespec="minutes"), user_id, account_id),
        )
        conn.commit()


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_setting(key: str) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def due_accounts() -> list[Account]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE reminded_at IS NULL ORDER BY creation_date"
        ).fetchall()
    now = datetime.now()
    return [
        account
        for account in [row_to_account(row) for row in rows]
        if reminder_due_at(account) <= now
    ]


def advance_reminder_cycle(user_id: str, account_id: str) -> None:
    """Start the next recurring reminder period from now."""
    now = datetime.now().replace(second=0, microsecond=0)
    with closing(db()) as conn:
        conn.execute(
            "UPDATE accounts SET creation_date = ?, reminded_at = NULL WHERE user_id = ? AND id = ?",
            (now.isoformat(timespec="minutes"), user_id, account_id),
        )
        conn.commit()


def mark_reminded(user_id: str, account_id: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            "UPDATE accounts SET reminded_at = ? WHERE user_id = ? AND id = ?",
            (datetime.now().isoformat(timespec="seconds"), user_id, account_id),
        )
        conn.commit()


def due_accounts_for_user(user_id: str) -> list[Account]:
    return [account for account in due_accounts() if account.user_id == user_id]


def upcoming_accounts(user_id: str, limit: int = 10) -> list[tuple[Account, datetime]]:
    """Return accounts sorted by next reminder due date (not yet reminded)."""
    accounts = [a for a in get_accounts(user_id) if not a.reminded_at]
    with_due = [(a, reminder_due_at(a)) for a in accounts]
    with_due.sort(key=lambda x: x[1])
    return with_due[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────
def current_user_id(update: Update) -> str:
    if not update.effective_user:
        raise RuntimeError("Missing Telegram user.")
    return str(update.effective_user.id)


def current_chat_id(update: Update) -> str:
    if not update.effective_chat:
        raise RuntimeError("Missing Telegram chat.")
    return str(update.effective_chat.id)


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USER_ID or current_user_id(update) == ALLOWED_USER_ID


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_allowed(update):
        return True
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.effective_message:
        await tracked_reply(context, update.effective_message, "This bot is private.")
    return False


async def track_bot_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    messages = context.user_data.setdefault("cleanup_messages", [])
    item = (chat_id, message_id)
    if item not in messages:
        messages.append(item)


async def delete_message_safely(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def delete_input_message(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if not message or not message.from_user or message.from_user.is_bot:
        return
    await delete_message_safely(context, message.chat_id, message.message_id)


async def auto_delete_bot_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if chat_id and message_id:
        await delete_message_safely(context, chat_id, message_id)


def schedule_bot_message_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    if AUTO_DELETE_AFTER_SECONDS < 1 or not context.job_queue:
        return
    context.job_queue.run_once(
        auto_delete_bot_message,
        when=AUTO_DELETE_AFTER_SECONDS,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"delete-message:{chat_id}:{message_id}",
    )


async def tracked_reply(context: ContextTypes.DEFAULT_TYPE, message, text: str, **kwargs):
    await cleanup_bot_messages(None, context)
    sent = await context.bot.send_message(chat_id=message.chat_id, text=text, **kwargs)
    if not text.startswith("Reminder:"):
        await track_bot_message(context, sent.chat_id, sent.message_id)
        schedule_bot_message_delete(context, sent.chat_id, sent.message_id)
    await delete_input_message(context, message)
    return sent


async def welcome_reply(context: ContextTypes.DEFAULT_TYPE, message):
    await cleanup_bot_messages(None, context)
    previous = context.chat_data.get("welcome_message")
    if previous:
        await delete_message_safely(context, previous["chat_id"], previous["message_id"])
    text = (
        "Welcome to LedgerPilot.\n\n"
        "Your account manager is ready. Press Start to open your controls."
    )
    sent = await context.bot.send_message(chat_id=message.chat_id, text=text, reply_markup=welcome_keyboard())
    context.chat_data["welcome_message"] = {"chat_id": sent.chat_id, "message_id": sent.message_id}
    await delete_input_message(context, message)
    return sent


async def cleanup_bot_messages(update: Update | None, context: ContextTypes.DEFAULT_TYPE) -> None:
    messages = context.user_data.pop("cleanup_messages", [])
    query = update.callback_query if update else None
    current = None
    if update and query and query.message:
        current = (query.message.chat_id, query.message.message_id)
    for chat_id, message_id in messages:
        if current and (chat_id, message_id) == current:
            continue
        await delete_message_safely(context, chat_id, message_id)


async def delete_chosen_message(update: Update) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    text = query.message.text or ""
    if text.startswith("Reminder:"):
        return
    try:
        await query.message.delete()
    except TelegramError:
        pass


# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_creation_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_creation_at(text: str) -> datetime | None:
    text = text.strip().lower()
    if text == "now":
        return datetime.now().replace(second=0, microsecond=0)
    if text == "today":
        return datetime.combine(date.today(), time.min)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    parsed_date = parse_creation_date(text)
    return datetime.combine(parsed_date, time.min) if parsed_date else None


def normalize_unit(unit: str) -> str | None:
    return {
        "m": "minutes", "min": "minutes", "mins": "minutes",
        "minute": "minutes", "minutes": "minutes",
        "h": "hours", "hr": "hours", "hrs": "hours",
        "hour": "hours", "hours": "hours",
        "d": "days", "day": "days", "days": "days",
    }.get(unit.strip().lower())


def parse_reminder(text: str) -> tuple[int, str] | None:
    match = re.match(r"^\s*(\d+)\s*([A-Za-z]+)\s*$", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = normalize_unit(match.group(2))
    if not unit or amount < 1 or amount > 525600:
        return None
    return amount, unit


def reminder_delta(amount: int, unit: str) -> timedelta:
    if unit == "minutes":
        return timedelta(minutes=amount)
    if unit == "hours":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def reminder_due_at(account: Account) -> datetime:
    return account.creation_at + reminder_delta(account.reminder_amount, account.reminder_unit)


def format_reminder_period(amount: int, unit: str) -> str:
    singular = unit[:-1] if amount == 1 else unit
    return f"{amount} {singular}"


def validate_gmail(text: str) -> bool:
    return bool(GMAIL_RE.match(text.strip()))


def clean_optional_text(text: str, max_length: int = 64) -> str | None:
    value = text.strip()
    if value == "-":
        return ""
    if len(value) > max_length:
        return None
    return value


# ── Quick-add free-form parser ────────────────────────────────────────────────
QUICK_ADD_HINT = (
    "⚡ Send all account data in one message, values in this order:\n\n"
    "<ID> <Gmail> <Platform> <Seller> <Country> <Created> <Reminder>\n\n"
    "Example:\n"
    "ACC001 john@gmail.com Bybit Ahmed Egypt today 4days\n\n"
    "• Name is optional — add it before Gmail if you want\n"
    "• Created: now / today / YYYY-MM-DD\n"
    "• Reminder: 4days / 2hours / 30minutes\n"
    "• Platform: Bybit / Bitget / KuCoin / MEX / MEXC / Other\n\n"
    "Send /cancel to abort."
)


def parse_freeform_quick_add(text: str) -> dict | str:
    """
    Parse free-form quick-add. Returns dict on success or error string.
    Format: ID [Name] Gmail Platform Seller Country Created Reminder
    Gmail is detected by @gmail.com, Created by date keywords, Reminder by digit+unit.
    """
    tokens = text.strip().split()
    if len(tokens) < 7:
        return "Too few values. Need at least: ID Gmail Platform Seller Country Created Reminder"

    # Find gmail
    gmail_idx = next((i for i, t in enumerate(tokens) if "@gmail.com" in t.lower()), None)
    if gmail_idx is None:
        return "Could not find a Gmail address (must end in @gmail.com)"
    if gmail_idx == 0:
        return "Account ID must come before the Gmail address"

    # Find reminder (last token matching digit+unit)
    reminder_idx = None
    for i in range(len(tokens) - 1, -1, -1):
        if re.match(r"^\d+[a-zA-Z]+$", tokens[i]):
            reminder_idx = i
            break
    if reminder_idx is None:
        return "Could not find a reminder (e.g. 4days, 2hours, 30minutes)"

    # Find created date (token before reminder that is a date keyword or date string)
    created_idx = None
    date_patterns = {"now", "today"}
    for i in range(reminder_idx - 1, -1, -1):
        t = tokens[i].lower()
        if t in date_patterns or re.match(r"^\d{4}-\d{2}-\d{2}$", t):
            created_idx = i
            break
        # Handle datetime split across two tokens: YYYY-MM-DD HH:MM
        if i + 1 < reminder_idx and re.match(r"^\d{4}-\d{2}-\d{2}$", t) and re.match(r"^\d{2}:\d{2}$", tokens[i + 1]):
            created_idx = i
            break

    if created_idx is None:
        return "Could not find a creation date (now / today / YYYY-MM-DD)"

    # Determine if datetime is two tokens
    created_str = tokens[created_idx]
    country_end = created_idx
    if (
        re.match(r"^\d{4}-\d{2}-\d{2}$", created_str)
        and created_idx + 1 < reminder_idx
        and re.match(r"^\d{2}:\d{2}$", tokens[created_idx + 1])
    ):
        created_str = f"{tokens[created_idx]} {tokens[created_idx + 1]}"
        country_end = created_idx

    # Tokens: [ID] [Name?] [Gmail] [Platform] [Seller...] [Country...] [Created] [Reminder]
    account_id = tokens[0]
    name = ""
    if gmail_idx > 1:
        # tokens 1 .. gmail_idx-1 are the name
        name = " ".join(tokens[1:gmail_idx])

    gmail = tokens[gmail_idx]
    platform_idx = gmail_idx + 1
    if platform_idx >= country_end:
        return "Could not find Platform after Gmail"

    platform_raw = tokens[platform_idx]
    platform = next((p for p in SUPPORTED_PLATFORMS if p.lower() == platform_raw.lower()), None)
    if not platform:
        return f"Unknown platform '{platform_raw}'. Use: {' / '.join(SUPPORTED_PLATFORMS)}"

    # Seller and Country are the remaining tokens between platform and created
    middle = tokens[platform_idx + 1:country_end]
    if len(middle) < 2:
        return "Need at least Seller and Country after the Platform"

    seller = middle[0]
    country = " ".join(middle[1:])

    reminder_str = tokens[reminder_idx]
    # Normalise reminder: "4days" -> "4 days"
    m = re.match(r"^(\d+)([a-zA-Z]+)$", reminder_str)
    if m:
        reminder_str = f"{m.group(1)} {m.group(2)}"

    return {
        "id": account_id,
        "name": name,
        "gmail": gmail,
        "platform": platform,
        "seller": seller,
        "country": country,
        "created": created_str,
        "reminder": reminder_str,
    }


# ── Keyboards ─────────────────────────────────────────────────────────────────
def bottom_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["⚡ Quick add", "➕ Add account"],
            ["✏️ Edit account", "🗑 Delete account"],
            ["👁 View accounts", "⚠️ Due now"],
            ["🔍 Search", "📅 Upcoming"],
            ["📂 Filter view", "📊 Stats"],
            ["⏰ Default reminder", "📥 Import CSV"],
            ["💾 Export backup"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start", callback_data="open_menu")]])


def platform_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(name, callback_data=f"platform:{name}")] for name in SUPPORTED_PLATFORMS]
        + [[InlineKeyboardButton("Back", callback_data="back")]]
    )


def reminder_keyboard(user_id: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if user_id:
        amount, unit = get_default_reminder(user_id)
        rows.append([
            InlineKeyboardButton(
                f"⭐ Default ({format_reminder_period(amount, unit)})",
                callback_data="reminder:default",
            )
        ])
    rows.extend([
        [
            InlineKeyboardButton("1 min", callback_data="reminder:1:minutes"),
            InlineKeyboardButton("30 min", callback_data="reminder:30:minutes"),
        ],
        [
            InlineKeyboardButton("1 hour", callback_data="reminder:1:hours"),
            InlineKeyboardButton("2 hours", callback_data="reminder:2:hours"),
        ],
        [
            InlineKeyboardButton("1 day", callback_data="reminder:1:days"),
            InlineKeyboardButton("3 days", callback_data="reminder:3:days"),
        ],
        [InlineKeyboardButton("Back", callback_data="back")],
    ])
    return InlineKeyboardMarkup(rows)


def default_reminder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("30 min", callback_data="default_reminder:30:minutes"),
                InlineKeyboardButton("1 hour", callback_data="default_reminder:1:hours"),
            ],
            [
                InlineKeyboardButton("1 day", callback_data="default_reminder:1:days"),
                InlineKeyboardButton("3 days", callback_data="default_reminder:3:days"),
            ],
            [InlineKeyboardButton("Back", callback_data="back")],
        ]
    )


def snooze_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("😴 Snooze 1h", callback_data=f"snooze:{account_id}:60"),
                InlineKeyboardButton("😴 Snooze 1 day", callback_data=f"snooze:{account_id}:1440"),
            ],
            [InlineKeyboardButton("✅ Mark done", callback_data=f"markdone:{account_id}")],
        ]
    )


def account_label(account: Account) -> str:
    return f"{account.name} ({account.id})" if account.name else f"Account ID: {account.id}"


def account_keyboard(prefix: str, accounts: Iterable[Account]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(account_label(account), callback_data=f"{prefix}:{account.id}")]
        for account in accounts
    ]
    rows.append([InlineKeyboardButton("Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def account_keyboard_with_actions(account: Account) -> InlineKeyboardMarkup:
    """Keyboard shown when viewing an account detail — includes extra actions."""
    rows = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"editpick:{account.id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"deletepick:{account.id}"),
        ],
        [
            InlineKeyboardButton("📋 Duplicate", callback_data=f"duplicatepick:{account.id}"),
            InlineKeyboardButton("🔄 Re-activate", callback_data=f"reactivate:{account.id}"),
        ],
        [InlineKeyboardButton("Back", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)


def delete_account_keyboard(accounts: Iterable[Account]) -> ReplyKeyboardMarkup:
    rows = [[account_label(account)] for account in accounts]
    rows.append(["Back"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def confirm_delete_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Confirm delete", callback_data="confirm_delete")],
            [InlineKeyboardButton("↩️ Undo (keep)", callback_data=f"undo_delete:{account_id}")],
        ]
    )


def field_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Name", callback_data=f"field:{account_id}:name")],
            [InlineKeyboardButton("Gmail", callback_data=f"field:{account_id}:gmail")],
            [InlineKeyboardButton("Platform", callback_data=f"field:{account_id}:platform")],
            [InlineKeyboardButton("Seller name", callback_data=f"field:{account_id}:seller_name")],
            [InlineKeyboardButton("Country", callback_data=f"field:{account_id}:country")],
            [InlineKeyboardButton("Creation time", callback_data=f"field:{account_id}:creation_date")],
            [InlineKeyboardButton("Reminder time", callback_data=f"field:{account_id}:reminder")],
            [InlineKeyboardButton("Back", callback_data="back")],
        ]
    )


def filter_keyboard(user_id: str) -> InlineKeyboardMarkup:
    accounts = get_accounts(user_id)
    platforms = sorted(set(a.platform for a in accounts if a.platform))
    countries = sorted(set(a.country for a in accounts if a.country))
    rows = []
    for p in platforms:
        rows.append([InlineKeyboardButton(f"🏦 {p}", callback_data=f"filter:platform:{p}")])
    for c in countries[:8]:
        rows.append([InlineKeyboardButton(f"🌍 {c}", callback_data=f"filter:country:{c}")])
    rows.append([InlineKeyboardButton("Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def format_account(account: Account) -> str:
    due_at = reminder_due_at(account)
    if account.reminded_at:
        status = "✅ marked done (re-activate to resume reminders)"
    else:
        status = f"⏰ due {due_at.strftime('%Y-%m-%d %H:%M')}"
    return (
        f"🆔 ID: {account.id}\n"
        f"👤 Name: {account.name or '-'}\n"
        f"📧 Gmail: {account.gmail}\n"
        f"🏦 Platform: {account.platform}\n"
        f"🤝 Seller: {account.seller_name or '-'}\n"
        f"🌍 Country: {account.country or '-'}\n"
        f"📅 Created: {account.creation_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"⏱ Remind after: {format_reminder_period(account.reminder_amount, account.reminder_unit)}\n"
        f"📌 Status: {status}"
    )


def get_default_reminder(user_id: str | None = None) -> tuple[int, str]:
    if user_id:
        amount = get_setting(f"default_reminder_amount:{user_id}")
        unit = get_setting(f"default_reminder_unit:{user_id}")
        if amount and unit:
            parsed_unit = normalize_unit(unit)
            try:
                parsed_amount = int(amount)
            except ValueError:
                parsed_amount = 0
            if parsed_amount > 0 and parsed_unit:
                return parsed_amount, parsed_unit
    amount = get_setting("default_reminder_amount")
    unit = get_setting("default_reminder_unit")
    if amount and unit:
        parsed_unit = normalize_unit(unit)
        try:
            parsed_amount = int(amount)
        except ValueError:
            parsed_amount = 0
        if parsed_amount > 0 and parsed_unit:
            return parsed_amount, parsed_unit
    return DEFAULT_REMINDER_AFTER_DAYS, "days"


def set_default_reminder(user_id: str, amount: int, unit: str) -> None:
    set_setting(f"default_reminder_amount:{user_id}", str(amount))
    set_setting(f"default_reminder_unit:{user_id}", unit)


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    set_setting(f"chat_id:{current_user_id(update)}", current_chat_id(update))
    await welcome_reply(context, update.effective_message)
    return ConversationHandler.END


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    accounts = get_accounts(current_user_id(update))
    message = update.callback_query.message if update.callback_query else update.effective_message
    if not accounts:
        await tracked_reply(context, message, "No accounts yet.", reply_markup=bottom_menu())
        return
    await tracked_reply(context, message, "Choose an account to view.", reply_markup=account_keyboard("viewpick", accounts))


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await tracked_reply(
        context,
        update.effective_message,
        "🔍 Search by account ID, name, Gmail, platform, seller, or country.",
        reply_markup=bottom_menu(),
    )
    return SEARCH_VALUE


async def search_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    results = search_accounts(current_user_id(update), update.effective_message.text)
    if not results:
        await tracked_reply(context, update.effective_message, "No matching accounts found.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(
        context,
        update.effective_message,
        f"Search results ({len(results)}). Choose an account to view.",
        reply_markup=account_keyboard("viewpick", results),
    )
    return ConversationHandler.END


async def filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    user_id = current_user_id(update)
    accounts = get_accounts(user_id)
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(
        context,
        update.effective_message,
        "📂 Filter by platform or country:",
        reply_markup=filter_keyboard(user_id),
    )
    return FILTER_VIEW


async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "back":
        await welcome_reply(context, query.message)
        return ConversationHandler.END
    _, field, value = query.data.split(":", 2)
    results = get_accounts_by_filter(current_user_id(update), field, value)
    if not results:
        await tracked_reply(context, query.message, f"No accounts found for {field} = {value}.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(
        context,
        query.message,
        f"📂 {field.capitalize()}: {value} — {len(results)} account(s). Choose to view:",
        reply_markup=account_keyboard("viewpick", results),
    )
    return ConversationHandler.END


async def upcoming_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    user_id = current_user_id(update)
    now = datetime.now()
    items = [
        (account, due_at)
        for account, due_at in upcoming_accounts(user_id, limit=25)
        if due_at > now
    ]
    if not items:
        await tracked_reply(context, update.effective_message, "No upcoming reminders.", reply_markup=bottom_menu())
        return ConversationHandler.END
    lines = ["📅 Upcoming reminders:\n"]
    for account, due_at in items[:10]:
        delta = due_at - now
        if delta.total_seconds() < 3600:
            when = f"in {int(delta.total_seconds() // 60)}m"
        elif delta.total_seconds() < 86400:
            when = f"in {int(delta.total_seconds() // 3600)}h"
        else:
            when = f"in {delta.days}d"
        label = account_label(account)
        lines.append(f"• {label} — {when} ({due_at.strftime('%Y-%m-%d %H:%M')})")
    await tracked_reply(context, update.effective_message, "\n".join(lines), reply_markup=bottom_menu())
    return ConversationHandler.END


async def due_now_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    user_id = current_user_id(update)
    overdue = due_accounts_for_user(user_id)
    message = update.callback_query.message if update.callback_query else update.effective_message
    if not overdue:
        await tracked_reply(context, message, "✅ No overdue accounts right now.", reply_markup=bottom_menu())
        return ConversationHandler.END
    now = datetime.now()
    lines = [f"⚠️ {len(overdue)} overdue account(s):\n"]
    for account in overdue[:10]:
        due_at = reminder_due_at(account)
        overdue_for = now - due_at
        if overdue_for.total_seconds() < 3600:
            ago = f"{int(overdue_for.total_seconds() // 60)}m ago"
        elif overdue_for.total_seconds() < 86400:
            ago = f"{int(overdue_for.total_seconds() // 3600)}h ago"
        else:
            ago = f"{overdue_for.days}d ago"
        lines.append(f"• {account_label(account)} — due {ago}")
    if len(overdue) > 10:
        lines.append(f"\n…and {len(overdue) - 10} more. Tap below to open one.")
    await tracked_reply(
        context,
        message,
        "\n".join(lines),
        reply_markup=account_keyboard("viewpick", overdue),
    )
    return ConversationHandler.END


async def stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    accounts = get_accounts(current_user_id(update))
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    now = datetime.now()
    due_now = sum(1 for a in accounts if reminder_due_at(a) <= now and not a.reminded_at)
    due_soon = sum(1 for a in accounts if now < reminder_due_at(a) <= now + timedelta(days=1) and not a.reminded_at)
    reminded = sum(1 for a in accounts if a.reminded_at)
    platforms = Counter(a.platform or "Other" for a in accounts)
    countries = Counter(a.country or "-" for a in accounts)
    platform_text = "\n".join(f"  • {name}: {count}" for name, count in platforms.most_common(8))
    country_text = "\n".join(f"  • {name}: {count}" for name, count in countries.most_common(8))
    text = (
        "📊 LedgerPilot Stats\n\n"
        f"Total accounts: {len(accounts)}\n"
        f"⚠️ Due now: {due_now}\n"
        f"🔜 Due in 24h: {due_soon}\n"
        f"✅ Marked done: {reminded}\n\n"
        f"By platform:\n{platform_text}\n\n"
        f"By country:\n{country_text}"
    )
    await tracked_reply(context, update.effective_message, text, reply_markup=bottom_menu())
    return ConversationHandler.END


async def export_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await cleanup_bot_messages(update, context)
    accounts = get_accounts(current_user_id(update))
    message = update.effective_message
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "gmail", "platform", "seller_name", "country",
                     "creation_at", "reminder_amount", "reminder_unit", "reminded_at"])
    for account in accounts:
        writer.writerow([
            account.id, account.name, account.gmail, account.platform,
            account.seller_name, account.country,
            account.creation_at.isoformat(timespec="minutes"),
            account.reminder_amount, account.reminder_unit, account.reminded_at or "",
        ])
    data = io.BytesIO(output.getvalue().encode("utf-8"))
    data.name = f"ledgerpilot-backup-{date.today().isoformat()}.csv"
    await context.bot.send_document(chat_id=message.chat_id, document=data, filename=data.name)
    await delete_input_message(context, message)
    return ConversationHandler.END


async def import_csv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    await tracked_reply(
        context,
        update.effective_message,
        "📥 Send a CSV file to import accounts.\n\n"
        "Required columns (same as export):\n"
        "id, gmail, platform, seller_name, country, creation_at, reminder_amount, reminder_unit\n\n"
        "Optional: name, reminded_at\n\n"
        "Send /cancel to abort.",
        reply_markup=bottom_menu(),
    )
    return IMPORT_CSV


async def import_csv_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    message = update.effective_message
    if not message.document:
        await tracked_reply(context, message, "Please send a CSV file.", reply_markup=bottom_menu())
        return IMPORT_CSV

    file = await context.bot.get_file(message.document.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    try:
        text = buf.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except Exception as e:
        await tracked_reply(context, message, f"⚠️ Could not read CSV: {e}", reply_markup=bottom_menu())
        return ConversationHandler.END

    required = {"id", "gmail", "platform", "seller_name", "country", "creation_at", "reminder_amount", "reminder_unit"}
    if not required.issubset(set(reader.fieldnames or [])):
        missing = required - set(reader.fieldnames or [])
        await tracked_reply(context, message, f"⚠️ Missing columns: {', '.join(missing)}", reply_markup=bottom_menu())
        return ConversationHandler.END

    user_id = current_user_id(update)
    chat_id = current_chat_id(update)
    added, skipped, errors = 0, 0, []

    for i, row in enumerate(rows, start=2):
        try:
            account_id = row["id"].strip()
            if not account_id:
                errors.append(f"Row {i}: empty ID")
                continue
            if get_account(user_id, account_id):
                skipped += 1
                continue
            gmail = row["gmail"].strip()
            if not validate_gmail(gmail):
                errors.append(f"Row {i} ({account_id}): invalid Gmail")
                continue
            platform_raw = row["platform"].strip()
            platform = next((p for p in SUPPORTED_PLATFORMS if p.lower() == platform_raw.lower()), platform_raw)
            creation_at = parse_creation_at(row["creation_at"].strip())
            if not creation_at:
                errors.append(f"Row {i} ({account_id}): bad creation_at")
                continue
            amount = int(row["reminder_amount"])
            unit = normalize_unit(row["reminder_unit"].strip()) or "days"
            create_account(
                user_id, chat_id, account_id,
                row.get("name", "").strip(),
                gmail, platform,
                row["seller_name"].strip(),
                row["country"].strip(),
                creation_at, amount, unit,
            )
            added += 1
        except Exception as e:
            errors.append(f"Row {i}: {e}")

    summary = f"📥 Import complete.\n✅ Added: {added}\n⏭ Skipped (duplicates): {skipped}"
    if errors:
        summary += f"\n⚠️ Errors ({len(errors)}):\n" + "\n".join(errors[:10])
    await tracked_reply(context, message, summary, reply_markup=bottom_menu())
    return ConversationHandler.END


async def view_account_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = query.data.split(":", 1)[1]
    account = get_account(current_user_id(update), account_id)
    if not account:
        await tracked_reply(context, query.message, "Account not found.", reply_markup=bottom_menu())
        return
    await tracked_reply(
        context, query.message,
        format_account(account),
        reply_markup=account_keyboard_with_actions(account),
    )


async def reactivate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = query.data.split(":", 1)[1]
    user_id = current_user_id(update)
    reset_reminded(user_id, account_id)
    account = get_account(user_id, account_id)
    await tracked_reply(
        context, query.message,
        f"🔄 Re-activated. Reminders will repeat every {format_reminder_period(account.reminder_amount, account.reminder_unit)}.\n\n{format_account(account)}",
        reply_markup=bottom_menu(),
    )


async def snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    _, account_id, minutes_str = query.data.split(":", 2)
    user_id = current_user_id(update)
    snooze_account(user_id, account_id, int(minutes_str))
    account = get_account(user_id, account_id)
    snooze_label = "1 hour" if minutes_str == "60" else "1 day"
    await tracked_reply(
        context, query.message,
        f"😴 Snoozed for {snooze_label}.\n\n{format_account(account)}",
        reply_markup=bottom_menu(),
    )


async def markdone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = query.data.split(":", 1)[1]
    user_id = current_user_id(update)
    mark_reminded(user_id, account_id)
    account = get_account(user_id, account_id)
    await tracked_reply(
        context, query.message,
        f"✅ Marked as done. Reminders paused until you re-activate.\n\n{format_account(account)}",
        reply_markup=bottom_menu(),
    )


async def view_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_accounts(update, context)
    return ConversationHandler.END


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    accounts = get_accounts(current_user_id(update))
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    message = update.callback_query.message if update.callback_query else update.effective_message
    await tracked_reply(context, message, "Choose an account to edit.", reply_markup=account_keyboard("editpick", accounts))
    return EDIT_SELECT


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    accounts = get_accounts(current_user_id(update))
    if not accounts:
        await tracked_reply(context, update.effective_message, "No accounts yet.", reply_markup=bottom_menu())
        return ConversationHandler.END
    context.user_data["delete_account_choices"] = {
        account_label(account).lower(): account.id for account in accounts
    } | {account.id.lower(): account.id for account in accounts}
    message = update.callback_query.message if update.callback_query else update.effective_message
    await tracked_reply(
        context, message,
        "Choose an account to delete.",
        reply_markup=delete_account_keyboard(accounts),
    )
    return DELETE_SELECT


async def duplicate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = query.data.split(":", 1)[1]
    account = get_account(current_user_id(update), account_id)
    if not account:
        await tracked_reply(context, query.message, "Account not found.", reply_markup=bottom_menu())
        return ConversationHandler.END
    context.user_data["duplicate_source_id"] = account_id
    await tracked_reply(
        context, query.message,
        f"📋 Duplicating:\n{format_account(account)}\n\nSend a new unique Account ID for the copy:",
    )
    return DUPLICATE_SELECT


async def duplicate_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    new_id = update.effective_message.text.strip()
    user_id = current_user_id(update)
    source_id = context.user_data.get("duplicate_source_id")
    if not source_id:
        await tracked_reply(context, update.effective_message, "Session expired.", reply_markup=bottom_menu())
        return ConversationHandler.END
    if not new_id or len(new_id) > 64:
        await tracked_reply(context, update.effective_message, "Send a valid ID under 64 characters.")
        return DUPLICATE_SELECT
    if get_account(user_id, new_id):
        await tracked_reply(context, update.effective_message, "That ID already exists. Send a different one.")
        return DUPLICATE_SELECT
    source = get_account(user_id, source_id)
    if not source:
        await tracked_reply(context, update.effective_message, "Source account not found.", reply_markup=bottom_menu())
        return ConversationHandler.END
    try:
        new_account = create_account(
            user_id, current_chat_id(update), new_id,
            source.name, source.gmail, source.platform,
            source.seller_name, source.country,
            datetime.now().replace(second=0, microsecond=0),
            source.reminder_amount, source.reminder_unit,
        )
    except sqlite3.IntegrityError:
        await tracked_reply(context, update.effective_message, "That ID already exists.", reply_markup=bottom_menu())
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(
        context, update.effective_message,
        f"📋 Duplicated successfully!\n\n{format_account(new_account)}",
        reply_markup=bottom_menu(),
    )
    return ConversationHandler.END


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    action = query.data
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if action == "view":
        await show_accounts(update, context)
        return ConversationHandler.END
    if action == "add":
        context.user_data.clear()
        await tracked_reply(context, query.message, "Send the account ID you want to use.")
        return ADD_ACCOUNT_ID
    if action == "edit":
        return await edit_start(update, context)
    if action == "delete":
        return await delete_start(update, context)
    if action == "settings":
        user_id = current_user_id(update)
        amount, unit = get_default_reminder(user_id)
        await tracked_reply(context, query.message,
            f"Default reminder for your new accounts: {format_reminder_period(amount, unit)}.\n"
            "Choose a preset, or send the new default like 45 minutes, 2 hours, or 3 days.",
            reply_markup=default_reminder_keyboard(),
        )
        return SET_DEFAULT_REMINDER
    return ConversationHandler.END


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    user_id = current_user_id(update)
    amount, unit = get_default_reminder(user_id)
    await tracked_reply(context, update.effective_message,
        f"Default reminder for your new accounts: {format_reminder_period(amount, unit)}.\n"
        "Choose a preset, or send the new default like 45 minutes, 2 hours, or 3 days.",
        reply_markup=default_reminder_keyboard(),
    )
    return SET_DEFAULT_REMINDER


async def set_default_reminder_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        _, amount, unit = query.data.split(":", 2)
        reminder = (int(amount), unit)
        message = query.message
    else:
        reminder = parse_reminder(update.effective_message.text)
        message = update.effective_message
    if not reminder:
        await tracked_reply(context, message, "Send a reminder like 30 minutes, 2 hours, or 3 days.")
        return SET_DEFAULT_REMINDER
    amount, unit = reminder
    set_default_reminder(current_user_id(update), amount, unit)
    await tracked_reply(context, message,
        f"✅ Default reminder updated to {format_reminder_period(amount, unit)}.",
        reply_markup=bottom_menu(),
    )
    return ConversationHandler.END


# ── Quick Add (free-form) ─────────────────────────────────────────────────────
async def quick_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(context, update.effective_message, QUICK_ADD_HINT)
    return QUICK_ADD


async def quick_add_parse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    text = update.effective_message.text or ""
    result = parse_freeform_quick_add(text)
    if isinstance(result, str):
        await tracked_reply(
            context, update.effective_message,
            f"⚠️ {result}\n\nTry again or send /cancel.\n\n{QUICK_ADD_HINT}",
        )
        return QUICK_ADD

    fields = result
    account_id = fields["id"]
    if get_account(current_user_id(update), account_id):
        await tracked_reply(context, update.effective_message,
            f"⚠️ Account ID '{account_id}' already exists. Use a different ID and try again.")
        return QUICK_ADD

    if not validate_gmail(fields["gmail"]):
        await tracked_reply(context, update.effective_message,
            "⚠️ Gmail is invalid. Must end in @gmail.com. Try again.")
        return QUICK_ADD

    creation_at = parse_creation_at(fields["created"])
    if not creation_at:
        await tracked_reply(context, update.effective_message,
            "⚠️ Date is invalid. Use: now / today / YYYY-MM-DD. Try again.")
        return QUICK_ADD

    reminder = parse_reminder(fields["reminder"])
    if not reminder:
        await tracked_reply(context, update.effective_message,
            "⚠️ Reminder is invalid. Use: 4days / 2hours / 30minutes. Try again.")
        return QUICK_ADD

    amount, unit = reminder
    try:
        account = create_account(
            current_user_id(update), current_chat_id(update),
            account_id, fields["name"], fields["gmail"], fields["platform"],
            fields["seller"], fields["country"], creation_at, amount, unit,
        )
    except sqlite3.IntegrityError:
        await tracked_reply(context, update.effective_message, "⚠️ Account ID already exists.", reply_markup=bottom_menu())
        return ConversationHandler.END

    context.user_data.clear()
    await tracked_reply(
        context, update.effective_message,
        f"✅ Account added!\n\n{format_account(account)}",
        reply_markup=bottom_menu(),
    )
    return ConversationHandler.END


# ── Step-by-step Add ──────────────────────────────────────────────────────────
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(context, update.effective_message, "Send the account ID you want to use.")
    return ADD_ACCOUNT_ID


async def add_account_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    account_id = update.effective_message.text.strip()
    if not account_id or len(account_id) > 64:
        await tracked_reply(context, update.effective_message, "Send a non-empty account ID under 64 characters.")
        return ADD_ACCOUNT_ID
    if get_account(current_user_id(update), account_id):
        await tracked_reply(context, update.effective_message, "This account ID already exists. Send a different one.")
        return ADD_ACCOUNT_ID
    context.user_data["new_account_id"] = account_id
    await tracked_reply(context, update.effective_message, "Send a name for this account, or send - to skip.")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = clean_optional_text(update.effective_message.text)
    if name is None:
        await tracked_reply(context, update.effective_message, "Send a name under 64 characters, or send - to skip.")
        return ADD_NAME
    context.user_data["new_name"] = name
    await tracked_reply(context, update.effective_message, "Send the Gmail address for this account.")
    return ADD_GMAIL


async def add_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gmail = update.effective_message.text.strip()
    if not validate_gmail(gmail):
        await tracked_reply(context, update.effective_message, "Please send a valid Gmail address ending in @gmail.com.")
        return ADD_GMAIL
    context.user_data["new_gmail"] = gmail
    await tracked_reply(context, update.effective_message, "Choose the platform.", reply_markup=platform_keyboard())
    return ADD_PLATFORM


async def add_platform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    context.user_data["new_platform"] = query.data.split(":", 1)[1]
    await tracked_reply(context, query.message, "Send the seller name.")
    return ADD_SELLER


async def add_seller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    seller_name = update.effective_message.text.strip()
    if not seller_name:
        await tracked_reply(context, update.effective_message, "Send the seller name.")
        return ADD_SELLER
    context.user_data["new_seller_name"] = seller_name
    await tracked_reply(context, update.effective_message, "Send the account country.")
    return ADD_COUNTRY


async def add_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    country = update.effective_message.text.strip()
    if not country:
        await tracked_reply(context, update.effective_message, "Send the country for this account.")
        return ADD_COUNTRY
    context.user_data["new_country"] = country
    await tracked_reply(context, update.effective_message, "Send the creation time as now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
    return ADD_DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    creation_at = parse_creation_at(update.effective_message.text)
    if not creation_at:
        await tracked_reply(context, update.effective_message, "Use now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
        return ADD_DATE
    context.user_data["new_creation_at"] = creation_at
    user_id = current_user_id(update)
    await tracked_reply(context, update.effective_message,
        "When should I remind you?\nChoose a button, or type like 15 minutes, 2 hours, or 3 days.",
        reply_markup=reminder_keyboard(user_id),
    )
    return ADD_REMINDER


async def add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        if query.data == "reminder:default":
            reminder = get_default_reminder(current_user_id(update))
        else:
            _, amount, unit = query.data.split(":", 2)
            reminder = (int(amount), unit)
        message = query.message
    else:
        reminder = parse_reminder(update.effective_message.text)
        message = update.effective_message
    if not reminder:
        await tracked_reply(context, message, "Send a reminder like 15 minutes, 2 hours, or 3 days.")
        return ADD_REMINDER
    amount, unit = reminder
    try:
        account = create_account(
            current_user_id(update), current_chat_id(update),
            context.user_data["new_account_id"],
            context.user_data.get("new_name", ""),
            context.user_data["new_gmail"],
            context.user_data["new_platform"],
            context.user_data["new_seller_name"],
            context.user_data["new_country"],
            context.user_data["new_creation_at"],
            amount, unit,
        )
    except sqlite3.IntegrityError:
        await tracked_reply(context, message, "This account ID already exists. Start again with /add.")
        return ConversationHandler.END
    context.user_data.clear()
    await tracked_reply(context, message, f"✅ Account added!\n\n{format_account(account)}", reply_markup=bottom_menu())
    return ConversationHandler.END


# ── Edit ──────────────────────────────────────────────────────────────────────
async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "cancel":
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=bottom_menu())
        return ConversationHandler.END
    account_id = query.data.split(":", 1)[1]
    if not get_account(current_user_id(update), account_id):
        await tracked_reply(context, query.message, "Account not found.", reply_markup=bottom_menu())
        return ConversationHandler.END
    await tracked_reply(context, query.message, "What do you want to edit?", reply_markup=field_keyboard(account_id))
    return EDIT_FIELD


async def edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    if query.data == "cancel":
        await tracked_reply(context, query.message, "Cancelled.", reply_markup=bottom_menu())
        return ConversationHandler.END
    _, account_id, field = query.data.split(":", 2)
    context.user_data["edit_account_id"] = account_id
    context.user_data["edit_field"] = field
    prompts = {
        "name": "Send the new account name, or send - to clear it.",
        "gmail": "Send the new Gmail address.",
        "platform": "Choose the new platform.",
        "seller_name": "Send the new seller name.",
        "country": "Send the new country.",
        "creation_date": "Send the new creation time as now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.",
        "reminder": "Choose a reminder preset, or send a custom time like 15 minutes, 2 hours, or 3 days.",
    }
    markup = platform_keyboard() if field == "platform" else reminder_keyboard() if field == "reminder" else None
    if markup:
        await tracked_reply(context, query.message, prompts[field], reply_markup=markup)
    else:
        await tracked_reply(context, query.message, prompts[field])
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data["edit_field"]
    account_id = context.user_data["edit_account_id"]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        message = query.message
        if field == "platform" and query.data.startswith("platform:"):
            value = query.data.split(":", 1)[1]
            update_account(current_user_id(update), account_id, field, value)
        elif field == "reminder" and query.data.startswith("reminder:"):
            _, amount, unit = query.data.split(":", 2)
            update_reminder(current_user_id(update), account_id, int(amount), unit)
        else:
            await tracked_reply(context, message, "Use one of the buttons.", reply_markup=bottom_menu())
            return EDIT_VALUE
        account = get_account(current_user_id(update), account_id)
        context.user_data.clear()
        await tracked_reply(context, message, f"✅ Account updated.\n\n{format_account(account)}", reply_markup=bottom_menu())
        return ConversationHandler.END
    message = update.effective_message
    value = message.text.strip()
    if field == "name":
        name = clean_optional_text(value)
        if name is None:
            await tracked_reply(context, message, "Send a name under 64 characters, or send - to clear it.")
            return EDIT_VALUE
        value = name
    if field == "gmail" and not validate_gmail(value):
        await tracked_reply(context, message, "Please send a valid Gmail address ending in @gmail.com.")
        return EDIT_VALUE
    if field == "creation_date":
        parsed = parse_creation_at(value)
        if not parsed:
            await tracked_reply(context, message, "Use now, today, YYYY-MM-DD, or YYYY-MM-DD HH:MM.")
            return EDIT_VALUE
        value = parsed.isoformat(timespec="minutes")
    if field == "reminder":
        reminder = parse_reminder(value)
        if not reminder:
            await tracked_reply(context, message, "Send a reminder like 15 minutes, 2 hours, or 3 days.")
            return EDIT_VALUE
        amount, unit = reminder
        update_reminder(current_user_id(update), account_id, amount, unit)
    else:
        update_account(current_user_id(update), account_id, field, value)
    account = get_account(current_user_id(update), account_id)
    context.user_data.clear()
    await tracked_reply(context, message, f"✅ Account updated.\n\n{format_account(account)}", reply_markup=bottom_menu())
    return ConversationHandler.END


# ── Delete ────────────────────────────────────────────────────────────────────
async def delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        message = query.message
        if query.data == "cancel":
            context.user_data.clear()
            await tracked_reply(context, message, "Cancelled.", reply_markup=bottom_menu())
            return ConversationHandler.END
        account_id = query.data.split(":", 1)[1]
    else:
        message = update.effective_message
        choice = message.text.strip()
        if choice.lower() in ("cancel", "back"):
            context.user_data.clear()
            await tracked_reply(context, message, "Cancelled.", reply_markup=bottom_menu())
            return ConversationHandler.END
        account_id = context.user_data.get("delete_account_choices", {}).get(choice.lower(), choice)
    account = get_account(current_user_id(update), account_id)
    if not account:
        context.user_data.clear()
        await tracked_reply(context, message, "Account not found.", reply_markup=bottom_menu())
        return ConversationHandler.END
    context.user_data["delete_account_id"] = account_id
    await tracked_reply(
        context, message,
        f"Delete this account?\n\n{format_account(account)}",
        reply_markup=confirm_delete_keyboard(account_id),
    )
    return DELETE_CONFIRM


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await delete_chosen_message(update)
    account_id = context.user_data.get("delete_account_id")
    if not account_id:
        await tracked_reply(context, query.message, "Delete session expired.", reply_markup=bottom_menu())
        return ConversationHandler.END
    if query.data == "confirm_delete":
        account = get_account(current_user_id(update), account_id)
        delete_account(current_user_id(update), account_id)
        context.user_data.clear()
        detail = f"\n\n{format_account(account)}" if account else ""
        await tracked_reply(context, query.message, f"🗑 Account deleted.{detail}", reply_markup=bottom_menu())
        return ConversationHandler.END
    if query.data.startswith("undo_delete:"):
        # User clicked Undo — account was never deleted, just go back
        context.user_data.clear()
        account = get_account(current_user_id(update), account_id)
        await tracked_reply(
            context, query.message,
            f"↩️ Kept. Account was not deleted.\n\n{format_account(account) if account else ''}",
            reply_markup=bottom_menu(),
        )
        return ConversationHandler.END
    return await back_to_start(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        await tracked_reply(context, update.callback_query.message, "Cancelled.", reply_markup=bottom_menu())
    else:
        await cleanup_bot_messages(update, context)
        await tracked_reply(context, update.effective_message, "Cancelled.", reply_markup=bottom_menu())
    context.user_data.clear()
    return ConversationHandler.END


async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await cleanup_bot_messages(update, context)
        await delete_chosen_message(update)
        message = update.callback_query.message
    else:
        await cleanup_bot_messages(update, context)
        message = update.effective_message
    context.user_data.clear()
    await welcome_reply(context, message)
    return ConversationHandler.END


async def open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await cleanup_bot_messages(update, context)
    await tracked_reply(context, query.message, "Controls are ready.", reply_markup=bottom_menu())
    return ConversationHandler.END


async def cleanup_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cleanup_bot_messages(update, context)
    await delete_input_message(context, update.effective_message)


# ── Reminder job ──────────────────────────────────────────────────────────────
async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for account in due_accounts():
        chat_id = account.chat_id or get_setting(f"chat_id:{account.user_id}")
        if not chat_id:
            logger.warning("Due reminder for user %s but no chat known.", account.user_id)
            continue
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=(
                f"Reminder: check this trading account.\n"
                f"Next reminder in {format_reminder_period(account.reminder_amount, account.reminder_unit)}.\n\n"
                f"{format_account(account)}"
            ),
            reply_markup=snooze_keyboard(account.id),
        )
        advance_reminder_cycle(account.user_id, account.id)
        await asyncio.sleep(0.3)


# ── Application ───────────────────────────────────────────────────────────────
def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Missing bot token. Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN on Tranger Cloud) "
            "in your environment variables."
        )
    application = Application.builder().token(BOT_TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("quickadd", quick_add_start),
            CommandHandler("add", add_start),
            CommandHandler("due", due_now_start),
            CommandHandler("settings", settings_start),
            MessageHandler(filters.Regex("(?i)^start$"), start),
            MessageHandler(filters.Regex("(?i)^back$"), back_to_start),
            MessageHandler(filters.Regex("(?i)^(⚡ quick add|quick add)$"), quick_add_start),
            MessageHandler(filters.Regex("(?i)^(👁 view accounts|view accounts)$"), view_start),
            MessageHandler(filters.Regex("(?i)^(⚠️ due now|due now)$"), due_now_start),
            MessageHandler(filters.Regex("(?i)^(🔍 search|search)$"), search_start),
            MessageHandler(filters.Regex("(?i)^(📊 stats|stats)$"), stats_start),
            MessageHandler(filters.Regex("(?i)^(➕ add account|add account)$"), add_start),
            MessageHandler(filters.Regex("(?i)^(✏️ edit account|edit account)$"), edit_start),
            MessageHandler(filters.Regex("(?i)^(🗑 delete account|delete account)$"), delete_start),
            MessageHandler(filters.Regex("(?i)^(⏰ default reminder|default reminder)$"), settings_start),
            MessageHandler(filters.Regex("(?i)^(💾 export backup|export backup)$"), export_backup),
            MessageHandler(filters.Regex("(?i)^(📥 import csv|import csv)$"), import_csv_start),
            MessageHandler(filters.Regex("(?i)^(📂 filter view|filter view)$"), filter_start),
            MessageHandler(filters.Regex("(?i)^(📅 upcoming|upcoming)$"), upcoming_start),
            CallbackQueryHandler(open_menu, pattern="^open_menu$"),
            CallbackQueryHandler(menu_callback, pattern="^(view|add|edit|delete|settings)$"),
        ],
        states={
            QUICK_ADD: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, quick_add_parse),
            ],
            ADD_ACCOUNT_ID: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_id),
            ],
            ADD_NAME: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_name),
            ],
            ADD_GMAIL: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_gmail),
            ],
            ADD_PLATFORM: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(add_platform, pattern="^platform:"),
            ],
            ADD_SELLER: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_seller),
            ],
            ADD_COUNTRY: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_country),
            ],
            ADD_DATE: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_date),
            ],
            ADD_REMINDER: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                CallbackQueryHandler(add_reminder, pattern="^reminder:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder),
            ],
            EDIT_SELECT: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(edit_select, pattern="^(editpick:|cancel$)"),
            ],
            EDIT_FIELD: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(edit_field, pattern="^(field:|cancel$)"),
            ],
            EDIT_VALUE: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(edit_value, pattern="^(platform:|reminder:)"),
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value),
            ],
            DELETE_SELECT: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_select),
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(delete_select, pattern="^(deletepick:|cancel$)"),
            ],
            DELETE_CONFIRM: [
                CallbackQueryHandler(delete_confirm, pattern="^(confirm_delete|undo_delete:)"),
                CallbackQueryHandler(back_to_start, pattern="^back$"),
            ],
            SET_DEFAULT_REMINDER: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(set_default_reminder_value, pattern="^default_reminder:"),
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_default_reminder_value),
            ],
            SEARCH_VALUE: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_value),
            ],
            IMPORT_CSV: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.Document.ALL, import_csv_receive),
                MessageHandler(filters.TEXT & ~filters.COMMAND, import_csv_receive),
            ],
            FILTER_VIEW: [
                CallbackQueryHandler(back_to_start, pattern="^back$"),
                CallbackQueryHandler(filter_callback, pattern="^filter:"),
            ],
            DUPLICATE_SELECT: [
                MessageHandler(filters.Regex("(?i)^(start|back)$"), back_to_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, duplicate_receive_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("due", due_now_start),
            MessageHandler(filters.Regex("(?i)^back$"), back_to_start),
            CallbackQueryHandler(back_to_start, pattern="^back$"),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
        ],
        allow_reentry=True,
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cleanup_text_messages), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("accounts", show_accounts))
    application.add_handler(CallbackQueryHandler(view_account_detail, pattern="^viewpick:"))
    application.add_handler(CallbackQueryHandler(duplicate_start, pattern="^duplicatepick:"))
    application.add_handler(CallbackQueryHandler(reactivate_callback, pattern="^reactivate:"))
    application.add_handler(CallbackQueryHandler(snooze_callback, pattern="^snooze:"))
    application.add_handler(CallbackQueryHandler(markdone_callback, pattern="^markdone:"))
    application.add_handler(conversation)
    application.job_queue.run_repeating(reminder_job, interval=30, first=10)
    application.job_queue.run_daily(reminder_job, time=time(hour=9, minute=0))
    return application


def main() -> None:
    init_db()
    logger.info("LedgerPilot starting (database: %s)", DATABASE_PATH)
    if not BOT_TOKEN:
        logger.error(
            "No bot token found. Set TELEGRAM_BOT_TOKEN or BOT_TOKEN in Tranger Cloud env vars."
        )
        raise SystemExit(1)
    logger.info("Bot token loaded.")
    asyncio.set_event_loop(asyncio.new_event_loop())
    application = build_application()
    logger.info("Bot started. Listening for Telegram updates.")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except Conflict:
        logger.error(
            "Telegram conflict: this token is already used by another running bot. "
            "Stop the local bot, wait 30 seconds, then restart on Tranger Cloud."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
