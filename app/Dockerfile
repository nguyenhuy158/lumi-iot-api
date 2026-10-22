FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY replay.py server.py d1_sync.py index.html ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SQLITE_TMPDIR=/data
USER 10001:10001
CMD ["python", "server.py"]
