# x86-64 · ARM(aarch64) 어디서도 돌아간다. python 공식 이미지가 두 아키텍처를 지원한다.
FROM python:3.13-slim

# 로그가 버퍼에 묶이지 않게(docker logs 로 바로 보이도록),
# 한글·이모지가 깨지지 않게 UTF-8 로 고정.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Seoul \
    DATA_DIR=/data

WORKDIR /app

# 의존성을 먼저 깔면 코드만 고쳤을 때 이 층을 다시 만들지 않는다.
# ARM 에서는 일부 패키지가 소스 빌드로 넘어갈 수 있어 빌드 도구를 잠깐 깔고 지운다.
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc python3-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY *.py ./
COPY cogs/ ./cogs/

# 봇은 root 권한이 필요 없다. 털렸을 때 컨테이너 안에서라도 root 를 잡지 못하게 한다.
# 코드(/app)는 root 소유로 두고 읽기만 허용한다 — 봇이 자기 코드를 고쳐 심지 못하게.
# 쓰기가 필요한 곳은 볼륨으로 붙는 /data 뿐이다.
RUN useradd --create-home --uid 10001 ctfbot \
    && mkdir -p /data \
    && chown ctfbot:ctfbot /data \
    && chmod -R a-w /app
USER ctfbot

CMD ["python", "bot.py"]
