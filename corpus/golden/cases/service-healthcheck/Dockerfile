FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home appuser

COPY main.py ./

USER appuser

EXPOSE 8000

CMD ["python", "main.py"]
