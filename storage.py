import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "wallet.db"
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    _lock.acquire()
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()
        _lock.release()


def init_db(path: str | Path | None = None) -> None:
    global DB_PATH
    if path is not None:
        DB_PATH = Path(path)
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                active_trip_id INTEGER,
                state TEXT NOT NULL DEFAULT 'idle',
                state_data TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                from_country TEXT NOT NULL,
                to_country TEXT NOT NULL,
                from_currency TEXT NOT NULL,
                to_currency TEXT NOT NULL,
                rate REAL NOT NULL,
                rate_is_manual INTEGER NOT NULL DEFAULT 0,
                home_balance REAL NOT NULL,
                dest_balance REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                dest_amount REAL NOT NULL,
                home_amount REAL NOT NULL,
                rate REAL NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_expenses (
                user_id INTEGER PRIMARY KEY,
                trip_id INTEGER NOT NULL,
                dest_amount REAL NOT NULL,
                home_amount REAL NOT NULL,
                rate REAL NOT NULL,
                token TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(user_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_trip ON expenses(trip_id, id);
            """
        )
        _ensure_expense_category(connection)


def _ensure_expense_category(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(expenses)").fetchall()
    }
    if "category" not in columns:
        connection.execute(
            "ALTER TABLE expenses ADD COLUMN category TEXT NOT NULL DEFAULT ''"
        )


def ensure_user(user_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )


def get_state(user_id: int) -> tuple[str, dict]:
    ensure_user(user_id)
    with _connect() as connection:
        row = connection.execute(
            "SELECT state, state_data FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    try:
        data = json.loads(row["state_data"])
    except (TypeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return row["state"], data


def set_state(user_id: int, state: str, data: dict | None = None) -> None:
    ensure_user(user_id)
    payload = json.dumps(data or {}, ensure_ascii=False)
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET state = ?, state_data = ? WHERE user_id = ?",
            (state, payload, user_id),
        )


def clear_state(user_id: int) -> None:
    set_state(user_id, "idle", {})


def _trip(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["rate_is_manual"] = bool(item["rate_is_manual"])
    return item


def _unique_title(connection: sqlite3.Connection, user_id: int, base: str) -> str:
    rows = connection.execute(
        "SELECT title FROM trips WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    titles = {row["title"] for row in rows}
    if base not in titles:
        return base
    number = 2
    while f"{base} ({number})" in titles:
        number += 1
    return f"{base} ({number})"


def create_trip(
    user_id: int,
    from_country: str,
    to_country: str,
    from_currency: str,
    to_currency: str,
    rate: float,
    rate_is_manual: bool,
    home_balance: float,
    dest_balance: float,
) -> dict:
    ensure_user(user_id)
    with _connect() as connection:
        title = _unique_title(connection, user_id, to_country)
        cursor = connection.execute(
            """
            INSERT INTO trips (
                user_id, title, from_country, to_country, from_currency, to_currency,
                rate, rate_is_manual, home_balance, dest_balance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                from_country,
                to_country,
                from_currency,
                to_currency,
                round(float(rate), 8),
                int(rate_is_manual),
                round(float(home_balance), 2),
                round(float(dest_balance), 2),
                now_iso(),
            ),
        )
        trip_id = cursor.lastrowid
        connection.execute(
            "UPDATE users SET active_trip_id = ?, state = 'idle', state_data = '{}' WHERE user_id = ?",
            (trip_id, user_id),
        )
        row = connection.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    return _trip(row)


def list_trips(user_id: int) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM trips WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [_trip(row) for row in rows]


def get_trip(user_id: int, trip_id: int) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
    return _trip(row)


def get_active_trip(user_id: int) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT trips.*
            FROM users
            JOIN trips ON trips.id = users.active_trip_id AND trips.user_id = users.user_id
            WHERE users.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return _trip(row)


def set_active_trip(user_id: int, trip_id: int) -> dict | None:
    trip = get_trip(user_id, trip_id)
    if trip is None:
        return None
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET active_trip_id = ?, state = 'idle', state_data = '{}' WHERE user_id = ?",
            (trip_id, user_id),
        )
    return trip


def update_rate(user_id: int, trip_id: int, rate: float) -> dict | None:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE trips
            SET rate = ?, rate_is_manual = 1
            WHERE id = ? AND user_id = ?
            """,
            (round(float(rate), 8), trip_id, user_id),
        )
        if cursor.rowcount == 0:
            return None
        row = connection.execute(
            "SELECT * FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()
    return _trip(row)


def save_pending(
    user_id: int,
    trip_id: int,
    dest_amount: float,
    home_amount: float,
    rate: float,
) -> str:
    token = uuid.uuid4().hex
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO pending_expenses (user_id, trip_id, dest_amount, home_amount, rate, token)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                trip_id = excluded.trip_id,
                dest_amount = excluded.dest_amount,
                home_amount = excluded.home_amount,
                rate = excluded.rate,
                token = excluded.token
            """,
            (
                user_id,
                trip_id,
                round(float(dest_amount), 2),
                round(float(home_amount), 2),
                round(float(rate), 8),
                token,
            ),
        )
    return token


def discard_pending(user_id: int, token: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM pending_expenses WHERE user_id = ? AND token = ?",
            (user_id, token),
        )
        return cursor.rowcount > 0


def get_pending(user_id: int, token: str) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT pending_expenses.dest_amount, pending_expenses.home_amount,
                   trips.to_currency, trips.from_currency
            FROM pending_expenses
            JOIN trips ON trips.id = pending_expenses.trip_id AND trips.user_id = pending_expenses.user_id
            WHERE pending_expenses.user_id = ? AND pending_expenses.token = ?
            """,
            (user_id, token),
        ).fetchone()
    return dict(row) if row else None


def apply_expense(user_id: int, token: str, category: str) -> dict | None:
    with _connect() as connection:
        pending = connection.execute(
            "SELECT * FROM pending_expenses WHERE user_id = ? AND token = ?",
            (user_id, token),
        ).fetchone()
        if pending is None:
            return None
        trip = connection.execute(
            "SELECT * FROM trips WHERE id = ? AND user_id = ?",
            (pending["trip_id"], user_id),
        ).fetchone()
        if trip is None:
            connection.execute(
                "DELETE FROM pending_expenses WHERE user_id = ? AND token = ?",
                (user_id, token),
            )
            return None
        new_dest = round(trip["dest_balance"] - pending["dest_amount"], 2)
        new_home = round(trip["home_balance"] - pending["home_amount"], 2)
        if not trip["rate_is_manual"]:
            connection.execute(
                """
                UPDATE trips
                SET dest_balance = ?, home_balance = ?, rate = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_dest, new_home, pending["rate"], trip["id"], user_id),
            )
        else:
            connection.execute(
                """
                UPDATE trips
                SET dest_balance = ?, home_balance = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_dest, new_home, trip["id"], user_id),
            )
        connection.execute(
            """
            INSERT INTO expenses (
                trip_id, user_id, dest_amount, home_amount, rate, category, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip["id"],
                user_id,
                pending["dest_amount"],
                pending["home_amount"],
                pending["rate"],
                category,
                now_iso(),
            ),
        )
        connection.execute(
            "DELETE FROM pending_expenses WHERE user_id = ? AND token = ?",
            (user_id, token),
        )
        updated = connection.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip["id"],),
        ).fetchone()
        result = _trip(updated)
        result["dest_amount"] = pending["dest_amount"]
        result["home_amount"] = pending["home_amount"]
        result["category"] = category
        return result


def expense_totals(user_id: int, trip_id: int) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT category,
                   ROUND(SUM(dest_amount), 2) AS dest_amount,
                   ROUND(SUM(home_amount), 2) AS home_amount
            FROM expenses
            WHERE user_id = ? AND trip_id = ?
            GROUP BY category
            """,
            (user_id, trip_id),
        ).fetchall()
    return [dict(row) for row in rows]


def list_expenses(user_id: int, trip_id: int, limit: int = 20) -> list[dict]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT expenses.*, trips.to_currency, trips.from_currency, trips.title
            FROM expenses
            JOIN trips ON trips.id = expenses.trip_id AND trips.user_id = expenses.user_id
            WHERE expenses.user_id = ? AND expenses.trip_id = ?
            ORDER BY expenses.id DESC
            LIMIT ?
            """,
            (user_id, trip_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]
