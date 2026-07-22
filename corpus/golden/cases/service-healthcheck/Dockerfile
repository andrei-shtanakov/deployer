FROM python:3.12-slim

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home appuser

COPY main.py ./

USER appuser

CMD ["python", "-m", "main"]
