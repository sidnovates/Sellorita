#!/bin/sh
# Renders the sample Reel scene the same way the ReelProductionAgent will:
# copy the scene into a per-job scratch dir, add assets, render, inspect output.
set -eu

JOB_DIR=/tmp/spike-job
OUT_DIR=${OUT_DIR:-/out}
rm -rf "$JOB_DIR" && mkdir -p "$JOB_DIR" "$OUT_DIR"
cp -r /work/scene/. "$JOB_DIR/"
cp /opt/vendor/gsap.min.js "$JOB_DIR/gsap.min.js"

# Stand-in "product photo" (the real agent downloads it from MinIO via presigned URL)
ffmpeg -loglevel error -f lavfi -i testsrc2=size=800x800 -frames:v 1 "$JOB_DIR/product.jpg"

echo "== versions"
node --version; ffmpeg -version | head -1; hyperframes --version

echo "== doctor"
hyperframes doctor || true

echo "== lint"
cd "$JOB_DIR"
hyperframes lint || true

echo "== render"
START=$(date +%s)
hyperframes render -c index.html -o "$OUT_DIR/spike_reel.mp4" --fps 30 --quality standard
END=$(date +%s)
echo "render_seconds=$((END - START))"

echo "== output"
ls -la "$OUT_DIR/spike_reel.mp4"
ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,nb_frames \
        -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT_DIR/spike_reel.mp4"
# Grab frames at 1s and 4s to check the animation actually moved
ffmpeg -loglevel error -y -ss 1 -i "$OUT_DIR/spike_reel.mp4" -frames:v 1 "$OUT_DIR/frame_1s.png"
ffmpeg -loglevel error -y -ss 4 -i "$OUT_DIR/spike_reel.mp4" -frames:v 1 "$OUT_DIR/frame_4s.png"
echo "SPIKE_OK"
