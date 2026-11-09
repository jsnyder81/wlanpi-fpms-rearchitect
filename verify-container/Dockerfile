FROM debian:trixie

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        nginx \
        python3 \
        openssl \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*
