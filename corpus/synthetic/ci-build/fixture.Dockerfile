FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen
CMD ["uv", "run", "python", "-c", "import ci_build; print(ci_build.GREETING)"]
