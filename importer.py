"""대회 사이트에서 받은 문제를 디스코드 구조로 옮기는 공통 로직.

`/ctf import`, `/ctf pull`, 대회 시작 시 자동 임포트가 모두 여기를 쓴다.
이미 가져온 문제는 건너뛰므로 몇 번을 실행해도 중복이 생기지 않는다.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import discord

import db
import platforms
import structure

log = logging.getLogger("ctfbot.importer")


async def _download_files(challenge, size_limit: int) -> list[discord.File]:
    """문제 첨부파일을 내려받아 디스코드 업로드용으로 만든다.

    실패하거나 서버 업로드 한도를 넘는 파일은 조용히 건너뛴다 (임포트는 계속).
    """
    files: list[discord.File] = []
    for att in challenge.attachments:
        data = await platforms.download(att.url, size_limit)
        if data is None:
            log.warning("첨부파일 건너뜀: %s (%s)", att.name, challenge.name)
            continue
        files.append(discord.File(io.BytesIO(data), filename=att.name))
    return files


@dataclass
class Result:
    platform: str
    added: int
    skipped: int
    names: list[str]

    def summary(self) -> str:
        if not self.added:
            return f"[{self.platform}] 새 문제 없음 (이미 {self.skipped}개 가져옴)"
        head = ", ".join(self.names[:5])
        more = f" 외 {self.added - 5}개" if self.added > 5 else ""
        text = f"[{self.platform}] 새 문제 **{self.added}개** — {head}{more}"
        if self.skipped:
            text += f"\n이미 있던 {self.skipped}개는 건너뜀"
        return text


async def sync(
    guild: discord.Guild,
    ctf,
    url: str,
    token: str,
    platform: str = "auto",
    progress=None,
) -> Result:
    """대회 사이트의 문제 목록을 가져와 없는 것만 채널/스레드로 만든다.

    progress 는 (지금까지 만든 수, 만들 총 수) 를 받는 async 콜백. 문제가 많아
    시간이 걸릴 때 "10/30 처리 중" 같은 안내를 갱신하는 데 쓴다.
    """
    detected, challenges = await platforms.fetch_challenges(url, token, platform)

    category = guild.get_channel(ctf["category_id"])
    if category is None:
        raise platforms.PlatformError("대회 카테고리를 찾을 수 없습니다.")

    already = {str(x) for x in await db.existing_ctfd_ids(ctf["id"])}
    aliases = await db.alias_map(guild.id)

    todo = [c for c in challenges if str(c.remote_id) not in already]
    skipped = len(challenges) - len(todo)
    added: list[str] = []

    for challenge in todo:
        # 대회 원본 분야명은 별칭 등록용으로 축적하고, 별칭표를 거쳐 표준 분야로 만든다.
        await db.add_seen_category(guild.id, challenge.category)
        resolved = structure.resolve_category(challenge.category, aliases)

        files = await _download_files(challenge, guild.filesize_limit)

        channel = await structure.get_or_create_category_channel(category, resolved)
        thread = await structure.create_challenge_thread(
            channel, challenge.name, description=challenge.description, files=files
        )
        await db.add_challenge(
            ctf["id"],
            challenge.name,
            resolved,
            channel.id,
            thread.id,
            challenge.remote_id,
        )
        added.append(challenge.name)

        # 5개마다(그리고 마지막에) 진행률을 알린다. 매번 하면 API 제한에 걸린다.
        if progress and (len(added) % 5 == 0 or len(added) == len(todo)):
            await progress(len(added), len(todo))

    log.info(
        "%s: %s개 추가, %s개 스킵 (%s)", ctf["name"], len(added), skipped, detected
    )
    return Result(detected, len(added), skipped, added)
