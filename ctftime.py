"""CTFtime.org 연동. 예정 대회 조회 + 대회 하나 가져오기.

CTFtime API 는 공개(키·로그인 불필요)지만, **기본 User-Agent 를 403 으로 막는다**
(Cloudflare). 그래서 모든 요청에 커스텀 UA 를 반드시 붙인다. 명령을 칠 때마다
API 를 때리지 않도록 예정 목록은 잠깐 캐싱하고, 실패하면 예외 대신 사람이 읽을
한글 메시지를 담은 CTFtimeError 를 던진다 (명령이 500 으로 죽지 않게).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("ctfbot.ctftime")

API = "https://ctftime.org/api/v1"

# 기본 aiohttp UA 는 Cloudflare 에 403 당한다. 브라우저처럼 보이는 UA + Accept 헤더로
# 엣지 차단을 피한다.
HEADERS = {
    "User-Agent": "ctf-bot/1.0 (team CTF Discord bot; +https://ctftime.org)",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = aiohttp.ClientTimeout(total=15)

# 예정 목록 캐시. 명령마다 CTFtime 을 때리지 않도록 10분 동안 재사용한다.
_CACHE_TTL = 600.0
_cache: dict = {"at": 0.0, "events": None}


class CTFtimeError(Exception):
    """CTFtime 을 읽지 못했을 때. 메시지를 사용자에게 그대로 보여줘도 되게 적는다."""


def parse_event_ref(ref: str) -> int | None:
    """'2401' 또는 'https://ctftime.org/event/2401' 에서 이벤트 번호를 뽑는다."""
    match = re.search(r"(\d+)", ref or "")
    return int(match.group(1)) if match else None


def _to_dt(iso: str | None) -> datetime | None:
    """CTFtime 응답의 ISO-8601 문자열('...+00:00' 또는 '...Z')을 UTC datetime 으로."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _clean(event: dict) -> dict:
    """API 원본 이벤트에서 우리가 쓰는 필드만 추린다. 없는 값은 방어적으로 처리."""
    return {
        "id": event.get("id"),
        "title": (event.get("title") or "").strip(),
        "start": _to_dt(event.get("start")),
        "finish": _to_dt(event.get("finish")),
        "url": (event.get("url") or "").strip(),
        "ctftime_url": (event.get("ctftime_url") or "").strip(),
        "format": (event.get("format") or "").strip(),
        "weight": event.get("weight") or 0,
        "onsite": bool(event.get("onsite")),
        "restrictions": (event.get("restrictions") or "").strip(),
    }


async def _get(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    """공통 GET. 커스텀 UA 필수. 실패는 전부 CTFtimeError 로 바꿔 던진다."""
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT) as resp:
            if resp.status == 403:
                raise CTFtimeError("CTFtime 이 접근을 막았어 (403). 잠시 뒤 다시 시도해줘.")
            if resp.status == 404:
                raise CTFtimeError("CTFtime 에서 그 대회를 못 찾았어 (번호를 확인해줘).")
            if resp.status != 200:
                raise CTFtimeError(f"CTFtime 응답이 이상해 (HTTP {resp.status}).")
            return await resp.json()
    except aiohttp.ClientError as exc:
        raise CTFtimeError(f"CTFtime 에 연결하지 못했어: {exc}") from exc


async def fetch_event(event_id: int) -> dict:
    """이벤트 하나를 가져온다. 없거나 못 읽으면 CTFtimeError."""
    async with aiohttp.ClientSession() as session:
        data = await _get(session, f"{API}/events/{event_id}/")
    if not isinstance(data, dict) or not data.get("id"):
        raise CTFtimeError(f"CTFtime 에서 {event_id} 번 대회를 못 찾았어.")
    return _clean(data)


async def fetch_upcoming(limit: int = 5) -> list[dict]:
    """다가오는 대회들 (시작 빠른 순). 10분 캐시. limit 은 1~10 으로 제한."""
    limit = max(1, min(limit, 10))
    now = time.time()

    if _cache["events"] is None or now - _cache["at"] > _CACHE_TTL:
        start = int(now)
        finish = start + 60 * 60 * 24 * 90  # 앞으로 90일 창
        async with aiohttp.ClientSession() as session:
            data = await _get(
                session,
                f"{API}/events/",
                params={"limit": "50", "start": str(start), "finish": str(finish)},
            )
        events = [_clean(e) for e in data if isinstance(e, dict)]
        far_future = datetime.max.replace(tzinfo=timezone.utc)
        events.sort(key=lambda e: e["start"] or far_future)
        _cache["events"] = events
        _cache["at"] = now
        log.info("CTFtime 예정 대회 %d개 갱신", len(events))

    return _cache["events"][:limit]
