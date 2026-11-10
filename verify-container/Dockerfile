FROM debian:trixie

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        nginx \
        python3 \
        openssl \
        ca-certificates \
        curl \
        procps \
        sudo \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Browser-driven menu e2e (drive_menu_ui.mjs): Playwright with real headless
# Chromium, plus axe-core for the accessibility pass. Installed in the image
# because the workspace is mounted read-only at /src.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    E2E_MODULES=/opt/e2e/
WORKDIR /opt/e2e
RUN npm init -y >/dev/null && \
    npm install --no-audit --no-fund --save-exact playwright@1.65.0 @axe-core/playwright@4.14.0 && \
    DEBIAN_FRONTEND=noninteractive npx playwright install --with-deps --only-shell chromium && \
    rm -rf /var/lib/apt/lists/* /root/.npm
WORKDIR /
