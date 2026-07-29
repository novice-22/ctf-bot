"""SQLite 저장소. 봇이 대회/문제/풀이를 기억하는 곳."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

# 컨테이너에서는 DATA_DIR 이 볼륨을 가리킨다. 그래야 이미지를 다시 만들어도 기록이 남는다.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
DB_PATH = DATA_DIR / "ctfbot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ctfs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    ctfd_url    TEXT,
    category_id INTEGER NOT NULL,
    role_id     INTEGER,
    status      TEXT    NOT NULL DEFAULT 'active',
    created_at  TEXT    NOT NULL,
    starts_at   TEXT,
    ends_at     TEXT,
    event_id    INTEGER,
    notice_channel_id INTEGER,
    notice_message_id INTEGER,
    announced_start   INTEGER NOT NULL DEFAULT 0,
    announced_end     INTEGER NOT NULL DEFAULT 0,
    auto_imported     INTEGER NOT NULL DEFAULT 0,
    -- 자동 임포트/pull 에 쓰려고 보관한다. 대회를 아카이브/삭제하면 지운다.
    platform    TEXT,
    api_token   TEXT
);

CREATE TABLE IF NOT EXISTS challenges (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ctf_id            INTEGER NOT NULL REFERENCES ctfs(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    category          TEXT    NOT NULL,
    channel_id        INTEGER NOT NULL,
    thread_id         INTEGER NOT NULL UNIQUE,
    solved            INTEGER NOT NULL DEFAULT 0,
    flag              TEXT,
    ctfd_challenge_id INTEGER,
    solve_message_id  INTEGER,
    UNIQUE (ctf_id, ctfd_challenge_id)
);

CREATE TABLE IF NOT EXISTS solves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,
    solved_at    TEXT    NOT NULL,
    UNIQUE (challenge_id, user_id)
);

CREATE TABLE IF NOT EXISTS category_aliases (
    guild_id  INTEGER NOT NULL,
    alias     TEXT    NOT NULL,
    canonical TEXT    NOT NULL,
    PRIMARY KEY (guild_id, alias)
);

CREATE TABLE IF NOT EXISTS seen_categories (
    guild_id INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, name)
);
"""

_conn: aiosqlite.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() 을 먼저 호출해야 합니다.")
    return _conn


# 이미 만들어진 DB에 나중에 추가된 컬럼들. (테이블, 컬럼, 정의)
MIGRATIONS = (
    ("ctfs", "ends_at", "TEXT"),
    ("ctfs", "event_id", "INTEGER"),
    ("ctfs", "starts_at", "TEXT"),
    ("ctfs", "notice_channel_id", "INTEGER"),
    ("ctfs", "notice_message_id", "INTEGER"),
    ("ctfs", "announced_start", "INTEGER NOT NULL DEFAULT 0"),
    ("ctfs", "announced_end", "INTEGER NOT NULL DEFAULT 0"),
    ("ctfs", "platform", "TEXT"),
    ("ctfs", "api_token", "TEXT"),
    ("ctfs", "auto_imported", "INTEGER NOT NULL DEFAULT 0"),
    ("challenges", "solve_message_id", "INTEGER"),
)


async def _migrate() -> None:
    for table, column, definition in MIGRATIONS:
        cur = await _conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in await cur.fetchall()}
        if column not in existing:
            await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    await _conn.commit()


async def init() -> None:
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA foreign_keys = ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()
    await _migrate()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


# ---------- ctfs ----------

def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


async def add_ctf(
    guild_id: int,
    name: str,
    category_id: int,
    role_id: int | None,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    event_id: int | None = None,
) -> int:
    """시각/이벤트 인자는 키워드로만 받는다 (순서가 밀려도 조용히 틀리지 않도록)."""
    cur = await conn().execute(
        """INSERT INTO ctfs
               (guild_id, name, category_id, role_id, created_at, starts_at, ends_at, event_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            guild_id,
            name,
            category_id,
            role_id,
            _now(),
            _iso(starts_at),
            _iso(ends_at),
            event_id,
        ),
    )
    await conn().commit()
    return cur.lastrowid


async def set_ctf_schedule(
    ctf_id: int, starts_at: datetime | None, ends_at: datetime | None
) -> None:
    """일정을 바꾼다.

    시각이 미래로 옮겨지면 그 알림은 아직 안 한 것으로 되돌린다 (다시 알려야 하므로).
    이미 지난 시각으로 두면 알림 상태는 그대로라 같은 알림이 두 번 뜨지 않는다.
    """
    now = datetime.now(timezone.utc)
    reset_start = starts_at is not None and starts_at > now
    reset_end = ends_at is not None and ends_at > now

    await conn().execute(
        """UPDATE ctfs
              SET starts_at = ?,
                  ends_at = ?,
                  announced_start = CASE WHEN ? THEN 0 ELSE announced_start END,
                  announced_end   = CASE WHEN ? THEN 0 ELSE announced_end END,
                  auto_imported   = CASE WHEN ? THEN 0 ELSE auto_imported END
            WHERE id = ?""",
        (_iso(starts_at), _iso(ends_at), reset_start, reset_end, reset_start, ctf_id),
    )
    await conn().commit()


async def set_notice_message(ctf_id: int, channel_id: int, message_id: int) -> None:
    await conn().execute(
        "UPDATE ctfs SET notice_channel_id = ?, notice_message_id = ? WHERE id = ?",
        (channel_id, message_id, ctf_id),
    )
    await conn().commit()


async def get_ctf_by_event(event_id: int) -> aiosqlite.Row | None:
    cur = await conn().execute("SELECT * FROM ctfs WHERE event_id = ?", (event_id,))
    return await cur.fetchone()


async def set_ctf_end(ctf_id: int, ends_at: datetime | None) -> None:
    await conn().execute(
        "UPDATE ctfs SET ends_at = ? WHERE id = ?", (_iso(ends_at), ctf_id)
    )
    await conn().commit()


async def ctfs_due_for_announce(kind: str) -> list[aiosqlite.Row]:
    """시작/종료 시각이 된 대회들 중 아직 알리지 않은 것. kind 는 'start' 또는 'end'.

    비교 기준을 '이번 분의 끝'으로 잡는다. 디스코드 이벤트 시각에는 15:40:00.845 처럼
    소수점 초가 섞여 있어서, 매분 0초에 확인하면 항상 한 박자 늦게 걸리기 때문이다.
    """
    column = "starts_at" if kind == "start" else "ends_at"
    flag = "announced_start" if kind == "start" else "announced_end"
    cutoff = datetime.now(timezone.utc).replace(second=59, microsecond=999999)
    cur = await conn().execute(
        f"SELECT * FROM ctfs WHERE {flag} = 0 AND {column} IS NOT NULL AND {column} <= ?",
        (cutoff.isoformat(),),
    )
    return list(await cur.fetchall())


async def mark_announced(ctf_id: int, kind: str) -> None:
    flag = "announced_start" if kind == "start" else "announced_end"
    await conn().execute(f"UPDATE ctfs SET {flag} = 1 WHERE id = ?", (ctf_id,))
    await conn().commit()


async def ctfs_due_for_auto_import(delay: timedelta) -> list[aiosqlite.Row]:
    """시작하고 delay 만큼 지난, 아직 자동 임포트 안 한 대회들.

    대회가 시작 정각에 문제를 열지 않는 경우가 흔해서 조금 기다렸다 가져온다.
    접속 정보가 없으면 가져올 방법이 없으니 대상에서 뺀다.
    """
    cutoff = datetime.now(timezone.utc).replace(second=59, microsecond=999999) - delay
    cur = await conn().execute(
        """SELECT * FROM ctfs
            WHERE auto_imported = 0
              AND api_token IS NOT NULL
              AND ctfd_url IS NOT NULL
              AND starts_at IS NOT NULL
              AND starts_at <= ?""",
        (cutoff.isoformat(),),
    )
    return list(await cur.fetchall())


async def mark_auto_imported(ctf_id: int) -> None:
    await conn().execute("UPDATE ctfs SET auto_imported = 1 WHERE id = ?", (ctf_id,))
    await conn().commit()


async def ctfs_due_for_archive(grace: timedelta) -> list[aiosqlite.Row]:
    """종료 시각 + 유예기간이 지난 진행중 대회들."""
    cutoff = (datetime.now(timezone.utc) - grace).isoformat()
    cur = await conn().execute(
        "SELECT * FROM ctfs WHERE status = 'active' AND ends_at IS NOT NULL AND ends_at <= ?",
        (cutoff,),
    )
    return list(await cur.fetchall())


async def get_ctf(ctf_id: int) -> aiosqlite.Row | None:
    cur = await conn().execute("SELECT * FROM ctfs WHERE id = ?", (ctf_id,))
    return await cur.fetchone()


async def get_ctf_by_category(category_id: int) -> aiosqlite.Row | None:
    cur = await conn().execute("SELECT * FROM ctfs WHERE category_id = ?", (category_id,))
    return await cur.fetchone()


async def get_ctf_by_name(guild_id: int, name: str) -> aiosqlite.Row | None:
    cur = await conn().execute(
        "SELECT * FROM ctfs WHERE guild_id = ? AND name = ? COLLATE NOCASE", (guild_id, name)
    )
    return await cur.fetchone()


async def list_all_ctfs(guild_id: int) -> list[aiosqlite.Row]:
    """아카이브된 것까지 포함한 전체 목록 (삭제 대상 고를 때 쓴다)."""
    cur = await conn().execute(
        "SELECT * FROM ctfs WHERE guild_id = ? ORDER BY created_at DESC", (guild_id,)
    )
    return list(await cur.fetchall())


async def list_active_ctfs(guild_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM ctfs WHERE guild_id = ? AND status = 'active' ORDER BY created_at DESC",
        (guild_id,),
    )
    return list(await cur.fetchall())


async def set_ctf_url(ctf_id: int, url: str) -> None:
    await conn().execute("UPDATE ctfs SET ctfd_url = ? WHERE id = ?", (url, ctf_id))
    await conn().commit()


async def set_credentials(ctf_id: int, url: str, token: str, platform: str) -> None:
    """자동 임포트/pull 에 쓸 접속 정보를 보관한다."""
    await conn().execute(
        "UPDATE ctfs SET ctfd_url = ?, api_token = ?, platform = ? WHERE id = ?",
        (url, token, platform, ctf_id),
    )
    await conn().commit()


async def clear_credentials(ctf_id: int) -> None:
    """대회가 끝나면 토큰을 지운다. 더 쓸 일이 없고, 남겨두면 유출 위험만 남는다."""
    await conn().execute("UPDATE ctfs SET api_token = NULL WHERE id = ?", (ctf_id,))
    await conn().commit()


async def archive_ctf(ctf_id: int) -> None:
    await conn().execute(
        "UPDATE ctfs SET status = 'archived', api_token = NULL WHERE id = ?", (ctf_id,)
    )
    await conn().commit()


async def delete_ctf(ctf_id: int) -> None:
    """대회와 딸린 문제/풀이 기록까지 전부 삭제 (ON DELETE CASCADE)."""
    await conn().execute("DELETE FROM ctfs WHERE id = ?", (ctf_id,))
    await conn().commit()


# ---------- challenges ----------

async def add_challenge(
    ctf_id: int,
    name: str,
    category: str,
    channel_id: int,
    thread_id: int,
    ctfd_challenge_id: int | str | None = None,
) -> int:
    cur = await conn().execute(
        """INSERT INTO challenges (ctf_id, name, category, channel_id, thread_id, ctfd_challenge_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ctf_id, name, category, channel_id, thread_id, ctfd_challenge_id),
    )
    await conn().commit()
    return cur.lastrowid


async def get_challenge_by_thread(thread_id: int) -> aiosqlite.Row | None:
    cur = await conn().execute("SELECT * FROM challenges WHERE thread_id = ?", (thread_id,))
    return await cur.fetchone()


async def list_challenges(ctf_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM challenges WHERE ctf_id = ? ORDER BY category, name", (ctf_id,)
    )
    return list(await cur.fetchall())


async def find_challenges_by_name(ctf_id: int, name: str) -> list[aiosqlite.Row]:
    """이름이 같은 문제들. 보통 하나지만, 분야가 다르면 겹칠 수 있다."""
    cur = await conn().execute(
        "SELECT * FROM challenges WHERE ctf_id = ? AND name = ? COLLATE NOCASE", (ctf_id, name)
    )
    return list(await cur.fetchall())


async def get_challenge_by_name(ctf_id: int, name: str) -> aiosqlite.Row | None:
    rows = await find_challenges_by_name(ctf_id, name)
    return rows[0] if rows else None


async def solvers_of(challenge_id: int) -> list[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT user_id, solved_at FROM solves WHERE challenge_id = ? ORDER BY solved_at",
        (challenge_id,),
    )
    return list(await cur.fetchall())


async def existing_ctfd_ids(ctf_id: int) -> set[int]:
    cur = await conn().execute(
        "SELECT ctfd_challenge_id FROM challenges WHERE ctf_id = ? AND ctfd_challenge_id IS NOT NULL",
        (ctf_id,),
    )
    return {row[0] for row in await cur.fetchall()}


async def mark_solved(
    challenge_id: int, flag: str | None, solve_message_id: int | None = None
) -> None:
    await conn().execute(
        """UPDATE challenges
              SET solved = 1,
                  flag = COALESCE(?, flag),
                  solve_message_id = ?
            WHERE id = ?""",
        (flag, solve_message_id, challenge_id),
    )
    await conn().commit()


async def mark_unsolved(challenge_id: int) -> None:
    """풀이 취소. 상태·풀이자·solves 알림 참조를 되돌린다 (플래그 기록은 남긴다)."""
    await conn().execute(
        "UPDATE challenges SET solved = 0, solve_message_id = NULL WHERE id = ?",
        (challenge_id,),
    )
    await conn().execute("DELETE FROM solves WHERE challenge_id = ?", (challenge_id,))
    await conn().commit()


async def delete_challenge(challenge_id: int) -> None:
    await conn().execute("DELETE FROM challenges WHERE id = ?", (challenge_id,))
    await conn().commit()


# ---------- solves ----------

async def add_solve(challenge_id: int, user_id: int) -> None:
    await conn().execute(
        "INSERT OR IGNORE INTO solves (challenge_id, user_id, solved_at) VALUES (?, ?, ?)",
        (challenge_id, user_id, _now()),
    )
    await conn().commit()


async def scoreboard(ctf_id: int) -> list[tuple[int, list[str]]]:
    """(user_id, 푼 문제 이름 목록). 많이 푼 사람 순."""
    cur = await conn().execute(
        """SELECT s.user_id, c.name
             FROM solves s
             JOIN challenges c ON c.id = s.challenge_id
            WHERE c.ctf_id = ?
            ORDER BY s.user_id, s.solved_at""",
        (ctf_id,),
    )
    by_user: dict[int, list[str]] = {}
    for row in await cur.fetchall():
        by_user.setdefault(row["user_id"], []).append(row["name"])
    return sorted(by_user.items(), key=lambda item: len(item[1]), reverse=True)


async def solve_counts(ctf_id: int) -> tuple[int, int]:
    """(푼 문제 수, 전체 문제 수)"""
    cur = await conn().execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(solved), 0) AS solved FROM challenges WHERE ctf_id = ?",
        (ctf_id,),
    )
    row = await cur.fetchone()
    return row["solved"], row["total"]


# ---------- category aliases ----------

async def set_alias(guild_id: int, alias: str, canonical: str) -> None:
    await conn().execute(
        """INSERT INTO category_aliases (guild_id, alias, canonical) VALUES (?, ?, ?)
           ON CONFLICT(guild_id, alias) DO UPDATE SET canonical = excluded.canonical""",
        (guild_id, alias, canonical),
    )
    await conn().commit()


async def remove_alias(guild_id: int, alias: str) -> None:
    await conn().execute(
        "DELETE FROM category_aliases WHERE guild_id = ? AND alias = ?", (guild_id, alias)
    )
    await conn().commit()



async def alias_map(guild_id: int) -> dict[str, str]:
    cur = await conn().execute(
        "SELECT alias, canonical FROM category_aliases WHERE guild_id = ?", (guild_id,)
    )
    return {row["alias"]: row["canonical"] for row in await cur.fetchall()}


async def add_seen_category(guild_id: int, name: str) -> None:
    await conn().execute(
        "INSERT OR IGNORE INTO seen_categories (guild_id, name) VALUES (?, ?)", (guild_id, name)
    )
    await conn().commit()


async def seen_categories(guild_id: int) -> list[str]:
    cur = await conn().execute(
        "SELECT name FROM seen_categories WHERE guild_id = ? ORDER BY name", (guild_id,)
    )
    return [row["name"] for row in await cur.fetchall()]


