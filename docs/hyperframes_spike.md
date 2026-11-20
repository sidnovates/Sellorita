# HyperFrames Spike Report (Day 1, Dev A)

**Result: ✅ PASS.** HyperFrames renders LLM-style HTML/CSS/GSAP scenes to `1080x1920` H.264 MP4 headless inside a Linux container. It also works as a non-root user, with no network access, and under CPU/memory limits. **The Playwright + FFmpeg fallback is not needed.**

Reproduce: `docker build -t sellorita-hf-spike spikes/hyperframes && docker run --rm --shm-size=512m -v <dir>:/out sellorita-hf-spike`

---

## 1. What HyperFrames is

* Open-source (Apache-2.0) HTML-to-video renderer by HeyGen: npm package **`hyperframes`** (CLI), tested at **v0.9.72**.
* Internally: Puppeteer drives **chrome-headless-shell**, seeks the GSAP timeline frame by frame, and pipes frames to **FFmpeg**. Same input gives the same output (deterministic).
* Requirements: **Node.js ≥ 22**, **FFmpeg/ffprobe** on `PATH`, and Chrome's shared libraries. Chrome itself is downloaded by `hyperframes browser ensure`.
* The "HyperFrames Claude Skill" is guidance for coding agents. **Runtime only needs the CLI.**
* `hyperframes render --docker` (its own Docker mode) is **not** used. We render in "local mode" inside our own worker image, because Docker-in-Docker doesn't work in K8s pods.

## 2. Measured results

| Test | Conditions | Render time (6 s, 180 frames) | Output |
| --- | --- | --- | --- |
| Run 1 | root, network on, all 22 CPUs, 64 MB shm | **10.1 s** (5 parallel capture workers) | h264, 1080x1920, 30 fps, 610 KB |
| Run 2 | **non-root uid 10001, `--network none`, `--cpus=2 --memory=2g`, `--shm-size=512m`** | **19.2 s** (1 worker, screenshot capture) | identical spec |

* Frame checks: at 1 s the hook is visible and the product image is scaling in; at 4 s the offer + CTA are visible. The GSAP timeline is seeked correctly.
* Rule of thumb on a **2-CPU pod: ~3 s of render per 1 s of video**, so a 15 s Reel takes about 50 s. HPA/KEDA sizing should assume roughly one concurrent render per 2 CPUs.
* **Image size: 1.81 GB.** Pod startup during KEDA scale-up includes the image pull, so pre-pull or use `imagePullPolicy: IfNotPresent`.

## 3. Install steps for `docker/Dockerfile.worker` (Dev B)

Copy from [`spikes/hyperframes/Dockerfile`](../spikes/hyperframes/Dockerfile). The essential blocks:

```dockerfile
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm

# Node 22 from the official image (no third-party apt repo)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# FFmpeg + unzip + Chrome shared libs + fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates unzip \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
        libpango-1.0-0 libcairo2 libxshmfence1 libx11-xcb1 libxext6 libxss1 \
        fonts-liberation fonts-noto-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# HyperFrames (pinned) + Chrome at BUILD time, moved to /opt for non-root use
ENV HYPERFRAMES_VERSION=0.9.72 HYPERFRAMES_CHROME_SANDBOX=0
RUN npm install -g hyperframes@${HYPERFRAMES_VERSION} \
 && hyperframes browser ensure \
 && mv /root/.cache/hyperframes/chrome /opt/hyperframes-chrome \
 && ln -s "$(find /opt/hyperframes-chrome -type f -name chrome-headless-shell | head -1)" \
          /usr/local/bin/chrome-headless-shell
ENV PRODUCER_HEADLESS_SHELL_PATH=/usr/local/bin/chrome-headless-shell

# GSAP vendored locally (renders must not depend on a CDN)
RUN mkdir -p /opt/vendor && cd /tmp && npm pack gsap@3.12.5 \
 && tar -xzf gsap-3.12.5.tgz && cp package/dist/gsap.min.js /opt/vendor/ && rm -rf package gsap-*.tgz

# ... then: install libs/common + worker requirements, copy worker code ...

RUN useradd --create-home --uid 10001 worker
USER worker
ENV HOME=/home/worker
```

### Required runtime settings

| Setting | Why | Docker Compose | Kubernetes |
| --- | --- | --- | --- |
| `/dev/shm` ≥ 256 MB (use 512 MB) | Chrome keeps frame buffers in shared memory; the 64 MB Docker default is flagged by `hyperframes doctor` and causes crashes on bigger scenes | `shm_size: "512m"` | `emptyDir: {medium: Memory, sizeLimit: 512Mi}` mounted at `/dev/shm` |
| `HYPERFRAMES_CHROME_SANDBOX=0` | Chrome's sandbox needs privileges containers don't have; HyperFrames already disables it in Docker/K8s, this makes it explicit | env | env |
| Writable `/tmp` | Per-job scratch dir + HyperFrames frames cache | default | `emptyDir` at `/tmp` if `readOnlyRootFilesystem: true` |
| CPU/memory | ~2 CPU / 2 GiB handles one render comfortably | `cpus: 2`, `mem_limit: 2g` | `requests: {cpu: "1", memory: 1Gi}`, `limits: {cpu: "2", memory: 2Gi}` |
| Non-root | Security best practice | `user: "10001"` | `securityContext: {runAsNonRoot: true, runAsUser: 10001}` |

No network access is needed at render time. Chrome and GSAP are baked into the image, and product photos are fetched by Python before rendering.

## 4. How the worker will call it (Dev A, Step 5)

The composition contract the LLM must follow (validated in the spike scene [`spikes/hyperframes/scene/index.html`](../spikes/hyperframes/scene/index.html)):

* Root element has `data-composition-id`, `data-width="1080"`, `data-height="1920"`, `data-duration`.
* Timed elements have class `clip` plus `data-start`, `data-duration` and `data-track-index`.
* A **paused** GSAP timeline registered as `window.__timelines[<composition-id>] = tl`.
* Assets referenced by **relative path** (`product.jpg`, `gsap.min.js`) from the job directory.

Per-job flow inside `ReelProductionAgent`:

1. `mkdir /tmp/{campaign_id}`, write the LLM's `index.html`, copy `/opt/vendor/gsap.min.js`, download product images from MinIO into the folder.
2. `hyperframes lint`: catches broken compositions **before** spending render time. Its errors become `qa_feedback` for the retry loop. `render --strict` blocks on lint errors.
3. `subprocess.run(["hyperframes", "render", "-c", "index.html", "-o", out, "--fps", "30", "--quality", "standard"], cwd=job_dir, timeout=180)`.
4. Validate with `ffprobe` (codec, 1080x1920, duration > 0), upload to MinIO, `rm -rf /tmp/{campaign_id}`.

## 5. Notes and caveats

* HyperFrames prints its own log lines (`[INFO] [Render:trace] {...}`, `@hf-progress {...}`) to stdout. The worker should **capture** the subprocess output and log a summary through structlog, not pass it through raw, so ELK keeps one JSON format.
* `hyperframes doctor` flags Docker, whisper-cpp, Kokoro TTS and MusicGen as missing. These are optional features we don't use and can be ignored.
* Lint warns `timeline_track_too_dense` above ~4 timed elements per file. It's only a warning and can be ignored for our short Reels.
* With `PRODUCER_HEADLESS_SHELL_PATH` set and 2 CPUs, the renderer chose single-worker "screenshot" capture instead of parallel "beginframe" capture. That's fine for correctness, and faster with more CPUs.
* Pin both `hyperframes` and `gsap` versions. HyperFrames releases often (0.9.x), and an unpinned upgrade could change output between CI builds.
