FROM python:3.13-slim

# Unbuffered output so container logs (including the first-run credentials
# banner) appear immediately in `docker compose logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ITAM_DB=/data/itam.db

WORKDIR /srv/itam

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user. The data directory is a mount point; setup.sh
# chowns the host directory to this uid so SQLite can write to it.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin itam \
    && mkdir -p /data && chown -R 10001:10001 /data /srv/itam
USER 10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# One worker on purpose: SQLite serialises writes anyway, and the sign-in
# lockout counter is per-process, so a single worker keeps it consistent.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
