FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY config ./config
COPY db ./db
COPY gateway ./gateway
COPY worker ./worker
COPY sim/net ./sim/net
