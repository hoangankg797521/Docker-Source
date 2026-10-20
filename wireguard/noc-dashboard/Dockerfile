FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080 DATA_DIR=/data
WORKDIR /app
COPY server.py /app/server.py
COPY static /app/static
RUN addgroup --system --gid 10001 app && adduser --system --uid 10001 --ingroup app app \
    && mkdir -p /data && chown app:app /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2)" || exit 1
CMD ["python", "/app/server.py"]

LABEL org.opencontainers.image.source="https://github.com/hoangankg797521/Docker-Source" org.opencontainers.image.licenses="AGPL-3.0-only"
