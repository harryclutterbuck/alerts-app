FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY margin ./margin
COPY start.sh .
RUN chmod +x start.sh

# Everything that must survive a restart lives in /data: attach a persistent volume there.
# (Runs as root inside the container because many hosts mount volumes owned by root.)
ENV MARGIN_DB=/data/margin.db \
    MARGIN_BACKUP_DIR=/data/backups \
    MARGIN_OUTBOX=/data/outbox \
    MARGIN_MAILER=smtp \
    MARGIN_TRUST_PROXY=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health', timeout=4)"
CMD ["./start.sh"]
