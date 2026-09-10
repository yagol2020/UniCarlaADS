#!/usr/bin/env bash
set -euo pipefail

# CARLA 的 world、ego 和运行阶段 tick 均由外部控制程序管理。
docker run -d \
    --name unicarlaads-carla-0916 \
    --network host \
    --gpus all \
    --privileged \
    --shm-size=2g \
    --env DISPLAY= \
    --env SDL_VIDEODRIVER=offscreen \
    carlasim/carla:0.9.16 \
    ./CarlaUE4.sh \
    --world-port=2000 \
    -RenderOffScreen \
    -nosound \
    -graphicsadapter=0
