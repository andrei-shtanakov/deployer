FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app

RUN echo 'deb http://10.255.255.1/debian bookworm main' > /etc/apt/sources.list.d/mirror.list \
    && apt-get -o Acquire::http::Timeout=15 -o Acquire::Retries=0 \
       -o APT::Update::Error-Mode=any update

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src/ci_build ./src/ci_build

RUN uv sync --frozen

RUN useradd --create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python"]
