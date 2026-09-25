# jev-oncall webhook server. Standard library only, so no pip install.
#
#     docker build -t jev-oncall .
#     docker run -p 8090:8090 -e TYPESAFE_API_KEY -e JEV_WEBHOOK_SECRET jev-oncall
#
# Mount your config and pass --config, or set JEV_ONCALL_CONFIG:
#     docker run -v $PWD/jev-oncall.toml:/config/jev-oncall.toml \
#         -e JEV_ONCALL_CONFIG=/config/jev-oncall.toml ... jev-oncall
FROM python:3.12-slim

WORKDIR /app
COPY triage.py server.py shadow.py evaluate.py generate_dashboard.py generate_alerts.py \
     topology.json jev-oncall.example.toml alerts.json ./

RUN useradd --system --uid 10001 --no-create-home jev \
    && mkdir /data && chown jev /data
USER jev
# The review store lives here: mount a volume so reviews survive a new container.
VOLUME /data

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)"]

ENTRYPOINT ["python", "server.py", "--host", "0.0.0.0", "--port", "8090"]
