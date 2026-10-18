"""/ctf 명령어."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import backup
import ctftime as ctftime_api
import db
import importer
import platforms
import structure
import views

log = logging.getLogger("ctfbot.ctf")


# 대회 종료 후 이만큼 지나면 스레드를 접는다 (채널은 그대로 둔다).
ARCHIVE_GRACE = timedelta(days=3)

DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _parse_datetime(raw: str) -> datetime | None:
    """사용자가 적은 시각을 파싱한다. 시간대는 서버 로컬로 해석한다."""
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).astimezone()
        except ValueError:
            continue
    return None


def _stored_dt(value: str | None) -> datetime | None:
    """DB에 저장된 ISO 문자열을 datetime 으로."""
    return datetime.fromisoformat(value) if value else None


def _progress_reporter(interaction: discord.Interaction):
    """임포트 진행률을 본인만 보이는 메시지로 갱신하는 콜백을 만든다."""

    async def report(done: int, total: int, phase: str = "create") -> None:
        # 단계마다 다르게 보여준다 — 대회 서버가 느리면 목록·상세 받는 데만 몇 분 걸려서,
        # 진행 표시가 없으면 "생각 중"에서 멈춘 것처럼 보인다.
        if phase == "join":
            # done 자리에 "몇 초째 진행 중인지"가 들어온다.
            text = (
                f"⏳ 이미 이 대회 문제를 불러오는 중이야 ({done}초째) — "
                "다시 안 눌러도 돼, 끝나면 같은 결과를 여기 보여줄게"
            )
        elif phase == "connect":
            text = "⏳ 대회 서버에서 문제 목록 받는 중… (서버가 느리면 몇 분 걸릴 수 있어)"
        elif phase == "fetch":
            text = f"⏳ 새 문제 정보 받는 중… {done}/{total}"
        else:
            text = f"⏳ 문제 스레드 만드는 중… {done}/{total}"
        try:
            await interaction.edit_original_response(content=text)
        except discord.HTTPException:
            pass

    return report


PLATFORM_NAMES = {"ctfd": "CTFd", "rctf": "rCTF", "noctf": "noCTF", "gzctf": "GZCTF"}


async def _verify_credentials(ctf) -> str:
    """저장된 주소·토큰으로 문제 '목록'만 한 번 받아본다. 결과를 한 줄로 돌려준다.

    상세(설명·첨부)는 안 받는다 — 모든 문제를 '이미 있음'으로 취급해 상세 요청이 0번이라,
    대회 서버엔 목록 요청 한두 번만 간다. 실패해도 예외를 던지지 않는다 (저장은 이미 됐다).
    """
    ctx = platforms.FetchContext(skip=lambda remote_id, name: True)
    try:
        detected, challenges = await platforms.fetch_challenges(
            ctf["ctfd_url"], ctf["api_token"], ctf["platform"] or "auto", ctx
        )
    except platforms.PlatformUnavailable:
        return "⏳ 대회 서버가 응답이 없어서 지금은 확인 못 했어 (저장은 됐어)"
    except platforms.PlatformError as exc:
        return f"⚠️ {exc}"
    except Exception as exc:
        log.exception("자격 확인 중 오류")
        return f"⚠️ 확인 중 오류: {str(exc) or type(exc).__name__}"

    text = f"✅ {PLATFORM_NAMES.get(detected, detected)} 연결 정상 — 지금 문제 {len(challenges)}개 보여"
    if ctf["auto_imported"]:
        # 자동 수집은 이미 끝났으니(실패했을 수도) 지금 직접 불러오라고 알려준다.
        text += " → 이제 `/ctf pull` 하면 불러와"
    return text


async def _finish_progress(interaction: discord.Interaction, text: str) -> None:
    """진행률(⏳) 메시지를 최종 결과로 바꿔 마무리한다. 편집이 안 되면 새로 보낸다."""
    # 디스코드 메시지는 2000자 제한 — 넘으면 편집·전송이 둘 다 거부돼 결과가 사라진다.
    if len(text) > 1900:
        text = text[:1900] + "…"
    try:
        await interaction.edit_original_response(content=text)
    except discord.HTTPException:
        await interaction.followup.send(text, ephemeral=True)


# 슬래시 명령어는 인원 수가 정해지지 않은 목록을 못 받는다. 그래서 문자열로 받아
# 그 안의 멘션을 읽는다 (`@a @b @c` 처럼 몇 명이든 적을 수 있다).
MENTION_RE = re.compile(r"<@!?(\d{15,25})>|(?<!\d)(\d{15,25})(?!\d)")


def _progress(solved: int, total: int) -> str:
    """진행률을 막대로. 예: ▰▰▰▱▱▱▱▱▱▱  3 / 10 solved (30%)"""
    if total <= 0:
        return "문제 없음"
    ratio = solved / total
    filled = round(ratio * 10)
    return f"{'▰' * filled}{'▱' * (10 - filled)}  **{solved} / {total}** solved ({ratio:.0%})"


def _join_names(names: list[str], limit: int = 900) -> str:
    """문제 이름을 쉼표로 잇되, 임베드 칸(1024자)을 넘지 않게 자른다."""
    out: list[str] = []
    used = 0
    for i, name in enumerate(names):
        if used + len(name) + 2 > limit:
            return ", ".join(out) + f" … 외 {len(names) - i}개"
        out.append(name)
        used += len(name) + 2
    return ", ".join(out)


def _parse_mentions(raw: str | None, exclude: int | None = None) -> list[int]:
    """문자열에서 유저 ID를 순서대로 뽑는다. 중복과 exclude 는 제외."""
    if not raw:
        return []
    ids: list[int] = []
    seen = {exclude} if exclude is not None else set()
    for match in MENTION_RE.finditer(raw):
        uid = int(match.group(1) or match.group(2))
        if uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    return ids


async def _resolve_ctf(interaction: discord.Interaction):
    """명령어를 실행한 위치에서 소속 대회를 찾는다."""
    channel = interaction.channel
    category = getattr(channel, "category", None)
    if category is None:
        return None
    return await db.get_ctf_by_category(category.id)


# 대회 카테고리 밖에서도 명령을 받아주는 지정 채널 (이모지 뗀 이름으로 비교).
COMMAND_CHANNEL_NAMES = ("ctf-bot-cmd", "bot-cmd")


def _is_command_channel(channel) -> bool:
    """지정된 커맨드 채널인지. 카테고리 밖에서 대회 이름을 받아 실행할 때 허용한다."""
    name = structure.strip_emoji(getattr(channel, "name", "") or "")
    return name in COMMAND_CHANNEL_NAMES


# 대회 생성·초대 같은 관리 명령은 이 역할(대소문자 무시)만 쓸 수 있다. 서버 관리자는 항상 허용.
LEAD_ROLE_NAMES = ("lead",)


def _has_lead(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.name.lower() in LEAD_ROLE_NAMES for r in member.roles)


def lead_only():
    """lead 역할(또는 관리자)만 통과시키는 앱 커맨드 체크."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if _has_lead(interaction.user):
            return True
        raise app_commands.CheckFailure(
            "이건 **lead** 역할만 쓸 수 있는 명령이야."
        )

    return app_commands.check(predicate)


async def _any_ctf_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """아카이브된 대회까지 포함. 정리 대상은 오히려 끝난 대회인 경우가 많다."""
    rows = await db.list_all_ctfs(interaction.guild_id)
    return [
        app_commands.Choice(name=row["name"], value=row["name"])
        for row in rows
        if current.lower() in row["name"].lower()
    ][:25]


async def _category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """표준 분야를 먼저, 그다음 `별칭 → 표준` 을 제안한다.

    자동완성은 강제가 아니라서 대회 고유의 분야명은 그냥 타이핑해서 넣어도 된다.
    그 경우에도 별칭표를 거쳐 표준 분야로 정리된다.
    """
    current = current.lower()
    aliases = await db.alias_map(interaction.guild_id)
    seen: set[str] = set()
    choices: list[app_commands.Choice[str]] = []

    def add(label: str, value: str, key: str) -> None:
        if key in seen or current not in label.lower():
            return
        seen.add(key)
        choices.append(app_commands.Choice(name=label[:100], value=value[:100]))

    # 1. 표준 분야
    for c in structure.CANONICAL_CATEGORIES:
        add(c, c, c)

    # 2. 이 대회에 이미 있는 분야 (표준에 없는 고유 이름)
    ctf = await _resolve_ctf(interaction)
    if ctf is not None:
        for row in await db.list_challenges(ctf["id"]):
            add(row["category"], row["category"], structure.channel_name(row["category"]))

    # 3. 별칭 — 서버 별칭이 기본 별칭보다 앞
    for table in (aliases, structure.DEFAULT_ALIASES):
        for alias, canonical in table.items():
            add(f"{alias} → {canonical}", canonical, alias)

    return choices[:25]


async def _member_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """대회 참가자를 제안한다. 고를 때마다 앞서 고른 사람 뒤에 이어 붙인다.

    선택지 value 는 100자 제한이라 자동완성으로는 4명 남짓까지만 이어 붙일 수 있다.
    그 이상은 직접 멘션을 타이핑하면 되고, 명령어 쪽은 인원 제한이 없다.
    """
    guild = interaction.guild
    if guild is None:
        return []

    ctf = await _resolve_ctf(interaction)
    role = guild.get_role(ctf["role_id"]) if ctf else None
    # 참가자(역할 보유자)를 먼저 제안하되, 역할이 없어도 도운 사람을 크레딧할 수 있게
    # 나머지 서버 멤버도 뒤에 포함한다 (운영자 권한으로만 보는 사람 등).
    if role is not None:
        role_ids = {m.id for m in role.members}
        pool = list(role.members) + [m for m in guild.members if m.id not in role_ids]
    else:
        pool = guild.members

    chosen = _parse_mentions(current)
    prefix = " ".join(f"<@{uid}>" for uid in chosen)
    # 마지막 멘션 뒤에 남은 글자를 검색어로 본다.
    keyword = MENTION_RE.sub("", current).strip().lower()

    chosen_names = [
        (guild.get_member(uid).display_name if guild.get_member(uid) else str(uid))
        for uid in chosen
    ]

    choices: list[app_commands.Choice[str]] = []
    for member in pool:
        if member.bot or member.id == interaction.user.id or member.id in chosen:
            continue
        if keyword and keyword not in member.display_name.lower() and keyword not in member.name.lower():
            continue

        value = f"{prefix} <@{member.id}>".strip()
        if len(value) > 100:
            break  # 더 이어 붙이면 잘려서 망가진다
        label = ", ".join(chosen_names + [member.display_name])
        choices.append(app_commands.Choice(name=label[:100], value=value))
        if len(choices) >= 25:
            break
    return choices


async def _alias_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """이 서버에서 추가한 별칭만 제안한다 (기본 제공 규칙은 지울 수 없다)."""
    saved = await db.alias_map(interaction.guild_id)
    return [
        app_commands.Choice(name=f"{alias} → {canonical}", value=alias)
        for alias, canonical in sorted(saved.items())
        if current.lower() in alias.lower()
    ][:25]


async def _seen_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """임포트하며 축적된 대회 원본 분야명을 제안한다."""
    names = await db.seen_categories(interaction.guild_id)
    return [
        app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:25]


async def _challenge_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """현재 대회의 문제 이름을 제안한다."""
    ctf = await _resolve_ctf(interaction)
    if ctf is None:
        return []
    rows = await db.list_challenges(ctf["id"])
    return [
        app_commands.Choice(name=f"[{row['category']}] {row['name']}", value=row["name"])
        for row in rows
        if current.lower() in row["name"].lower()
    ][:25]


# /ctf invite·kick 에서 쓰는 멤버 다중 선택 드롭다운.
# 봇이 멤버 목록을 직접 채워서, 대상 전원이 한눈에 보이게 한다 (디스코드 기본 UserSelect 는
# 몇 명씩만 보여주고 검색해야 해서, 목록형 Select 로 만든다). 옵션은 최대 25개.
MEMBER_SELECT_LIMIT = 25


class _MemberActionSelect(discord.ui.Select):
    def __init__(self, ctf_id: int, role_id: int, mode: str, members: list) -> None:
        verb = "초대할" if mode == "invite" else "내보낼"
        options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=f"@{m.name}"[:100],
            )
            for m in members[:MEMBER_SELECT_LIMIT]
        ]
        super().__init__(
            placeholder=f"{verb} 사람을 골라줘 (여러 명 가능)",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.ctf_id = ctf_id
        self.role_id = role_id
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        # 드롭다운을 지우고 "처리 중"으로 바꾼 뒤 실제 작업을 한다 (역할·스레드는 시간이 걸린다).
        await interaction.response.edit_message(content="⏳ 처리 중…", view=None)
        guild = interaction.guild
        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.edit_original_response(content="이 대회의 참가 역할을 못 찾았어.")
            return

        ids = [int(v) for v in self.values]
        if self.mode == "invite":
            threads = []
            for ch in await db.list_challenges(self.ctf_id):
                th = guild.get_channel_or_thread(ch["thread_id"])
                if isinstance(th, discord.Thread):
                    threads.append(th)
            done, failed = [], []
            for uid in ids:
                member = guild.get_member(uid)
                if member is None:
                    failed.append(f"<@{uid}>")
                    continue
                try:
                    await member.add_roles(role, reason="CTF 초대 (invite)")
                except discord.HTTPException:
                    failed.append(member.mention)
                    continue
                for th in threads:
                    try:
                        await th.add_user(member)
                    except discord.HTTPException:
                        pass
                    await asyncio.sleep(0.2)
                done.append(member.mention)
            parts = []
            if done:
                parts.append(f"✅ 참여시킴: {', '.join(done)}")
            if failed:
                parts.append(
                    f"⚠️ 실패: {', '.join(failed)} — 봇 역할이 대회 역할보다 위에 있는지 확인해줘."
                )
        else:  # kick
            done, missing = [], []
            for uid in ids:
                member = guild.get_member(uid)
                if member is None or role not in member.roles:
                    missing.append(f"<@{uid}>")
                    continue
                try:
                    await member.remove_roles(role, reason="CTF 내보내기 (kick)")
                    done.append(member.mention)
                except discord.HTTPException:
                    missing.append(member.mention)
            parts = []
            if done:
                parts.append(f"✅ 내보냄: {', '.join(done)}")
            if missing:
                parts.append(f"⚠️ 대상 아님/실패: {', '.join(missing)}")

        await interaction.edit_original_response(
            content="\n".join(parts) or "아무도 안 바뀌었어."
        )


class _MemberActionView(discord.ui.View):
    def __init__(self, ctf_id: int, role_id: int, mode: str, members: list) -> None:
        super().__init__(timeout=300)
        self.add_item(_MemberActionSelect(ctf_id, role_id, mode, members))


class CTF(commands.Cog):
    group = app_commands.Group(name="ctf", description="CTF 관리", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------- 대회 ----------

    @group.command(name="new", description="새 CTF를 만들어줄게")
    @app_commands.describe(
        name="대회 이름 (카테고리 이름이 됩니다). CTFtime 번호를 주면 자동으로 채워져",
        ctftime="CTFtime 대회 번호 또는 링크 (예: 2401 / ctftime.org/event/2401)",
        url="대회 사이트 주소 (CTFtime 번호를 주면 자동으로 채워져). GZCTF는 게임 주소 …/games/5",
        token="토큰/쿠키. 넣어두면 시작할 때 문제를 자동으로 가져와 (CTFd·rCTF·noCTF·GZCTF)",
        starts="대회 시작 시각 (예: 2026-08-01 09:00). CTFtime 값을 덮어써",
        ends="대회 종료 시각 (예: 2026-08-02 21:00). 넣으면 3일 뒤 자동 아카이브",
        private="비공개로 만들래? 공지·참가버튼·디스코드 이벤트를 안 만들어 (지정한 사람만 /ctf invite 로 추가)",
    )
    @lead_only()
    async def new(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        ctftime: str | None = None,
        url: str | None = None,
        token: str | None = None,
        starts: str | None = None,
        ends: str | None = None,
        private: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # CTFtime 번호를 주면 이름·일정·주소를 채운다. 직접 적은 값이 언제나 우선.
        starts_at = ends_at = None
        ctftime_link = None
        if ctftime:
            event_id = ctftime_api.parse_event_ref(ctftime)
            if event_id is None:
                await interaction.followup.send(
                    f"`{ctftime}` 에서 CTFtime 번호를 못 읽었어. `2401` 이나 링크를 줘.",
                    ephemeral=True,
                )
                return
            try:
                event = await ctftime_api.fetch_event(event_id)
            except ctftime_api.CTFtimeError as exc:
                await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
                return
            name = name or event["title"]
            url = url or event["url"] or None
            starts_at, ends_at = event["start"], event["finish"]
            ctftime_link = f"https://ctftime.org/event/{event['id']}"

        if not name:
            await interaction.followup.send(
                "대회 이름을 적거나 CTFtime 번호를 줘.", ephemeral=True
            )
            return
        # 디스코드 역할·카테고리 이름은 100자 제한. CTFtime 제목이 길 수 있어 자른다.
        name = name.strip()[:100]

        if await db.get_ctf_by_name(interaction.guild.id, name):
            await interaction.followup.send(f"`{name}` 은(는) 이미 있어.", ephemeral=True)
            return

        # 직접 적은 시각이 있으면 CTFtime 값을 덮어쓴다.
        for label, raw in (("starts", starts), ("ends", ends)):
            if raw:
                parsed = _parse_datetime(raw)
                if parsed is None:
                    await interaction.followup.send(
                        f"`{raw}` 를 시각으로 못 읽겠어. `2026-08-01 21:00` 형태로 적어줘.",
                        ephemeral=True,
                    )
                    return
                if label == "starts":
                    starts_at = parsed
                else:
                    ends_at = parsed

        # 공개 대회는 참가 공지를 공지 채널(ctf-notice)에 올린다. 명령 친 채널에는 안 올린다 —
        # 대회 카테고리는 참가자에게만 보이므로 공지는 모두 보이는 곳에 있어야 의미가 있다.
        # 비공개(private) 대회는 공지·참가버튼을 아예 안 올리므로 공지 채널이 없어도 된다.
        notice = None
        if not private:
            notice = structure.find_notice_channel(interaction.guild)
            if notice is None:
                await interaction.followup.send(
                    "공지 채널이 없어서 대회를 안 만들었어.\n"
                    f"`ctf-notice` 채널을 만들고 다시 실행해줘. "
                    f"(찾는 이름: {', '.join(structure.NOTICE_CHANNEL_NAMES)})",
                    ephemeral=True,
                )
                return

        ctf_id, category = await structure.create_ctf(
            interaction.guild, name, starts_at=starts_at, ends_at=ends_at
        )
        # 주소+토큰을 함께 저장하면 시작 시 자동 임포트가 켜진다. 주소만 있으면 주소만.
        if url and token:
            await db.set_credentials(ctf_id, url, token, "auto")
        elif url:
            await db.set_ctf_url(ctf_id, url)
        log.info("CTF 생성: %s (id=%s, %s ~ %s, private=%s)", name, ctf_id, starts_at, ends_at, private)

        if private:
            message = (
                f"🔒 **{name}** 을 비공개로 만들었어. 공지·참가버튼·디스코드 이벤트는 안 올렸어.\n"
                f"참여시킬 사람은 이 대회 카테고리 안에서 `/ctf invite @사람들` 로 추가해줘."
            )
        else:
            try:
                await views.post_announcement(notice, ctf_id, name, starts_at, ends_at)
            except discord.HTTPException:
                await interaction.followup.send(
                    f"**{name}** 은 만들었는데 {notice.mention} 에 공지를 못 올렸어. "
                    f"봇이 그 채널에 쓸 수 있는지 확인해줘.",
                    ephemeral=True,
                )
                return
            message = f"**{name}** 만들었어. {notice.mention} 에 참가 공지 올렸어."
        if url and token:
            message += "\n🔗 주소·토큰 저장 완료 — 시작하면 문제를 자동으로 가져올게."
        elif url:
            message += (
                f"\n🔗 대회 주소 연결됨: {url}\n"
                f"`/ctf creds token:토큰` 으로 토큰까지 저장하면 시작할 때 자동으로 가져와."
            )
        elif token:
            message += (
                "\n⚠️ 토큰만 받고 대회 주소(url)가 없어서 저장 못 했어. "
                "`/ctf creds url:대회주소 token:토큰` 으로 넣어줘."
            )

        # 비공개 대회는 디스코드 이벤트를 안 만든다 — 이벤트는 서버 전체에 이름이 보여서다.
        if private:
            if starts_at and ends_at:
                message += "\n(비공개라 디스코드 이벤트는 안 만들었어.)"
            await interaction.followup.send(message, ephemeral=True)
            return

        # 일정이 다 있으면 디스코드 예약 이벤트도 만든다. 이 이벤트가 다시
        # on_scheduled_event_create 를 부르지만, 방금 만든 대회를 이름으로 찾아
        # 건너뛰므로 중복 생성되지 않는다. (external 이벤트라 시작·종료·위치가 필수)
        if starts_at and ends_at:
            kwargs = dict(
                name=name,
                start_time=starts_at,
                end_time=ends_at,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=(url or ctftime_link or name)[:100],
                reason="CTF 대회 이벤트",
            )
            if ctftime_link:
                kwargs["description"] = f"CTFtime: {ctftime_link}"
            try:
                sched = await interaction.guild.create_scheduled_event(**kwargs)
                await db.set_ctf_event(ctf_id, sched.id)
                message += "\n📅 디스코드 이벤트도 만들었어."
            except discord.Forbidden:
                message += (
                    "\n⚠️ 디스코드 이벤트는 못 만들었어 — 봇에 **이벤트 관리(Manage Events)** 권한을 줘."
                )
            except discord.HTTPException as exc:
                message += f"\n⚠️ 디스코드 이벤트 생성 실패: {exc}"
        elif ctftime or starts or ends:
            message += "\n(일정이 없어서 디스코드 이벤트는 안 만들었어 — `starts:`·`ends:` 둘 다 있어야 해.)"

        await interaction.followup.send(message, ephemeral=True)

    @group.command(name="invite", description="지정한 사람만 이 대회에 참여시킬게 (비공개 대회용)")
    @lead_only()
    async def invite(self, interaction: discord.Interaction) -> None:
        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.response.send_message(
                "대회 카테고리 안에서 실행해줘.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(ctf["role_id"]) if ctf["role_id"] else None
        if role is None:
            await interaction.response.send_message(
                "이 대회의 참가 역할을 못 찾았어.", ephemeral=True
            )
            return
        # 봇을 뺀 서버 멤버 전원 (이름순). 아직 참가 안 한 사람이 위로 오게 정렬.
        members = sorted(
            (m for m in interaction.guild.members if not m.bot),
            key=lambda m: (role in m.roles, m.display_name.lower()),
        )
        if not members:
            await interaction.response.send_message("초대할 멤버가 없어.", ephemeral=True)
            return
        note = ""
        if len(members) > MEMBER_SELECT_LIMIT:
            note = f"\n(멤버가 많아 {MEMBER_SELECT_LIMIT}명까지만 목록에 떠 — 더 있으면 나눠서 초대해줘.)"
        await interaction.response.send_message(
            "초대할 사람을 골라줘 — 한 번에 여러 명 선택 가능:" + note,
            view=_MemberActionView(ctf["id"], ctf["role_id"], "invite", members),
            ephemeral=True,
        )

    @group.command(name="kick", description="이 대회에서 내보낼게 (참가 역할 회수)")
    @lead_only()
    async def kick(self, interaction: discord.Interaction) -> None:
        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.response.send_message(
                "대회 카테고리 안에서 실행해줘.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(ctf["role_id"]) if ctf["role_id"] else None
        if role is None:
            await interaction.response.send_message(
                "이 대회의 참가 역할을 못 찾았어.", ephemeral=True
            )
            return
        # 내보내기 대상은 현재 참가자(역할 보유자)만 목록에 띄운다.
        members = sorted(
            (m for m in role.members if not m.bot),
            key=lambda m: m.display_name.lower(),
        )
        if not members:
            await interaction.response.send_message(
                "이 대회에 참가자가 없어.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "내보낼 사람을 골라줘 (여러 명 선택 가능):",
            view=_MemberActionView(ctf["id"], ctf["role_id"], "kick", members),
            ephemeral=True,
        )

    @group.command(name="help", description="명령어 사용법을 간단히 보여줄게 (나만 보임)")
    async def help_(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📖 /ctf 명령어 안내",
            description=(
                "🔒 = **lead** 역할만 사용 가능\n"
                "대부분 **대회 카테고리 안 채널**에서 실행해 (솔브는 **문제 스레드 안**)"
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="🏁 대회 만들기 🔒",
            value=(
                "`/ctf new ctftime:2401`\n"
                "→ CTFtime 번호로 이름·일정·주소 자동 + 디스코드 이벤트 생성\n"
                "`/ctf new name:대회이름 url:https://ctf.site token:토큰`\n"
                "→ 직접 입력. 토큰까지 넣으면 **대회 시작 30초 뒤부터 문제 자동 수집** (최대 3번)\n"
                "`/ctf new ctftime:2401 private:true`\n"
                "→ 비공개 대회 (공지·참가버튼 없음, invite로만 참여)"
            ),
            inline=False,
        )
        embed.add_field(
            name="👥 참가하기",
            value=(
                "공개 대회 → `ctf-notice` 공지의 **[참가할게]** 버튼\n"
                "`/ctf invite` 🔒 → 드롭다운에서 여러 명 골라 한 번에 초대\n"
                "`/ctf kick` 🔒 → 참가자 중 골라서 내보내기"
            ),
            inline=False,
        )
        embed.add_field(
            name="📥 문제 불러오기",
            value=(
                "`/ctf creds url:https://ctf.site token:토큰` 🔒\n"
                "→ 주소·토큰 저장 (저장하면 바로 연결 확인, 토큰만 바꿀 땐 `token:` 만)\n"
                "`/ctf import` 🔒 → 처음 한 번 전체 불러오기\n"
                "`/ctf pull` → 새로 나온 문제만 추가 (누구나)\n"
                "`/ctf creds check:True` 🔒 → 지금 저장된 주소·토큰이 실제로 되는지 확인\n"
                "지원: CTFd · rCTF · noCTF · GZCTF (GZCTF는 `…/games/번호` 주소)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧩 풀이",
            value=(
                "`/ctf joinall` → 모든 문제 스레드에 한 번에 참여\n"
                "`/ctf solve` → 풀이 등록, #solves 에 알림 (스레드당 1번)\n"
                "`/ctf solve with:@철수 @영희` → 같이 푼 사람 함께 (@ 치고 골라)\n"
                "`/ctf unsolve` → 잘못 등록했을 때 취소"
            ),
            inline=False,
        )
        embed.add_field(
            name="✏️ 문제 관리",
            value=(
                "`/ctf createchallenge category:web name:문제이름` → 수동 추가\n"
                "`/ctf deletechallenge` → (스레드 안) 이 문제 삭제, 기록은 백업\n"
                "`/ctf button name:문제이름` → 지워진 참가 버튼 다시 올리기\n"
                "`/ctf alias add from:binary to:pwn` → 분야 이름 묶기"
            ),
            inline=False,
        )
        embed.add_field(
            name="📊 확인",
            value=(
                "`/ctf challenges` → 문제 목록 (`category:web` 로 분야만)\n"
                "`/ctf scoreboard` → 누가 몇 개 풀었나\n"
                "`/ctf upcoming days:7` → 7일 안에 시작하는 CTFtime 대회"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ 운영 🔒",
            value=(
                "`/ctf schedule ends:2026-10-20 21:00` → 일정 변경 (공지도 같이 수정)\n"
                "`/ctf archive` → 스레드 접어 정리 (종료 3일 뒤엔 자동)\n"
                "`/ctf delete name:대회이름` → 대회 통째 삭제 (삭제 전 백업)"
            ),
            inline=False,
        )
        embed.set_footer(text="자동 수집이 실패하면 /ctf pull · 이 안내는 나에게만 보여")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="upcoming", description="CTFtime 진행중·예정 대회를 보여줄게")
    @app_commands.describe(
        days="지금 시간 기준으로 며칠 이내 시작하는 대회를 불러와. 예) 오늘 수요일에 7 넣으면 다음 주 수요일까지",
    )
    async def upcoming(
        self, interaction: discord.Interaction, days: int | None = None
    ) -> None:
        # 예정 대회는 팀 모두가 보게 공개로 답한다 (본인만 보이는 ephemeral 아님).
        await interaction.response.defer()

        try:
            if days is not None:
                events = await ctftime_api.fetch_upcoming(within_days=days)
            else:
                events = await ctftime_api.fetch_upcoming()
        except ctftime_api.CTFtimeError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return

        if not events:
            span = f"앞으로 {days}일 안에" if days else "다가오는"
            await interaction.followup.send(f"{span} 열리는 CTF가 없어.")
            return

        header = (
            f"🗓️ **진행중 · {days}일 내 시작 CTF**" if days else "🗓️ **진행중 · 다가오는 CTF**"
        )
        lines = [header]
        chars = len(header)
        for i, event in enumerate(events):
            if event.get("running"):
                mark = "🔴"
                when = (
                    f"{discord.utils.format_dt(event['finish'], 'R')} 종료"
                    if event["finish"]
                    else "진행중"
                )
            else:
                mark = "🆕"
                when = discord.utils.format_dt(event["start"], "R") if event["start"] else "?"
            where = "온사이트" if event["onsite"] else "온라인"
            detail = f"{where} {event['format']}".strip()
            weight = f" · weight {event['weight']:.0f}" if event["weight"] else ""
            main = f"`{event['id']}` {mark} **{event['title']}** — {when} · {detail}{weight}"

            # 사이트 링크 + CTFtime 링크. <> 로 감싸 미리보기 카드는 숨긴다.
            link_parts = []
            if event["url"]:
                link_parts.append(f"🔗 <{event['url']}>")
            link_parts.append(f"CTFtime <https://ctftime.org/event/{event['id']}>")
            link = "　" + " · ".join(link_parts)

            # 디스코드 2000자 제한. 넘기 전에 멈추고 남은 개수를 알린다.
            if chars + len(main) + len(link) + 2 > 1900:
                lines.append(f"…외 {len(events) - i}개 더 (기간·개수를 좁혀봐)")
                break
            lines.append(main)
            lines.append(link)
            chars += len(main) + len(link) + 2

        await interaction.followup.send("\n".join(lines))

    @group.command(name="import", description="저장된 정보로 문제를 불러올게 (토큰 저장은 /ctf creds)")
    @app_commands.describe(
        token="일회성 토큰/쿠키 (안 주면 저장된 값을 씀). 저장은 안 해 — 저장은 /ctf creds",
        url="대회 주소 (안 주면 저장된 주소를 씀). GZCTF는 게임 주소 …/games/5",
        platform="플랫폼 (안 고르면 자동 판별)",
        name="대회 이름. #ctf-bot-cmd 처럼 카테고리 밖에서 실행할 때 어느 대회인지 지정",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="자동 판별", value="auto"),
            app_commands.Choice(name="CTFd", value="ctfd"),
            app_commands.Choice(name="rCTF", value="rctf"),
            app_commands.Choice(name="noCTF", value="noctf"),
            app_commands.Choice(name="GZCTF", value="gzctf"),
        ]
    )
    @app_commands.autocomplete(name=_any_ctf_autocomplete)
    @lead_only()
    async def import_(
        self,
        interaction: discord.Interaction,
        token: str | None = None,
        url: str | None = None,
        platform: str = "auto",
        name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            # 카테고리 밖이면 지정 커맨드 채널에서 대회 이름을 줘야 한다.
            if not _is_command_channel(interaction.channel):
                await interaction.followup.send(
                    "대회 카테고리 안이나 `#ctf-bot-cmd` 에서 실행해줘.", ephemeral=True
                )
                return
            if not name:
                await interaction.followup.send(
                    "여기선 어느 대회인지 몰라. `name:` 으로 대회를 골라줘.", ephemeral=True
                )
                return
            ctf = await db.get_ctf_by_name(interaction.guild.id, name)
            if ctf is None:
                await interaction.followup.send(
                    f"`{name}` 대회를 못 찾았어.", ephemeral=True
                )
                return

        # 주소·토큰은 저장된 값을 기본으로 쓴다. 인자로 주면 이번 한 번만 쓰고 저장하지 않는다.
        target = url or ctf["ctfd_url"]
        if not target:
            await interaction.followup.send(
                "대회 주소를 모르겠어. `url` 을 적거나 `/ctf creds` 로 주소를 넣어줘.",
                ephemeral=True,
            )
            return

        use_token = token or ctf["api_token"]
        if not use_token:
            await interaction.followup.send(
                "토큰이 없어. `/ctf creds token:...` 로 먼저 넣거나 `token:` 을 줘.",
                ephemeral=True,
            )
            return

        try:
            result = await importer.sync(
                interaction.guild, ctf, target, use_token, platform,
                progress=_progress_reporter(interaction),
            )
        except platforms.PlatformError as exc:
            # "⏳ 받는 중…" 진행 메시지를 그대로 두지 않고 결과로 바꿔 쓴다.
            await _finish_progress(interaction, f"⚠️ 불러오지 못했어: {exc}")
            return

        # 연결정보 저장(덮어쓰기)은 하지 않는다 — 저장은 /ctf creds 로만 한다.
        # 진행 메시지(⏳)를 최종 요약으로 바꿔 완료 표시한다.
        mark = "✅" if result.complete else "⚠️"
        await _finish_progress(interaction, f"{mark} {result.summary()}")

    @group.command(name="pull", description="새로 추가된 문제만 가져올게")
    async def pull(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        if not ctf["ctfd_url"] or not ctf["api_token"]:
            # 저장은 /ctf creds 로만 된다 (/ctf import 에 준 주소·토큰은 그 한 번만 쓰고 저장 안 함).
            missing = "·".join(
                w for w, v in (("주소", ctf["ctfd_url"]), ("토큰", ctf["api_token"])) if not v
            )
            await interaction.followup.send(
                f"아직 대회 {missing}이(가) 저장 안 돼 있어.\n"
                "**lead** 가 `/ctf creds url:대회주소 token:토큰` 으로 저장하면 그다음부터 `/ctf pull` 이 돼.",
                ephemeral=True,
            )
            return

        try:
            result = await importer.sync(
                interaction.guild,
                ctf,
                ctf["ctfd_url"],
                ctf["api_token"],
                ctf["platform"] or "auto",
                progress=_progress_reporter(interaction),
            )
        except platforms.PlatformError as exc:
            # "⏳ 받는 중…" 진행 메시지를 그대로 두지 않고 결과로 바꿔 쓴다.
            await _finish_progress(interaction, f"⚠️ 불러오지 못했어: {exc}")
            return

        mark = "✅" if result.complete else "⚠️"
        await _finish_progress(interaction, f"{mark} {result.summary()}")

    @group.command(name="creds", description="대회 연결정보(주소·토큰·자동임포트) 확인·설정")
    @app_commands.describe(
        name="대회 이름 (카테고리 안에서 실행하면 생략 가능)",
        token="설정할 토큰/쿠키 (넣으면 저장만, 임포트는 안 해)",
        url="설정할 대회 주소. GZCTF는 게임 주소 …/games/5",
        platform="플랫폼 (토큰과 함께 지정)",
        clear="저장된 토큰을 지울래?",
        check="저장된 주소·토큰이 실제로 되는지 지금 확인할래? (대회 서버에 목록만 한 번 물어봐)",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="자동 판별", value="auto"),
            app_commands.Choice(name="CTFd", value="ctfd"),
            app_commands.Choice(name="rCTF", value="rctf"),
            app_commands.Choice(name="noCTF", value="noctf"),
            app_commands.Choice(name="GZCTF", value="gzctf"),
        ]
    )
    @app_commands.autocomplete(name=_any_ctf_autocomplete)
    @lead_only()
    async def creds(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        token: str | None = None,
        url: str | None = None,
        platform: str | None = None,
        clear: bool = False,
        check: bool = False,
    ) -> None:
        # 토큰까지 그대로 보여주므로 반드시 본인만 보이게(ephemeral) 답한다.
        await interaction.response.defer(ephemeral=True)

        if name:
            ctf = await db.get_ctf_by_name(interaction.guild.id, name)
            if ctf is None:
                await interaction.followup.send(f"`{name}` 대회를 못 찾았어.", ephemeral=True)
                return
        else:
            ctf = await _resolve_ctf(interaction)
            if ctf is None:
                await interaction.followup.send(
                    "대회 카테고리 안에서 실행하거나 `name:` 으로 대회를 골라줘.", ephemeral=True
                )
                return

        # ---- 변경 요청 먼저 반영 ----
        changed = None
        if clear:
            await db.clear_credentials(ctf["id"])
            changed = "🗑️ 저장된 토큰을 지웠어."
        elif token or url:
            final_url = url or ctf["ctfd_url"]
            if token and not final_url:
                await interaction.followup.send(
                    "토큰을 저장하려면 대회 주소도 필요해. `url:` 도 같이 줘.", ephemeral=True
                )
                return
            if token:
                await db.set_credentials(
                    ctf["id"], final_url, token, platform or ctf["platform"] or "auto"
                )
                changed = "✅ 주소·토큰 저장했어."
            else:  # url 만
                await db.set_ctf_url(ctf["id"], url)
                changed = "✅ 주소 저장했어."
        if changed:
            ctf = await db.get_ctf(ctf["id"])

        # ---- 확인: 주소·토큰을 바꿨거나 check 를 켜면 대회 서버에 목록만 한 번 물어본다 ----
        # (틀린 토큰이 저장된 채로 대회를 맞지 않게 — 오늘처럼 무효 토큰이 들어가는 걸 바로 잡는다)
        verify = None
        if (changed and not clear) or check:
            if ctf["ctfd_url"] and ctf["api_token"]:
                verify = await _verify_credentials(ctf)
            elif check:
                missing = [w for w, v in (("주소", ctf["ctfd_url"]), ("토큰", ctf["api_token"])) if not v]
                verify = (
                    f"⚠️ 확인할 수 없어 — {'·'.join(missing)}이(가) 저장 안 돼 있어. "
                    "`/ctf creds url:… token:…` 으로 먼저 넣어줘."
                )

        # ---- 상태 출력 ----
        status = "아카이브" if ctf["status"] == "archived" else "진행중"
        url_disp = ctf["ctfd_url"] or "없음"
        platform_disp = {
            "ctfd": "CTFd",
            "rctf": "rCTF",
            "noctf": "noCTF",
            "gzctf": "GZCTF",
        }.get(ctf["platform"], "자동판별")
        token_disp = f"`{ctf['api_token']}`" if ctf["api_token"] else "없음"

        starts = _stored_dt(ctf["starts_at"])
        ends = _stored_dt(ctf["ends_at"])
        if starts or ends:
            s = discord.utils.format_dt(starts, "f") if starts else "?"
            e = discord.utils.format_dt(ends, "f") if ends else "?"
            schedule = f"{s} ~ {e}"
        else:
            schedule = "없음"

        # 자동 임포트 조건: 주소 + 토큰 + 시작시각. 이미 했으면 완료 표시.
        if ctf["auto_imported"]:
            auto = "☑️ 이미 임포트됨 (완료)"
        elif ctf["ctfd_url"] and ctf["api_token"] and ctf["starts_at"]:
            auto = "✅ 켜짐 — 시작하면 자동 실행"
        else:
            missing = []
            if not ctf["ctfd_url"]:
                missing.append("주소")
            if not ctf["api_token"]:
                missing.append("토큰")
            if not ctf["starts_at"]:
                missing.append("시작시각")
            auto = "✗ 꺼짐 — " + "·".join(missing) + " 없음"

        lines = [
            f"🏷️ **{ctf['name']}** ({status})",
            f"📅 일정: {schedule}",
            f"🔗 주소: {url_disp}",
            f"🧩 플랫폼: {platform_disp}",
            f"🔑 토큰: {token_disp}",
            f"⚙️ 자동 임포트: {auto}",
        ]
        if verify:
            lines.append(f"🔎 확인: {verify}")
        head = (changed + "\n\n") if changed else ""
        await interaction.followup.send(head + "\n".join(lines), ephemeral=True)

    @group.command(name="archive", description="대회를 지금 바로 정리할게 (스레드만 접음)")
    @lead_only()
    async def archive(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        category = interaction.guild.get_channel(ctf["category_id"])
        count = await structure.archive_threads(category) if category else 0

        await db.archive_ctf(ctf["id"])
        solved, total = await db.solve_counts(ctf["id"])
        await interaction.followup.send(
            f"**{ctf['name']}** 정리했어. 스레드 {count}개 보관, 최종 {solved}/{total} solved.\n"
            f"채널은 그대로 남아 있어.",
            ephemeral=True,
        )

    @group.command(name="schedule", description="대회 일정을 바꿀게 (공지도 같이 고쳐 씀)")
    @app_commands.describe(
        starts="시작 시각 (예: 2026-08-01 09:00)",
        ends="종료 시각 (예: 2026-08-02 21:00). 종료 3일 뒤 자동 아카이브",
        clear="일정을 지울래? (자동 아카이브도 꺼짐)",
    )
    @lead_only()
    async def schedule(
        self,
        interaction: discord.Interaction,
        starts: str | None = None,
        ends: str | None = None,
        clear: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        if clear:
            starts_at = ends_at = None
        else:
            # 안 적은 쪽은 지금 값을 그대로 둔다.
            starts_at = _stored_dt(ctf["starts_at"])
            ends_at = _stored_dt(ctf["ends_at"])
            for label, raw in (("starts", starts), ("ends", ends)):
                if not raw:
                    continue
                parsed = _parse_datetime(raw)
                if parsed is None:
                    await interaction.followup.send(
                        f"`{raw}` 를 시각으로 못 읽겠어. `2026-08-01 21:00` 형태로 적어줘.",
                        ephemeral=True,
                    )
                    return
                if label == "starts":
                    starts_at = parsed
                else:
                    ends_at = parsed

            if starts_at is None and ends_at is None:
                await interaction.followup.send(
                    "`starts` 나 `ends` 중 하나는 적어줘. 지우려면 `clear:True`.",
                    ephemeral=True,
                )
                return

        await db.set_ctf_schedule(ctf["id"], starts_at, ends_at)

        # 이미 올라간 공지도 새 일정으로 고쳐 쓴다.
        ctf = await db.get_ctf(ctf["id"])
        await views.update_announcement(interaction.guild, ctf, starts_at, ends_at)

        if starts_at is None and ends_at is None:
            note = "일정을 지웠어. 자동 아카이브도 꺼졌어."
        else:
            parts = []
            if starts_at:
                parts.append(f"시작: {discord.utils.format_dt(starts_at, 'f')}")
            if ends_at:
                parts.append(f"종료: {discord.utils.format_dt(ends_at, 'f')}")
                parts.append(
                    f"자동 아카이브: {discord.utils.format_dt(ends_at + ARCHIVE_GRACE, 'f')}"
                )
            note = "\n".join(parts)

        await interaction.followup.send(f"**{ctf['name']}**\n{note}", ephemeral=True)

    @group.command(name="delete", description="대회를 통째로 삭제할게 (채널/역할/기록 전부)")
    @app_commands.describe(name="삭제할 대회 이름")
    @app_commands.autocomplete(name=_any_ctf_autocomplete)
    @lead_only()
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await db.get_ctf_by_name(interaction.guild_id, name)
        if ctf is None:
            await interaction.followup.send(f"`{name}` 이라는 대회가 없어.", ephemeral=True)
            return

        category = interaction.guild.get_channel(ctf["category_id"])

        view = views.ConfirmDelete(interaction.user.id, label="삭제할게")
        await interaction.followup.send(
            f"⚠️ **{ctf['name']}** 대회 전체\n"
            f"복구할 수 없습니다. 삭제하시겠습니까?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.confirmed:
            await interaction.followup.send("취소했어. 아무것도 안 지웠어.", ephemeral=True)
            return

        # 지우기 전에 기록부터 남긴다. 대화와 첨부 파일까지 챙기느라 시간이 걸린다.
        path = backup_channel = None
        if backup.enabled():
            await interaction.followup.send(
                "대화와 첨부 파일을 백업하는 중이야. 잠깐 기다려줘…", ephemeral=True
            )
            payload, collector = await backup.build_ctf(ctf, interaction.guild)
            path, backup_channel = await backup.save(
                interaction.guild, payload, collector, interaction.user, ctf["name"]
            )

        # 하나가 실패해도(권한/일시적 오류) 나머지는 계속 지운다. DB 행은 마지막에
        # 반드시 지워 상태가 어긋나지 않게 하고, 못 지운 건 사용자에게 알린다.
        failed = []
        if category is not None:
            for channel in list(category.channels):
                try:
                    await channel.delete(reason=f"CTF 삭제: {name}")
                except discord.HTTPException:
                    failed.append(f"#{channel.name}")
            try:
                await category.delete(reason=f"CTF 삭제: {name}")
            except discord.HTTPException:
                failed.append(f"카테고리 {category.name}")

        role = interaction.guild.get_role(ctf["role_id"])
        if role is not None:
            try:
                await role.delete(reason=f"CTF 삭제: {name}")
            except discord.HTTPException:
                failed.append(f"역할 {role.name}")

        # 참가 공지(ctf-notice)는 카테고리 밖이라 함께 안 지워진다. 따로 지운다.
        if ctf["notice_channel_id"] and ctf["notice_message_id"]:
            notice = interaction.guild.get_channel(ctf["notice_channel_id"])
            if notice is not None:
                try:
                    msg = await notice.fetch_message(ctf["notice_message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await db.delete_ctf(ctf["id"])
        log.info("CTF 삭제: %s (백업: %s)", name, path.name if path else "꺼짐")

        leftover = (
            f"\n⚠️ 이건 못 지웠어 (권한 확인): {', '.join(failed)}" if failed else ""
        )
        if path is not None:
            where = backup_channel.mention if backup_channel else f"`backups/{path.name}`"
            await interaction.followup.send(
                f"**{ctf['name']}** 지웠어. 기록은 {where} 에 백업해뒀어.{leftover}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"**{ctf['name']}** 지웠어. (백업 꺼짐){leftover}", ephemeral=True
            )

    # ---------- 문제 ----------

    @group.command(name="createchallenge", description="새 문제를 추가할게")
    @app_commands.describe(category="문제 분류 (목록에서 고르거나 직접 입력)", name="문제 이름")
    @app_commands.autocomplete(category=_category_autocomplete)
    async def createchallenge(
        self, interaction: discord.Interaction, category: str, name: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        discord_category = interaction.guild.get_channel(ctf["category_id"])
        if discord_category is None:
            await interaction.followup.send("대회 카테고리가 사라졌어.", ephemeral=True)
            return

        aliases = await db.alias_map(interaction.guild_id)
        category = structure.resolve_category(category, aliases)
        channel = await structure.get_or_create_category_channel(discord_category, category)
        thread = await structure.create_challenge_thread(channel, name)
        await db.add_challenge(ctf["id"], name, category, channel.id, thread.id)

        # 스레드와 참가 버튼이 이미 채널에 보이므로 확인 메시지는 생략한다.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    @group.command(name="button", description="문제 참가 버튼을 다시 올릴게 (실수로 지웠을 때)")
    @app_commands.describe(name="버튼을 다시 만들 문제")
    @app_commands.autocomplete(name=_challenge_autocomplete)
    async def button(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        # 이름이 같은 문제가 여러 개일 수 있어(분야가 다르거나 중복 등록) 전부 처리한다.
        matches = await db.find_challenges_by_name(ctf["id"], name)
        if not matches:
            await interaction.followup.send(f"`{name}` 문제가 없어.", ephemeral=True)
            return

        # 기존 스레드를 가리키는 참가 버튼만 다시 올린다 — 스레드·내용은 그대로 둔다.
        posted = []
        for challenge in matches:
            channel = interaction.guild.get_channel(challenge["channel_id"])
            thread = interaction.guild.get_channel_or_thread(challenge["thread_id"])
            if channel is None or thread is None:
                continue
            await channel.send(
                f"**{challenge['name']}**",
                view=views.challenge_view(challenge["thread_id"]),
            )
            posted.append(thread.mention)

        if not posted:
            await interaction.followup.send(
                "문제 채널이나 스레드를 못 찾았어 (지워졌을 수 있어).", ephemeral=True
            )
        elif len(posted) > 1:
            await interaction.followup.send(
                f"이름이 같은 문제가 {len(posted)}개라 전부 버튼을 다시 올렸어: "
                + ", ".join(posted),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{posted[0]} 참가 버튼 다시 올렸어.", ephemeral=True
            )

    @group.command(name="joinall", description="이 대회의 모든 문제 스레드에 한 번에 참여할게")
    async def joinall(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        challenges = await db.list_challenges(ctf["id"])
        if not challenges:
            await interaction.followup.send("아직 문제가 없어.", ephemeral=True)
            return

        joined = missing = 0
        for i, ch in enumerate(challenges):
            thread = interaction.guild.get_channel_or_thread(ch["thread_id"])
            if not isinstance(thread, discord.Thread):
                missing += 1
                continue
            try:
                await thread.add_user(interaction.user)
                joined += 1
            except discord.HTTPException:
                missing += 1
            # 진행률 갱신 + 디스코드 레이트리밋 완화용 간격
            if i and i % 10 == 0:
                try:
                    await interaction.edit_original_response(
                        content=f"⏳ 참여 중… {joined}/{len(challenges)}"
                    )
                except discord.HTTPException:
                    pass
            await asyncio.sleep(0.25)

        message = f"✅ **{joined}개** 문제 스레드에 참여했어."
        if missing:
            message += f" ({missing}개는 스레드를 못 찾음)"
        try:
            await interaction.edit_original_response(content=message)
        except discord.HTTPException:
            await interaction.followup.send(message, ephemeral=True)

    @group.command(name="deletechallenge", description="필요 없는 문제는 치워줄게")
    @app_commands.describe(name="지울 문제 (안 적으면 지금 있는 스레드의 문제)")
    @app_commands.autocomplete(name=_challenge_autocomplete)
    async def deletechallenge(
        self, interaction: discord.Interaction, name: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if name is not None:
            ctf_row = await _resolve_ctf(interaction)
            if ctf_row is None:
                await interaction.followup.send(
                    "대회 카테고리 안에서 실행해줘.", ephemeral=True
                )
                return
            matches = await db.find_challenges_by_name(ctf_row["id"], name)
            if not matches:
                await interaction.followup.send(
                    f"`{name}` 이라는 문제가 없어.", ephemeral=True
                )
                return
            if len(matches) > 1:
                # 엉뚱한 걸 지우지 않도록, 어느 분야인지 정해질 때까지 멈춘다.
                where = ", ".join(f"`{m['category']}`" for m in matches)
                await interaction.followup.send(
                    f"`{name}` 이라는 문제가 {where} 에 있어. "
                    f"해당 문제 스레드 안에서 실행해줘.",
                    ephemeral=True,
                )
                return
            challenge = matches[0]
        else:
            challenge = await db.get_challenge_by_thread(interaction.channel_id)
            if challenge is None:
                await interaction.followup.send(
                    "문제 스레드 안에서 실행하거나, `name` 에 문제 이름을 적어줘.",
                    ephemeral=True,
                )
                return

        mark = structure.SOLVED if challenge["solved"] else structure.UNSOLVED

        view = views.ConfirmDelete(interaction.user.id, label="삭제할게")
        await interaction.followup.send(
            f"⚠️ {mark} **{challenge['name']}** [{challenge['category']}]\n"
            f"복구할 수 없습니다. 삭제하시겠습니까?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.confirmed:
            await interaction.followup.send("취소했어. 안 지웠어.", ephemeral=True)
            return

        ctf = await db.get_ctf(challenge["ctf_id"])
        path = None
        if backup.enabled():
            payload, collector = await backup.build_challenge(ctf, challenge, interaction.guild)
            path, _ = await backup.save(
                interaction.guild,
                payload,
                collector,
                interaction.user,
                ctf["name"] if ctf else "unknown",
                challenge["name"],
            )

        await db.delete_challenge(challenge["id"])
        log.info("문제 삭제: %s (백업: %s)", challenge["name"], path.name if path else "꺼짐")

        thread = interaction.guild.get_channel_or_thread(challenge["thread_id"])
        if thread is not None:
            await thread.delete(reason="문제 삭제")

        # 스레드 밖에서 지웠다면 이 응답이 살아있으므로 결과를 알려준다.
        if interaction.channel_id != challenge["thread_id"]:
            note = "기록은 백업해뒀어." if path is not None else "(백업 꺼짐)"
            await interaction.followup.send(
                f"**{challenge['name']}** 지웠어. {note}", ephemeral=True
            )

    @group.command(name="solve", description="풀었어? 그럼 등록해")
    @app_commands.describe(with_="같이 푼 사람 — @멘션으로 여러 명 (@철수 @영희). @ 치고 골라")
    @app_commands.rename(with_="with")
    async def solve(
        self, interaction: discord.Interaction, with_: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        challenge = await db.get_challenge_by_thread(interaction.channel_id)
        if challenge is None:
            await interaction.followup.send("문제 스레드 안에서 실행해줘.", ephemeral=True)
            return

        contributor_ids = _parse_mentions(with_, exclude=interaction.user.id)
        if with_ and not contributor_ids:
            await interaction.followup.send(
                "같이 푼 사람을 못 찾겠어. `@이름` 으로 멘션해줘.", ephemeral=True
            )
            return

        ctf = await db.get_ctf(challenge["ctf_id"])

        # 원자적 게이트: 스레드당 solve 는 딱 한 번. 여러 번·동시에 눌러도 첫 호출만
        # 통과한다. 이미 풀렸으면 '이미 솔브됨'만 알리고 끝 (unsolve 하면 다시 열림).
        if not await db.claim_solve(challenge["id"]):
            # 누가 등록했는지 알려준다 (스레드 이름 ✅ 는 디스코드 제한으로 늦게 바뀔 수 있어서
            # 화면만 보면 "안 풀렸는데 왜?" 가 된다). 멘션 알림은 안 가게 이름만 보여준다.
            solvers = await db.solvers_of(challenge["id"])
            who = f" — <@{solvers[0]['user_id']}> 이(가) 등록했어" if solvers else ""
            await interaction.followup.send(
                f"이미 솔브된 문제야{who}.\n"
                "잘못 등록된 거면 `/ctf unsolve` 한 뒤 다시 해줘.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # 처음 푼 경우에만 풀이자 기록 + 스레드 ✅ + #solves 공지.
        await db.add_solve(challenge["id"], interaction.user.id)
        for uid in contributor_ids:
            await db.add_solve(challenge["id"], uid)

        # #solves 공지를 올리고 그 알림 ID 를 저장한다 (unsolve 하면 그 알림을 지운다).
        # 스레드 이름(✅)보다 먼저 한다 — 이름 변경은 디스코드 제한으로 몇 분 밀릴 수 있다.
        solve_message_id = None
        category = interaction.guild.get_channel(ctf["category_id"])
        if category is not None:
            msg = await structure.post_to(
                category,
                "solves",
                embed=structure.solve_embed(
                    challenge["name"],
                    challenge["category"],
                    interaction.user,
                    contributor_ids,
                ),
            )
            solve_message_id = msg.id if msg else None
        await db.set_solve_message(challenge["id"], solve_message_id)

        # 스레드 이름 ✅ 는 뒤에서 맞춘다 (기다리지 않는다). 제한 때문에 늦어질 상황이면 안내한다.
        delayed = False
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            delayed = structure.sync_thread_name(
                thread, challenge["id"], structure.thread_name(challenge["name"], solved=True)
            )
        if delayed:
            posted = " #solves 에도 올렸어." if solve_message_id else ""
            await interaction.followup.send(
                f"✅ 솔브 등록했어.{posted}{structure.RENAME_DELAY_NOTE}", ephemeral=True
            )
            return
        # 결과는 #solves 알림과 스레드 이름(✅)으로 보이므로 확인 메시지는 생략한다.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    @group.command(name="unsolve", description="아직 안 풀었는데 실수로 등록했어? 되돌릴게")
    async def unsolve(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        challenge = await db.get_challenge_by_thread(interaction.channel_id)
        if challenge is None:
            await interaction.followup.send("문제 스레드 안에서 실행해줘.", ephemeral=True)
            return
        if not challenge["solved"]:
            await interaction.followup.send("이 문제는 아직 안 풀린 상태야.", ephemeral=True)
            return

        ctf = await db.get_ctf(challenge["ctf_id"])

        # solves 채널에 올라간 축하 알림을 지운다.
        removed = False
        if challenge["solve_message_id"]:
            category = interaction.guild.get_channel(ctf["category_id"])
            if category is not None:
                removed = await structure.delete_in(
                    category, "solves", challenge["solve_message_id"]
                )

        await db.mark_unsolved(challenge["id"])

        # 스레드 이름 ❌ 는 뒤에서 맞춘다 (명령이 디스코드 이름 변경 제한을 기다리지 않게).
        delayed = False
        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            delayed = structure.sync_thread_name(
                thread, challenge["id"], structure.thread_name(challenge["name"], solved=False)
            )

        solved, total = await db.solve_counts(ctf["id"])
        note = "" if removed else "\n(`#solves` 알림은 못 찾아서 그대로야)"
        if delayed:
            note += structure.RENAME_DELAY_NOTE
        await interaction.followup.send(
            f"**{challenge['name']}** 풀이를 취소했어. ({solved}/{total} solved){note}",
            ephemeral=True,
        )

    # ---------- 분야 별칭 ----------

    alias_group = app_commands.Group(
        name="alias",
        description="분야 이름 바꿔치기 규칙 (예: 대회의 binary 를 우리 pwn 으로)",
        parent=group,
    )

    @alias_group.command(name="list", description="지금 걸려 있는 별칭을 보여줄게")
    async def alias_list(self, interaction: discord.Interaction) -> None:
        saved = await db.alias_map(interaction.guild_id)

        embed = discord.Embed(
            title="분야 별칭",
            description=(
                "대회마다 분야 이름이 달라서, 아래 규칙으로 우리 표준 분야에 맞춘다.\n"
                "`/ctf import` 와 `/ctf createchallenge` 에 적용된다."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name=f"이 서버에서 추가한 것 ({len(saved)}개)",
            value=(
                "\n".join(f"`{a}` → **{c}**" for a, c in sorted(saved.items()))[:1024]
                if saved
                else "없음 — `/ctf alias add` 로 추가"
            ),
            inline=False,
        )
        defaults = "\n".join(
            f"`{a}` → **{c}**" for a, c in sorted(structure.DEFAULT_ALIASES.items())
        )
        embed.add_field(
            name=f"기본 제공 ({len(structure.DEFAULT_ALIASES)}개)",
            value=defaults[:1024],
            inline=False,
        )
        embed.set_footer(text="같은 이름을 추가하면 기본 제공 규칙을 덮어쓴다")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alias_group.command(name="add", description="별칭을 추가하거나 바꿀게 (from → to)")
    @app_commands.describe(
        from_="대회에서 쓰는 이름 (예: binary)",
        to="우리 표준 분야 (예: pwn)",
    )
    @app_commands.rename(from_="from")
    @app_commands.autocomplete(from_=_seen_autocomplete, to=_category_autocomplete)
    async def alias_add(
        self, interaction: discord.Interaction, from_: str, to: str
    ) -> None:
        slug = structure.channel_name(from_)
        target = to.strip()

        saved = await db.alias_map(interaction.guild_id)
        before = saved.get(slug) or structure.DEFAULT_ALIASES.get(slug)

        await db.set_alias(interaction.guild_id, slug, target)

        if before == target:
            note = " (원래도 같은 규칙이었어)"
        elif before:
            note = f" (`{slug}` → **{before}** 였던 걸 바꿨어)"
        else:
            note = ""
        await interaction.response.send_message(
            f"`{slug}` → **{target}**{note}\n"
            f"이제 대회에서 `{from_}` 로 오는 문제는 **{target}** 채널로 들어가.",
            ephemeral=True,
        )

    @alias_group.command(name="remove", description="추가했던 별칭을 지울게")
    @app_commands.describe(from_="지울 별칭 (이 서버에서 추가한 것만)")
    @app_commands.rename(from_="from")
    @app_commands.autocomplete(from_=_alias_autocomplete)
    async def alias_remove(self, interaction: discord.Interaction, from_: str) -> None:
        saved = await db.alias_map(interaction.guild_id)
        slug = structure.channel_name(from_)
        if slug not in saved:
            fallback = structure.DEFAULT_ALIASES.get(slug)
            hint = (
                f"\n`{slug}` → **{fallback}** 는 기본 제공 규칙이라 지울 수 없어. "
                f"다르게 쓰고 싶으면 `/ctf alias add` 로 덮어써."
                if fallback
                else ""
            )
            await interaction.response.send_message(
                f"이 서버에 `{slug}` 별칭이 없어.{hint}", ephemeral=True
            )
            return

        await db.remove_alias(interaction.guild_id, slug)
        fallback = structure.DEFAULT_ALIASES.get(slug)
        note = f" 이제 기본 규칙(**{fallback}**)이 적용돼." if fallback else ""
        await interaction.response.send_message(
            f"`{slug}` → **{saved[slug]}** 별칭을 지웠어.{note}", ephemeral=True
        )

    # ---------- 현황 ----------

    @group.command(name="scoreboard", description="누가 얼마나 풀었나 볼까")
    async def scoreboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.")
            return

        rows = await db.scoreboard(ctf["id"])
        solved, total = await db.solve_counts(ctf["id"])

        embed = discord.Embed(
            title=f"{ctf['name']} 현황",
            description=_progress(solved, total),
            colour=discord.Colour.blurple(),
        )
        if rows:
            # 등수는 매기지 않는다. 팀 안에서 서로 줄 세울 이유가 없다.
            # 임베드 전체 6000자 제한이 있어, 근처에 오면 멈추고 생략 표시한다.
            total = len(embed.title or "") + len(embed.description or "")
            for user_id, names in rows[:20]:
                field_name = f"{len(names)}문제"
                field_value = f"<@{user_id}>\n{_join_names(names)}"
                if total + len(field_name) + len(field_value) > 5500:
                    embed.set_footer(text="일부 인원은 생략됐어 (너무 길어서)")
                    break
                embed.add_field(name=field_name, value=field_value, inline=False)
                total += len(field_name) + len(field_value)
        else:
            embed.add_field(name="푼 사람", value="아직 없음", inline=False)

        await interaction.followup.send(embed=embed)

    @group.command(name="challenges", description="문제 목록 보여줄게")
    @app_commands.describe(
        category="특정 분야만 볼래? (안 고르면 전체)",
        name="특정 문제 하나만 자세히 볼래?",
    )
    @app_commands.autocomplete(category=_category_autocomplete, name=_challenge_autocomplete)
    async def challenges(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        name: str | None = None,
    ) -> None:
        await interaction.response.defer()

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.")
            return

        # 문제 이름을 고르면 그 문제 상세만 보여준다.
        if name is not None:
            row = await db.get_challenge_by_name(ctf["id"], name)
            if row is None:
                await interaction.followup.send(f"`{name}` 이라는 문제가 없어.")
                return

            mark = structure.SOLVED if row["solved"] else structure.UNSOLVED
            embed = discord.Embed(
                title=f"{mark} {row['name']}",
                description=f"분야: **{row['category']}** · 스레드: <#{row['thread_id']}>",
                colour=discord.Colour.green() if row["solved"] else discord.Colour.red(),
            )
            solvers = await db.solvers_of(row["id"])
            if solvers:
                embed.add_field(
                    name="Solved by",
                    value=" ".join(f"<@{s['user_id']}>" for s in solvers),
                    inline=False,
                )
            if row["flag"]:
                embed.add_field(name="Flag", value=f"`{row['flag']}`", inline=False)
            await interaction.followup.send(embed=embed)
            return

        rows = await db.list_challenges(ctf["id"])
        if category is not None:
            wanted = structure.channel_name(category)
            rows = [r for r in rows if structure.channel_name(r["category"]) == wanted]
        if not rows:
            what = f"`{category}` 분야 문제가" if category else "문제가"
            await interaction.followup.send(f"{what} 없어. `/ctf import` 로 불러와.")
            return

        by_category: dict[str, list[str]] = {}
        for row in rows:
            mark = structure.SOLVED if row["solved"] else structure.UNSOLVED
            by_category.setdefault(row["category"], []).append(f"{mark} <#{row['thread_id']}>")

        solved, total = await db.solve_counts(ctf["id"])
        embed = discord.Embed(
            title=f"{ctf['name']} 문제",
            description=_progress(solved, total),
            colour=discord.Colour.blurple(),
        )
        # 임베드는 필드 25개·전체 6000자 제한. 분야명이 길거나 많으면 잘라낸다.
        chars = len(embed.title or "") + len(embed.description or "")
        omitted = 0
        for cat_name, items in sorted(by_category.items()):
            field_name = cat_name[:256]
            field_value = "\n".join(items)[:1024]
            if len(embed.fields) >= 25 or chars + len(field_name) + len(field_value) > 5500:
                omitted += 1
                continue
            embed.add_field(name=field_name, value=field_value, inline=True)
            chars += len(field_name) + len(field_value)
        if omitted:
            embed.set_footer(text=f"분야 {omitted}개는 생략됐어 (너무 많아서)")

        await interaction.followup.send(embed=embed)

    # ---------- 에러 ----------

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            message = "이 명령어를 쓸 권한이 없어."
        elif isinstance(error, app_commands.CheckFailure):
            # lead_only()·MissingRole 등 체크 실패. 메시지가 있으면 그대로 보여준다.
            message = str(error) or "이 명령어를 쓸 권한이 없어."
        elif isinstance(original, discord.Forbidden):
            message = (
                "봇 권한이 부족해. 채널/역할/스레드 관리 권한을 확인해줘.\n"
                "역할 부여가 안 되면 봇 역할이 대회 역할보다 위에 있는지도 봐줘."
            )
        elif isinstance(original, TimeoutError):
            # 대회 서버·디스코드가 느려서 시간 초과. 예전엔 메시지가 빈 채로 "문제가 생겼어:" 만 떴다.
            log.warning("명령어 시간 초과: %s", interaction.command and interaction.command.qualified_name)
            message = "응답이 너무 늦어서 시간 초과됐어 (대회 서버가 느리거나 터진 상태일 수 있어). 잠시 뒤 다시 해줘."
        else:
            log.exception("명령어 오류", exc_info=error)
            # 예외 메시지가 비어 있으면 최소한 종류라도 보여준다.
            message = f"문제가 생겼어: {str(original) or type(original).__name__}"

        # 응답 창이 이미 닫혔으면(15분 초과 등) 보내기도 실패한다 — 로그만 남기고 넘어간다.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("오류 안내를 못 보냄: %s", message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CTF(bot))
