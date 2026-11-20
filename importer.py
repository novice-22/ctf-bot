"""대회 사이트에서 받은 문제를 디스코드 구조로 옮기는 공통 로직.

`/ctf import`, `/ctf pull`, 대회 시작 시 자동 임포트가 모두 여기를 쓴다.
이미 가져온 문제는 건너뛰므로 몇 번을 실행해도 중복이 생기지 않는다.

대회 서버가 느리거나 터져도 버티도록 만든다:
- 이미 있는 문제는 상세(설명·첨부)를 다시 받지 않는다 (헛요청·대기 시간 절약).
- 서버가 응답 없어 상세를 못 받은 문제는 이번에 만들지 않고 미룬다 (다음 pull 에 이어서).
- 문제 하나 만들다 실패해도 나머지는 계속 만든다.
- 첨부파일은 문제를 만든 뒤 따로 올린다. 못 올린 파일은 그 스레드에 안내를 남긴다.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field, replace

import discord

import db
import platforms
import structure

log = logging.getLogger("ctfbot.importer")

# 같은 대회를 이 시간(초) 안에 또 불러오면 대회 서버에 다시 묻지 않고 방금 결과를 돌려준다.
# 여러 명이 pull 을 연타해도 대회 서버엔 한 번만 가게 해서, 약한 서버를 괴롭히거나
# 우리 IP 가 차단되는 걸 막는다.
COOLDOWN = 20.0

# 대회별로 지금 돌고 있는 불러오기 작업과 그 설정(주소·토큰·플랫폼).
_running: dict[int, tuple[asyncio.Task, tuple, float]] = {}
# 대회별 마지막 결과: (끝난 시각, 설정, Result 또는 PlatformError).
_recent: dict[int, tuple[float, tuple, object]] = {}


async def _report(progress, done: int, total: int, phase: str) -> None:
    """진행률 콜백 호출. 표시가 실패해도 임포트는 계속한다."""
    if progress is None:
        return
    try:
        await progress(done, total, phase)
    except Exception:
        pass


async def _post_attachments(thread: discord.Thread, challenge, size_limit: int) -> int:
    """첨부파일을 받아 스레드에 올린다. 못 올린 파일 수를 돌려준다.

    문제를 만든 '뒤에' 따로 올린다 — 예전엔 파일 업로드가 실패하면 스레드만 생기고
    기록이 안 남아서, 다음 pull 때 같은 문제가 또 만들어질 수 있었다.
    못 올린 파일은 그 스레드에 "사이트에서 직접 받아줘" 안내를 남긴다.
    """
    if not challenge.attachments:
        return 0

    received: list[tuple[str, bytes]] = []
    missing: list[str] = []
    for att in challenge.attachments:
        data = await platforms.download(att.url, size_limit)
        if data is None:
            log.warning("첨부파일 건너뜀: %s (%s)", att.name, challenge.name)
            missing.append(att.name)
        else:
            received.append((att.name, data))

    # 한 메시지에 최대 10개. 묶음으로 실패하면(합계 용량 초과 등) 하나씩 다시 올린다.
    for i in range(0, len(received), 10):
        batch = received[i : i + 10]
        try:
            await thread.send(
                files=[discord.File(io.BytesIO(data), filename=name) for name, data in batch]
            )
        except discord.HTTPException:
            for name, data in batch:
                try:
                    await thread.send(file=discord.File(io.BytesIO(data), filename=name))
                except discord.HTTPException:
                    log.warning("첨부파일 업로드 실패: %s (%s)", name, challenge.name)
                    missing.append(name)

    if missing:
        limit_mb = size_limit // (1024 * 1024)
        names = ", ".join(f"`{name}`" for name in missing)
        try:
            await thread.send(
                f"📎 첨부파일 {len(missing)}개는 못 올렸어: {names}\n"
                f"너무 크거나(이 서버 업로드 한도 {limit_mb}MB) 대회 서버가 응답을 안 했어 — "
                f"대회 사이트에서 직접 받아줘."
            )
        except discord.HTTPException:
            pass
    return len(missing)


def _names(names: list[str], limit: int = 5) -> str:
    head = ", ".join(names[:limit])
    return head + (f" 외 {len(names) - limit}개" if len(names) > limit else "")


@dataclass
class Result:
    platform: str
    added: int
    skipped: int
    names: list[str]
    # 서버가 응답 없어 이번에 못 받은 문제 — 다음 pull 에 이어서 받는다.
    deferred: list[str] = field(default_factory=list)
    # 디스코드에 만들다 실패한 문제 — 기록이 안 남았으니 다음 pull 에 다시 시도된다.
    failed: list[str] = field(default_factory=list)
    # 못 올린 첨부파일 수 (해당 스레드에 안내를 남겼다).
    missing_files: int = 0
    # 방금 불러온 결과를 재사용했다면 그게 몇 초 전 결과인지 (새로 불러왔으면 None).
    cached_age: float | None = None

    @property
    def complete(self) -> bool:
        """다시 시도할 게 남지 않았는지 (첨부 누락은 재시도로 안 풀리니 제외)."""
        return not self.deferred and not self.failed

    def summary(self, details: bool = True) -> str:
        """결과 요약. details=False 면 성공 부분만 (팀 채널에 올리는 자동 임포트용)."""
        if self.added:
            lines = [f"[{self.platform}] 새 문제 **{self.added}개** — {_names(self.names)}"]
        elif self.complete:
            lines = [f"[{self.platform}] 새 문제 없음 (이미 {self.skipped}개 가져옴)"]
        else:
            lines = [f"[{self.platform}] 이번엔 새로 만든 문제가 없어"]
        if self.skipped and self.added:
            lines.append(f"이미 있던 {self.skipped}개는 건너뜀")
        if details:
            lines += self.problem_lines()
        if self.cached_age is not None:
            lines.insert(
                0,
                f"⏱️ {self.cached_age:.0f}초 전에 불러온 결과야 "
                f"(대회 서버 보호용 — {COOLDOWN:.0f}초 지나면 다시 불러올 수 있어)",
            )
        return "\n".join(lines)

    def problem_lines(self) -> list[str]:
        lines = []
        if self.deferred:
            lines.append(
                f"⏳ 대회 서버가 응답이 없어 못 받은 문제 {len(self.deferred)}개: "
                f"{_names(self.deferred)} — 잠시 뒤 `/ctf pull` 하면 이어서 추가돼"
            )
        if self.failed:
            lines.append(
                f"⚠️ 디스코드에 못 만든 문제 {len(self.failed)}개: {_names(self.failed)} "
                f"— 다시 `/ctf pull` 하면 재시도해"
            )
        if self.missing_files:
            lines.append(
                f"📎 못 올린 첨부파일 {self.missing_files}개 — 해당 문제 스레드에 안내를 남겼어"
            )
        return lines


async def sync(
    guild: discord.Guild,
    ctf,
    url: str,
    token: str,
    platform: str = "auto",
    progress=None,
) -> Result:
    """대회 사이트의 문제를 불러온다. 여러 명이 동시에·연달아 불러도 안전하다.

    - 같은 대회를 이미 불러오는 중이면 새로 요청하지 않고 그 결과를 같이 받는다.
      (예전엔 두 명이 동시에 pull 하면 둘 다 "아직 없음"으로 보고 각자 만들어
      같은 문제 스레드가 두 개씩 생길 수 있었다.)
    - 방금(COOLDOWN 초 안에) 같은 설정으로 불러왔으면 대회 서버에 다시 묻지 않고
      그 결과(실패였다면 그 실패)를 돌려준다.
    - 자동 임포트도 여기를 지나므로, 자동 수집과 수동 pull 이 겹쳐도 중복이 안 생긴다.
    """
    cid = ctf["id"]
    key = (url, token, platform)
    loop = asyncio.get_running_loop()

    while True:
        running = _running.get(cid)
        if running is None or running[0].done():
            break
        task, running_key, started = running
        # 누른 사람에게만 "이미 처리 중 — 다시 안 눌러도 돼" 를 보여주고, 끝나면 같은 결과를 준다.
        await _report(progress, int(loop.time() - started), 0, "join")
        if running_key == key:
            return await asyncio.shield(task)
        # 다른 주소·토큰으로 불러오는 중 — 동시에 만들면 중복이 생기니 끝나길 기다렸다 내 걸로 한다.
        try:
            await asyncio.shield(task)
        except Exception:
            pass

    recent = _recent.get(cid)
    if recent is not None and recent[1] == key:
        age = loop.time() - recent[0]
        if age < COOLDOWN:
            outcome = recent[2]
            if isinstance(outcome, platforms.PlatformError):
                raise platforms.PlatformError(
                    f"{outcome}\n(⏱️ {age:.0f}초 전에 시도한 결과야 — "
                    f"{COOLDOWN:.0f}초 지나면 다시 할 수 있어)"
                )
            return replace(outcome, cached_age=age)

    # 확인부터 등록까지 await 이 없어서, 동시에 들어와도 둘 중 하나만 작업을 시작한다.
    task = asyncio.create_task(_run(guild, ctf, url, token, platform, progress, key))
    _running[cid] = (task, key, loop.time())
    task.add_done_callback(lambda t: _forget(cid, t))
    return await asyncio.shield(task)


def _forget(cid: int, task: asyncio.Task) -> None:
    current = _running.get(cid)
    if current is not None and current[0] is task:
        del _running[cid]


async def _run(guild, ctf, url, token, platform, progress, key) -> Result:
    """실제 불러오기를 하고, 결과(또는 실패)를 COOLDOWN 동안 재사용하도록 기록한다."""
    loop = asyncio.get_running_loop()
    try:
        result = await _sync(guild, ctf, url, token, platform, progress)
    except platforms.PlatformError as exc:
        _recent[ctf["id"]] = (loop.time(), key, exc)
        raise
    _recent[ctf["id"]] = (loop.time(), key, result)
    return result


async def _sync(
    guild: discord.Guild,
    ctf,
    url: str,
    token: str,
    platform: str = "auto",
    progress=None,
) -> Result:
    """대회 사이트의 문제 목록을 가져와 없는 것만 채널/스레드로 만든다.

    progress 는 (done, total, phase) 를 받는 async 콜백. phase 는
    "connect"(목록 받는 중) → "fetch"(새 문제 상세 받는 중) → "create"(스레드 만드는 중).
    """
    category = guild.get_channel(ctf["category_id"])
    if category is None:
        raise platforms.PlatformError("대회 카테고리를 찾을 수 없습니다.")

    already_ids = {str(x) for x in await db.existing_ctfd_ids(ctf["id"])}
    seen_names = await db.existing_challenge_names(ctf["id"])
    known_names = set(seen_names)

    def is_known(remote_id: str, name: str) -> bool:
        return str(remote_id) in already_ids or name.strip().lower() in known_names

    async def fetch_progress(done: int, total: int) -> None:
        await _report(progress, done, total, "fetch")

    await _report(progress, 0, 0, "connect")
    ctx = platforms.FetchContext(skip=is_known, progress=fetch_progress)
    detected, challenges = await platforms.fetch_challenges(url, token, platform, ctx)

    # 자동 판별로 알아낸 플랫폼을 기억한다 — 다음부터는 그 플랫폼으로 바로 간다
    # (대회 서버가 과부하일 때 다른 플랫폼까지 두드려 보는 헛요청을 막는다).
    if (ctf["platform"] or "auto") == "auto" and url == ctf["ctfd_url"]:
        await db.set_platform(ctf["id"], detected)

    aliases = await db.alias_map(guild.id)

    # remote_id 로도, 이름으로도 중복을 막는다. 수동으로 만든 문제(remote_id 없음)와
    # 같은 이름이 임포트로 또 생기는 것을 방지하고, 같은 배치 안의 중복도 거른다.
    todo = []
    for c in challenges:
        key = c.name.strip().lower()
        if str(c.remote_id) in already_ids or key in seen_names:
            continue
        seen_names.add(key)
        todo.append(c)
    skipped = len(challenges) - len(todo)

    added: list[str] = []
    failed: list[str] = []
    missing_files = 0

    for i, challenge in enumerate(todo, 1):
        try:
            # 대회 원본 분야명은 별칭 등록용으로 축적하고, 별칭표를 거쳐 표준 분야로 만든다.
            await db.add_seen_category(guild.id, challenge.category)
            resolved = structure.resolve_category(challenge.category, aliases)
            channel = await structure.get_or_create_category_channel(category, resolved)
            thread = await structure.create_challenge_thread(
                channel, challenge.name, description=challenge.description
            )
            await db.add_challenge(
                ctf["id"],
                challenge.name,
                resolved,
                channel.id,
                thread.id,
                challenge.remote_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 한 문제 때문에 전체가 멈추지 않게 건너뛴다. 기록이 안 남았으니 다음 pull 때 다시 시도된다.
            log.exception("%s: 문제 '%s' 만들기 실패 — 건너뜀", ctf["name"], challenge.name)
            failed.append(challenge.name)
            continue

        added.append(challenge.name)
        missing_files += await _post_attachments(thread, challenge, guild.filesize_limit)

        # 5개마다(그리고 마지막에) 진행률을 알린다. 매번 하면 API 제한에 걸린다.
        if i % 5 == 0 or i == len(todo):
            await _report(progress, i, len(todo), "create")

    log.info(
        "%s: %s개 추가, %s개 스킵, %s개 미룸, %s개 실패, 첨부 누락 %s (%s)",
        ctf["name"], len(added), skipped, len(ctx.deferred), len(failed), missing_files, detected,
    )
    return Result(
        detected,
        len(added),
        skipped,
        added,
        deferred=list(ctx.deferred),
        failed=failed,
        missing_files=missing_files,
    )
