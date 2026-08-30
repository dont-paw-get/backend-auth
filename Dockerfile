# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# 파이썬 런타임 최적화 설정
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 활용)
#
# build-essential / libpq-dev 는 설치하지 않습니다. requirements.txt 의 모든
# 패키지가 amd64/aarch64 wheel 을 제공해(psycopg[binary] 는 libpq 를 wheel 에
# 번들) 소스 컴파일이 전혀 없기 때문입니다. 이 apt 설치는 arm64 를 QEMU 로
# 에뮬레이션하는 멀티아키 빌드에서 가장 비싼 단계였습니다. wheel 이 없는
# 패키지를 나중에 추가하면 pip install 이 CI 에서 실패하므로, 그때 이 설치를
# 되살리면 됩니다.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 애플리케이션 소스
COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# 비루트 사용자로 실행
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# APP_HOST / APP_PORT 는 ConfigMap 으로 주입됨 (기본 0.0.0.0:8000)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
