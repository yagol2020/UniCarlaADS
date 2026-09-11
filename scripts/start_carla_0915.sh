#!/usr/bin/env bash
set -euo pipefail

# CARLA 的 world、ego 和运行阶段 tick 均由外部控制程序管理。
docker run -d \
    --name unicarlaads-carla-0915 \
    --network host \
    --gpus all \
    --privileged \
    --shm-size=2g \
    --env DISPLAY= \
    carlasim/carla:0.9.15 \
    ./CarlaUE4.sh \
    --world-port=2000 \
    -RenderOffScreen \
    -nosound \
    -graphicsadapter=0
