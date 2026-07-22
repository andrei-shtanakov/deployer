FROM python:3.12-slim
WORKDIR /app
COPY main.py app.py ./
CMD ["python", "app.py"]
