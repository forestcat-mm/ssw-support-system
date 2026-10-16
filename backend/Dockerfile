# ビルドコンテキストはリポジトリのルート（templates/forms/ と mappings/ を参照するため）。
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# 仮想環境は /app の外に置く（開発時の ./:/app マウントで隠れないように）。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /opt/venv /app \
    && chown app:app /opt/venv /app

USER app
WORKDIR /app/backend

COPY --chown=app:app backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-cache

COPY --chown=app:app . /app

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
