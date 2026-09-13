#!/usr/bin/env bash
set -euo pipefail

# Autoware 全栈镜像，构建较慢，需要 autoware_carla_launch 及其子模块已初始化。
#
# 可选加速：
#   1. 复用已有 Rust 目录（包含 registry/ 与 toolchains/）离线构建：
#      UNICARLA_CARGO_CACHE=/path/to/autoware_carla_launch/rust ./scripts/build_autoware.sh
#   2. 跳过 carla-sys 的 libcarla 联网下载（目录内放
#      libcarla_client.0.9.16-x86_64-unknown-linux-gnu.tar.zstd）：
#      UNICARLA_CARLA_PREBUILD=/path/to/carla-prebuild ./scripts/build_autoware.sh
ARGS=(
    --file docker/autoware/Dockerfile
    --tag unicarlaads-autoware:latest
)

if [[ -n "${UNICARLA_CARGO_CACHE:-}" ]]; then
    ARGS+=(--build-context "cargo_cache=${UNICARLA_CARGO_CACHE}")
    ARGS+=(--build-arg CARGO_OFFLINE=1)
fi

if [[ -n "${UNICARLA_CARLA_PREBUILD:-}" ]]; then
    ARGS+=(--build-context "carla_prebuild=${UNICARLA_CARLA_PREBUILD}")
fi

docker build "${ARGS[@]}" .
