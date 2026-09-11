#!/usr/bin/env bash
set -euo pipefail

# 下载 LEAD 默认的 seed0 检查点到 lead/checkpoints/resnet34_v1.5.0/seed0。
# 检查点不在 git 仓库中，固定使用 LEAD v1.5.0 CI 相同的 Hugging Face revision。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${ROOT}/lead/checkpoints"
REVISION="ce8185f03ce3448e22f2d4fb36c572ad8f9636bd"

if [[ -s "${TARGET}/resnet34_v1.5.0/seed0/model_0030.pth" ]]; then
    echo "检查点已存在: ${TARGET}/resnet34_v1.5.0/seed0"
    exit 0
fi

python -m pip install --no-cache-dir "huggingface-hub[hf_xet]==0.36.2"

hf download ln2697/transfuser-carla-123d \
    --revision "${REVISION}" \
    --include "resnet34_v1.5.0/seed0/*" \
    --local-dir "${TARGET}"

echo "检查点已保存到 ${TARGET}/resnet34_v1.5.0/seed0"
