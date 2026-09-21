#!/usr/bin/env bash
set -euo pipefail

# Autoware 覆盖率镜像：基于 unicarlaads-autoware:latest，用 gcov 插桩重编
# Autoware 感知/预测/规划（含决策）模块，产出 unicarlaads-autoware:coverage。
# 跑完 demo 后用 demo_autoware.py --coverage 采集覆盖率。
#
# 源码准备（二选一）：
#   1. 已有本地源码时指定父目录（需包含 autoware_universe/ 与 autoware_core/）：
#      UNICARLA_AUTOWARE_SRC=/path/to/parent ./scripts/build_autoware_coverage.sh
#   2. 不指定时自动 clone 固定 tag 到 .cache/autoware_coverage/repos/。
#
# 可选：
#   UNICARLA_COVERAGE_PACKAGES="pkgA pkgB" 覆盖默认插桩包列表（默认 = 两个仓库
#                                          planning/ 与 perception/ 下全部包）；
#   UNICARLA_COVERAGE_JOBS=4               覆盖编译并行度（默认 4，防内存打满）。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE_DIR="${UNICARLA_COVERAGE_CACHE:-$ROOT/.cache/autoware_coverage}"
REPOS_DIR="${UNICARLA_AUTOWARE_SRC:-$CACHE_DIR/repos}"
STAGE_DIR="$CACHE_DIR/context"
UNIVERSE_TAG="${UNICARLA_AUTOWARE_UNIVERSE_TAG:-0.51.0}"
CORE_TAG="${UNICARLA_AUTOWARE_CORE_TAG:-1.8.0}"
JOBS="${UNICARLA_COVERAGE_JOBS:-4}"

# 默认插桩 planning/ 与 perception/ 下的全部包（含预测、决策模块），
# 可用 UNICARLA_COVERAGE_PACKAGES 覆盖成更小的列表。
scan_packages() {
    find "$REPOS_DIR/autoware_universe/planning" \
         "$REPOS_DIR/autoware_universe/perception" \
         "$REPOS_DIR/autoware_core/planning" \
         "$REPOS_DIR/autoware_core/perception" \
         -name package.xml 2>/dev/null |
        xargs -r sed -n 's:.*<name>\([^<]*\)</name>.*:\1:p' |
        sort -u
}
if ! docker image inspect unicarlaads-autoware:latest >/dev/null 2>&1; then
    echo "缺少基础镜像 unicarlaads-autoware:latest，请先运行 ./scripts/build_autoware.sh" >&2
    exit 1
fi

mkdir -p "$REPOS_DIR"
REPOS_DIR="$(realpath "$REPOS_DIR")"
if [[ ! -d "$REPOS_DIR/autoware_universe" ]]; then
    echo "[coverage] clone autoware_universe ${UNIVERSE_TAG} -> $REPOS_DIR"
    git clone --depth 1 --branch "$UNIVERSE_TAG" \
        https://github.com/autowarefoundation/autoware_universe.git \
        "$REPOS_DIR/autoware_universe"
fi
if [[ ! -d "$REPOS_DIR/autoware_core" ]]; then
    echo "[coverage] clone autoware_core ${CORE_TAG} -> $REPOS_DIR"
    git clone --depth 1 --branch "$CORE_TAG" \
        https://github.com/autowarefoundation/autoware_core.git \
        "$REPOS_DIR/autoware_core"
fi

if [[ -n "${UNICARLA_COVERAGE_PACKAGES:-}" ]]; then
    PACKAGES="$UNICARLA_COVERAGE_PACKAGES"
else
    PACKAGES="$(scan_packages | tr '\n' ' ')"
fi
if [[ -z "${PACKAGES// /}" ]]; then
    echo "未找到任何待插桩的包，请检查源码目录或设置 UNICARLA_COVERAGE_PACKAGES" >&2
    exit 1
fi
echo "[coverage] 待插桩包数: $(echo "$PACKAGES" | wc -w)"

# 只把两个仓库（去掉 .git）传给 BuildKit：指定大目录作源码父目录时
# 也避免整个目录被传输成构建上下文。
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
cp -a "$REPOS_DIR/autoware_universe" "$STAGE_DIR/"
cp -a "$REPOS_DIR/autoware_core" "$STAGE_DIR/"
rm -rf "$STAGE_DIR/autoware_universe/.git" "$STAGE_DIR/autoware_core/.git"

# 主上下文用 docker/autoware（只含 Dockerfile），源码与 worker 通过命名上下文传入。
docker build \
    --file "$ROOT/docker/autoware/Dockerfile.coverage" \
    --tag unicarlaads-autoware:coverage \
    --build-context "autoware_src=$STAGE_DIR" \
    --build-context "worker_src=$ROOT/unicarla_ads" \
    --build-arg "COVERAGE_PACKAGES=$PACKAGES" \
    --build-arg "COVERAGE_BUILD_JOBS=$JOBS" \
    "$@" \
    "$ROOT/docker/autoware"
