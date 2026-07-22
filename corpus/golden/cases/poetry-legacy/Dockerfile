FROM python:3.12-slim AS builder

ENV POETRY_VIRTUALENVS_IN_PROJECT=1

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.4.1

COPY pyproject.toml poetry.lock ./

RUN poetry install --no-root --only main --no-interaction --no-ansi

FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home appuser

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY main.py ./

USER appuser

EXPOSE 8000

CMD ["python", "main.py"]
