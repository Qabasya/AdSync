# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-alpine AS builder
WORKDIR /app

# Отдельный слой под зависимости: код меняется чаще, чем pyproject.toml/uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src

FROM ghcr.io/astral-sh/uv:python3.12-alpine
WORKDIR /app

RUN addgroup -g 1000 appuser \
    && adduser -D -u 1000 -G appuser -s /sbin/nologin appuser

COPY --from=builder --chown=appuser:appuser /app/.venv ./.venv
COPY --from=builder --chown=appuser:appuser /app/src ./src

USER appuser
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "src/main.py"]
