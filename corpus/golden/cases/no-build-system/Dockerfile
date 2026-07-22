FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project

COPY main.py ./

RUN useradd --create-home appuser
USER appuser

CMD ["uv", "run", "--no-sync", "python", "main.py"]
