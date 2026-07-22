FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    gcc \
    libpq-dev \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get purge -y gcc libpq-dev libc6-dev && apt-get autoremove -y

COPY main.py ./

RUN useradd --create-home appuser
USER appuser

CMD ["python", "main.py"]
