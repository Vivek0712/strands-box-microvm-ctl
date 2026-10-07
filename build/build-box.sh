#!/bin/sh
# Build Strands Box for aarch64 Linux, natively, in the same distribution as the Lambda MicroVMs base
# image, so the binaries link against the glibc the VM has. Box publishes no Linux release yet.
#
#   ./build/build-box.sh [BOX_REF]      # default: main
#
# Needs Docker on an arm64 host (Apple silicon or Graviton). Writes image/box-core/{box,
# strands-box-sock-alias,strands-box-contain-trampoline}. The three ship together: the box resolves the
# trampoline from beside its own executable and refuses to start without it.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
ref=${1:-main}
src=${BOX_SRC:-$here/build/box-src}
[ -d "$src/.git" ] || git clone -q https://github.com/strands-agents/box.git "$src"
git -C "$src" fetch -q origin && git -C "$src" checkout -q "$ref"
mkdir -p "$here/image/box-core"
docker volume create box-target >/dev/null
docker run --rm --platform linux/arm64 -v "$src:/src" -v box-target:/target -v "$here/image/box-core:/out" \
  amazonlinux:2023 bash -euc '
    dnf install -y -q gcc gcc-c++ make git tar gzip clang perl cmake binutils >/dev/null
    curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none >/dev/null
    . "$HOME/.cargo/env"
    cd /src
    CARGO_TARGET_DIR=/target cargo build --release --locked --all-features -p strands-box -p strands-box-containment
    cp /target/release/strands-box /out/box
    cp /target/release/strands-box-sock-alias /target/release/strands-box-contain-trampoline /out/
    strip /out/box /out/strands-box-sock-alias /out/strands-box-contain-trampoline
    chmod 0755 /out/*
    /out/box --version'
