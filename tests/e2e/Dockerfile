FROM python:3.14-slim

# System deps: curl for install scripts, gnupg for Chrome repo signing,
# procps for pkill/pgrep used in test teardown
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg procps \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome from Google's apt repo
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# uv (Astral install script)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv

# scrut (official install script, latest version)
RUN curl --proto '=https' --tlsv1.2 -sSf \
    https://facebookincubator.github.io/scrut/install.sh \
    | sh -s -- -p /usr/local/bin

# Non-root user for Chrome
RUN useradd -m -s /bin/bash odda \
    && mkdir -p /app && chown odda:odda /app

WORKDIR /app

# Deps layer: copy manifest + lockfile, create venv (cached when only source changes)
COPY --chown=odda:odda pyproject.toml uv.lock ./
USER odda
RUN uv venv

# Source layer: copy source + tests, install the project
COPY --chown=odda:odda src/ ./src/
COPY --chown=odda:odda tests/ ./tests/
COPY --chown=odda:odda README.md ./
RUN uv pip install --no-cache -e ".[dev]"

ENV PATH="/app/.venv/bin:${PATH}"

CMD ["scrut", "test", "tests/e2e/scrut/"]