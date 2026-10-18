"""대회 플랫폼별 문제 목록 읽기 (CTFd, rCTF, …).

설계 원칙(PLAN.md 섹션 2): 대회 사이트에는 **문제 목록을 얻는 데 필요한 최소한의
읽기 요청**만 보낸다 (rCTF는 인증 교환 1회가 추가로 필요하다). 플래그 제출,
스코어보드 조회, 폴링 등 다른 요청은 어떤 이유로도 추가하지 않는다.
자동화된 요청은 대회 규칙 위반 소지가 있고 팀 계정을 위험에 빠뜨린다.

새 플랫폼을 추가하려면: fetch_xxx 함수를 만들고 FETCHERS 에 등록한다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_REDIRECTS = 3
MAX_BODY = 5 * 1024 * 1024  # 문제 목록치고 넉넉한 상한

# 문제별 상세를 연속으로 빠르게 요청하면 CTFd/Cloudflare 레이트리밋에 막혀
# 설명이 빈 채로 온다. 상세 요청 사이에 이만큼 쉬어 요청 속도를 낮춘다.
DETAIL_DELAY = 0.2

# 기본 aiohttp UA("Python/aiohttp")는 Cloudflare 봇 탐지에 쉽게 걸린다.
# 브라우저처럼 보이는 UA 를 붙여 차단 문턱을 낮춘다.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 로컬에 띄운 CTFd 로 시험할 때만 켠다. 서버에서는 절대 켜지 않는다.
ALLOW_PRIVATE_TARGETS = os.getenv("ALLOW_PRIVATE_TARGETS") == "1"


class PlatformError(Exception):
    pass


class PlatformUnavailable(PlatformError):
    """대회 서버가 응답하지 않음 (시간 초과·연결 끊김·과부하·일시 차단).

    토큰이나 주소가 틀린 게 아니라 '지금' 안 되는 것이라, 잠시 뒤 다시 하면 된다.
    이 경우엔 다른 플랫폼으로 헛시도하지 않는다 — 같은 서버라 똑같이 안 된다.
    """


class PlatformRejected(PlatformError):
    """대회 서버가 분명히 그 플랫폼으로 응답했지만 거절함 (토큰 무효·만료, 대회 시작 전 등).

    플랫폼이 확실하므로 다른 플랫폼으로 헛시도하지 않고 이 안내만 보여준다.
    (예전엔 토큰이 틀리면 4개 플랫폼을 다 찔러보고 엉뚱한 안내까지 섞어 보여줬다)
    """


def _server_message(payload) -> str:
    """에러 응답 JSON 에서 서버가 준 설명을 뽑는다 (CTFd·rCTF 는 message 를 준다)."""
    if isinstance(payload, dict):
        msg = payload.get("message") or payload.get("title") or ""
        return str(msg).strip()[:200]
    return ""


SERVER_UNAVAILABLE_MSG = (
    "대회 서버가 응답이 없어 (시간 초과·과부하). 서버가 느리거나 터진 상태야 — "
    "잠시 뒤 `/ctf pull` 로 다시 해줘."
)

# 문제 상세 조회가 이만큼 연달아 실패하면 서버가 버거운 것으로 보고 이번엔 그만 받는다.
DETAIL_FAIL_LIMIT = 3


@dataclass
class FetchContext:
    """fetch_challenges 에 넘기는 선택 옵션.

    skip: (remote_id, 이름) → 이미 디스코드에 있는 문제면 True. 그런 문제는 상세(설명·첨부)를
          다시 받지 않는다 — 어차피 건너뛰므로 대회 서버에 헛요청만 늘린다.
    progress: (처리한 수, 받을 수) 를 받는 async 콜백. 상세를 받는 동안 진행률 표시용.
    deferred: 서버가 응답이 없어 이번에 못 받은 문제 이름이 쌓인다 (다음 pull 에 이어서 받는다).
    """

    skip: Callable[[str, str], bool] | None = None
    progress: Callable[[int, int], Awaitable[None]] | None = None
    deferred: list[str] = field(default_factory=list)


async def _with_details(
    entries: list[tuple[str, str, str]],
    fetch_detail: Callable[[str], Awaitable[tuple[str, tuple]]],
    ctx: FetchContext | None,
) -> list[Challenge]:
    """목록 항목에 상세(설명·첨부)를 붙인다. 이미 있는 문제는 건너뛰고 새 문제만 조회한다.

    entries: [(remote_id, 이름, 분야)]. fetch_detail(remote_id) → (설명, 첨부들).
    서버가 응답하지 않은 문제(PlatformUnavailable)는 이번에 만들지 않고 ctx.deferred 로 넘긴다 —
    빈 설명으로 만들어 버리면 다음 pull 때 중복 방지에 걸려 다시는 못 채운다.
    연달아 DETAIL_FAIL_LIMIT 번 실패하면 나머지는 요청도 안 보낸다 (죽은 서버를 계속 두드리지 않게).
    """
    ctx = ctx or FetchContext()
    known = [bool(ctx.skip and ctx.skip(rid, name)) for rid, name, _ in entries]
    total = known.count(False)
    out: list[Challenge] = []
    handled = 0
    fails = 0
    for (rid, name, category), is_known in zip(entries, known):
        if is_known:
            out.append(Challenge(remote_id=rid, name=name, category=category))
            continue
        handled += 1
        if fails >= DETAIL_FAIL_LIMIT:
            ctx.deferred.append(name)
            continue
        await asyncio.sleep(DETAIL_DELAY)  # 연속 요청 레이트리밋 회피
        try:
            description, files = await fetch_detail(rid)
        except PlatformUnavailable:
            fails += 1
            ctx.deferred.append(name)
        else:
            fails = 0
            out.append(
                Challenge(
                    remote_id=rid,
                    name=name,
                    category=category,
                    description=description,
                    attachments=files,
                )
            )
        if ctx.progress and (handled % 3 == 0 or handled == total):
            try:
                await ctx.progress(handled, total)
            except Exception:
                pass  # 진행률 표시 실패가 임포트를 망치면 안 된다
    if not out and ctx.deferred:
        raise PlatformUnavailable(SERVER_UNAVAILABLE_MSG)
    return out


def _check_public(url: str) -> None:
    """대회 주소가 진짜 바깥 주소인지 확인한다.

    `/ctf import` 는 사람이 준 주소로 요청을 보내므로, 그대로 두면 서버 내부망이나
    클라우드 메타데이터(169.254.169.254) 를 찔러보는 통로가 된다. 관리자만 쓰는
    명령이라도 이런 요청이 나갈 이유가 없으니 사설/링크로컬 대역은 막는다.
    """
    if ALLOW_PRIVATE_TARGETS:
        return

    host = urllib.parse.urlparse(url).hostname
    if not host:
        raise PlatformError("주소에서 호스트를 읽지 못했습니다.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise PlatformError(f"주소를 찾을 수 없습니다: {host}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise PlatformError(
                f"내부 주소({ip})로는 요청하지 않습니다. 대회 사이트 주소를 확인해주세요."
            )


class _SafeResolver(aiohttp.ThreadedResolver):
    """DNS 조회 결과를 그 자리에서 검증하는 리졸버.

    _check_public 은 요청 전에 getaddrinfo 로 한 번 확인하지만, aiohttp 는 접속할 때
    DNS 를 또 조회한다. 그 틈에 짧은 TTL 로 공인 IP → 내부 IP 로 바꾸면(DNS 리바인딩)
    검사를 우회한다. 이 리졸버는 aiohttp 가 실제 접속에 쓸 바로 그 조회 결과를
    검증하므로, 검사와 접속이 같은 IP 임을 보장한다.
    """

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await super().resolve(host, port, family)
        for info in infos:
            ip = ipaddress.ip_address(info["host"])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise PlatformError(f"내부 주소({ip})로는 요청하지 않습니다.")
        return infos


def _make_session(**kwargs) -> aiohttp.ClientSession:
    """SSRF 방어 리졸버를 단 세션. 로컬 시험용 ALLOW_PRIVATE_TARGETS 면 끈다."""
    if ALLOW_PRIVATE_TARGETS:
        return aiohttp.ClientSession(**kwargs)
    connector = aiohttp.TCPConnector(resolver=_SafeResolver())
    return aiohttp.ClientSession(connector=connector, **kwargs)


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str  # 절대 URL (다운로드에 바로 쓸 수 있게 사이트 루트를 붙여둔다)


@dataclass(frozen=True)
class Challenge:
    remote_id: str
    name: str
    category: str
    description: str = ""
    attachments: tuple[Attachment, ...] = ()


# 사람이 브라우저 주소창에서 복사해 오기 쉬운 경로들. 사이트 루트만 있으면 되므로 떼어낸다.
SITE_PATHS = (
    "challenges",
    "scoreboard",
    "users",
    "teams",
    "settings",
    "profile",
    "login",
    "register",
    "notifications",
    "api/v1/challenges",
    "api/v1",
    "api",
)


def _clean_url(url: str) -> str:
    """주소를 사이트 루트로 다듬는다.

    브라우저에서 그대로 복사하면 .../challenges 같은 경로가 붙어 오기 쉬운데,
    우리는 루트에 /api/... 를 붙여 쓰므로 그대로 두면 요청이 엉뚱한 데로 간다.
    """
    url = url.strip().split("?")[0].split("#")[0].rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # 긴 경로부터 본다. 안 그러면 /api/v1/challenges 에서 challenges 만 떨어져 나간다.
    lowered = url.lower()
    for path in sorted(SITE_PATHS, key=len, reverse=True):
        suffix = "/" + path
        if lowered.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


async def _request(
    session: aiohttp.ClientSession, method: str, url: str, **kwargs
) -> tuple[int, dict | None]:
    """_request_once 를 감싸 시간 초과·연결 끊김을 사람이 읽을 PlatformError 로 바꾼다.

    대회 서버가 몰려서 느리면 aiohttp 가 TimeoutError 를 내는데, 이건 ClientError 가 아니라
    그대로 새어 나가 명령이 "문제가 생겼어: " (빈 메시지)로 죽었다.
    """
    try:
        return await _request_once(session, method, url, **kwargs)
    except TimeoutError as exc:
        raise PlatformUnavailable(SERVER_UNAVAILABLE_MSG) from exc
    except aiohttp.ClientError as exc:
        raise PlatformUnavailable(f"대회 사이트에 접속하지 못했어: {exc}") from exc


async def _request_once(
    session: aiohttp.ClientSession, method: str, url: str, **kwargs
) -> tuple[int, dict | None]:
    """요청을 보내고 (상태코드, JSON) 을 돌려준다.

    리다이렉트를 라이브러리에 맡기지 않고 직접 따라간다. 자동으로 따라가게 두면
    공인 주소가 내부 주소로 넘겨주는 순간 _check_public 이 무의미해지고,
    Authorization 헤더에 담긴 토큰까지 그 주소로 새어 나간다.
    """
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", USER_AGENT)  # Cloudflare 봇 차단 완화
    origin = urllib.parse.urlparse(url).hostname

    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url)
        try:
            resp = await session.request(
                method, url, headers=headers, allow_redirects=False, **kwargs
            )
        except aiohttp.ClientError as exc:
            raise PlatformUnavailable(f"대회 사이트에 접속하지 못했어: {exc}") from exc

        async with resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise PlatformError(f"리다이렉트 주소가 비어 있습니다 (HTTP {resp.status}).")
                url = urllib.parse.urljoin(url, location)

                # 다른 호스트로 넘어가면 인증 정보를 들고 가지 않는다.
                # 헤더뿐 아니라 쿠키(CTFd session, GZCTF_Token)도 지운다 — 세션
                # 쿠키 잼은 도메인 없이 저장돼 모든 호스트로 전송되므로, 안 지우면
                # 리다이렉트된 공격 호스트로 토큰이 새어 나간다.
                if urllib.parse.urlparse(url).hostname != origin:
                    headers.pop("Authorization", None)
                    session.cookie_jar.clear()
                    origin = urllib.parse.urlparse(url).hostname

                # 301/302/303 은 관례상 GET 으로 바뀐다.
                if resp.status in (301, 302, 303):
                    method = "GET"
                    kwargs.pop("json", None)
                continue

            # 응답 크기를 제한한다. 무한정 읽으면 메모리를 다 쓰고 컨테이너가 죽는다.
            body = await resp.content.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise PlatformError("응답이 너무 큽니다. 대회 사이트 주소가 맞는지 확인해주세요.")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, None

    raise PlatformError("리다이렉트가 너무 많습니다. 주소를 확인해주세요.")


# ---------- CTFd ----------

def _ctfd_auth(token: str) -> tuple[dict, dict]:
    """토큰인지 세션 쿠키인지 판별해 (headers, cookies) 를 만든다.

    CTFd 3.x 는 Access Token(`ctfd_...`) 을, 구버전이나 API 를 막은 대회는
    Access Tokens 탭 자체가 없다. 그럴 땐 브라우저 개발자도구에서 복사한
    `session` 쿠키를 대신 넣으면 된다.
    """
    token = token.strip()
    headers = {"Content-Type": "application/json"}
    if token.startswith("ctfd_"):
        headers["Authorization"] = f"Token {token}"
        return headers, {}
    # 'session=...' 통째로 붙여넣어도, 값만 붙여넣어도 받는다.
    if token.lower().startswith("session="):
        token = token.split("=", 1)[1]
    return headers, {"session": token}


async def fetch_ctfd(url: str, token: str, ctx: FetchContext | None = None) -> list[Challenge]:
    """CTFd: 문제 목록 + 문제별 상세(설명·첨부파일).

    목록만으로는 설명·파일이 안 와서, 문제마다 상세 조회를 한 번씩 더 한다.
    인증은 Access Token 또는 세션 쿠키 둘 다 받는다.
    """
    headers, cookies = _ctfd_auth(token)
    async with _make_session(timeout=TIMEOUT, cookies=cookies) as session:
        status, payload = await _request(
            session, "GET", f"{url}/api/v1/challenges", headers=headers
        )

        if status in (401, 403):
            msg = _server_message(payload)
            if isinstance(payload, dict):
                # /api/v1/challenges 가 JSON 으로 거절했다 = CTFd 가 확실하다.
                low = msg.lower()
                if "start" in low or "begun" in low:
                    raise PlatformRejected(
                        f"CTFd: 대회가 아직 시작 전이라 문제를 못 봐 (서버: {msg}). "
                        "시작한 뒤 `/ctf pull` 해줘."
                    )
                raise PlatformRejected(
                    "CTFd: 토큰이 거부됐어 — 틀렸거나 만료·삭제된 토큰이야"
                    + (f" (서버: {msg})" if msg else "")
                    + ".\n대회 사이트 → Settings → Access Tokens 에서 새로 만들고"
                    "(만료일은 비워둬) `/ctf creds token:새토큰` 한 뒤 `/ctf pull` 해줘.\n"
                    "(브라우저로는 문제가 보이는데 봇만 안 되면 거의 이 경우야)"
                )
            raise PlatformError("CTFd: 인증이 거부됐습니다. 토큰이나 세션 쿠키를 확인해주세요.")
        if status == 200 and not isinstance(payload, dict):
            # 일부 CTFd 는 미인증 요청을 403 대신 /login 으로 리다이렉트한다. 그러면
            # 로그인 HTML 이 와서 JSON 파싱이 안 된다 → 사실상 인증 문제로 본다.
            raise PlatformError(
                "CTFd: 로그인이 안 됐어 — 토큰이 유효한지(만료·팀 가입 여부) 확인해줘. "
                "(미인증 요청이 로그인 페이지로 넘어갔어)"
            )
        if status != 200 or not isinstance(payload, dict):
            raise PlatformError(f"CTFd: 응답이 올바르지 않습니다 (HTTP {status}).")
        if not payload.get("success"):
            raise PlatformError("CTFd: 실패 응답을 보냈습니다. 토큰을 확인해주세요.")

        entries = []
        for item in payload.get("data") or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            # 아직 공개 안 된 문제(hidden)는 건너뛴다. 목록에 나와도 state 로 걸러진다.
            if item.get("state") == "hidden":
                continue
            category = (item.get("category") or "misc").strip() or "misc"
            entries.append((str(item.get("id")), name, category))
        challenges = await _with_details(
            entries, lambda cid: _ctfd_detail(session, url, headers, cid), ctx
        )

    if not challenges:
        raise PlatformError("CTFd: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


def _is_transient(status: int, payload) -> bool:
    """상세 응답이 '지금만' 안 되는 경우인지 — 과부하(429·5xx), 또는 JSON 대신 HTML.

    HTML 은 레이트리밋으로 로그인 페이지에 튕겼거나 Cloudflare 가 막은 것이다.
    (잠긴 문제 403, 없는 문제 404 처럼 영영 안 되는 건 일시적이 아니다)
    """
    return status == 429 or status >= 500 or (status == 200 and not isinstance(payload, dict))


async def _ctfd_detail(
    session: aiohttp.ClientSession, url: str, headers: dict, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """문제 하나의 설명과 첨부파일을 가져온다.

    느리거나 레이트리밋(로그인 리다이렉트/차단)이면 잠깐 쉬었다 한 번 더 받는다.
    그래도 안 되면 PlatformUnavailable — 이번엔 그 문제를 만들지 않고 다음 pull 로 미룬다.
    잠긴 문제(403)·없는 문제(404)처럼 영영 안 되는 건 빈 설명으로 넘어간다.
    """
    transient = False
    for attempt in range(2):
        try:
            status, payload = await _request(
                session, "GET", f"{url}/api/v1/challenges/{cid}", headers=headers
            )
        except PlatformUnavailable:
            status, payload, transient = 0, None, True
        except PlatformError:
            status, payload, transient = 0, None, False
        else:
            transient = _is_transient(status, payload)

        if status == 200 and isinstance(payload, dict) and payload.get("success"):
            data = payload.get("data") or {}
            description = (data.get("description") or "").strip()
            files = []
            for raw in data.get("files") or []:
                # CTFd 파일 경로는 보통 상대경로(/files/...)라 사이트 루트를 붙인다.
                full = urllib.parse.urljoin(url + "/", raw.lstrip("/"))
                name = urllib.parse.urlparse(full).path.rsplit("/", 1)[-1] or "file"
                files.append(Attachment(name=name, url=full))
            return description, tuple(files)

        if attempt == 0:
            await asyncio.sleep(1.0)  # 레이트리밋 완화 대기 후 한 번 더
    if transient:
        raise PlatformUnavailable(SERVER_UNAVAILABLE_MSG)
    return "", ()


# ---------- rCTF ----------

async def fetch_rctf(url: str, token: str, ctx: FetchContext | None = None) -> list[Challenge]:
    """rCTF: 팀 토큰으로 로그인 교환 1회 → GET /api/v1/challs.

    토큰은 팀 로그인 링크(…/login?token=XXX)의 token 값. URL 인코딩된 채로 줘도 처리한다.
    """
    token = token.strip()
    if "%" in token:
        token = urllib.parse.unquote(token)
    if "token=" in token:
        # 로그인 링크를 통째로 붙여넣은 경우
        token = urllib.parse.unquote(token.split("token=", 1)[1].split("&", 1)[0])

    async with _make_session(timeout=TIMEOUT) as session:
        _, auth = await _request(
            session, "POST", f"{url}/api/v1/auth/login", json={"teamToken": token}
        )

        if not isinstance(auth, dict) or auth.get("kind") != "goodLogin":
            if isinstance(auth, dict) and auth.get("kind"):
                # rCTF 형식으로 거절했다 = rCTF 가 확실하다.
                raise PlatformRejected(
                    f"rCTF: 팀 토큰이 거부됐어 ({auth.get('kind')}: {_server_message(auth)}). "
                    "팀 로그인 링크(…/login?token=…)의 token 값을 다시 확인해서 "
                    "`/ctf creds token:…` 해줘."
                )
            raise PlatformError("rCTF: 팀 토큰이 거부됐습니다. 로그인 링크의 token 값을 확인해주세요.")

        bearer = auth.get("data", {}).get("authToken")
        status, payload = await _request(
            session, "GET", f"{url}/api/v1/challs", headers={"Authorization": f"Bearer {bearer}"}
        )

    if status != 200 or not isinstance(payload, dict) or payload.get("kind") != "goodChallenges":
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == "badNotStarted":
            raise PlatformRejected(
                "rCTF: 대회가 아직 시작 전이라 문제가 안 열렸어. 시작한 뒤 `/ctf pull` 해줘."
            )
        if kind:
            # 로그인까지 됐으니 rCTF 는 확실하다 — 서버가 준 이유를 그대로 보여준다.
            raise PlatformRejected(
                f"rCTF: 문제 목록을 거절했어 ({kind}: {_server_message(payload)}). "
                "문제 공개 전일 수 있어 — 잠시 뒤 `/ctf pull` 해줘."
            )
        raise PlatformError(f"rCTF: 문제 목록을 받지 못했습니다 (HTTP {status}). 대회 시작 전일 수 있습니다.")

    challenges = []
    for item in payload.get("data") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        # rCTF 는 목록 응답에 설명·파일이 이미 들어있어 추가 조회가 필요 없다.
        files = tuple(
            Attachment(name=(f.get("name") or "file"), url=urllib.parse.urljoin(url + "/", f.get("url", "")))
            for f in item.get("files") or []
            if f.get("url")
        )
        challenges.append(
            Challenge(
                remote_id=str(item.get("id")),
                name=name,
                category=(item.get("category") or "misc").strip() or "misc",
                description=(item.get("description") or "").strip(),
                attachments=files,
            )
        )
    if not challenges:
        raise PlatformError("rCTF: 문제가 하나도 없습니다.")
    return challenges


# ---------- noCTF ----------

def _first_tag(val) -> str | None:
    """분야 값에서 '주 분야' 하나만 뽑는다.

    noCTF 는 여러 분야를 쉼표로 이어 한 문자열에 담는다("crypto,misc",
    "beginner,rev,web"). 대회 사이트도 맨 앞 분야로 문제를 묶으므로
    (예: beginner,* → beginner), 우리도 첫 번째만 쓴다. 안 그러면 채널이
    'crypto-misc' 처럼 합쳐져 생긴다.
    """
    if isinstance(val, list):
        val = val[0] if val else None
    if isinstance(val, str) and val.strip():
        first = re.split(r"[,\s/|]+", val.strip())[0]
        return first or val.strip()
    return None


def _noctf_category(tags) -> str:
    """noCTF 문제의 tags(Record)에서 분야를 뽑는다. 못 찾으면 misc.

    tags 형태가 대회마다 달라서 방어적으로 본다: 'category' 키를 먼저 찾고,
    없으면 아무 값이나 첫 번째를 쓴다. 여러 분야가 묶여 있으면 첫 번째만 쓴다.
    """
    if isinstance(tags, dict):
        # 'difficulty'·'author' 같은 다른 태그보다 분야 키를 먼저 집는다.
        val = (
            tags.get("category")
            or tags.get("Category")
            or tags.get("categories")
            or tags.get("Categories")
        )
        first = _first_tag(val)
        if first:
            return first
        # 분야 키가 없을 때만 다른 값에서 방어적으로 찾는다 (difficulty/author 제외).
        for key, v in tags.items():
            if str(key).lower() in ("difficulty", "author", "level"):
                continue
            first = _first_tag(v)
            if first:
                return first
    else:
        first = _first_tag(tags)
        if first:
            return first
    return "misc"


async def fetch_noctf(url: str, token: str, ctx: FetchContext | None = None) -> list[Challenge]:
    """noCTF: 목록(GET …/challenges) → 문제별 상세(…/challenges/{id}).

    토큰은 로그인 후 브라우저에서 복사한 세션 토큰 (noCTF 는 Access Token 개념이 없다).
    공개된 뒤엔 토큰 없이도 목록이 열리는 배포가 있어 토큰은 있으면 붙이고 없으면 생략한다.

    배포마다 API 경로가 다르다: 같은 오리진의 `/api/challenges` 를 쓰는 곳도 있고,
    별도 API 서브도메인에서 `/challenges` (접두어 없음)를 쓰는 곳도 있어 둘 다 시도한다.
    목록엔 분야(tags)만, 상세에 설명·첨부가 들어있어 CTFd 처럼 문제마다 상세를 한 번 더 본다.
    """
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with _make_session(timeout=TIMEOUT) as session:
        # `/api/challenges` 와 `/challenges` 를 차례로 시도하고, 되는 접두어를 기억한다.
        prefix = None
        denied = False
        for candidate in ("/api", ""):
            status, payload = await _request(
                session, "GET", f"{url}{candidate}/challenges", headers=headers
            )
            if status in (401, 403):
                denied = True
                continue
            if status == 200 and isinstance(payload, dict) and isinstance(
                payload.get("data"), dict
            ) and payload["data"].get("challenges") is not None:
                prefix = candidate
                break
        if prefix is None:
            if denied:
                raise PlatformError(
                    "noCTF: 접근이 거부됐습니다. Bearer 토큰을 확인하거나, "
                    "대회가 아직 시작 전인지 확인해주세요."
                )
            raise PlatformError("noCTF: 문제 목록을 읽지 못했습니다.")

        entries = []
        for item in payload["data"]["challenges"]:
            if item.get("hidden"):
                continue
            name = (item.get("title") or "").strip()
            if not name:
                continue
            entries.append((str(item.get("id")), name, _noctf_category(item.get("tags"))))
        challenges = await _with_details(
            entries, lambda cid: _noctf_detail(session, url, prefix, headers, cid), ctx
        )

    if not challenges:
        raise PlatformError("noCTF: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


async def _noctf_detail(
    session: aiohttp.ClientSession, url: str, prefix: str, headers: dict, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """noCTF 문제 하나의 설명·첨부파일.

    서버 무응답·과부하면 PlatformUnavailable (다음 pull 로 미룬다), 그 밖의 실패는 빈 값.
    """
    try:
        status, payload = await _request(
            session, "GET", f"{url}{prefix}/challenges/{cid}", headers=headers
        )
    except PlatformUnavailable:
        raise
    except PlatformError:
        return "", ()
    if _is_transient(status, payload):
        raise PlatformUnavailable(SERVER_UNAVAILABLE_MSG)
    if status != 200 or not isinstance(payload, dict):
        return "", ()

    data = payload.get("data") or {}
    description = (data.get("description") or "").strip()

    files = []
    for raw in (data.get("metadata") or {}).get("files") or []:
        furl = raw.get("url") or ""
        if not furl:
            continue
        full = furl if furl.startswith("http") else urllib.parse.urljoin(url + "/", furl.lstrip("/"))
        name = raw.get("filename") or urllib.parse.urlparse(full).path.rsplit("/", 1)[-1] or "file"
        files.append(Attachment(name=name, url=full))
    return description, tuple(files)


# ---------- GZCTF ----------

def _gzctf_origin_and_game(url: str) -> tuple[str, int]:
    """'https://site/games/5' 에서 (사이트 origin, 게임번호) 를 뽑는다."""
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    match = re.search(r"/games?/(\d+)", parsed.path)
    if not match:
        raise PlatformError(
            "GZCTF 는 게임 주소가 필요해. `https://사이트/games/5` 처럼 "
            "게임 번호가 든 주소를 줘."
        )
    return origin, int(match.group(1))


async def fetch_gzctf(url: str, token: str, ctx: FetchContext | None = None) -> list[Challenge]:
    """GZCTF: GZCTF_Token 쿠키로 게임 문제 목록 → 문제별 상세(설명·첨부).

    토큰은 로그인 후 브라우저에서 복사한 `GZCTF_Token` 쿠키 값. 그 계정이 해당
    게임에 참가(수락)돼 있어야 하고, 대회가 진행 중이어야 문제가 보인다.
    목록은 분야별로 묶여 오고, 설명·첨부는 문제별 상세에만 있어 한 번 더 조회한다.
    """
    token = token.strip()
    if token.lower().startswith("gzctf_token="):
        token = token.split("=", 1)[1]
    origin, game_id = _gzctf_origin_and_game(url)

    async with _make_session(
        timeout=TIMEOUT, cookies={"GZCTF_Token": token}
    ) as session:
        status, payload = await _request(
            session, "GET", f"{origin}/api/game/{game_id}/details"
        )
        if status == 401:
            raise PlatformRejected(
                "GZCTF: 인증이 거부됐어 — GZCTF_Token 쿠키가 틀렸거나 만료됐어. 브라우저에서 "
                "다시 로그인한 뒤 쿠키 값을 `/ctf creds token:…` 으로 넣어줘."
            )
        if status == 400:
            reason = payload.get("title") if isinstance(payload, dict) else None
            raise PlatformRejected(
                f"GZCTF: 문제를 못 봤어 ({reason or '참가 미승인/시작 전/종료'}). "
                "그 계정이 게임에 참가·수락됐는지, 대회가 진행 중인지 확인해줘."
            )
        if status != 200 or not isinstance(payload, dict):
            raise PlatformError(f"GZCTF: 응답이 올바르지 않습니다 (HTTP {status}).")

        # challenges 는 {분야: [문제,...]} 딕셔너리라 펼친다.
        entries = []
        for category, arr in (payload.get("challenges") or {}).items():
            for item in arr or []:
                name = (item.get("title") or "").strip()
                cid = item.get("id")
                if not name or cid is None:
                    continue
                entries.append((str(cid), name, (category or "misc").strip() or "misc"))
        challenges = await _with_details(
            entries, lambda cid: _gzctf_detail(session, origin, game_id, cid), ctx
        )

    if not challenges:
        raise PlatformError("GZCTF: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


async def _gzctf_detail(
    session: aiohttp.ClientSession, origin: str, game_id: int, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """GZCTF 문제 상세: 설명(content) + 첨부(context.url). 컨테이너 문제는 파일 없음.

    서버 무응답·과부하면 PlatformUnavailable (다음 pull 로 미룬다), 그 밖의 실패는 빈 값.
    """
    try:
        status, payload = await _request(
            session, "GET", f"{origin}/api/game/{game_id}/challenges/{cid}"
        )
    except PlatformUnavailable:
        raise
    except PlatformError:
        return "", ()
    if _is_transient(status, payload):
        raise PlatformUnavailable(SERVER_UNAVAILABLE_MSG)
    if status != 200 or not isinstance(payload, dict):
        return "", ()

    description = (payload.get("content") or "").strip()

    files = []
    furl = (payload.get("context") or {}).get("url")
    if furl:
        full = furl if furl.startswith("http") else urllib.parse.urljoin(origin + "/", furl.lstrip("/"))
        name = urllib.parse.urlparse(full).path.rsplit("/", 1)[-1] or "file"
        files.append(Attachment(name=name, url=full))
    return description, tuple(files)


async def download(url: str, max_bytes: int) -> bytes | None:
    """첨부파일 다운로드. 시간 초과·연결 끊김이면 그 파일만 건너뛴다 (None)."""
    try:
        return await _download_once(url, max_bytes)
    except (TimeoutError, aiohttp.ClientError):
        return None


async def _download_once(url: str, max_bytes: int) -> bytes | None:
    """첨부파일을 내려받는다. 내부주소·리다이렉트 검사를 매 홉마다 하고, 크기를 제한한다.

    실패하면 None (임포트 자체는 계속 진행한다).
    """
    origin = urllib.parse.urlparse(url).hostname
    async with _make_session(timeout=TIMEOUT) as session:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                _check_public(url)
            except PlatformError:
                return None
            try:
                resp = await session.get(url, allow_redirects=False)
            except aiohttp.ClientError:
                return None
            async with resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return None
                    url = urllib.parse.urljoin(url, loc)
                    origin = urllib.parse.urlparse(url).hostname
                    continue
                if resp.status != 200:
                    return None
                data = await resp.content.read(max_bytes + 1)
                return None if len(data) > max_bytes else data
    return None


# ---------- 지문 감지 (어떤 플랫폼인지 먼저 훑어본다) ----------

# 프론트엔드(SPA) 프레임워크 표시. 이게 보이는데 어떤 fetcher 도 못 읽으면,
# 이 주소는 '화면용'이고 실제 API 는 딴 데 있다는 뜻이다 (예: api.사이트, scoreboard.).
_SPA_MARKERS = (
    "_app/immutable",       # SvelteKit (noCTF 등)
    "sveltekit",
    "__next_data__",        # Next.js
    "window.__nuxt",        # Nuxt
    'id="root"',            # 흔한 React 마운트 지점
    "data-reactroot",
    "blazor",               # GZCTF 등
)

# Cloudflare "브라우저 확인/Just a moment" 차단(UAM) 페이지에만 나오는 표시.
# 주의: `/cdn-cgi/challenge-platform` 스크립트는 정상 사이트에도 붙는 수동 탐지라
# 마커로 쓰면 멀쩡한 CTFd 를 차단으로 오판한다. 실제 차단 페이지 신호만 쓴다.
_CLOUDFLARE_MARKERS = (
    "just a moment",           # UAM 인터스티셜 제목
    "cf-browser-verification",
    "_cf_chl_opt",             # 챌린지 페이지 JS 변수
    "cf-challenge-running",
)


async def _probe_site(url: str) -> dict:
    """대회 첫 페이지를 딱 한 번 훑어 어떤 종류인지 힌트를 모은다.

    요청을 몰아 보내지 않으려고 여기서 단 한 번만 GET 한다 (실패하면 빈 dict).
    Cloudflare 차단 페이지는 403/503 으로 오므로 상태코드와 무관하게 본문을 읽는다.
    """
    body = b""
    try:
        async with _make_session(timeout=TIMEOUT) as session:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    _check_public(url)
                except PlatformError:
                    return {}
                try:
                    resp = await session.get(
                        url,
                        allow_redirects=False,
                        headers={"User-Agent": USER_AGENT},
                    )
                except aiohttp.ClientError:
                    return {}
                async with resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return {}
                        url = urllib.parse.urljoin(url, loc)
                        continue
                    body = await resp.content.read(256 * 1024)
                    break
    except Exception:
        return {}
    low = body.decode("utf-8", "ignore").lower()
    return {
        "cloudflare": any(m in low for m in _CLOUDFLARE_MARKERS),
        "spa": any(m in low for m in _SPA_MARKERS),
        "ctfd": "ctfd" in low,
    }


# ---------- 공통 진입점 ----------

FETCHERS = {
    "ctfd": fetch_ctfd,
    "rctf": fetch_rctf,
    "noctf": fetch_noctf,
    "gzctf": fetch_gzctf,
}


async def fetch_challenges(
    url: str, token: str, platform: str = "auto", ctx: FetchContext | None = None
) -> tuple[str, list[Challenge]]:
    """(플랫폼 이름, 문제 목록) 반환. platform='auto' 면 지문으로 순서를 정해 시도한다.

    ctx 를 주면 이미 있는 문제는 상세를 다시 받지 않고(ctx.skip), 서버가 응답 없어
    못 받은 문제는 ctx.deferred 에 모인다. 이미 있는 문제도 목록에는 포함돼 돌아온다.
    """
    url = _clean_url(url)
    _check_public(url)

    if platform != "auto":
        return platform, await FETCHERS[platform](url, token, ctx)

    # 첫 페이지를 한 번 훑어서 힌트를 얻는다 (요청은 한 번만 더 쓴다).
    info = await _probe_site(url)

    # Cloudflare 봇 차단이면 더 찔러봐야 다 막히고 IP 만 위험해진다 — 바로 멈춘다.
    if info.get("cloudflare"):
        raise PlatformError(
            "이 사이트는 Cloudflare 봇 차단('Just a moment…')이 켜져 있어서 "
            "서버에서 자동으로 못 불러와. 잠시 뒤 다시 시도하거나, 계속 막히면 "
            "문제를 수동으로 등록해줘 (`/ctf createchallenge`)."
        )

    # CTFd 표시가 보이면 CTFd 를 먼저 시도한다 (엉뚱한 순서로 헛요청을 줄인다).
    order = list(FETCHERS.items())
    if info.get("ctfd"):
        order.sort(key=lambda kv: kv[0] != "ctfd")

    errors = []
    for name, fetcher in order:
        try:
            return name, await fetcher(url, token, ctx)
        except (PlatformUnavailable, PlatformRejected):
            # 서버 무응답이거나, 플랫폼이 확실한데 거절(토큰 무효·시작 전)한 경우 —
            # 다른 플랫폼으로 찔러봐야 소용없고 엉뚱한 "SPA 주소" 안내만 섞인다. 원인 그대로 알린다.
            raise
        except PlatformError as exc:
            errors.append(str(exc))

    detail = "\n".join(f"- {e}" for e in errors)

    # 프론트엔드(SPA)인데 아무 fetcher 도 못 읽었다 → 실제 API 주소가 따로 있다.
    if info.get("spa"):
        raise PlatformError(
            f"`{url}` 은 화면용 프론트엔드(SPA) 페이지 같아. 대회의 **실제 API 주소**는 "
            "보통 따로 있어 (예: `api.사이트/…`, `scoreboard.사이트/…`). 브라우저에서 "
            "문제 목록 페이지를 연 뒤 개발자도구(F12) → Network 탭에서 문제를 불러오는 "
            "요청의 주소를 확인해, 그 주소로 다시 해줘.\n" + detail
        )

    raise PlatformError(
        f"`{url}` 을(를) 지원하는 플랫폼(CTFd, rCTF, noCTF, GZCTF)으로 인식하지 못했습니다.\n"
        + detail
        + "\n\n주소가 대회 사이트의 **첫 페이지**인지 확인해주세요 "
        "(문제 페이지 주소가 아니라 `https://ctf.example.com` 형태)."
    )
