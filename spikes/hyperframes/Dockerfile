# HyperFrames rendering spike: proves the worker base image can render HTML/GSAP -> MP4
# headless on Linux. Dev B: the RUN blocks below are the install steps to reuse in
# docker/Dockerfile.worker (see docs/hyperframes_spike.md).

# Node 22+ is required by hyperframes; copy it from the official image instead of
# adding a third-party apt repo.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

# 1. Node.js runtime (binary + npm/npx)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# 2. FFmpeg (encoding) + shared libraries headless Chrome needs + fonts for text rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates unzip \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
        libpango-1.0-0 libcairo2 libxshmfence1 libx11-xcb1 libxext6 libxss1 \
        fonts-liberation fonts-noto-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# 3. HyperFrames CLI (pinned) + headless Chrome downloaded at build time, not at render time.
#    `browser ensure` downloads into /root/.cache; move it to /opt so a non-root user can
#    run it, and point the renderer at it explicitly.
ENV HYPERFRAMES_VERSION=0.9.72 \
    HYPERFRAMES_CHROME_SANDBOX=0
RUN npm install -g hyperframes@${HYPERFRAMES_VERSION} \
 && hyperframes browser ensure \
 && mv /root/.cache/hyperframes/chrome /opt/hyperframes-chrome \
 && ln -s "$(find /opt/hyperframes-chrome -type f -name chrome-headless-shell | head -1)" \
          /usr/local/bin/chrome-headless-shell \
 && chrome-headless-shell --version
ENV PRODUCER_HEADLESS_SHELL_PATH=/usr/local/bin/chrome-headless-shell

# 4. Vendor GSAP locally so renders never depend on a CDN being reachable
RUN mkdir -p /opt/vendor && cd /tmp && npm pack gsap@3.12.5 >/dev/null \
 && tar -xzf gsap-3.12.5.tgz && cp package/dist/gsap.min.js /opt/vendor/gsap.min.js \
 && rm -rf /tmp/package /tmp/gsap-*.tgz

WORKDIR /work
COPY scene/ /work/scene/
COPY render.sh /work/render.sh
RUN chmod +x /work/render.sh

# 5. Run as an unprivileged user (K8s securityContext: runAsNonRoot)
RUN useradd --create-home --uid 10001 worker && mkdir -p /out && chown worker /out
USER worker
ENV HOME=/home/worker

CMD ["/work/render.sh"]
