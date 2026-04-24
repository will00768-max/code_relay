"""
SQLite stats.db：Token 消耗统计 & 余额快照。
"""
import os
import sqlite3
import threading
from datetime import date, datetime

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_FILE = os.path.join(_DATA_DIR, "stats.db")
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_stats (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                     TEXT    NOT NULL,
                ts_full                TEXT    NOT NULL DEFAULT '',
                model                  TEXT    NOT NULL,
                input_tokens           INTEGER NOT NULL DEFAULT 0,
                input_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                input_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens          INTEGER NOT NULL DEFAULT 0,
                total_tokens           INTEGER NOT NULL DEFAULT 0
            )
        """)
        # 兼容旧表：补充缺失列（若不存在）
        for col, definition in [
            ("ts_full",                 "TEXT NOT NULL DEFAULT ''"),
            ("input_cache_hit_tokens",  "INTEGER NOT NULL DEFAULT 0"),
            ("input_cache_miss_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE token_stats ADD COLUMN {col} {definition}")
            except Exception:
                pass
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


def record_tokens(
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    input_cache_hit_tokens: int = 0,
    input_cache_miss_tokens: int = 0,
):
    today = date.today().isoformat()
    ts_full = datetime.now().strftime("%H:%M:%S")
    with _db_lock:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO token_stats "
                "(ts, ts_full, model, input_tokens, input_cache_hit_tokens, input_cache_miss_tokens, output_tokens, total_tokens) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (today, ts_full, model, input_tokens, input_cache_hit_tokens, input_cache_miss_tokens, output_tokens, total_tokens),
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
                "SELECT model, SUM(input_tokens) as inp, SUM(input_cache_hit_tokens) as hit, "
                "SUM(input_cache_miss_tokens) as miss, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats WHERE ts = ? GROUP BY model",
                (today,),
            ).fetchall()
            row_total = conn.execute(
                "SELECT model, SUM(input_tokens) as inp, SUM(input_cache_hit_tokens) as hit, "
                "SUM(input_cache_miss_tokens) as miss, SUM(output_tokens) as out, SUM(total_tokens) as tot "
                "FROM token_stats GROUP BY model",
            ).fetchall()

    def calc_cost(rows) -> tuple:
        inp = hit = miss = out = tot = 0
        cost = 0.0
        for r in rows:
            m = (r["model"] or "").lower()
            pin, pout = _PRICE.get(m, _DEFAULT_PRICE)
            inp  += r["inp"] or 0
            hit  += r["hit"] or 0
            miss += r["miss"] or 0
            out  += r["out"] or 0
            tot  += r["tot"] or 0
            cost += (r["inp"] or 0) / 1_000_000 * pin
            cost += (r["out"] or 0) / 1_000_000 * pout
        return inp, hit, miss, out, tot, cost

    ti, th, tm, to_, tt, tc = calc_cost(row_today)
    ai, ah, am, ao, at_, ac = calc_cost(row_total)
    return {
        "today": {
            "input_tokens":            ti,
            "input_cache_hit_tokens":  th,
            "input_cache_miss_tokens": tm,
            "output_tokens":           to_,
            "total_tokens":            tt,
            "cost_cny":                round(tc, 6),
        },
        "total": {
            "input_tokens":            ai,
            "input_cache_hit_tokens":  ah,
            "input_cache_miss_tokens": am,
            "output_tokens":           ao,
            "total_tokens":            at_,
            "cost_cny":                round(ac, 6),
        },
    }


def get_call_details() -> dict:
    """按模型分组的调用统计 + 最近 30 条调用记录。"""
    today = date.today().isoformat()
    with _db_lock:
        with _get_db() as conn:
            # 今日按模型分组
            by_model_today = conn.execute(
                """SELECT model,
                          COUNT(*) as calls,
                          SUM(input_tokens) as inp,
                          SUM(input_cache_hit_tokens) as hit,
                          SUM(input_cache_miss_tokens) as miss,
                          SUM(output_tokens) as out,
                          SUM(total_tokens) as tot
                   FROM token_stats WHERE ts = ? GROUP BY model ORDER BY tot DESC""",
                (today,),
            ).fetchall()
            # 总计按模型分组
            by_model_total = conn.execute(
                """SELECT model,
                          COUNT(*) as calls,
                          SUM(input_tokens) as inp,
                          SUM(input_cache_hit_tokens) as hit,
                          SUM(input_cache_miss_tokens) as miss,
                          SUM(output_tokens) as out,
                          SUM(total_tokens) as tot
                   FROM token_stats GROUP BY model ORDER BY tot DESC"""
            ).fetchall()
            # 最近 200 条（前端分页）
            recent = conn.execute(
                """SELECT ts, ts_full, model,
                          input_tokens, input_cache_hit_tokens, input_cache_miss_tokens,
                          output_tokens, total_tokens
                   FROM token_stats ORDER BY id DESC LIMIT 200"""
            ).fetchall()

    def rows_to_list(rows):
        return [
            {
                "model":                    r["model"],
                "calls":                    r["calls"],
                "input_tokens":             r["inp"] or 0,
                "input_cache_hit_tokens":   r["hit"] or 0,
                "input_cache_miss_tokens":  r["miss"] or 0,
                "output_tokens":            r["out"] or 0,
                "total_tokens":             r["tot"] or 0,
            }
            for r in rows
        ]

    return {
        "by_model_today": rows_to_list(by_model_today),
        "by_model_total": rows_to_list(by_model_total),
        "recent": [
            {
                "time":                     (r["ts"] + " " + r["ts_full"]).strip() if r["ts_full"] else r["ts"],
                "model":                    r["model"],
                "input_tokens":             r["input_tokens"],
                "input_cache_hit_tokens":   r["input_cache_hit_tokens"] or 0,
                "input_cache_miss_tokens":  r["input_cache_miss_tokens"] or 0,
                "output_tokens":            r["output_tokens"],
                "total_tokens":             r["total_tokens"],
            }
            for r in recent
        ],
    }


def get_chart_data(days: int = 30) -> dict:
    """
    返回最近 N 天的每日图表数据：
    - labels: 日期标签列表
    - cost: 每日估算消费（元）
    - calls: 每日调用次数
    - tokens: 每日合计 token 数
    - input_cache_hit_tokens: 每日缓存命中输入 token
    - input_cache_miss_tokens: 每日缓存未命中输入 token
    - output_tokens: 每日输出 token
    - by_model: 各模型的 { calls, tokens, cache_hit, cache_miss, output }
    """
    from datetime import timedelta
    today = date.today()
    date_list = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]

    with _db_lock:
        with _get_db() as conn:
            rows = conn.execute(
                """SELECT ts, model,
                          COUNT(*) as calls,
                          SUM(input_tokens) as inp,
                          SUM(input_cache_hit_tokens) as hit,
                          SUM(input_cache_miss_tokens) as miss,
                          SUM(output_tokens) as out,
                          SUM(total_tokens) as tot
                   FROM token_stats
                   WHERE ts >= ?
                   GROUP BY ts, model
                   ORDER BY ts""",
                (date_list[0],),
            ).fetchall()

    # 按日期 + 模型整理
    from collections import defaultdict
    day_data: dict[str, dict] = {
        d: {"calls": 0, "tokens": 0, "cost": 0.0, "hit": 0, "miss": 0, "out": 0}
        for d in date_list
    }
    model_day: dict[str, dict[str, dict]] = defaultdict(
        lambda: {d: {"calls": 0, "tokens": 0, "hit": 0, "miss": 0, "out": 0} for d in date_list}
    )

    for r in rows:
        d = r["ts"]
        if d not in day_data:
            continue
        m = (r["model"] or "").lower()
        pin, pout = _PRICE.get(m, _DEFAULT_PRICE)
        cost = (r["inp"] or 0) / 1_000_000 * pin + (r["out"] or 0) / 1_000_000 * pout
        day_data[d]["calls"]  += r["calls"]
        day_data[d]["tokens"] += r["tot"]  or 0
        day_data[d]["cost"]   += cost
        day_data[d]["hit"]    += r["hit"]  or 0
        day_data[d]["miss"]   += r["miss"] or 0
        day_data[d]["out"]    += r["out"]  or 0
        model_day[r["model"]][d]["calls"]  += r["calls"]
        model_day[r["model"]][d]["tokens"] += r["tot"]  or 0
        model_day[r["model"]][d]["hit"]    += r["hit"]  or 0
        model_day[r["model"]][d]["miss"]   += r["miss"] or 0
        model_day[r["model"]][d]["out"]    += r["out"]  or 0

    labels = [d[5:] for d in date_list]   # "MM-DD" 格式，更简洁
    cost   = [round(day_data[d]["cost"], 4) for d in date_list]
    calls  = [day_data[d]["calls"]          for d in date_list]
    tokens = [day_data[d]["tokens"]         for d in date_list]
    cache_hit  = [day_data[d]["hit"]  for d in date_list]
    cache_miss = [day_data[d]["miss"] for d in date_list]
    output_tok = [day_data[d]["out"]  for d in date_list]

    by_model = {}
    for model, dmap in model_day.items():
        by_model[model] = {
            "calls":      [dmap[d]["calls"]  for d in date_list],
            "tokens":     [dmap[d]["tokens"] for d in date_list],
            "cache_hit":  [dmap[d]["hit"]    for d in date_list],
            "cache_miss": [dmap[d]["miss"]   for d in date_list],
            "output":     [dmap[d]["out"]    for d in date_list],
        }

    return {
        "labels":                  labels,
        "cost":                    cost,
        "calls":                   calls,
        "tokens":                  tokens,
        "input_cache_hit_tokens":  cache_hit,
        "input_cache_miss_tokens": cache_miss,
        "output_tokens":           output_tok,
        "by_model":                by_model,
    }

