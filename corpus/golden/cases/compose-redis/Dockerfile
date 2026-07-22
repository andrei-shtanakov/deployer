FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

RUN useradd --create-home appuser
USER appuser

ENV REDIS_URL=redis://cache:6379/0

CMD ["python", "main.py"]
