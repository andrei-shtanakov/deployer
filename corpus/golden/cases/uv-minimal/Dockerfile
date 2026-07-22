FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/uv_minimal ./src/uv_minimal

RUN uv sync --frozen

RUN useradd --create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python"]
