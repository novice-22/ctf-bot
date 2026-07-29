"""CTF 팀 디스코드 봇 진입점."""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

import db
import views

# 윈도우 콘솔 기본 인코딩(cp949)은 한글 로그와 ✅/❌ 를 출력하다 죽는다.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
# 콤마로 여러 서버 지정 가능: GUILD_ID=123,456
GUILD_IDS = [int(x) for x in os.getenv("GUILD_ID", "").replace(" ", "").split(",") if x]

COGS = ("cogs.ctf", "cogs.events")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ctfbot")


class CTFBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # /ctf solve 의 with 자동완성에서 팀원 목록을 뽑으려면 멤버 정보가 필요하다.
        # 개발자 포털 > 봇 > Server Members Intent 를 켜야 한다.
        intents.members = True
        # 임포트한 문제 이름/설명에 @everyone·역할멘션이 섞여도 핑이 울리지 않게
        # 전역 기본을 '멘션 안 함'으로 둔다. 진짜 핑이 필요한 곳(structure.post_to)은
        # 거기서 allowed_mentions 를 직접 지정해 덮어쓴다.
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def setup_hook(self) -> None:
        await db.init()

        # 재시작해도 과거에 올린 버튼들이 계속 작동하도록 등록.
        self.add_dynamic_items(views.JoinButton, views.ChallengeJoinButton)

        for cog in COGS:
            await self.load_extension(cog)

        if GUILD_IDS:
            # 서버 하나가 실패해도 (봇이 없거나 권한이 없거나) 나머지는 정상 동작해야 한다.
            for guild_id in GUILD_IDS:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                except discord.Forbidden:
                    log.warning(
                        "서버 %s 에 접근할 수 없어 건너뜁니다. "
                        "봇이 그 서버에 있는지, applications.commands 스코프로 초대했는지 확인하세요.",
                        guild_id,
                    )
                except discord.HTTPException as exc:
                    log.warning("서버 %s 동기화 실패: %s", guild_id, exc)
                else:
                    log.info("명령어를 서버 %s 에 동기화했습니다 (즉시 반영)", guild_id)
        else:
            await self.tree.sync()
            log.info("명령어를 전역 동기화했습니다 (반영까지 최대 1시간)")

    async def on_ready(self) -> None:
        log.info("로그인: %s", self.user)

    async def close(self) -> None:
        await db.close()
        await super().close()


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            ".env 에 DISCORD_TOKEN 이 없습니다.\n"
            ".env.example 을 .env 로 복사한 뒤 토큰을 넣어주세요."
        )
    CTFBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
