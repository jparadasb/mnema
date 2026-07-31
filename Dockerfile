# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim-bookworm

ARG MNEMA_VERSION=0.1.0
LABEL org.opencontainers.image.title="Mnema" \
      org.opencontainers.image.version="${MNEMA_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system --gid 10001 mnema \
 && useradd --system --uid 10001 --gid mnema --home /nonexistent --shell /usr/sbin/nologin mnema
RUN apt-get update \
 && apt-get install --yes --no-install-recommends \
      ca-certificates curl gnupg rclone=1.60.1+dfsg-2+b5 \
 && curl --fail --silent --show-error https://kopia.io/signing-key --output /tmp/kopia-key.asc \
 && test "$(gpg --show-keys --with-colons /tmp/kopia-key.asc | grep '^fpr:' | head -n 1 | cut -d: -f10)" = "7FB99DFD47809F0D5339D7D92273699AFD56A556" \
 && gpg --batch --yes --dearmor --output /etc/apt/keyrings/kopia-keyring.gpg /tmp/kopia-key.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/kopia-keyring.gpg] https://packages.kopia.io/apt/ stable main" > /etc/apt/sources.list.d/kopia.list \
 && apt-get update \
 && apt-get install --yes --no-install-recommends kopia=0.23.1 \
 && rm -rf /var/lib/apt/lists/* /tmp/kopia-key.asc
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --upgrade "pip==25.1.1" \
 && python -m pip install .
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["mnema"]
CMD ["web"]
