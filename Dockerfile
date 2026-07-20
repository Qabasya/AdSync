# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app

# Отдельный слой под зависимости: код меняется чаще, чем pyproject.toml/uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder --chown=appuser:appuser /app /app

USER appuser
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "src/main.py"]
