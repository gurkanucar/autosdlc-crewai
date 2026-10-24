# Toolchains for generated projects. All stages share Debian bookworm so the copied binaries find the same glibc.
FROM node:22-bookworm-slim AS node
FROM golang:1.24-bookworm AS go
FROM rust:1-slim-bookworm AS rust
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    AUTOSDLC_WORKSPACE=/data \
    GOPATH=/data/.go \
    GOCACHE=/data/.cache/go-build \
    NPM_CONFIG_CACHE=/data/.npm \
    PNPM_HOME=/data/.pnpm \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/data/.cargo \
    PATH=/usr/local/go/bin:/usr/local/cargo/bin:/data/.go/bin:/data/.pnpm:$PATH

# git and a C compiler: Go modules fetched from git, native npm addons and cgo (e.g. go-sqlite3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js, npm and pnpm. Only node and npm are copied: /usr/local also holds Python in this image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g pnpm

COPY --from=go /usr/local/go /usr/local/go

# Rust toolchain (rustup proxies + toolchain). CARGO_HOME points into /data so the non-root user can cache crates there.
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup

WORKDIR /app

# Dependencies first so code changes do not invalidate this layer.
COPY pyproject.toml ./
COPY src ./src
RUN pip install . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY streamlit_app.py ./
COPY .streamlit ./.streamlit

# projects/ and runs/ live under the workspace; mount a volume on /data to keep them.
# /ms-playwright is writable because a generated Node project may install its own Playwright browser build.
RUN useradd --create-home --uid 1000 autosdlc \
    && mkdir -p /data /data/.tmp \
    && chown -R autosdlc:autosdlc /data /app /ms-playwright
USER autosdlc
ENV TMPDIR=/data/.tmp

VOLUME /data
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "streamlit_app.py", \
     "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true", \
     "--server.showEmailPrompt", "false", "--browser.gatherUsageStats", "false"]
