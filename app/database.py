"""
SQLite stats.db：Token 消耗统计 & 余额快照。
"""
import os
import sqlite3
import threading
from datetime import date, datetime

_DB_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stats.db")
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_stats (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,
                model         TEXT    NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON token_stats(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshot (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT    NOT NULL,
                date    TEXT    NOT NULL,
                balance REAL    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bdate ON balance_snapshot(date)")
        conn.commit()


def record_tokens(model: str, input_tokens: int, output_tokens: int, total_tokens: int):
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO token_stats (ts, model, input_tokens, output_tokens, total_tokens) VALUES (?,?,?,?,?)",
                (today, model, input_tokens, output_tokens, total_tokens),
            )
            conn.commit()


def save_balance_snapshot(balance: float):
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO balance_snapshot (ts, date, balance) VALUES (?,?,?)",
                (now, today, balance),
            )
            conn.commit()


def get_balance_stats() -> dict:
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            latest = conn.execute(
                "SELECT balance FROM balance_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()
            today_first = conn.execute(
                "SELECT balance FROM balance_snapshot WHERE date = ? ORDER BY id ASC LIMIT 1",
                (today,),
            ).fetchone()
            all_first = conn.execute(
                "SELECT balance FROM balance_snapshot ORDER BY id ASC LIMIT 1"
            ).fetchone()

    current = latest["balance"] if latest else None
    today_spent = round(today_first["balance"] - current, 6) if (today_first and current is not None) else None
    total_spent = round(all_first["balance"] - current, 6) if (all_first and current is not None) else None
    return {
        "current_balance": current,
        "today_spent":     today_spent,
        "total_spent":     total_spent,
    }


# 单价表（元/百万tokens）
_PRICE: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash":  (1.0,  2.0),
    "deepseek-v4-pro":    (12.0, 24.0),
    "deepseek-chat":      (1.0,  2.0),
    "deepseek-reasoner":  (4.0,  16.0),
}
_DEFAULT_PRICE = (1.0, 2.0)


def get_token_summary() -> dict:
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            row_today = conn.execute(
                "SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats WHERE ts = ? GROUP BY model",
                (today,),
            ).fetchall()
            row_total = conn.execute(
                "SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats GROUP BY model",
            ).fetchall()

    def calc_cost(rows) -> tuple[int, int, int, float]:
        inp = out = tot = 0
        cost = 0.0
        for r in rows:
            m = (r["model"] or "").lower()
            pin, pout = _PRICE.get(m, _DEFAULT_PRICE)
            inp  += r["inp"] or 0
            out  += r["out"] or 0
            tot  += r["tot"] or 0
            cost += (r["inp"] or 0) / 1_000_000 * pin
            cost += (r["out"] or 0) / 1_000_000 * pout
        return inp, out, tot, cost

    ti, to_, tt, tc = calc_cost(row_today)
    ai, ao, at_, ac = calc_cost(row_total)
    return {
        "today": {
            "input_tokens":  ti,
            "output_tokens": to_,
            "total_tokens":  tt,
            "cost_cny":      round(tc, 6),
        },
        "total": {
            "input_tokens":  ai,
            "output_tokens": ao,
            "total_tokens":  at_,
            "cost_cny":      round(ac, 6),
        },
    }
