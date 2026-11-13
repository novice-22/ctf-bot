# CTF 팀 디스코드 봇

CTF 대회가 열리면 **문제 목록이 디스코드에 자동으로 펼쳐지고**, 문제마다 전용 스레드에서 토론하고, 누가 뭘 풀었는지 자동으로 집계하는 팀 협업 봇.

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

> **이 저장소에는 토큰·서버 ID 같은 개인 설정값이 하나도 들어있지 않다.**
> 각자 자기 봇을 만들고, `.env` 파일에 자기 값을 채워서 실행한다. 아래 순서대로 따라 하면 된다.

---

## 설치 순서 한눈에 보기

1. [디스코드 봇 만들기](#1-디스코드-봇-만들기) — 개발자 포털에서 봇 생성, **토큰** 받기
2. [봇을 내 서버에 초대](#2-봇을-내-서버에-초대)
3. [설정값 채우기 (`.env`)](#3-설정값-채우기-env) — 토큰·서버 ID·알림 받을 사람 ID
4. [실행](#4-실행) — [내 PC](#4-a-내-pc에서-실행) 또는 [클라우드 서버(24시간)](#4-b-클라우드-서버에서-24시간-실행-권장)
5. [디스코드 서버 준비](#5-디스코드-서버-준비) — `lead` 역할, `ctf-notice` 채널

---

## 기능

- **문제 자동 불러오기** — CTFd · rCTF · noCTF · GZCTF · Flugsicherung(Platypwn) 에서 문제 목록·설명·첨부파일을 가져와 분야별 채널 + 문제별 스레드로 만든다
- **대회 시작 자동 수집** — 주소·토큰을 저장해두면 시작 30초·1분·2분 뒤 최대 3번 시도. 실패하면 운영자에게만 DM
- **CTFtime 연동** — `/ctf upcoming` 으로 예정 대회 조회, 대회 번호만 주면 이름·일정·주소 자동 입력
- **디스코드 이벤트 연동** — 서버 이벤트를 만들면 대회 구조 자동 생성 + 참가 공지
- **공개 / 비공개 대회** — 공개는 참가 버튼으로 누구나, 비공개는 운영진이 고른 사람만
- **풀이 집계** — `/ctf solve` 로 스레드 ✅ + `#solves` 알림(스레드당 1번) + 스코어보드
- **종료 리포트 & 자동 정리** — 대회가 끝나면 분야별·기여자 요약, 3일 뒤 스레드 자동 보관
- **삭제 전 백업** — 대회/문제를 지우면 대화·첨부·플래그를 ZIP 으로 보관
- **권한 구분** — 대회 생성·초대 같은 관리 명령은 `lead` 역할만
- **다중 서버** — 봇 하나로 여러 디스코드 서버를 데이터 분리해 운영

---

## 요구사항

- 디스코드 계정 + 봇을 넣을 서버의 **서버 관리** 권한
- 실행 환경: **Docker**(권장) 또는 **Python 3.11 이상**
- 시작 알림·자동 수집·자동 정리를 제대로 쓰려면 **항상 켜져 있는 곳**(클라우드 서버)에서 돌리는 것을 권장

---

## 1. 디스코드 봇 만들기

1. <https://discord.com/developers/applications> 접속 → 디스코드 계정으로 로그인
2. 오른쪽 위 **New Application** → 이름 입력(예: `우리팀 CTF봇`) → 약관 동의 → **Create**
3. 왼쪽 메뉴 **Bot** 에서:
   - **Reset Token** → **Yes, do it!** → 나온 토큰을 **Copy** 해서 메모장에 잠깐 붙여둔다
     (3단계에서 `.env` 의 `DISCORD_TOKEN` 에 넣는다)
     > 토큰은 이때 **한 번만** 보인다. 잃어버리면 다시 Reset Token 하면 된다 (이전 토큰은 무효가 됨).
     > **토큰은 봇의 비밀번호다.** 채팅·스크린샷·깃허브 어디에도 올리지 말 것.
   - 아래로 내려 **Privileged Gateway Intents** 의 **SERVER MEMBERS INTENT 를 켜고 Save Changes**
     > 이걸 안 켜면 봇이 켜지자마자 `PrivilegedIntentsRequired` 오류로 꺼진다
     > (`/ctf invite` 에서 서버 멤버 목록을 띄우는 데 필요). PRESENCE · MESSAGE CONTENT 는 필요 없다.
   - (선택) 봇 이름·프로필 사진도 여기서 바꾼다. 바꾸면 디스코드에 바로 반영된다.
   - (선택) **PUBLIC BOT** 을 끄면 다른 사람이 내 봇을 자기 서버에 초대하지 못한다.
     끌 때 오류가 나면 왼쪽 **Installation** 메뉴에서 Install Link 를 **None** 으로 바꾼 뒤 다시 끈다.
4. 왼쪽 메뉴 **General Information** 에서 **APPLICATION ID** 를 복사해둔다 (2단계 초대 링크에 쓴다)

---

## 2. 봇을 내 서버에 초대

아래 주소의 `<APPLICATION_ID>` 를 1-4에서 복사한 값으로 바꿔서 브라우저 주소창에 붙여넣는다:

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&permissions=403995479120&scope=bot+applications.commands
```

→ 봇을 넣을 서버 선택 → **승인**. 이 링크에는 봇에 필요한 권한만 들어있다:

| 권한 | 쓰는 곳 |
|------|---------|
| 채널 보기 · 메시지 보내기 · 메시지 기록 보기 | 기본 동작, 삭제 전 백업 |
| 채널 관리 | 대회 카테고리·분야 채널 만들기/지우기 |
| 역할 관리 | 대회 참가 역할 만들기·주기·회수 |
| 공개/비공개 스레드 만들기 · 스레드 관리 · 스레드에서 메시지 보내기 | 문제 스레드, ✅/❌ 표시, 자동 정리 |
| 링크 첨부 · 파일 첨부 · 반응 추가 | 알림 카드, 문제 첨부파일 |
| 이벤트 관리 | 디스코드 이벤트 만들기·연동 |

> 직접 고르고 싶으면 개발자 포털 **OAuth2 → URL Generator** 에서 Scopes 에 `bot`, `applications.commands` 를 체크하고
> Bot Permissions 에서 위 권한을 체크하면 같은 링크가 만들어진다. 내 서버라 귀찮으면 **Administrator** 하나만 줘도 된다.

---

## 3. 설정값 채우기 (`.env`)

이 저장소에는 실제 설정값이 없고 **양식(`.env.example`)만** 있다. 복사해서 `.env` 를 만든 뒤 내 값을 채운다.

```bash
cp .env.example .env          # Windows(PowerShell·명령 프롬프트): copy .env.example .env
```

| 이름 | 필요 | 넣을 값 | 구하는 법 |
|------|------|---------|-----------|
| `DISCORD_TOKEN` | **필수** | 봇 토큰 | [1단계](#1-디스코드-봇-만들기) Bot → Reset Token |
| `GUILD_ID` | 권장 | 봇을 쓸 디스코드 서버 ID. 여러 개면 콤마로 `111,222` | 개발자 모드 켠 뒤 **서버 아이콘 우클릭 → 서버 ID 복사하기** |
| `ALERT_USER_ID` | 권장 | 자동 수집이 끝내 실패했을 때 DM 받을 사람(보통 나)의 사용자 ID. 비우면 서버 주인에게 | 개발자 모드 켠 뒤 **내 프로필(이름) 우클릭 → 사용자 ID 복사하기** |
| `BACKUP_ENABLED` | 선택 | `1`(기본) 대회/문제 삭제 전 ZIP 백업, `0` 끔 | — |
| `ALLOW_PRIVATE_TARGETS` | 선택 | 내 PC 에 띄운 CTFd 로 시험할 때만 `1`. 평소엔 꺼둔다 | — |
| `DATA_DIR` | 선택 | (Python 직접 실행 시) DB·백업 저장 폴더. 안 적으면 프로젝트 폴더. **Docker 에서는 쓰지 말 것** | — |

**디스코드 개발자 모드 켜기** — 디스코드 앱 왼쪽 아래 ⚙️ **사용자 설정 → 고급 → 개발자 모드** 켜기.
그러면 서버·사람·채널을 우클릭했을 때 **ID 복사하기** 메뉴가 생긴다.

다 채운 `.env` 예시 (숫자는 예시일 뿐, 내 값으로):

```ini
DISCORD_TOKEN=여기에_1단계에서_복사한_토큰
GUILD_ID=123456789012345678
ALERT_USER_ID=987654321098765432
BACKUP_ENABLED=1
```

> - `GUILD_ID` 를 비우면 명령어가 '전역' 으로 등록돼 디스코드에 보이기까지 **최대 1시간** 걸린다. 넣어두면 즉시 보인다.
> - `.env` 는 `.gitignore` 에 들어있어 커밋되지 않는다. **공유·업로드 금지.** 토큰이 샜다면 개발자 포털에서 바로 Reset Token.
> - 값을 바꾼 뒤에는 봇을 다시 켜야 적용된다 (Docker 는 `docker compose up -d --force-recreate`).

---

## 4. 실행

둘 중 하나를 고른다.

| | 4-A. 내 PC | 4-B. 클라우드 서버 |
|--|-----------|-------------------|
| 장점 | 바로 시작, 무료 | 24시간 켜져 있음 |
| 단점 | PC 가 꺼지거나 잠자면 봇도 꺼짐 → 그동안 시작 알림·자동 수집·자동 정리가 안 됨 | 서버 준비 필요 |
| 추천 | 테스트, 잠깐 쓸 때 | 팀에서 계속 쓸 때 |

> ⚠️ **같은 봇 토큰으로 두 곳에서 동시에 켜지 말 것.** 알림·자동 작업이 두 번씩 일어나고 기록(DB)이 갈라진다.

### 4-A. 내 PC에서 실행

#### 방법 1 — Python 으로 바로

1. **Python 3.11 이상** 설치 — <https://www.python.org/downloads/>
   (Windows 는 설치 첫 화면에서 **Add python.exe to PATH** 체크)
2. **코드 받기** — `git clone https://github.com/novice-22/ctf-bot.git`
   또는 GitHub 페이지의 **Code → Download ZIP** 후 압축 풀기
3. 그 폴더에서 터미널을 열고:

   **Windows (PowerShell)**
   ```powershell
   cd ctf-bot
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   copy .env.example .env
   notepad .env          # 3단계 표대로 채우고 저장
   python bot.py
   ```
   > `Activate.ps1` 에서 "스크립트를 실행할 수 없으므로…" 오류가 나면
   > `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 를 한 번 실행하고 다시 한다.

   **macOS / Linux**
   ```bash
   cd ctf-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env             # 3단계 표대로 채우고 저장
   python bot.py
   ```
4. 로그에 `로그인: 봇이름#1234` 가 나오면 성공. 끌 때는 `Ctrl + C`.
5. 다음부터는 폴더에서 가상환경만 켜고(`.venv\Scripts\Activate.ps1` / `source .venv/bin/activate`) `python bot.py`.

- 기록(DB)은 프로젝트 폴더의 `ctfbot.db`, 백업 ZIP 은 `backups/` 에 쌓인다.
- `starts:` 같은 시각은 **이 PC 의 시간대**로 해석된다.

#### 방법 2 — Docker Desktop

1. **Docker Desktop** 설치 후 실행 — <https://www.docker.com/products/docker-desktop/>
2. 코드 받고 `.env` 채우기 (방법 1의 2~3번 중 `.env` 부분까지)
3. 그 폴더에서:
   ```bash
   docker compose up -d --build
   docker logs -f ctf-bot     # "로그인: ..." 이 보이면 성공. Ctrl+C 는 로그 보기만 끈다
   ```
4. 끄기 `docker compose stop` · 다시 켜기 `docker compose start`

### 4-B. 클라우드 서버에서 24시간 실행 (권장)

리눅스 서버면 어디든 된다 (Oracle Cloud 무료 티어, AWS, GCP, 아무 VPS).

- **사양**: 1 vCPU · 1GB RAM 이면 충분하다 (평소 메모리 150MB 안팎). x86 · ARM 둘 다 된다.
- **OS**: 아래 설명은 Ubuntu 22.04 / 24.04 기준.
- **방화벽**: 봇은 바깥으로 나가는 연결만 쓴다. **새로 열어야 할 포트는 없다** (SSH 만 있으면 됨).

**① 서버 접속** — 서버를 만들 때 받은 SSH 키와 서버 공인 IP 로:

```bash
ssh -i <내 SSH 키 파일> ubuntu@<서버 공인 IP>
```

(사용자 이름은 이미지마다 다르다. Ubuntu 는 보통 `ubuntu`.)

**② Docker 설치** (서버마다 처음 한 번)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit        # 로그아웃 → 다시 ssh 접속해야 sudo 없이 docker 가 된다
```

다시 접속해서 `docker compose version` 이 나오면 OK.

**③ 코드 받기**

```bash
git clone https://github.com/novice-22/ctf-bot.git
cd ctf-bot
```

**④ 설정 채우기**

```bash
cp .env.example .env
nano .env         # 3단계 표대로 채움 → Ctrl+O, Enter 로 저장 → Ctrl+X 로 나가기
chmod 600 .env    # 나만 읽을 수 있게
```

**⑤ 실행**

```bash
docker compose up -d --build
docker logs -f ctf-bot     # "로그인: 봇이름#1234" 확인 후 Ctrl+C (봇은 계속 돈다)
```

`restart: unless-stopped` 가 걸려 있어서 서버가 재부팅되거나 봇이 죽어도 **자동으로 다시 켜진다.**

**자주 쓰는 운영 명령** (모두 `ctf-bot` 폴더에서)

| 하고 싶은 것 | 명령 |
|-------------|------|
| 돌고 있는지 보기 | `docker ps` |
| 최근 로그 보기 | `docker logs --tail 100 ctf-bot` |
| 새 코드로 업데이트 | `git pull && docker compose up -d --build` |
| `.env` 바꾼 뒤 적용 | `docker compose up -d --force-recreate` |
| 잠깐 멈추기 / 다시 켜기 | `docker compose stop` / `docker compose start` |
| 완전히 내리기 (기록은 남음) | `docker compose down` |

> ⚠️ `docker compose down -v` 는 **기록(DB)까지 지운다.** 쓰지 말 것.

- 컨테이너 시간대는 **한국 시간(Asia/Seoul)** 으로 고정돼 있다 → `starts:` 같은 시각은 한국 시간으로 적는다.
- 기록(DB)·백업 ZIP 은 Docker 볼륨 `ctf-bot_ctfbot_data` 에 저장된다
  (폴더 이름을 바꿔서 받았다면 `<폴더이름>_ctfbot_data` — `docker volume ls` 로 확인).

---

## 5. 디스코드 서버 준비

봇을 켠 뒤 디스코드 서버에서 **한 번만** 해둔다.

1. **`lead` 역할** — 서버 설정 → 역할 → 역할 만들기 → 이름 `lead` (대소문자 무관) → 운영진에게 준다.
   대회 만들기·문제 불러오기·초대 같은 **관리 명령(🔒)은 `lead` 역할이나 서버 관리자만** 쓸 수 있다. 나머지는 누구나.
2. **공지 채널** — 모두가 볼 수 있는 `ctf-notice` 채널을 만든다 (`bot-notice`, `notice`, `공지` 도 인식하고, 앞에 이모지가 붙어도 된다).
   공개 대회를 만들면 여기에 **[참가할게]** 버튼이 달린 공지가 올라간다.
   없으면 서버의 시스템 메시지 채널(환영 메시지 채널)에 올리고, 그것도 없으면 공개 대회를 만들지 않는다.
3. **(선택) 명령 채널** — `ctf-bot-cmd` 채널을 만들어두면 대회 카테고리 밖에서도 `/ctf import name:대회이름` 처럼 대회를 골라 실행할 수 있다.
4. **봇 역할 위치** — 서버 설정 → 역할에서 봇 역할이 대회 참가 역할들보다 **위**에 있어야 한다.
   보통은 자동으로 그렇게 되고, 참가 역할 주기가 실패하면 봇 역할을 위로 끌어올린다.
5. **확인** — 아무 채널에서 `/ctf help` 를 쳐서 안내가 나오면 준비 끝.

(백업용 `🗄️-ctf-backup` 채널은 봇이 처음 백업할 때 알아서 만든다. 일반 멤버에게는 안 보인다.)

---

## 6. 사용법

🔒 = `lead` 역할(또는 서버 관리자)만 사용. 대부분 **대회 카테고리 안 채널**에서 실행하고, 풀이 명령은 **문제 스레드 안**에서 실행한다.
자세한 안내는 디스코드에서 `/ctf help` (나만 보임).

### 대회 만들기 🔒

```
/ctf upcoming days:7          ← 7일 안에 시작하는 CTFtime 대회와 번호 확인
/ctf new ctftime:2401         ← 번호만 주면 이름·일정·주소 자동 + 참가 공지 + 디스코드 이벤트
```

직접 입력할 수도 있다:

```
/ctf new name:SomeCTF 2026 url:https://somectf.com token:<토큰> starts:2026-08-01 09:00 ends:2026-08-02 21:00
```

- `url` + `token` 까지 넣으면 **대회 시작 30초·1분·2분 뒤 문제를 자동으로 가져온다** (시작 시각이 있어야 함. 성공하면 남은 시도는 건너뜀).
  3번 다 실패하면 `ALERT_USER_ID` 에게만 DM 이 가고, 그때는 `/ctf pull` 로 직접 가져온다.
- `ends` 를 넣으면 끝날 때 결과 리포트가 올라오고, 3일 뒤 스레드가 자동으로 정리된다.

**비공개 대회** (정해진 사람만):

```
/ctf new ctftime:2401 private:true
```

공지·참가 버튼·디스코드 이벤트를 만들지 않는다. 대회 카테고리 안에서 `/ctf invite` 🔒 → 드롭다운에서 여러 명을 골라 한 번에 초대한다 (`/ctf kick` 🔒 으로 내보내기).

**디스코드 이벤트로 만들기**: 서버에 이벤트를 만들면 봇이 같은 이름의 대회를 자동으로 만든다.
이벤트 위치나 설명에 대회 주소(`https://…`)를, 설명에 `token: 토큰` 한 줄을 적으면 자동 수집도 켜진다 (토큰 줄은 봇이 읽고 바로 지운다).

### 참가하기

- 공개 대회: `ctf-notice` 공지의 **[참가할게]** 버튼 → 대회 카테고리가 보인다
- 문제 스레드: 분야 채널의 문제별 **[참가하기]** 버튼, 또는 `/ctf joinall` 로 모든 문제 스레드에 한 번에

### 문제 불러오기

```
/ctf creds url:https://somectf.com token:<토큰>   🔒 주소·토큰 저장 (저장하면 바로 연결 확인)
/ctf import                                      🔒 처음 한 번 전체 불러오기 (저장된 주소·토큰 사용)
/ctf pull                                        새로 나온 문제만 추가 (누구나)
/ctf creds check:True                            🔒 저장된 주소·토큰이 지금 되는지 확인
/ctf creds                                       🔒 저장된 연결 정보 보기 (나만 보임)
```

- 플랫폼은 자동으로 알아낸다. `/ctf new` 에서 `url`·`token`(+시작 시각)을 넣었다면 시작할 때 자동으로 불러오니 따로 할 필요 없다.
- 이미 받은 문제는 다시 만들지 않는다. 여러 명이 동시에 `/ctf pull` 해도 대회 사이트에는 한 번만 요청한다.
- 대회 서버가 느리거나 터졌으면 받은 데까지만 만들고, 나머지는 다음 `/ctf pull` 때 이어서 받는다.

**플랫폼별 `token` · `url` 구하는 법**

| 플랫폼 | `token` 에 넣을 값 | `url` 에 넣을 값 |
|--------|-------------------|-----------------|
| **CTFd** | 대회 사이트 → **Settings → Access Tokens → Generate** 로 만든 `ctfd_…` 토큰. 그 메뉴가 없으면 브라우저의 `session` 쿠키 값 | 대회 사이트 주소 (`https://ctf.example.com`) |
| **rCTF** | 팀 로그인 링크 `…/login?token=XXX` 의 token 값 (링크를 통째로 붙여넣어도 됨) | 대회 사이트 주소 |
| **noCTF** | 로그인한 브라우저의 Bearer 토큰 (F12 → Network 탭 → 아무 API 요청의 `Authorization: Bearer …` 값) | API 주소가 사이트와 다를 수 있다. F12 → Network 탭에서 `challenges` 요청이 가는 주소의 앞부분 |
| **GZCTF** | 로그인한 브라우저의 `GZCTF_Token` 쿠키 값. 그 계정이 대회에 참가 승인돼 있어야 함 | 게임 주소 `https://…/games/5` |
| **Flugsicherung** (Platypwn) | 로그인 토큰 `eyJ…` — **3분이면 만료**돼서 저장하지 말고 아래 방법으로 복사해 바로 `/ctf import token:` | 대회 사이트 주소 (API 주소는 봇이 알아서 찾음) |

> **쿠키 값 복사하는 법**: 대회 사이트에서 F12 → **Application** 탭(파이어폭스는 Storage) → 왼쪽 **Cookies** → 사이트 주소 → 이름(`session`, `GZCTF_Token`) 옆의 Value 복사.
> 자동으로 못 불러오는 대회(Cloudflare 보호 등)는 `/ctf createchallenge category:pwn name:문제이름` 으로 직접 만든다.

<details>
<summary><b>Flugsicherung(Platypwn) 토큰 복사하는 법</b> — 토큰이 쿠키·Storage 에 없고 메모리에만 있다</summary>

1. 대회 사이트에서 F12 → **Console** 탭 → 아래를 붙여넣고 Enter (붙여넣기가 막히면 먼저 `allow pasting` 입력)
   ```js
   (() => {
     const orig = window.fetch;
     window.fetch = async (input, init = {}) => {
       const h = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
       const auth = h.get('authorization') || '';
       if (auth.startsWith('Bearer ')) {
         window.__tok = auth.slice(7);
         window.fetch = orig;
         console.log('토큰 잡음 → copy(__tok) 로 복사');
       }
       return orig(input, init);
     };
     console.log('준비됨 — 왼쪽 메뉴에서 아무 메뉴나 눌러줘');
   })();
   ```
2. 왼쪽 메뉴(예: **Your team**)를 누른다 (새로고침 X)
3. `copy(__tok)` 입력 → 클립보드에 복사됨 → **3분 안에** `/ctf import url:대회사이트주소 token:붙여넣기`
4. 새 문제가 나오면 같은 방법으로 다시 import (이미 받은 문제는 건너뜀). `/ctf pull` 은 저장된 토큰이 만료돼서 안 된다.

</details>

### 문제 풀었을 때

문제 스레드 안에서:

```
/ctf solve                       ← 풀이 등록
/ctf solve with:@철수 @영희      ← 같이 푼 사람 함께 (@ 치고 고르기, 여러 명 가능)
/ctf unsolve                     ← 잘못 등록했을 때 취소
```

- 스레드 이름이 ✅ 로 바뀌고 `#solves` 에 알림이 올라간다. **스레드당 한 번만** 등록된다 (다시 하려면 `/ctf unsolve` 후).
- 스레드 이름 변경은 디스코드 제한(스레드마다 **10분에 2번**) 때문에 몇 분 늦을 수 있다. 등록·알림은 바로 된다.

> 플래그는 **사람이 대회 사이트에서 직접** 제출한다. `/ctf solve` 는 디스코드에 기록만 남긴다.

### 전체 명령어

| 명령어 | 설명 |
|--------|------|
| `/ctf help` | 명령어 안내 (나만 보임) |
| `/ctf new` 🔒 | 대회 만들기 (`ctftime` · `url` · `token` · `starts` · `ends` · `private`) |
| `/ctf invite` · `/ctf kick` 🔒 | 대회에 사람 초대 / 내보내기 (드롭다운에서 여러 명) |
| `/ctf creds` 🔒 | 대회 주소·토큰 보기·저장·확인 (`check:True`) |
| `/ctf import` 🔒 | 문제 전체 불러오기 |
| `/ctf pull` | 새로 나온 문제만 불러오기 |
| `/ctf joinall` | 모든 문제 스레드에 한 번에 참여 |
| `/ctf solve` · `/ctf unsolve` | 풀이 등록 / 취소 |
| `/ctf challenges` | 문제 목록 + 진행률 (`category:` 로 분야만) |
| `/ctf scoreboard` | 누가 몇 개 풀었나 |
| `/ctf upcoming` | CTFtime 진행 중·예정 대회 (`days:` 일 이내) |
| `/ctf createchallenge` | 문제 직접 추가 |
| `/ctf deletechallenge` | 문제 지우기 (기록은 백업) |
| `/ctf button` | 실수로 지운 문제 참가 버튼 다시 올리기 |
| `/ctf alias add/list/remove` | 분야 이름 묶기 (대회의 `binary` → 우리 `pwn`) |
| `/ctf schedule` 🔒 | 대회 일정 변경 (공지도 같이 수정) |
| `/ctf archive` 🔒 | 대회 정리 — 스레드 접기 (종료 3일 뒤엔 자동) |
| `/ctf delete` 🔒 | 대회 통째로 삭제 (삭제 전 백업) |

---

## 7. 문제가 생겼을 때

| 증상 | 해결 |
|------|------|
| 봇이 켜지자마자 꺼지고 `PrivilegedIntentsRequired` | 개발자 포털 → Bot → **SERVER MEMBERS INTENT** 켜고 저장 → 봇 다시 켜기 |
| `.env 에 DISCORD_TOKEN 이 없습니다` | `.env` 파일 이름·위치 확인 (`bot.py` 와 같은 폴더, 이름이 `.env.txt` 가 아닌지) |
| `Improper token has been passed` | 토큰이 틀렸거나 Reset 됨 → 새로 받아서 `.env` 에 넣기 |
| 슬래시 명령어가 안 보임 | `GUILD_ID` 확인 → 봇 다시 켜기 → 디스코드 새로고침(`Ctrl + R`). 초대할 때 `applications.commands` 가 빠졌으면 2단계 링크로 다시 초대 |
| "이건 **lead** 역할만…" | 그 사람에게 `lead` 역할 주기 |
| "공지 채널이 없어서 대회를 안 만들었어" | `ctf-notice` 채널 만들기 (또는 `private:true`) |
| 참가 버튼을 눌러도 역할이 안 생김 | 봇 역할을 대회 참가 역할보다 위로 올리기 |
| 문제를 못 불러옴 | 메시지 안내를 따른다. 토큰 문제면 `/ctf creds token:새토큰` (바로 확인됨), 대회 서버가 느리면 잠시 뒤 `/ctf pull` |
| 스레드 ✅/❌ 가 늦게 바뀜 | 디스코드 제한(10분에 2번) 때문이며 정상. 몇 분 뒤 맞춰진다 |
| 시각이 9시간 어긋남 | 시간대 문제. Docker 는 한국 시간 고정, Python 직접 실행은 PC 시간대 기준 |
| 로그에 `Privileged message content intent is missing` 경고 | 무시해도 된다 (이 봇은 메시지 내용을 읽지 않는다) |

---

## 대회 사이트에 하는 일 / 안 하는 일

| 동작 | |
|------|--|
| 문제 목록·상세 읽기 (`/ctf import` · `/ctf pull` · 시작 시 자동 수집 · `/ctf creds` 확인) | ⭕ |
| 플래그 제출 | ❌ 절대 안 함 |
| 스코어보드 조회 / 주기적 폴링 | ❌ 안 함 |

- 이미 받은 문제는 상세를 다시 요청하지 않는다. 같은 대회에 동시에 들어온 요청은 하나로 합치고, 20초 안의 재요청은 직전 결과를 다시 쓴다.
- 대회 서버가 연달아 응답하지 않으면 바로 멈추고 다음 `/ctf pull` 로 미룬다 (무한 재시도 없음).

봇의 역할은 **디스코드에 기록을 남기는 것**이다. 대회 플랫폼을 대신 조작하지 않는다 — 자동화된 요청은 대회 규칙 위반 소지가 있고 팀 계정을 위험에 빠뜨리기 때문이다.

---

## 데이터 & 백업

- 모든 기록은 SQLite 파일 하나(`ctfbot.db`)에 저장된다. Docker 면 `ctfbot_data` 볼륨, Python 이면 프로젝트 폴더(또는 `DATA_DIR`).
- 대회/문제를 지우면 대화·첨부·플래그가 ZIP 으로 백업된다 (`BACKUP_ENABLED=1`).
- 서버 통째 백업 (Docker 볼륨 → 현재 폴더에 `tar.gz`):

```bash
docker run --rm -v ctf-bot_ctfbot_data:/data -v "$PWD":/out alpine \
    tar czf /out/ctfbot-backup.tar.gz -C /data .
```

---

## 보안 참고

- **토큰은 `.env` 안에만.** 실수로 어딘가 올렸다면 개발자 포털에서 즉시 Reset Token.
- 컨테이너는 non-root · 읽기 전용 파일시스템 · 열린 포트 없음으로 잠가뒀다 (`docker-compose.yml`).
- `/ctf import` 는 사용자가 준 주소로 요청하므로 내부망/사설 IP 접근을 막는다. 내 PC 의 CTFd 로 시험할 때만 `ALLOW_PRIVATE_TARGETS=1` 로 푼다.
- `ctfbot.db` · 백업 ZIP 에는 플래그와 대화가 들어있으니 공개된 곳에 올리지 말 것.

---

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
