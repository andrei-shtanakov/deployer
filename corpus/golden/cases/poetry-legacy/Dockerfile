FROM python:3.12-slim AS builder

ENV POETRY_VIRTUALENVS_IN_PROJECT=1

RUN pip install --no-cache-dir poetry==2.4.1

WORKDIR /app

COPY pyproject.toml poetry.lock ./

RUN poetry install --no-root --only main --no-interaction --no-ansi

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY main.py ./

RUN useradd --create-home appuser
USER appuser

CMD ["python", "main.py"]
