"""대회 플랫폼별 문제 목록 읽기 (CTFd, rCTF, …).

설계 원칙(PLAN.md 섹션 2): 대회 사이트에는 **문제 목록을 얻는 데 필요한 최소한의
읽기 요청**만 보낸다 (rCTF는 인증 교환 1회가 추가로 필요하다). 플래그 제출,
스코어보드 조회, 폴링 등 다른 요청은 어떤 이유로도 추가하지 않는다.
자동화된 요청은 대회 규칙 위반 소지가 있고 팀 계정을 위험에 빠뜨린다.

새 플랫폼을 추가하려면: fetch_xxx 함수를 만들고 FETCHERS 에 등록한다.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_REDIRECTS = 3
MAX_BODY = 5 * 1024 * 1024  # 문제 목록치고 넉넉한 상한

# 로컬에 띄운 CTFd 로 시험할 때만 켠다. 서버에서는 절대 켜지 않는다.
ALLOW_PRIVATE_TARGETS = os.getenv("ALLOW_PRIVATE_TARGETS") == "1"


class PlatformError(Exception):
    pass


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
    """요청을 보내고 (상태코드, JSON) 을 돌려준다.

    리다이렉트를 라이브러리에 맡기지 않고 직접 따라간다. 자동으로 따라가게 두면
    공인 주소가 내부 주소로 넘겨주는 순간 _check_public 이 무의미해지고,
    Authorization 헤더에 담긴 토큰까지 그 주소로 새어 나간다.
    """
    headers = dict(kwargs.pop("headers", {}) or {})
    origin = urllib.parse.urlparse(url).hostname

    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url)
        try:
            resp = await session.request(
                method, url, headers=headers, allow_redirects=False, **kwargs
            )
        except aiohttp.ClientError as exc:
            raise PlatformError(f"대회 사이트에 접속하지 못했습니다: {exc}") from exc

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


async def fetch_ctfd(url: str, token: str) -> list[Challenge]:
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
            raise PlatformError("CTFd: 인증이 거부됐습니다. 토큰이나 세션 쿠키를 확인해주세요.")
        if status != 200 or not isinstance(payload, dict):
            raise PlatformError(f"CTFd: 응답이 올바르지 않습니다 (HTTP {status}).")
        if not payload.get("success"):
            raise PlatformError("CTFd: 실패 응답을 보냈습니다. 토큰을 확인해주세요.")

        challenges = []
        for item in payload.get("data") or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            # 아직 공개 안 된 문제(hidden)는 건너뛴다. 목록에 나와도 state 로 걸러진다.
            if item.get("state") == "hidden":
                continue
            cid = item.get("id")
            description, files = await _ctfd_detail(session, url, headers, cid)
            challenges.append(
                Challenge(
                    remote_id=str(cid),
                    name=name,
                    category=(item.get("category") or "misc").strip() or "misc",
                    description=description,
                    attachments=files,
                )
            )

    if not challenges:
        raise PlatformError("CTFd: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


async def _ctfd_detail(
    session: aiohttp.ClientSession, url: str, headers: dict, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """문제 하나의 설명과 첨부파일을 가져온다. 실패해도 빈 값으로 넘어간다."""
    try:
        status, payload = await _request(
            session, "GET", f"{url}/api/v1/challenges/{cid}", headers=headers
        )
    except PlatformError:
        return "", ()
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        return "", ()

    data = payload.get("data") or {}
    description = (data.get("description") or "").strip()

    files = []
    for raw in data.get("files") or []:
        # CTFd 파일 경로는 보통 상대경로(/files/...)라 사이트 루트를 붙여 절대 URL 로 만든다.
        full = urllib.parse.urljoin(url + "/", raw.lstrip("/"))
        name = urllib.parse.urlparse(full).path.rsplit("/", 1)[-1] or "file"
        files.append(Attachment(name=name, url=full))
    return description, tuple(files)


# ---------- rCTF ----------

async def fetch_rctf(url: str, token: str) -> list[Challenge]:
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
            raise PlatformError("rCTF: 팀 토큰이 거부됐습니다. 로그인 링크의 token 값을 확인해주세요.")

        bearer = auth.get("data", {}).get("authToken")
        status, payload = await _request(
            session, "GET", f"{url}/api/v1/challs", headers={"Authorization": f"Bearer {bearer}"}
        )

    if status != 200 or not isinstance(payload, dict) or payload.get("kind") != "goodChallenges":
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

def _noctf_category(tags) -> str:
    """noCTF 문제의 tags(Record)에서 분야를 뽑는다. 못 찾으면 misc.

    tags 형태가 대회마다 달라서 방어적으로 본다: 'category' 키를 먼저 찾고,
    없으면 아무 값이나 첫 번째를 쓴다.
    """
    if isinstance(tags, dict):
        val = tags.get("category") or tags.get("Category")
        if isinstance(val, list):
            val = val[0] if val else None
        if isinstance(val, str) and val.strip():
            return val.strip()
        for v in tags.values():
            if isinstance(v, list) and v:
                return str(v[0])
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(tags, list) and tags:
        return str(tags[0])
    return "misc"


async def fetch_noctf(url: str, token: str) -> list[Challenge]:
    """noCTF: Bearer 토큰으로 GET /api/challenges (목록) → 문제별 상세.

    토큰은 로그인 후 브라우저에서 복사한 세션 JWT (noCTF 는 Access Token 개념이 없다).
    목록엔 분야(tags)만, 상세(/api/challenges/{id})에 설명·첨부가 들어있어
    CTFd 처럼 문제마다 상세를 한 번 더 본다.
    """
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async with _make_session(timeout=TIMEOUT) as session:
        status, payload = await _request(
            session, "GET", f"{url}/api/challenges", headers=headers
        )
        if status in (401, 403):
            raise PlatformError(
                "noCTF: 접근이 거부됐습니다. Bearer 토큰을 확인하거나, "
                "대회가 아직 시작 전인지 확인해주세요."
            )
        if status != 200 or not isinstance(payload, dict):
            raise PlatformError(f"noCTF: 응답이 올바르지 않습니다 (HTTP {status}).")

        data = payload.get("data")
        items = data.get("challenges") if isinstance(data, dict) else None
        if items is None:
            raise PlatformError("noCTF: 문제 목록을 읽지 못했습니다.")

        challenges = []
        for item in items:
            if item.get("hidden"):
                continue
            name = (item.get("title") or "").strip()
            if not name:
                continue
            cid = item.get("id")
            description, files = await _noctf_detail(session, url, headers, cid)
            challenges.append(
                Challenge(
                    remote_id=str(cid),
                    name=name,
                    category=_noctf_category(item.get("tags")),
                    description=description,
                    attachments=files,
                )
            )

    if not challenges:
        raise PlatformError("noCTF: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


async def _noctf_detail(
    session: aiohttp.ClientSession, url: str, headers: dict, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """noCTF 문제 하나의 설명·첨부파일. 실패해도 빈 값으로 넘어간다."""
    try:
        status, payload = await _request(
            session, "GET", f"{url}/api/challenges/{cid}", headers=headers
        )
    except PlatformError:
        return "", ()
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


async def fetch_gzctf(url: str, token: str) -> list[Challenge]:
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
            raise PlatformError("GZCTF: 인증이 거부됐습니다. GZCTF_Token 쿠키를 확인해주세요.")
        if status == 400:
            reason = payload.get("title") if isinstance(payload, dict) else None
            raise PlatformError(
                f"GZCTF: 문제를 못 봤어 ({reason or '참가 미승인/시작 전/종료'}). "
                "그 계정이 게임에 참가·수락됐는지, 대회가 진행 중인지 확인해줘."
            )
        if status != 200 or not isinstance(payload, dict):
            raise PlatformError(f"GZCTF: 응답이 올바르지 않습니다 (HTTP {status}).")

        # challenges 는 {분야: [문제,...]} 딕셔너리라 펼친다.
        challenges = []
        for category, arr in (payload.get("challenges") or {}).items():
            for item in arr or []:
                name = (item.get("title") or "").strip()
                cid = item.get("id")
                if not name or cid is None:
                    continue
                description, files = await _gzctf_detail(session, origin, game_id, cid)
                challenges.append(
                    Challenge(
                        remote_id=str(cid),
                        name=name,
                        category=(category or "misc").strip() or "misc",
                        description=description,
                        attachments=files,
                    )
                )

    if not challenges:
        raise PlatformError("GZCTF: 문제가 하나도 없습니다. 대회 시작 전일 수 있습니다.")
    return challenges


async def _gzctf_detail(
    session: aiohttp.ClientSession, origin: str, game_id: int, cid
) -> tuple[str, tuple[Attachment, ...]]:
    """GZCTF 문제 상세: 설명(content) + 첨부(context.url). 컨테이너 문제는 파일 없음."""
    try:
        status, payload = await _request(
            session, "GET", f"{origin}/api/game/{game_id}/challenges/{cid}"
        )
    except PlatformError:
        return "", ()
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


# ---------- 공통 진입점 ----------

FETCHERS = {
    "ctfd": fetch_ctfd,
    "rctf": fetch_rctf,
    "noctf": fetch_noctf,
    "gzctf": fetch_gzctf,
}


async def fetch_challenges(url: str, token: str, platform: str = "auto") -> tuple[str, list[Challenge]]:
    """(플랫폼 이름, 문제 목록) 반환. platform='auto' 면 순서대로 시도한다."""
    url = _clean_url(url)
    _check_public(url)

    if platform != "auto":
        return platform, await FETCHERS[platform](url, token)

    errors = []
    for name, fetcher in FETCHERS.items():
        try:
            return name, await fetcher(url, token)
        except PlatformError as exc:
            errors.append(str(exc))

    raise PlatformError(
        f"`{url}` 을(를) 지원하는 플랫폼(CTFd, rCTF, noCTF, GZCTF)으로 인식하지 못했습니다.\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\n주소가 대회 사이트의 **첫 페이지**인지 확인해주세요 "
        "(문제 페이지 주소가 아니라 `https://ctf.example.com` 형태)."
    )
