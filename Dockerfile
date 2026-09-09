# syntax=docker/dockerfile:1
#
# Napkin Studio OS as one container: the axum host and the shell. One process,
# no Python, and no API key.
#
# Inference is the visitor's own: they paste an Anthropic key, it stays in their
# browser, and their browser calls Claude directly. The host assembles the
# prompt — schema, digests, provenance, split for caching — and hands it over.
# So this image costs whoever runs it nothing per draft, and holds nobody's
# credentials.
#
#   docker build -t napkin .
#   docker run -p 8080:8080 -v napkin:/data napkin
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
      ca-certificates tini \
 && if [ "$WITH_PDF" = "1" ]; then \
      apt-get install -y --no-install-recommends chromium; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# The knowledge digests are compiled into the binary, so there is nothing to
# copy but the binary, the shell, and one seed template.
WORKDIR /srv/napkin
COPY --from=host /src/target/release/napkin-web /usr/local/bin/napkin-web
COPY --from=host /seed/ /srv/napkin/seed/
COPY --from=shell /src/app/dist/ /srv/napkin/dist/

ENV NAPKIN_WEB_DATA=/data \
    NAPKIN_WEB_STATIC=/srv/napkin/dist \
    NAPKIN_WEB_SEED=/srv/napkin/seed \
    PORT=8080
VOLUME ["/data"]
EXPOSE 8080

# tini for signal handling; one process, nothing to supervise.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/napkin-web"]
