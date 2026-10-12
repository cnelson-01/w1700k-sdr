#!/bin/sh
# Builds the unit images (make images dumper) in Linux, from a non-Linux host with Docker.
# The tree is built inside a Docker volume: Buildroot and the kernel need a case-sensitive
# filesystem, which a macOS bind mount is not. Results land in platform/build/images/ and
# platform/build/dumper/ here.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VOLUME=${W1700K_VOLUME:-w1700k-build}
docker build -q -t w1700k-build "$ROOT/docker" >/dev/null
docker volume create "$VOLUME" >/dev/null
# Sync the committed and working-tree files (not build outputs) into the volume.
(cd "$ROOT" && git ls-files -z --cached --others --exclude-standard | xargs -0 tar -cf - --no-mac-metadata --no-xattrs) |
    docker run --rm -i -v "$VOLUME":/work w1700k-build sh -c 'mkdir -p src && tar -xf - -C src'
docker run --rm -v "$VOLUME":/work -w /work/src w1700k-build make images dumper JOBS="${JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}"
mkdir -p "$ROOT/platform/build"
docker run --rm -v "$VOLUME":/work w1700k-build tar -cf - -C src/platform/build images dumper |
    tar -xf - -C "$ROOT/platform/build"
ls -l "$ROOT/platform/build/images"
