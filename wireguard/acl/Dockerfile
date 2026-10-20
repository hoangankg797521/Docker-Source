FROM python:3.12.10-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends nftables iproute2 wireguard-tools iputils-ping curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY acl.py /app/acl.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/app/acl.py"]

LABEL org.opencontainers.image.source="https://github.com/hoangankg797521/Docker-Source" org.opencontainers.image.licenses="AGPL-3.0-only"
