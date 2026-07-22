"""Legacy-format Poetry service fixture: Flask app with /health."""

from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health() -> str:
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
