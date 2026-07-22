FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home appuser

COPY app.py main.py ./

USER appuser

CMD ["python", "app.py"]
