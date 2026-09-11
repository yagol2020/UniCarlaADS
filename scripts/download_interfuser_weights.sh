#!/usr/bin/env bash
set -euo pipefail

# 下载 InterFuser 预训练权重到 InterFuser/leaderboard/team_code/interfuser.pth.tar。
# 权重不在 git 仓库中，默认从 UniCarlaADS 的 GitHub Release 下载。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${ROOT}/InterFuser/leaderboard/team_code/interfuser.pth.tar"

GITHUB_REPO="yagol2020/UniCarlaADS"
GITHUB_TAG="v0.1"
ASSET_NAME="interfuser.pth.tar"
BROWSER_URL="https://github.com/${GITHUB_REPO}/releases/download/${GITHUB_TAG}/${ASSET_NAME}"

if [[ -s "${TARGET}" ]]; then
    echo "权重已存在: ${TARGET}"
    exit 0
fi

CURL_ARGS=(-fL --retry 2 --connect-timeout 15)
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    CURL_ARGS+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

# 权重约 607MB，小于该值说明下载到的是错误页面。
MIN_SIZE=$((100 * 1024 * 1024))

finish() {
    mv "${TARGET}.part" "${TARGET}"
    echo "权重已保存到 ${TARGET}"
    exit 0
}

# 私有仓库不能用 browser_download_url（带 token 也返回 404），改走 API 资产接口。
download_github_release() {
    local asset_url
    asset_url="$(curl "${CURL_ARGS[@]}" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${GITHUB_TAG}" \
        | python3 -c 'import json,sys; print(next(a["url"] for a in json.load(sys.stdin)["assets"] if a["name"]==sys.argv[1]))' "${ASSET_NAME}")" || return 1
    curl "${CURL_ARGS[@]}" -H "Accept: application/octet-stream" \
        -o "${TARGET}.part" "${asset_url}"
}

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "尝试通过 GitHub API 下载 Release 资产"
    if download_github_release && [[ "$(stat -c %s "${TARGET}.part")" -ge "${MIN_SIZE}" ]]; then
        finish
    fi
    rm -f "${TARGET}.part"
fi

URLS=(
    "${INTERFUSER_WEIGHTS_URL:-}"
    "${BROWSER_URL}"
    "http://43.163.208.95/s/jRfM/download"
    "http://43.163.208.95/s/jRfM"
)

for url in "${URLS[@]}"; do
    [[ -z "${url}" ]] && continue
    echo "尝试下载: ${url}"
    if curl "${CURL_ARGS[@]}" -o "${TARGET}.part" "${url}" \
        && [[ "$(stat -c %s "${TARGET}.part")" -ge "${MIN_SIZE}" ]]; then
        finish
    fi
    rm -f "${TARGET}.part"
done

echo "自动下载失败。若仓库为私有，请设置 GITHUB_TOKEN（需 repo 读取权限）后重试；"
echo "也可手动下载 InterFuser 预训练权重（见 InterFuser/README.md 的 Pretrain weights 一节）"
echo "放到 ${TARGET}，或用 INTERFUSER_WEIGHTS_URL 指定其他镜像。"
exit 1
