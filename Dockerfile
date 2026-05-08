FROM python:3.11-slim AS builder
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

COPY --from=builder /root/.local /root/.local
COPY app.py .
COPY rag/ rag/
COPY data/ data/

ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

EXPOSE 5000
CMD ["gunicorn", "-w", "1", "--threads", "2", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
