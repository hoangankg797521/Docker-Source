ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
FROM ${PYTHON_IMAGE}
ARG VERSION=2026.10.20.1
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="IPAM Network Operations" \
      org.opencontainers.image.description="IPAM, Scanner, Discovery, Topology, Server Connections, HA and offline PDF/HTML exports" \
      org.opencontainers.image.source="https://github.com/hoangankg797521/Docker-Source" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_PORT=8088 DB_BACKEND=postgres
RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap arp-scan iputils-ping fping net-tools iproute2 traceroute snmp \
    fonts-dejavu-core ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY db/schema.sql ./distribution/schema.sql
COPY scripts/bootstrap.py ./distribution/bootstrap.py
COPY scripts/apply_compatibility.py ./distribution/apply_compatibility.py
COPY release.json ./distribution/release.json
RUN python /app/distribution/apply_compatibility.py && mkdir -p /data
VOLUME ["/data"]
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('APP_PORT','8088')+'/healthz',timeout=4)"
ENTRYPOINT ["python", "/app/distribution/bootstrap.py"]
CMD ["python", "-m", "app.main"]
