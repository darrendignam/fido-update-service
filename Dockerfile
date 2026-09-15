# Alpine, not slim: Debian glibc starts threads with clone3, which the seccomp
# profile of Docker < 20.10.10 rejects, so uvicorn cannot start on older hosts
# (the OPF build box runs Docker 19.03).
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="fidosigs" \
      org.opencontainers.image.description="FIDO format signature update service" \
      org.opencontainers.image.vendor="Open Preservation Foundation" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fidosigs ./fidosigs

RUN adduser -D -H -u 1000 fidosigs
USER fidosigs

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO /dev/null http://127.0.0.1:5000/format/latest || exit 1

# Proxy headers are trusted from any peer because the service is only ever
# reached through a proxy on a private Docker network.
ENTRYPOINT ["uvicorn", "fidosigs.main:APP", "--host", "0.0.0.0", "--port", "5000", "--proxy-headers", "--forwarded-allow-ips", "*"]
