# syntax=docker/dockerfile:1
#
# Napkin Studio OS as one container: the axum host, the shell, and the
# synthesis agent on the Anthropic Messages API. No Claude Code CLI — a CLI
# login is one person's subscription, which is not what a shared host should be
# spending. NAPKIN_BACKEND=api makes that explicit and refuses to start without
# a key.
#
#   docker build -t napkin .
#   docker run -p 8080:8080 -e ANTHROPIC_API_KEY=sk-ant-... -v napkin:/data napkin
#
# Build with --build-arg WITH_PDF=0 to drop Chromium (~400 MB); HTML export
# still works and PDF export tells the user why it can't.

# ── the shell ────────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS shell
WORKDIR /src/app
# Dependencies first, so editing a component doesn't re-run npm ci.
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/ ./
RUN npm run build

# ── the host, and the seed template ──────────────────────────────────────────
FROM rust:1-slim-bookworm AS host
# `ring` (via rustls) needs a C compiler; nothing here needs OpenSSL.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates/ ./crates/
COPY app/templates/ ./app/templates/
# The Tauri app is a workspace member that needs GTK/WebKit to build. It is not
# part of this image, so drop it rather than resolve its whole dependency tree.
RUN sed -i 's#, "app/src-tauri"##' Cargo.toml
RUN cargo build --release -p napkin-web
# Bake Brief Maker in, so a first visitor lands on a launcher with an app in it.
RUN cargo run --release -p clan-sdk --example make_brief_maker \
 && mkdir -p /seed && mv brief-maker.app.clan /seed/

# ── runtime ──────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS runtime
ARG WITH_PDF=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates tini \
 && if [ "$WITH_PDF" = "1" ]; then \
      apt-get install -y --no-install-recommends chromium; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# Debian marks the system Python externally managed (PEP 668), so the SDK goes
# in a venv rather than being forced past that with --break-system-packages.
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
COPY mock-agent/requirements.txt /tmp/requirements.txt
RUN python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

WORKDIR /srv/napkin
COPY mock-agent/ ./mock-agent/
COPY engine/packs_dist/ ./engine/packs_dist/
COPY --from=host /src/target/release/napkin-web /usr/local/bin/napkin-web
COPY --from=host /seed/ /srv/napkin/seed/
COPY --from=shell /src/app/dist/ /srv/napkin/dist/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV NAPKIN_BACKEND=api \
    NAPKIN_WEB_DATA=/data \
    NAPKIN_WEB_STATIC=/srv/napkin/dist \
    NAPKIN_WEB_SEED=/srv/napkin/seed \
    NAPKIN_AGENT_URL=http://127.0.0.1:8787 \
    PORT=8080
VOLUME ["/data"]
EXPOSE 8080

# tini reaps the agent when it exits and forwards signals to both processes.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
