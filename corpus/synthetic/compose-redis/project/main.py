"""Compose fixture: Flask service whose /health proves Redis wiring."""

import os

import redis
from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health() -> str:
    redis.Redis.from_url(os.environ["REDIS_URL"]).ping()
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
