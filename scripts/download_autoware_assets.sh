#!/usr/bin/env bash
set -euo pipefail

# 下载 Autoware 需要的 Town01 地图与模型权重到 submodule 目录。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}/autoware_carla_launch"

if ! command -v gdown >/dev/null 2>&1; then
    python3 -m pip install --user gdown
fi

./script/setup/download_map.sh
./script/setup/download_models.sh

echo "地图与模型已下载到 ${ROOT}/autoware_carla_launch"
echo "首次仿真会在容器内自动编译 TensorRT 引擎；"
echo "如希望提前预编译，可进入 unicarlaads-autoware 容器执行 script/setup/build_models.sh。"
