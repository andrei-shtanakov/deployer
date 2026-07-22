FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app

ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src/uv_minimal ./src/uv_minimal
RUN uv sync --frozen

FROM python:3.12-slim

RUN useradd --create-home appuser

WORKDIR /app

COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

CMD ["python", "-m", "uv_minimal"]
