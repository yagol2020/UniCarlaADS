#!/usr/bin/env bash
set -euo pipefail

# 使用仓库根目录作为构建上下文，InterFuser 源码不会被修改。
docker build \
    --file docker/interfuser/Dockerfile \
    --tag unicarlaads-interfuser:latest \
    .
