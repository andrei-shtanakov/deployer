FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 appuser

COPY app.py main.py ./

USER appuser

EXPOSE 8000

CMD ["python", "app.py"]
