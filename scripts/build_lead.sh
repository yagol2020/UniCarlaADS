#!/usr/bin/env bash
set -euo pipefail

# 使用 LEAD 自带的 0.9.16 agents、Leaderboard 和默认 seed0 检查点。
docker build \
    --file docker/lead/Dockerfile \
    --tag unicarlaads-lead:latest \
    .
