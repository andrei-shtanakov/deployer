FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project --extra cli

FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 appuser

COPY --from=build /app/.venv /app/.venv

COPY main.py ./

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

CMD ["python", "main.py"]
