# mail-shadow-mcp hosting portal
#
# The portal talks to the host Docker daemon through /var/run/docker.sock and
# creates one mail-shadow-mcp container per mailbox.  It is not a sandbox: a
# process with the Docker socket is effectively root on the host.  Read
# portal/README.md before exposing this to the internet.

FROM python:3.12-alpine

RUN apk add --no-cache ca-certificates tzdata docker-cli

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MSM_DATA_DIR=/srv/msm \
    MSM_BACKEND_MODE=network

COPY app ./app
COPY cli.py ./

RUN mkdir -p /srv/msm && chmod 700 /srv/msm

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1

CMD ["python", "-m", "app"]
