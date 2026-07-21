FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY main.py ./
CMD ["uv", "run", "--no-sync", "python", "main.py"]
