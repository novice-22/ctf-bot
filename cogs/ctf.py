"""/ctf 명령어."""

from __future__ import annotations

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

    async def report(done: int, total: int) -> None:
        try:
            await interaction.edit_original_response(
                content=f"⏳ 문제 가져오는 중… {done}/{total}"
            )
        except discord.HTTPException:
            pass

    return report


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
    pool = role.members if role is not None else guild.members

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
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def new(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        ctftime: str | None = None,
        url: str | None = None,
        token: str | None = None,
        starts: str | None = None,
        ends: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # CTFtime 번호를 주면 이름·일정·주소를 채운다. 직접 적은 값이 언제나 우선.
        starts_at = ends_at = None
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

        # 참가 공지는 항상 공지 채널(ctf-notice)에만 올린다.
        # 명령을 친 채널에는 올리지 않는다 — 대회 카테고리는 참가자에게만 보이므로
        # 공지가 모두에게 보이는 곳에 있어야 의미가 있다.
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
        log.info("CTF 생성: %s (id=%s, %s ~ %s)", name, ctf_id, starts_at, ends_at)

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
                f"토큰까지 주면 시작할 때 자동으로 가져와 — `token:` 옵션이나 `/ctf import`."
            )
        elif token:
            message += "\n⚠️ 토큰만 받고 대회 주소(url)가 없어서 저장 못 했어. `url:` 도 같이 줘."
        await interaction.followup.send(message, ephemeral=True)

    @group.command(name="upcoming", description="CTFtime 예정 대회를 보여줄게")
    @app_commands.describe(limit="몇 개 볼래? (기본 5, 최대 10)")
    async def upcoming(self, interaction: discord.Interaction, limit: int = 5) -> None:
        # 예정 대회는 팀 모두가 보게 공개로 답한다 (본인만 보이는 ephemeral 아님).
        await interaction.response.defer()

        try:
            events = await ctftime_api.fetch_upcoming(limit)
        except ctftime_api.CTFtimeError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return

        if not events:
            await interaction.followup.send("다가오는 CTF가 없어.")
            return

        lines = ["🆕 **다가오는 CTF**"]
        for event in events:
            when = discord.utils.format_dt(event["start"], "R") if event["start"] else "?"
            where = "온사이트" if event["onsite"] else "온라인"
            detail = f"{where} {event['format']}".strip()
            weight = f" · weight {event['weight']:.0f}" if event["weight"] else ""
            lines.append(
                f"`{event['id']}` **{event['title']}** — {when} · {detail}{weight}"
            )
        lines.append("\n만들려면 `/ctf new ctftime:<번호>` 로 번호를 넣어줘.")
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
    @app_commands.checks.has_permissions(manage_channels=True)
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
            await interaction.followup.send(f"불러오지 못했어: {exc}", ephemeral=True)
            return

        # 연결정보 저장(덮어쓰기)은 하지 않는다 — 저장은 /ctf creds 로만 한다.
        await interaction.followup.send(result.summary(), ephemeral=True)

    @group.command(name="pull", description="새로 추가된 문제만 가져올게")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def pull(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        ctf = await _resolve_ctf(interaction)
        if ctf is None:
            await interaction.followup.send("대회 카테고리 안에서 실행해줘.", ephemeral=True)
            return

        if not ctf["ctfd_url"] or not ctf["api_token"]:
            await interaction.followup.send(
                "아직 대회 사이트를 연결 안 했어. `/ctf import` 로 주소와 토큰을 한 번 넣어줘.",
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
            await interaction.followup.send(f"불러오지 못했어: {exc}", ephemeral=True)
            return

        await interaction.followup.send(result.summary(), ephemeral=True)

    @group.command(name="creds", description="대회 연결정보(주소·토큰·자동임포트) 확인·설정")
    @app_commands.describe(
        name="대회 이름 (카테고리 안에서 실행하면 생략 가능)",
        token="설정할 토큰/쿠키 (넣으면 저장만, 임포트는 안 해)",
        url="설정할 대회 주소. GZCTF는 게임 주소 …/games/5",
        platform="플랫폼 (토큰과 함께 지정)",
        clear="저장된 토큰을 지울래?",
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
    @app_commands.checks.has_permissions(manage_channels=True)
    async def creds(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        token: str | None = None,
        url: str | None = None,
        platform: str | None = None,
        clear: bool = False,
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
        head = (changed + "\n\n") if changed else ""
        await interaction.followup.send(head + "\n".join(lines), ephemeral=True)

    @group.command(name="archive", description="대회를 지금 바로 정리할게 (스레드만 접음)")
    @app_commands.checks.has_permissions(manage_channels=True)
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
    @app_commands.checks.has_permissions(manage_channels=True)
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
    @app_commands.checks.has_permissions(manage_channels=True)
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
    @app_commands.checks.has_permissions(manage_channels=True)
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

    @group.command(name="deletechallenge", description="필요 없는 문제는 치워줄게")
    @app_commands.describe(name="지울 문제 (안 적으면 지금 있는 스레드의 문제)")
    @app_commands.autocomplete(name=_challenge_autocomplete)
    @app_commands.checks.has_permissions(manage_channels=True)
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
    @app_commands.describe(with_="같이 푼 사람 (여러 명 가능)")
    @app_commands.rename(with_="with")
    @app_commands.autocomplete(with_=_member_autocomplete)
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

        await db.add_solve(challenge["id"], interaction.user.id)
        for uid in contributor_ids:
            await db.add_solve(challenge["id"], uid)

        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            new_name = structure.thread_name(challenge["name"], solved=True)
            if thread.name != new_name:
                await thread.edit(name=new_name)

        # solves 알림 메시지 ID 를 기억해뒀다가, unsolve 하면 그 알림을 지운다.
        solve_message_id = None
        category = interaction.guild.get_channel(ctf["category_id"])
        if category is not None:
            # 이미 풀린 상태에서 다시 호출하면(협력자 추가 등) 이전 알림을 지우고 새로 올린다.
            if challenge["solved"] and challenge["solve_message_id"]:
                await structure.delete_in(category, "solves", challenge["solve_message_id"])
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

        await db.mark_solved(challenge["id"], None, solve_message_id)

        # 결과는 스레드 이름(✅)과 #solves 알림으로 이미 보이므로 확인 메시지는 생략한다.
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

        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            new_name = structure.thread_name(challenge["name"], solved=False)
            if thread.name != new_name:
                await thread.edit(name=new_name)

        solved, total = await db.solve_counts(ctf["id"])
        note = "" if removed else "\n(`#solves` 알림은 못 찾아서 그대로야)"
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
    @app_commands.checks.has_permissions(manage_channels=True)
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
    @app_commands.checks.has_permissions(manage_channels=True)
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
        elif isinstance(original, discord.Forbidden):
            message = (
                "봇 권한이 부족해. 채널/역할/스레드 관리 권한을 확인해줘.\n"
                "역할 부여가 안 되면 봇 역할이 대회 역할보다 위에 있는지도 봐줘."
            )
        else:
            log.exception("명령어 오류", exc_info=error)
            message = f"문제가 생겼어: {original}"

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CTF(bot))
