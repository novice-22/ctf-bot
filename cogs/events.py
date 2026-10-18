"""디스코드 이벤트 연동 + 자동 아카이브.

디스코드 기본 "이벤트"에 대회를 등록하면, **만드는 즉시** 대회 구조를 생성하고
ctf-notice 에 참가 버튼이 달린 공지를 올린다. 대회 시작 며칠 전부터 팀원이
미리 참가 신청을 해둘 수 있다.

이벤트의 종료 시각은 대회에 기록해두고, 종료 후 3일이 지나면 문제 스레드를
조용히 접는다. 채널과 카테고리는 절대 지우지 않는다.

시간 확인은 봇 내부 타이머로만 한다 (대회 사이트에 폴링하지 않는다).
대회 주소·토큰이 저장돼 있으면 시작 직후 문제를 자동으로 가져온다 (아니면 `/ctf import`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import db
import importer
import platforms
import structure
import views
from cogs.ctf import ARCHIVE_GRACE

log = logging.getLogger("ctfbot.events")

# 대회 시작 후 이 시점들에 문제를 가져온다. 성공하면 남은 시도는 건너뛴다.
# (시작 정각엔 문제를 안 여는 대회가 많아 30초 뒤부터, 최대 3번)
IMPORT_SCHEDULE = (timedelta(seconds=30), timedelta(seconds=60), timedelta(minutes=2))
# 루프가 1분 주기라, 이만큼 앞서 시작하는 대회는 미리 예약해 둔다.
IMPORT_LOOKAHEAD = timedelta(minutes=1)
# 자동 임포트 실패 같은 운영 알림을 DM 으로 받을 사람. 비우면 서버장.
ALERT_USER_ID = int(os.getenv("ALERT_USER_ID") or 0)

# 이벤트 설명에 `token: xxx` 로 적어두면 읽어서 저장하고 그 줄을 지운다.
TOKEN_RE = re.compile(r"^\s*token\s*[:=]\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 대회별 자동 임포트 예약 작업 (중복 예약 방지, 메모리에만 둔다)
        self._import_tasks: dict[int, asyncio.Task] = {}
        # 봇이 켜진 뒤 첫 주기에 스레드 이름(✅/❌)을 실제 상태와 한 번 맞춘다.
        self._names_checked = False

    async def cog_load(self) -> None:
        self.check_archive.start()

    async def cog_unload(self) -> None:
        self.check_archive.cancel()
        for task in self._import_tasks.values():
            task.cancel()

    # ---------- 이벤트 → 대회 ----------

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        log.info("이벤트 생성 감지: %s", event.name)
        await self._create_ctf_for(event)

    @commands.Cog.listener()
    async def on_scheduled_event_update(
        self, before: discord.ScheduledEvent, after: discord.ScheduledEvent
    ) -> None:
        log.info("이벤트 수정 감지: %s", after.name)

        # 봇이 꺼져 있는 동안 만들어진 이벤트라면 이때라도 만든다.
        ctf = await db.get_ctf_by_event(after.id)
        if ctf is None:
            await self._create_ctf_for(after)
            return

        # 이벤트를 나중에 고쳐서 주소/토큰을 적어 넣는 경우도 받아준다.
        # (만들 때만 읽으면, 뒤늦게 적어 넣은 값이 영영 무시된다.)
        await self._read_credentials(after, ctf)

        # 일정이 바뀌면 대회 기록과 이미 올라간 공지를 함께 고친다.
        if (before.start_time, before.end_time) != (after.start_time, after.end_time):
            await db.set_ctf_schedule(ctf["id"], after.start_time, after.end_time)
            ctf = await db.get_ctf(ctf["id"])
            await views.update_announcement(
                after.guild or self.bot.get_guild(after.guild_id),
                ctf,
                after.start_time,
                after.end_time,
            )
            log.info(
                "%s 일정 변경: %s ~ %s", ctf["name"], after.start_time, after.end_time
            )

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        # 이벤트를 지웠다고 대회를 지우지는 않는다 (실수로 지우면 기록이 통째로 날아간다).
        # 정리는 사람이 /ctf delete 로 직접 한다.
        ctf = await db.get_ctf_by_event(event.id)
        if ctf is not None:
            log.info(
                "이벤트 삭제됨: %s — 대회는 그대로 둡니다. 지우려면 /ctf delete name:%s",
                event.name,
                ctf["name"],
            )

    @staticmethod
    def _find_url(event: discord.ScheduledEvent) -> str | None:
        """이벤트 위치/설명에 적힌 대회 주소를 찾는다."""
        for text in (event.location, event.description):
            if not text:
                continue
            match = re.search(r"https?://[^\s<>\"']+", text)
            if match:
                return match.group(0).rstrip(".,)")
        return None

    @staticmethod
    def _find_token(event: discord.ScheduledEvent) -> str | None:
        """이벤트 설명에 적어둔 API 토큰을 찾는다.

        `token: xxx` 형태로 적으면 읽는다. 읽은 뒤에는 설명에서 지운다 —
        이벤트 설명은 서버원 모두에게 보이므로 남겨둘 값이 아니다.
        """
        if not event.description:
            return None
        match = TOKEN_RE.search(event.description)
        return match.group(1).strip() if match else None

    async def _consume_token(self, event: discord.ScheduledEvent) -> str | None:
        """토큰을 읽고 이벤트 설명에서 지운다."""
        token = self._find_token(event)
        if token is None:
            return None

        cleaned = TOKEN_RE.sub("", event.description).strip()
        try:
            await event.edit(description=cleaned or None)
        except discord.HTTPException:
            log.warning(
                "%s: 이벤트 설명에서 토큰을 지우지 못했습니다. 직접 지워주세요.", event.name
            )
        return token

    async def _read_credentials(self, event: discord.ScheduledEvent, ctf) -> None:
        """이벤트에 적힌 주소/토큰을 대회에 반영한다. 이미 있는 값은 덮어쓰지 않는다."""
        url = self._find_url(event) or (ctf["ctfd_url"] if ctf else None)
        token = await self._consume_token(event)

        if token:
            if not url:
                log.warning("%s: 토큰만 있고 주소가 없습니다", event.name)
                return
            await db.set_credentials(ctf["id"], url, token, "auto")
            log.info("%s: 이벤트에서 주소와 토큰을 읽었습니다 (%s)", event.name, url)
        elif url and not (ctf and ctf["ctfd_url"]):
            await db.set_ctf_url(ctf["id"], url)
            log.info("%s: 이벤트에서 주소를 읽었습니다 (%s)", event.name, url)

    async def _create_ctf_for(self, event: discord.ScheduledEvent) -> None:
        """이벤트 하나에 대응하는 대회를 만들고 참가 공지를 올린다."""
        guild = event.guild or self.bot.get_guild(event.guild_id)
        if guild is None:
            log.warning("서버를 찾을 수 없어 이벤트 %s 를 건너뜁니다", event.id)
            return

        name = event.name.strip()

        if await db.get_ctf_by_event(event.id) or await db.get_ctf_by_name(guild.id, name):
            log.info("%s 는 이미 있어 건너뜁니다", name)
            return

        notice = structure.find_notice_channel(guild)
        if notice is None:
            log.warning(
                "공지 채널이 없어 %s 를 만들지 않았습니다. %s 중 하나를 만들어주세요.",
                name,
                ", ".join(structure.NOTICE_CHANNEL_NAMES),
            )
            return

        try:
            ctf_id, _ = await structure.create_ctf(
                guild,
                name,
                starts_at=event.start_time,
                ends_at=event.end_time,
                event_id=event.id,
            )
            await self._read_credentials(event, await db.get_ctf(ctf_id))
            await views.post_announcement(
                notice, ctf_id, name, event.start_time, event.end_time
            )
        except discord.Forbidden:
            log.warning("권한이 부족해 %s 를 만들지 못했습니다", name)
            return

        log.info(
            "이벤트로 CTF 생성: %s (id=%s, %s ~ %s)",
            name,
            ctf_id,
            event.start_time,
            event.end_time,
        )

    # ---------- 시간에 맞춰 도는 일들 ----------

    @tasks.loop(minutes=1)
    async def check_archive(self) -> None:
        # 이 루프가 예외로 죽으면 시작·종료 알림, 자동 임포트, 자동 아카이브가 전부
        # 조용히 멈춘다. discord.ext.tasks 는 네트워크 계열 예외만 자동 재시작하므로,
        # DB 오류 등 어떤 예외든 여기서 잡아 로그만 남기고 다음 분에 계속한다.
        try:
            await self._tick()
        except Exception:
            log.exception("정기 작업 중 오류 — 다음 분에 계속합니다")

    async def _tick(self) -> None:
        if not self._names_checked:
            self._names_checked = True
            try:
                await self._reconcile_thread_names()
            except Exception:
                log.exception("스레드 이름 맞추기 실패")

        for kind in ("start", "end"):
            for ctf in await db.ctfs_due_for_announce(kind):
                try:
                    await self._announce(ctf, kind)
                except Exception:
                    log.exception("대회 %s %s 알림 실패", ctf["name"], kind)
                finally:
                    # 실패해도 다시 시도하지 않는다. 1분마다 같은 오류를 반복하느니
                    # 한 번 놓치는 편이 낫다.
                    await db.mark_announced(ctf["id"], kind)

        # 곧 시작하거나 이미 시작한 대회는 +30초·+60초·+2분 시도를 예약한다.
        # (지연을 음수로 주면 '앞으로 1분 안에 시작하는 대회'까지 잡힌다.)
        for ctf in await db.ctfs_due_for_auto_import(-IMPORT_LOOKAHEAD):
            task = self._import_tasks.get(ctf["id"])
            if task is not None and not task.done():
                continue  # 이미 예약돼 돌고 있음
            self._import_tasks[ctf["id"]] = asyncio.create_task(
                self._run_import_schedule(ctf["id"]), name=f"auto-import-{ctf['id']}"
            )

        for ctf in await db.ctfs_due_for_archive(ARCHIVE_GRACE):
            try:
                await self._auto_archive(ctf)
            except Exception:
                log.exception("대회 %s 자동 아카이브 실패", ctf["name"])

    @check_archive.error
    async def _check_archive_error(self, exc: BaseException) -> None:
        # _tick 을 감싸도 못 잡는 경우(before_loop 등)를 대비한 백스톱. 죽으면 되살린다.
        log.exception("정기 작업 루프가 중단됨 — 재시작합니다", exc_info=exc)
        self.check_archive.restart()

    @check_archive.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()
        # 매분 0초에 돌게 맞춰둔다. 그래야 12:36 시작인 대회가 12:36:00 에 알려진다
        # (안 맞추면 봇을 켠 시각의 초에 걸려서 최대 1분 늦게 나간다).
        now = datetime.now(timezone.utc)
        await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000)

    async def _announce(self, ctf, kind: str) -> None:
        """대회 시작/종료를 general 채널에 알린다."""
        guild = self.bot.get_guild(ctf["guild_id"])
        if guild is None:
            return
        category = guild.get_channel(ctf["category_id"])
        if category is None:
            return

        if kind == "start":
            message = f"🚩 **{ctf['name']}** 시작했습니다!"
        else:
            message = await self._build_end_report(guild, ctf)

        await structure.post_to(category, "general", message)
        log.info("%s %s 알림 게시", ctf["name"], kind)

    async def _build_end_report(self, guild: discord.Guild, ctf) -> str:
        """종료 알림에 붙일 간단한 리포트. 최종 스코어 + 분야별 + 기여자."""
        solved, total = await db.solve_counts(ctf["id"])
        header = (
            f"🏁 **{ctf['name']}** 종료됐습니다. 수고하셨습니다! "
            f"(최종 **{solved} / {total}** solved)"
        )
        # 문제를 한 번도 임포트하지 않은 대회는 붙일 게 없으니 헤더만 남긴다.
        if total == 0:
            return header

        lines = [header]

        # 분야별: 푼 수 / 전체 수
        per_cat: dict[str, list[int]] = {}
        for ch in await db.list_challenges(ctf["id"]):
            bucket = per_cat.setdefault(ch["category"], [0, 0])
            bucket[1] += 1
            if ch["solved"]:
                bucket[0] += 1
        if per_cat:
            parts = [f"{cat} {s}/{t}" for cat, (s, t) in sorted(per_cat.items())]
            lines.append("📊 분야별: " + " · ".join(parts))

        # 기여: 많이 푼 사람 순. 나간 멤버는 이름만 대체해 표시한다.
        # 이름만 쓰고 멘션은 하지 않는다 (종료 알림이 팀 전체를 핑하면 시끄럽다).
        board = await db.scoreboard(ctf["id"])
        if board:
            shown = board[:10]
            names = []
            for user_id, solved_names in shown:
                member = guild.get_member(user_id)
                label = member.display_name if member else "(나간 멤버)"
                names.append(f"{label} {len(solved_names)}")
            line = "🏅 기여: " + " · ".join(names)
            if len(board) > len(shown):
                line += f" 외 {len(board) - len(shown)}명"
            lines.append(line)

        return "\n".join(lines)

    async def _reconcile_thread_names(self) -> None:
        """봇이 꺼지거나 재시작되는 사이 어긋난 스레드 이름(✅/❌)을 실제 상태에 맞춘다.

        진행 중인 대회의, 지금 열려 있는(캐시에 있는) 스레드만 본다. 이름이 이미 맞으면 손대지
        않으므로 디스코드 이름 변경 제한(10분에 2번)을 헛되이 쓰지 않는다. 한 번만 돈다.
        """
        fixed = 0
        for guild in self.bot.guilds:
            for ctf in await db.list_all_ctfs(guild.id):
                if ctf["status"] != "active":
                    continue
                for ch in await db.list_challenges(ctf["id"]):
                    thread = guild.get_channel_or_thread(ch["thread_id"])
                    if not isinstance(thread, discord.Thread):
                        continue
                    want = structure.thread_name(ch["name"], solved=bool(ch["solved"]))
                    if thread.name != want:
                        structure.sync_thread_name(thread, ch["id"], want)
                        fixed += 1
        if fixed:
            log.info("스레드 이름 %d개를 실제 풀이 상태에 맞추는 중", fixed)

    async def _run_import_schedule(self, ctf_id: int) -> None:
        """대회 시작 +30초 · +60초 · +2분에 문제를 가져온다. 성공하면 남은 시도는 안 한다.

        봇이 늦게 켜져 시점이 이미 다 지났으면 한 번만 바로 시도한다 (연달아 두드리지 않게).
        모두 실패하면 대회 채널이 아니라 서버장(또는 ALERT_USER_ID)에게만 DM 으로 알린다.
        """
        try:
            ctf = await db.get_ctf(ctf_id)
            if ctf is None or not ctf["starts_at"]:
                return
            start = datetime.fromisoformat(ctf["starts_at"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            times = [start + d for d in IMPORT_SCHEDULE if start + d >= now - timedelta(seconds=5)]
            if not times:
                times = [now]

            error = None
            for i, when in enumerate(times, 1):
                delay = (when - datetime.now(timezone.utc)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                # 기다리는 사이 수동 pull · 토큰 변경 · 대회 삭제가 있었을 수 있어 다시 읽는다.
                ctf = await db.get_ctf(ctf_id)
                if ctf is None or ctf["auto_imported"]:
                    return
                ok, error = await self._auto_import(ctf)
                if ok:
                    await db.mark_auto_imported(ctf_id)
                    return
                log.info("%s 자동 임포트 %d/%d회 실패: %s", ctf["name"], i, len(times), error)

            await db.mark_auto_imported(ctf_id)
            await self._notify_owner(
                ctf,
                f"⚠️ **{ctf['name']}** 문제를 자동으로 못 불러왔어 ({len(times)}번 시도).\n"
                f"{(error or '')[:1500]}\n"
                f"대회 카테고리에서 `/ctf pull` 로 다시 해줘.",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("대회 %s 자동 임포트 예약 실행 실패", ctf_id)
            try:
                await db.mark_auto_imported(ctf_id)  # 매분 같은 오류로 재예약되지 않게
            except Exception:
                pass
        finally:
            self._import_tasks.pop(ctf_id, None)

    async def _auto_import(self, ctf) -> tuple[bool, str | None]:
        """문제를 한 번 가져온다. (성공 여부, 실패 이유).

        성공 결과만 대회의 bot-cmd 에 올린다. 실패는 여기서 알리지 않는다 —
        재시도가 남았을 수 있고, 최종 실패는 _notify_owner 로 본인에게만 보낸다.
        """
        guild = self.bot.get_guild(ctf["guild_id"])
        if guild is None:
            return False, "서버를 찾을 수 없어"
        category = guild.get_channel(ctf["category_id"])
        if category is None:
            return False, "대회 카테고리를 찾을 수 없어"

        try:
            result = await importer.sync(
                guild,
                ctf,
                ctf["ctfd_url"],
                ctf["api_token"],
                ctf["platform"] or "auto",
            )
        except platforms.PlatformError as exc:
            return False, str(exc)
        except Exception as exc:
            log.exception("%s 자동 임포트 중 예상 못한 오류", ctf["name"])
            return False, f"예상 못한 오류: {exc}"

        # 팀 채널엔 성공한 부분만 올린다. 못 받은 문제 같은 실패 내용은 마지막에 본인 DM 으로만.
        # 방금 누가 pull 한 결과를 재사용한 거면(cached_age) 팀 채널에 또 올리지 않는다.
        if result.added and result.cached_age is None:
            await structure.post_to(
                category, "bot-cmd", f"📥 {result.summary(details=False)}"
            )
        if not result.complete:
            # 서버가 느려 일부만 받았다 — 다음 예약 시도에서 남은 것만 이어서 받는다.
            return False, "\n".join(result.problem_lines())
        return True, None

    async def _notify_owner(self, ctf, text: str) -> None:
        """운영 알림(자동 임포트 실패 등)을 서버장에게만 DM 으로 보낸다.

        대회 채널에 올리면 팀원·관리자 모두 보게 되므로 DM 을 쓴다.
        ALERT_USER_ID 를 지정하면 서버장 대신 그 사람에게 보낸다. DM 이 막혀 있으면 로그만 남긴다.
        """
        guild = self.bot.get_guild(ctf["guild_id"])
        uid = ALERT_USER_ID or (guild.owner_id if guild else 0)
        if not uid:
            log.warning("알림 받을 사람을 못 찾아 DM 을 건너뜁니다: %s", text[:100])
            return
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(text)
        except discord.HTTPException as exc:
            log.warning("알림 DM 실패 (%s): %s", uid, exc)

    async def _auto_archive(self, ctf) -> None:
        """종료 후 유예기간이 지난 대회의 스레드를 접는다. 채널은 절대 지우지 않는다."""
        guild = self.bot.get_guild(ctf["guild_id"])
        if guild is None:
            return
        category = guild.get_channel(ctf["category_id"])
        if category is None:
            # 카테고리가 사라졌으면 더 볼 것도 없으니 상태만 정리한다.
            await db.archive_ctf(ctf["id"])
            return

        count = await structure.archive_threads(category)
        await db.archive_ctf(ctf["id"])
        # 조용히 정리한다. 채널에 공지는 남기지 않는다.
        log.info("자동 아카이브: %s (스레드 %s개)", ctf["name"], count)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
