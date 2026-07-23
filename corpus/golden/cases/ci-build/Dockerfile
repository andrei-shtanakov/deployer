FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src/ci_build ./src/ci_build

RUN uv sync --frozen

RUN useradd --create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python"]
