# CTF 팀 디스코드 봇

CTF 대회를 여는 순간 **문제 목록이 디스코드에 자동으로 펼쳐지고**, 문제마다 전용 스레드에서 토론하고, 누가 뭘 풀었는지 자동으로 집계하는 팀 협업 봇.

대회 사이트에는 **문제 목록을 읽는 것 외에 아무 요청도 보내지 않는다** — 플래그 제출은 사람이 직접 하고, 봇은 디스코드에 기록만 남긴다.

```
▼ SomeCTF 2026          ← 카테고리 = 대회
   # general
   # 🔑-credentials
   # 📝-notes
   # 🎉-solves
   # 🌐-web              ← 채널 = 분야
      🧵 ❌ babyxss      ← 스레드 = 문제 (미해결)
      🧵 ✅ jwt-forge    ← 스레드 = 문제 (해결)
   # 🐚-pwn
      🧵 ✅ ret2win
```

---

## 기능

- **자동 임포트** — CTFd · rCTF · noCTF · GZCTF 4개 플랫폼에서 문제 목록·설명·첨부파일을 가져와 분야별 채널 + 문제별 스레드로 생성
- **CTFtime 연동** — `/ctf upcoming` 으로 예정 대회 조회, 대회 번호만 주면 이름·일정·주소 자동 채움
- **디스코드 이벤트 연동** — 이벤트를 만들어두면 봇이 대회 구조를 자동 생성, 시작 시각에 문제 자동 임포트(토큰 저장 시)
- **풀이 집계** — `/ctf solve` 로 ✅ 표시 + `#solves` 공지 + 스코어보드
- **종료 리포트 & 자동 아카이브** — 대회 끝나면 분야별·기여자 요약, 3일 뒤 스레드 자동 보관
- **삭제 전 백업** — 대회/문제를 지우면 대화·첨부·플래그를 ZIP으로 보관 (끌 수 있음)
- **다중 서버** — 봇 하나로 여러 디스코드 서버를 데이터 분리해 운영

---

## 요구사항

- Python 3.11+ (Docker로 돌리면 신경 안 써도 됨)
- 디스코드 봇 토큰
- 이 봇을 돌릴 곳 — **로컬 PC든 직접 운영하는 원격 서버(VPS)든 상관없음.** 24시간 켜두려면 상시 구동 서버를 권장.

---

## 1. 디스코드 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** 탭에서 토큰 발급(Reset Token)
2. **Bot 탭에서 `Server Members Intent` 켜기** (`/ctf solve with:` 자동완성에 팀원 목록을 쓰기 위해 필요)
3. **OAuth2 → URL Generator**
   - Scopes: `bot` + `applications.commands`
   - 권한: 본인 서버면 **Administrator** 하나가 제일 간단. 아니면 아래 개별 권한:
     - 채널 관리 · 역할 관리 · 스레드 관리 · 공개 스레드 만들기
     - 메시지 보내기(+스레드에서) · 링크 임베드 · 파일 첨부 · 메시지 기록 보기 · 반응 추가 · 메시지 관리
     - 이벤트 관리 (디스코드 이벤트 연동용)
4. 생성된 URL로 서버에 초대

---

## 2. 설정

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

`.env` 를 열어 채운다:

```ini
DISCORD_TOKEN=발급받은_봇_토큰
GUILD_ID=              # (선택) 서버 ID. 넣으면 슬래시 명령어 즉시 반영, 비우면 최대 1시간
BACKUP_ENABLED=1       # (선택) 삭제 시 백업. 끄려면 0
```

> **서버 ID**: 디스코드 설정 → 고급 → 개발자 모드 ON → 서버 우클릭 → 서버 ID 복사. 여러 서버는 콤마로 (`GUILD_ID=111,222`).
>
> `.env` 는 `.gitignore` 로 막혀 있어 커밋되지 않는다. **토큰을 채팅·코드·깃허브 어디에도 넣지 말 것.**

---

## 3. 실행

### Docker (권장)

```bash
docker compose up -d --build
docker logs -f ctf-bot        # "로그인: ..." 나오면 성공
```

`.env` 값을 바꾼 뒤에는 재적용을 위해 재생성한다:

```bash
docker compose up -d --force-recreate
```

### 또는 Python 직접

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

---

## 4. 디스코드 서버 준비

- **공지 채널 하나** — `ctf-notice`(또는 `notice`, `공지`) 이름의 채널을 만들어 둔다. 대회 참가 공지가 여기 올라간다. (없으면 서버 시스템 채널로 대체)
- **봇 역할을 위로** — 서버 설정 → 역할에서 봇 역할을 대회 참가 역할보다 **위**에 둔다. 안 그러면 참가 역할 부여가 실패한다.
- (백업용 `ctf-backup` 채널은 봇이 필요할 때 알아서 만든다.)

---

## 사용법

### 대회 만들기

```
/ctf new name:SomeCTF 2026 starts:2026-08-01 09:00 ends:2026-08-02 21:00
```

CTFtime에 있는 대회면 번호만 주면 이름·일정·주소가 자동으로 채워진다:

```
/ctf upcoming                    # 예정 대회 목록에서 번호 확인
/ctf new ctftime:2401            # 그 번호로 자동 생성
```

또는 **디스코드 이벤트**를 만들어두면 봇이 대회 구조를 자동 생성한다. 참가는 공지에 뜨는 **참가 버튼**으로 한다.

### 문제 불러오기

대회 카테고리 안에서(또는 `#ctf-bot-cmd` 에서 `name:` 지정):

```
/ctf import token:<토큰>
```

플랫폼은 자동 판별되며, 대회 만들 때 `token:` 을 같이 넣어두면 **시작 시각에 자동 임포트**된다. 연결 상태는 `/ctf creds` 로 확인·수정한다.

**플랫폼별 토큰/쿠키 얻는 법:**

| 플랫폼 | `token` 에 넣을 값 | `url` |
|--------|-------------------|-------|
| **CTFd** | Settings → Access Tokens 에서 발급 (없는 구버전은 브라우저의 `session` 쿠키 값) | 대회 사이트 주소 |
| **rCTF** | 팀 로그인 링크 `.../login?token=XXX` 의 token 값 (링크 통째로도 OK) | 대회 사이트 주소 |
| **noCTF** | 로그인 후 브라우저에서 복사한 세션 Bearer 토큰 | 대회 사이트 주소 |
| **GZCTF** | 로그인 후 브라우저의 `GZCTF_Token` 쿠키 값 (계정이 해당 게임에 참가·수락돼 있어야 함) | 게임 주소 `.../games/5` |

자동 임포트가 안 되는 플랫폼이면 수동으로:

```
/ctf createchallenge category:pwn name:babyheap
```

### 문제 풀었을 때

문제 스레드 안에서:

```
/ctf solve with:@동료
```

스레드가 ✅ 로 바뀌고 `#solves` 에 공지된다. `with` 는 공동 풀이자(선택). 실수했으면 `/ctf unsolve`.

> 플래그는 **사람이 대회 사이트에서 직접** 제출한다. `/ctf solve` 는 디스코드에 기록만 남긴다.

### 그 외 명령어

| 명령어 | 설명 |
|--------|------|
| `/ctf upcoming` | CTFtime 예정 대회 |
| `/ctf creds` | 대회 연결정보(주소·토큰·자동임포트) 확인·설정 |
| `/ctf pull` | 새로 추가된 문제만 가져오기 |
| `/ctf challenges` | 문제 목록 + 진행률 |
| `/ctf scoreboard` | 누가 몇 개 풀었나 |
| `/ctf schedule` | 대회 일정 변경 |
| `/ctf archive` | 대회 종료 → 스레드 일괄 보관 |
| `/ctf delete` | 대회 통째로 삭제 (백업 후) |
| `/ctf alias add/list/remove` | 분야 이름 매핑 (대회의 `binary` → 우리 `pwn`) |

---

## 대회 사이트에 하는 일 / 안 하는 일

| 동작 | |
|------|--|
| 문제 목록·상세 읽기 (`/ctf import`·`/ctf pull` 실행 시) | ⭕ |
| 플래그 제출 | ❌ 절대 안 함 |
| 스코어보드 조회 / 주기적 폴링 | ❌ 안 함 |

봇의 역할은 **디스코드에 기록을 남기는 것**이다. 대회 플랫폼을 대신 조작하지 않는다 — 자동화된 요청은 대회 규칙 위반 소지가 있고 팀 계정을 위험에 빠뜨리기 때문이다.

---

## 데이터 & 백업

- 모든 기록은 SQLite 파일 하나(`ctfbot.db`)에 저장된다. Docker면 `ctfbot_data` 볼륨, Python이면 프로젝트 폴더.
- 대회/문제를 지우면 대화·첨부·플래그가 ZIP으로 백업된다(`BACKUP_ENABLED=1`).
- 백업 예시 (Docker 볼륨):

```bash
docker run --rm -v ctf-bot_ctfbot_data:/data -v "$PWD":/out alpine \
    tar czf /out/ctfbot-backup.tar.gz -C /data .
```

---

## 보안 참고

- **토큰은 `.env` 안에만.** 유출 의심되면 개발자 포털에서 즉시 토큰 재발급.
- 컨테이너는 non-root · 읽기전용 파일시스템 · 포트 없음으로 하드닝돼 있다 (`docker-compose.yml`).
- `/ctf import` 는 사용자가 준 주소로 요청하므로 내부망/사설 IP 접근을 차단한다. 로컬 CTFd로 시험할 때만 `ALLOW_PRIVATE_TARGETS=1` 로 푼다.
- `ctfbot.db`·백업 ZIP에는 플래그가 들어있으니 공개 장소에 올리지 말 것.

---

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
