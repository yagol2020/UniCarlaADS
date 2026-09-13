#!/usr/bin/env bash
set -euo pipefail

# CARLA 的 world、ego 和 tick 均由外部控制程序管理。
docker run -d \
    --name unicarlaads-carla-0916 \
    --network host \
    --runtime=nvidia \
    --gpus all \
    --privileged \
    --shm-size=2g \
    --env DISPLAY= \
    carlasim/carla:0.9.16 \
    ./CarlaUE4.sh \
    --world-port=2000 \
    -RenderOffScreen \
    -nosound \
    -graphicsadapter=0
