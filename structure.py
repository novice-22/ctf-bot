"""디스코드 구조 생성 로직.

    카테고리(대회) > 채널(분류) > 스레드(문제)

디스코드는 카테고리 중첩을 지원하지 않으므로 스레드를 세 번째 단계로 쓴다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

import discord

import db
import views

log = logging.getLogger("ctfbot.structure")

SOLVED = "✅"
UNSOLVED = "❌"

# 대회 생성 시 기본으로 깔리는 공용 채널. {키: 실제 채널 이름}
# 키는 코드에서 채널을 찾을 때 쓰고, 값은 디스코드에 보이는 이름(이모지 포함)이다.
COMMON_CHANNELS = {
    "general": "general",
    "credentials": "🔑-credentials",
    "notes": "📝-notes",
    "solves": "🎉-solves",
    "bot-cmd": "🤖-bot-cmd",
}

# 문제 분야 채널 앞에 붙일 이모지.
CATEGORY_EMOJI = {
    "pwn": "🐚",
    "rev": "⚙️",
    "crypto": "🔑",
    "web": "🌐",
    "forensic": "🔬",
    "misc": "🔍",
    "osint": "🕵️",
    "ai": "🤖",
    "cloud": "☁️",
    "blockchain": "₿",
    "hardware": "🔌",
    "mobile": "📱",
}

# 자동완성에서 먼저 보여줄 표준 분야 (순서 유지).
CANONICAL_CATEGORIES = tuple(CATEGORY_EMOJI)

# 참가 버튼이 붙은 대회 공지를 올릴 공개 채널 후보 (순서대로 탐색).
NOTICE_CHANNEL_NAMES = ("ctf-notice", "bot-notice", "notice", "공지")

# 삭제 전 백업을 보관할 채널. 플래그가 들어있으므로 @everyone 은 볼 수 없다.
# 대회 카테고리 밖(서버 최상단)에 두어야 대회를 지워도 백업이 살아남는다.
BACKUP_CHANNEL_NAMES = ("ctf-backup", "backup", "백업")
BACKUP_CHANNEL_DISPLAY = "🗄️-ctf-backup"

# 7일. 대회 중에 스레드가 저절로 접히지 않도록.
AUTO_ARCHIVE = 10080


def channel_name(raw: str) -> str:
    """디스코드 채널 이름 규칙(소문자, 공백 불가)에 맞게 정리. 이모지는 떨어져 나간다."""
    name = re.sub(r"[^\w\-]+", "-", raw.strip().lower(), flags=re.UNICODE).strip("-")
    return (name or "misc")[:100]


def strip_emoji(name: str) -> str:
    """'🔑-credentials' → 'credentials'. 채널을 찾을 때 이모지 유무와 무관하게 비교하려고 쓴다."""
    return re.sub(r"^[^\w\-]+-?", "", name, flags=re.UNICODE)


def category_channel_name(raw: str) -> str:
    """문제 분야 채널의 표시 이름. 아는 분야면 이모지를 붙인다."""
    slug = channel_name(raw)
    emoji = CATEGORY_EMOJI.get(slug)
    return f"{emoji}-{slug}" if emoji else slug


# 대회마다 제각각인 분야명 → 우리 표준 분야. 서버별 /ctf alias 로 추가/덮어쓰기 가능.
# 키는 channel_name() 을 거친 형태(소문자, 공백은 하이픈)여야 한다.
DEFAULT_ALIASES = {
    # web
    "website": "web",
    "webex": "web",
    "web-exploitation": "web",
    "webhacking": "web",
    "web-hacking": "web",
    "webappsec": "web",
    "websec": "web",
    # pwn
    "pwnable": "pwn",
    "pwnage": "pwn",
    "exploitation": "pwn",
    "binary-exploitation": "pwn",
    "binary": "pwn",
    "binexp": "pwn",
    # rev
    "reverse": "rev",
    "reverse-engineering": "rev",
    "reversing": "rev",
    "re": "rev",
    # crypto
    "cryptography": "crypto",
    "cry": "crypto",
    # forensic
    "forensics": "forensic",
    "dfir": "forensic",
    # 그 외
    "machine-learning": "ai",
    "ml": "ai",
    "smart-contracts": "blockchain",
    "hw": "hardware",
}


def resolve_category(raw: str, guild_aliases: dict[str, str]) -> str:
    """분야명을 별칭표에 따라 표준 분야로 바꾼다. 서버 별칭이 기본 별칭보다 우선.

    표준 분야는 대소문자가 달라도 같은 것으로 본다 ('Web' 과 'web' 이 따로 쌓이면
    문제 목록이 갈라진다). 모르는 분야는 대회가 쓰는 표기를 그대로 살린다.
    """
    slug = channel_name(raw)
    resolved = guild_aliases.get(slug) or DEFAULT_ALIASES.get(slug)
    if resolved:
        return resolved
    return slug if slug in CATEGORY_EMOJI else raw


def thread_name(name: str, solved: bool) -> str:
    return f"{SOLVED if solved else UNSOLVED} {name}"[:100]


# ---------- 스레드 이름(✅/❌) 맞추기 ----------
# 디스코드는 채널·스레드 이름 변경을 '채널마다 10분에 2번'으로 막는다 (디스코드 API 제한).
# 예전엔 solve/unsolve 가 이름 바꾸기를 기다리느라 최대 10분 멈췄고, 그사이 쌓인 명령들이
# 늦게 한꺼번에 풀리면서 솔브 알림이 중복되고 ✅/❌ 가 실제 상태와 어긋났다.
# 이제 이름은 뒤에서 따로 바꾸고, 바꿀 때마다 '그 순간의 실제 상태(DB)'에 맞춘다.
RENAME_LIMIT = 2
RENAME_WINDOW = 600.0
RENAME_DELAY_NOTE = "\n(스레드 이름은 디스코드 제한 — 이름 변경 10분에 2번 — 때문에 몇 분 뒤에 바뀌어)"
_rename_times: dict[int, list[float]] = {}
_rename_tasks: dict[int, asyncio.Task] = {}


def _rename_wait(thread_id: int) -> float:
    """지금 이름을 바꾸면 제한에 걸리는지. 걸리면 풀릴 때까지 남은 초, 아니면 0."""
    now = time.monotonic()
    recent = [t for t in _rename_times.get(thread_id, []) if now - t < RENAME_WINDOW]
    _rename_times[thread_id] = recent
    if len(recent) < RENAME_LIMIT:
        return 0.0
    return recent[-RENAME_LIMIT] + RENAME_WINDOW - now + 1.0


def sync_thread_name(thread: discord.Thread, challenge_id: int, expected: str) -> bool:
    """스레드 이름을 실제 풀이 상태에 맞추는 작업을 뒤에서 건다 (명령은 기다리지 않는다).

    같은 스레드에 이미 걸린 작업이 있으면 새로 만들지 않는다 — 그 작업이 이름을 바꾸기
    직전에 DB 를 다시 읽으므로 최신 상태로 맞춰진다. 이름 변경이 디스코드 제한으로
    미뤄질 상황이면 True (호출한 쪽이 "몇 분 뒤 바뀌어" 라고 안내할 수 있게).
    """
    pending = _rename_tasks.get(thread.id)
    busy = pending is not None and not pending.done()
    if not busy:
        _rename_tasks[thread.id] = asyncio.create_task(_sync_thread_name(thread, challenge_id))
    return thread.name != expected and (busy or _rename_wait(thread.id) > 0)


async def _sync_thread_name(thread: discord.Thread, challenge_id: int) -> None:
    thread_id = thread.id
    try:
        # 바꾸는 사이 상태가 또 바뀌었으면 다시 맞춘다 — 최대 3번 (무한 반복 없음).
        for _ in range(3):
            # 제한에 걸려 있으면 풀릴 때까지 먼저 기다리고, 그다음에 상태를 읽는다.
            # (상태를 먼저 읽고 기다리면, 기다리는 사이 unsolve 해도 낡은 이름으로 바뀐다.)
            wait = _rename_wait(thread_id)
            if wait > 0:
                await asyncio.sleep(wait)
            row = await db.get_challenge_by_thread(thread_id)
            if row is None or row["id"] != challenge_id:
                return
            want = thread_name(row["name"], solved=bool(row["solved"]))
            if thread.name == want:
                return
            thread = await thread.edit(name=want)
            _rename_times.setdefault(thread_id, []).append(time.monotonic())
    except discord.HTTPException as exc:
        log.warning("스레드 이름 변경 실패 (%s): %s", thread_id, exc)
    except Exception:
        log.exception("스레드 이름 맞추기 오류 (%s)", thread_id)
    finally:
        if _rename_tasks.get(thread_id) is asyncio.current_task():
            del _rename_tasks[thread_id]


async def create_ctf(
    guild: discord.Guild,
    name: str,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    event_id: int | None = None,
) -> tuple[int, discord.CategoryChannel]:
    """대회 카테고리 + 참가 역할 + 공용 채널을 만들고 DB에 등록한다.

    중간에 실패하면(권한 부족 등) 만들다 만 역할·카테고리를 되돌린다.
    반쪽짜리 대회(고아 역할/카테고리, 공지 없는 DB 행)를 남기지 않기 위해서다.
    """
    role = None
    category = None
    try:
        role = await guild.create_role(name=name, mentionable=True, reason="CTF 참가 역할")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
        }
        category = await guild.create_category(name, overwrites=overwrites, reason="CTF 생성")

        for display in COMMON_CHANNELS.values():
            await category.create_text_channel(display)

        ctf_id = await db.add_ctf(
            guild.id,
            name,
            category.id,
            role.id,
            starts_at=starts_at,
            ends_at=ends_at,
            event_id=event_id,
        )
        return ctf_id, category
    except Exception:
        await _rollback_ctf(role, category)
        raise


async def _rollback_ctf(
    role: discord.Role | None, category: discord.CategoryChannel | None
) -> None:
    """create_ctf 가 중간에 실패했을 때 만들어둔 역할·채널을 최선을 다해 지운다."""
    if category is not None:
        for channel in list(category.channels):
            try:
                await channel.delete(reason="CTF 생성 실패 롤백")
            except discord.HTTPException:
                pass
        try:
            await category.delete(reason="CTF 생성 실패 롤백")
        except discord.HTTPException:
            pass
    if role is not None:
        try:
            await role.delete(reason="CTF 생성 실패 롤백")
        except discord.HTTPException:
            pass


async def get_or_create_category_channel(
    category: discord.CategoryChannel, raw_name: str
) -> discord.TextChannel:
    """문제 분류(pwn, crypto…) 채널을 찾거나 만든다."""
    slug = channel_name(raw_name)
    for channel in category.text_channels:
        if strip_emoji(channel.name) == slug:
            return channel
    return await category.create_text_channel(
        category_channel_name(raw_name), reason="문제 분류 채널"
    )


async def create_challenge_thread(
    channel: discord.TextChannel,
    name: str,
    description: str | None = None,
    files: list[discord.File] | None = None,
) -> discord.Thread:
    """분야 채널에 문제 스레드를 만들고, 채널에는 참가 버튼만 남긴다.

    **비공개 스레드**를 쓴다. 공개 스레드는 디스코드가 채널에 "스레드를 시작했어요"
    미리보기를 자동으로 붙여서, 문제가 많아지면 채널이 그 줄로 도배된다. 비공개
    스레드는 그런 흔적을 남기지 않으므로 채널에는 우리가 올린 버튼 메시지만 보인다.

    설명과 첨부파일은 스레드 안에 올린다. 참가 버튼을 누른 사람만 스레드에 들어간다.
    """
    try:
        thread = await channel.create_thread(
            name=thread_name(name, solved=False),
            type=discord.ChannelType.private_thread,
            invitable=True,
            auto_archive_duration=AUTO_ARCHIVE,
            reason="문제 스레드",
        )
    except discord.Forbidden:
        # "비공개 스레드 만들기" 권한이 없으면 공개 스레드로 물러난다.
        log.warning(
            "비공개 스레드 권한이 없어 공개 스레드로 만듭니다. "
            "서버 설정 > 역할 > (봇 역할) 에서 '비공개 스레드 만들기' 를 켜주세요."
        )
        thread = await channel.create_thread(
            name=thread_name(name, solved=False),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=AUTO_ARCHIVE,
            reason="문제 스레드",
        )

    # 채널에는 참가 버튼만. 상태(✅/❌)는 스레드 이름이 들고 있으므로 이름만 적는다.
    await channel.send(f"**{name}**", view=views.challenge_view(thread.id))

    # 설명·첨부파일은 스레드 안에 올린다.
    if description:
        for chunk in _split(description.strip(), 1900):
            await thread.send(chunk)
    if files:
        # 디스코드는 한 메시지에 파일 10개까지.
        for i in range(0, len(files), 10):
            await thread.send(files=files[i : i + 10])

    return thread


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


def find_notice_channel(guild: discord.Guild):
    """참가 공지를 올릴 공개 채널을 찾는다 (텍스트/공지/포럼). 없으면 시스템 채널로 폴백."""
    candidates = list(guild.text_channels) + list(guild.forums)
    for name in NOTICE_CHANNEL_NAMES:
        for channel in candidates:
            if strip_emoji(channel.name) == name:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages or (
                    isinstance(channel, discord.ForumChannel) and perms.create_public_threads
                ):
                    return channel
    system = guild.system_channel
    if system is not None and system.permissions_for(guild.me).send_messages:
        return system
    return None


async def get_or_create_backup_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """백업 보관 채널을 찾거나 만든다.

    백업 파일에는 플래그가 들어있으므로 @everyone 은 보지 못하게 만든다
    (관리자는 디스코드 규칙상 항상 볼 수 있다). 대회 카테고리에 넣지 않아야
    대회를 삭제해도 백업이 함께 사라지지 않는다.
    """
    for name in BACKUP_CHANNEL_NAMES:
        for channel in guild.text_channels:
            if strip_emoji(channel.name) == name:
                return channel

    try:
        return await guild.create_text_channel(
            BACKUP_CHANNEL_DISPLAY,
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True),
            },
            topic="삭제된 대회/문제의 기록 보관소. 플래그가 포함되어 있습니다.",
            reason="CTF 백업 채널",
        )
    except discord.Forbidden:
        return None


async def archive_threads(category: discord.CategoryChannel) -> int:
    """문제 스레드를 접는다.

    채널과 카테고리는 **절대 건드리지 않는다.** 아카이브는 보기 정리일 뿐이고,
    내용은 전부 그대로 남아 언제든 다시 읽을 수 있다. 삭제는 /ctf delete 만 한다.
    """
    count = 0
    for channel in category.text_channels:
        for thread in channel.threads:
            if not thread.archived:
                await thread.edit(archived=True)
                count += 1
    return count


async def post_to(
    category: discord.CategoryChannel,
    key: str,
    message: str | None = None,
    embed: discord.Embed | None = None,
    file: discord.File | None = None,
) -> discord.Message | None:
    """공용 채널에 메시지/임베드/파일. key 는 COMMON_CHANNELS 의 키.

    이모지를 뗀 이름으로 비교해서, 이모지가 붙기 전에 만든 대회에서도 찾을 수 있다.
    보낸 메시지를 돌려준다 (나중에 그 메시지를 지워야 할 때 쓴다). 채널이 없으면 None.
    """
    for channel in category.text_channels:
        if strip_emoji(channel.name) == key:
            kwargs = {"content": message, "embed": embed}
            if file is not None:
                kwargs["file"] = file
            # 대회 역할 멘션은 실제로 알림이 울려야 한다 (@everyone 은 막아둔다).
            kwargs["allowed_mentions"] = discord.AllowedMentions(
                everyone=False, roles=True, users=True
            )
            return await channel.send(**{k: v for k, v in kwargs.items() if v is not None})

    log.warning("'%s' 채널이 없어 메시지를 보내지 못했습니다 (%s)", key, category.name)
    return None


async def delete_in(category: discord.CategoryChannel, key: str, message_id: int) -> bool:
    """공용 채널에서 특정 메시지를 지운다. 성공하면 True."""
    for channel in category.text_channels:
        if strip_emoji(channel.name) == key:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
                return True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
    return False


def solve_embed(
    challenge_name: str,
    category_name: str,
    finder: discord.Member,
    contributor_ids: list[int] | None = None,
) -> discord.Embed:
    """풀이 알림. 명령을 친 사람이 플래그를 찾은 사람, with 로 넣은 사람은 협력자."""
    embed = discord.Embed(
        title="🎉 Challenge solved!",
        description=f"**{challenge_name}** [{category_name}]",
        colour=discord.Colour.green(),
    )
    embed.add_field(name="Flag found by", value=finder.mention, inline=False)
    if contributor_ids:
        embed.add_field(
            name="Contributors",
            value=" ".join(f"<@{uid}>" for uid in contributor_ids),
            inline=False,
        )
    embed.set_thumbnail(url=finder.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    return embed
