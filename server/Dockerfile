FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sshdesk_server ./sshdesk_server

RUN useradd --system --uid 10001 sshdesk && mkdir -p /data && chown sshdesk /data
USER sshdesk
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "sshdesk_server.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
