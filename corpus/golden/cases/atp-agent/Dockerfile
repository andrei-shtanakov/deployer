FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home appuser

COPY agent.py ./

USER appuser

CMD ["python", "agent.py"]
