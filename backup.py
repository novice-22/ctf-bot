"""삭제 전 기록 백업.

디스코드 채널은 한 번 지우면 되돌릴 수 없다. 그래서 지우기 직전에
스레드 안에 있는 것을 전부 챙긴다: 문제 정보, 대화 내용, 첨부 파일까지.

결과는 ZIP 하나로 묶어서 두 군데에 남긴다:
  1. 백업 채널 (디스코드에서 바로 내려받기)
  2. backups/ 폴더 (서버가 날아가도 남는 사본)

첨부 파일은 링크만 저장하면 채널이 지워질 때 같이 죽으므로, 실제로 받아서 넣는다.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import discord

import db
import structure

log = logging.getLogger("ctfbot.backup")

BACKUP_DIR = db.DATA_DIR / "backups"


def enabled() -> bool:
    """삭제 시 백업 여부. BACKUP_ENABLED=0 이면 끈다 (테스트 중 임시로 끌 때)."""
    return os.getenv("BACKUP_ENABLED", "1") != "0"

# 첨부 파일을 통째로 받다 보면 커질 수 있어 상한을 둔다.
MAX_ATTACHMENT = 8 * 1024 * 1024  # 파일 하나당 8MB
MAX_TOTAL_ATTACHMENTS = 200 * 1024 * 1024  # 백업 전체 200MB


def _safe(name: str) -> str:
    """파일 이름으로 쓸 수 있게 정리."""
    return re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")[:60] or "untitled"


class Collector:
    """스레드 내용을 모으면서 첨부 파일을 함께 담아두는 상자."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.total = 0
        self.skipped: list[str] = []

    async def take(self, attachment: discord.Attachment, folder: str) -> str | None:
        """첨부 파일을 받아 보관하고, ZIP 안에서의 경로를 돌려준다."""
        if attachment.size > MAX_ATTACHMENT or self.total >= MAX_TOTAL_ATTACHMENTS:
            self.skipped.append(f"{folder}/{attachment.filename} ({attachment.size:,}B)")
            return None
        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound):
            self.skipped.append(f"{folder}/{attachment.filename} (내려받기 실패)")
            return None

        path = f"files/{folder}/{attachment.filename}"
        # 같은 이름이 겹치면 뒤에 번호를 붙인다.
        n = 1
        while path in self.files:
            stem, _, ext = attachment.filename.rpartition(".")
            suffix = f".{ext}" if stem else ""
            path = f"files/{folder}/{stem or attachment.filename}_{n}{suffix}"
            n += 1

        self.files[path] = data
        self.total += len(data)
        return path


async def _collect_thread(
    thread: discord.Thread | None, collector: Collector, folder: str
) -> list[dict]:
    """스레드의 모든 메시지를 오래된 순으로 모은다."""
    if thread is None:
        return []

    messages = []
    try:
        async for message in thread.history(limit=None, oldest_first=True):
            record = {
                "author": message.author.display_name,
                "author_id": message.author.id,
                "at": message.created_at.isoformat(),
                "content": message.content,
            }
            if message.attachments:
                record["attachments"] = [
                    {
                        "filename": a.filename,
                        "size": a.size,
                        "saved_as": await collector.take(a, folder),
                    }
                    for a in message.attachments
                ]
            if message.embeds:
                record["embeds"] = [e.to_dict() for e in message.embeds]
            messages.append(record)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.exception("스레드 %s 내용을 읽지 못했습니다", folder)

    return messages


def _embed_lines(embed: dict) -> list[str]:
    """임베드(솔브 알림 등)를 읽을 수 있는 줄로 편다."""
    lines = []
    for key in ("title", "description"):
        if embed.get(key):
            lines.append(str(embed[key]))
    for field in embed.get("fields") or []:
        lines.append(f"{field.get('name', '')}: {field.get('value', '')}")
    if embed.get("footer", {}).get("text"):
        lines.append(embed["footer"]["text"])
    return lines


def _transcript(messages: list[dict]) -> str:
    """사람이 바로 읽을 수 있는 형태의 대화록."""
    lines = []
    for m in messages:
        stamp = m["at"][:19].replace("T", " ")
        lines.append(f"[{stamp}] {m['author']}: {m['content']}")
        # 봇이 보낸 솔브 알림처럼 본문 없이 임베드만 있는 메시지도 풀어서 적는다.
        for embed in m.get("embeds") or []:
            for line in _embed_lines(embed):
                lines.append(f"    | {line}")
        for a in m.get("attachments", []):
            where = a["saved_as"] or "(저장 안 됨)"
            lines.append(f"    📎 {a['filename']} → {where}")
    return "\n".join(lines)


async def _challenge_record(row, guild, collector: Collector) -> dict:
    solvers = await db.solvers_of(row["id"])
    folder = f"{_safe(row['category'])}/{_safe(row['name'])}"

    thread = None
    if guild is not None:
        channel = guild.get_channel_or_thread(row["thread_id"])
        if isinstance(channel, discord.Thread):
            thread = channel
    messages = await _collect_thread(thread, collector, folder)

    return {
        "name": row["name"],
        "category": row["category"],
        "solved": bool(row["solved"]),
        "flag": row["flag"],
        "thread_id": row["thread_id"],
        "solvers": [
            {"user_id": s["user_id"], "solved_at": s["solved_at"]} for s in solvers
        ],
        "messages": messages,
    }


async def _collect_common_channels(
    category: discord.CategoryChannel | None, collector: Collector
) -> dict[str, list[dict]]:
    """general, notes, credentials 같은 공용 채널의 대화도 함께 챙긴다."""
    if category is None:
        return {}

    result = {}
    for channel in category.text_channels:
        name = structure.strip_emoji(channel.name)
        if name not in structure.COMMON_CHANNELS:
            continue
        messages = []
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                record = {
                    "author": message.author.display_name,
                    "author_id": message.author.id,
                    "at": message.created_at.isoformat(),
                    "content": message.content,
                }
                if message.attachments:
                    record["attachments"] = [
                        {
                            "filename": a.filename,
                            "size": a.size,
                            "saved_as": await collector.take(a, f"_channels/{name}"),
                        }
                        for a in message.attachments
                    ]
                if message.embeds:
                    record["embeds"] = [e.to_dict() for e in message.embeds]
                messages.append(record)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("채널 %s 를 읽지 못했습니다", channel.name)
        result[name] = messages
    return result


async def build_ctf(ctf, guild: discord.Guild | None = None) -> tuple[dict, Collector]:
    collector = Collector()
    rows = await db.list_challenges(ctf["id"])
    solved, total = await db.solve_counts(ctf["id"])
    category = guild.get_channel(ctf["category_id"]) if guild else None

    payload = {
        "type": "ctf",
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "ctf": {
            "name": ctf["name"],
            "url": ctf["ctfd_url"],
            "status": ctf["status"],
            "created_at": ctf["created_at"],
            "ends_at": ctf["ends_at"],
        },
        "summary": {"solved": solved, "total": total},
        "channels": await _collect_common_channels(category, collector),
        "challenges": [await _challenge_record(row, guild, collector) for row in rows],
    }
    return payload, collector


async def build_challenge(ctf, row, guild: discord.Guild | None = None) -> tuple[dict, Collector]:
    collector = Collector()
    payload = {
        "type": "challenge",
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "ctf": {"name": ctf["name"] if ctf else None},
        "challenge": await _challenge_record(row, guild, collector),
    }
    return payload, collector


def write(payload: dict, collector: Collector, *parts: str) -> Path:
    """백업을 ZIP 으로 저장하고 경로를 돌려준다."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{stamp}_{'_'.join(_safe(p) for p in parts)}.zip"

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json.dumps(payload, ensure_ascii=False, indent=2))

        # 사람이 바로 읽을 대화록도 같이 넣는다.
        if payload["type"] == "ctf":
            for name, messages in payload.get("channels", {}).items():
                if messages:
                    zf.writestr(f"transcripts/_{name}.txt", _transcript(messages))
            for c in payload["challenges"]:
                if c["messages"]:
                    zf.writestr(
                        f"transcripts/{_safe(c['category'])}/{_safe(c['name'])}.txt",
                        _transcript(c["messages"]),
                    )
        else:
            c = payload["challenge"]
            if c["messages"]:
                zf.writestr(f"transcripts/{_safe(c['name'])}.txt", _transcript(c["messages"]))

        for name, data in collector.files.items():
            zf.writestr(name, data)

        if collector.skipped:
            zf.writestr("SKIPPED.txt", "\n".join(collector.skipped))

    return path


def _summary_embed(
    payload: dict, collector: Collector, deleted_by: discord.abc.User
) -> discord.Embed:
    if payload["type"] == "ctf":
        info = payload["ctf"]
        summary = payload["summary"]
        message_count = sum(len(c["messages"]) for c in payload["challenges"])
        message_count += sum(len(m) for m in payload.get("channels", {}).values())
        # 문제 목록은 ZIP 안에 있으므로 임베드에는 요약만 적는다.
        # (문제가 수십 개면 임베드가 화면을 다 차지한다.)
        categories = sorted({c["category"] for c in payload["challenges"]})
        embed = discord.Embed(
            title=f"🗄️ 대회 삭제됨 — {info['name']}",
            description=(
                f"문제 **{summary['total']}개** ({summary['solved']}개 해결) · "
                f"메시지 **{message_count}개** · 파일 **{len(collector.files)}개**\n"
                f"분야: {', '.join(categories) if categories else '없음'}"
            ),
            colour=discord.Colour.dark_red(),
        )
    else:
        c = payload["challenge"]
        mark = "✅" if c["solved"] else "❌"
        embed = discord.Embed(
            title=f"🗄️ 문제 삭제됨 — {c['name']}",
            description=(
                f"{mark} {c['category']} · {payload['ctf']['name'] or '?'}\n"
                f"메시지 **{len(c['messages'])}개** · 파일 **{len(collector.files)}개**"
            ),
            colour=discord.Colour.dark_red(),
        )
        if c["solvers"]:
            embed.add_field(
                name="푼 사람",
                value=" ".join(f"<@{s['user_id']}>" for s in c["solvers"]),
                inline=False,
            )

    if collector.skipped:
        embed.add_field(
            name="⚠️ 못 담은 파일", value=f"{len(collector.skipped)}개 (SKIPPED.txt 참고)", inline=False
        )
    embed.set_footer(text=f"삭제: {deleted_by.display_name} · 첨부 ZIP 에 전체 기록")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def save(
    guild: discord.Guild, payload: dict, collector: Collector, deleted_by: discord.abc.User, *parts: str
) -> tuple[Path, discord.TextChannel | None]:
    """백업을 파일과 백업 채널 양쪽에 남긴다."""
    path = write(payload, collector, *parts)

    channel = await structure.get_or_create_backup_channel(guild)
    if channel is None:
        return path, None

    embed = _summary_embed(payload, collector, deleted_by)
    try:
        limit = guild.filesize_limit
        if path.stat().st_size <= limit:
            await channel.send(embed=embed, file=discord.File(path))
        else:
            # 첨부 한도를 넘으면 요약만 올리고 파일은 디스크에만 남긴다.
            embed.add_field(
                name="⚠️ 파일이 너무 큼",
                value=f"`backups/{path.name}` 에 저장했습니다 ({path.stat().st_size:,}B)",
                inline=False,
            )
            await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("백업 채널에 올리지 못했습니다 (파일은 %s 에 있음)", path)
        return path, None

    return path, channel
