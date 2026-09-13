# Autoware 全栈 ADS 镜像：包含 Autoware、zenoh bridge 与 UniCarlaADS worker。
# 代码均来自 submodule，编译前用 autoware_overlay/ 中的文件动态替换。
#
# 可选：通过 --build-context cargo_cache=<已构建过的 rust 目录> 复用 Rust
# 离线缓存（registry + toolchains），显著减少构建时联网。
FROM scratch AS cargo_cache

# 可选：通过 --build-context carla_prebuild=<含 libcarla 预编译包的目录>
# 跳过 carla-sys 构建时的联网下载（见 autoware_overlay/carla_prebuild/）。
FROM scratch AS carla_prebuild

FROM ghcr.io/autowarefoundation/autoware:universe-devel-cuda-jazzy-1.8.0

ENV DEBIAN_FRONTEND=noninteractive

# 与上游 Dockerfile_autoware 对齐的依赖（基础镜像已安装时自动跳过）。
RUN if ! dpkg -s ca-certificates chrony curl git libclang-dev llvm-dev cmake \
        python3-pip ros-jazzy-moveit-msgs ros-jazzy-object-recognition-msgs \
        ros-jazzy-octomap-msgs ros-jazzy-rmw-zenoh-cpp ros-jazzy-rviz2 \
        ros-jazzy-topic-tools ros-jazzy-xacro unzip wget >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends \
            ca-certificates \
            chrony \
            curl \
            git \
            libclang-dev \
            llvm-dev \
            cmake \
            python3-pip \
            ros-jazzy-moveit-msgs \
            ros-jazzy-object-recognition-msgs \
            ros-jazzy-octomap-msgs \
            ros-jazzy-rmw-zenoh-cpp \
            ros-jazzy-rviz2 \
            ros-jazzy-topic-tools \
            ros-jazzy-xacro \
            unzip \
            wget \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# carla-sys（autocxx 0.26）需要 clang-12，从 jammy 仓库取。
RUN if ! dpkg -s clang-12 libclang-12-dev >/dev/null 2>&1; then \
        echo "deb [trusted=yes] http://archive.ubuntu.com/ubuntu jammy universe" \
            > /etc/apt/sources.list.d/jammy.list \
        && echo "deb [trusted=yes] http://archive.ubuntu.com/ubuntu jammy-updates universe" \
            >> /etc/apt/sources.list.d/jammy.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends clang-12 libclang-12-dev \
        && rm /etc/apt/sources.list.d/jammy.list \
        && apt-get update \
        && rm -rf /var/lib/apt/lists/*; \
    fi

ENV RUSTUP_HOME=/opt/rust \
    CARGO_HOME=/opt/rust \
    PATH=/opt/rust/bin:${PATH} \
    LLVM_CONFIG_PATH=/usr/bin/llvm-config-12 \
    LIBCLANG_PATH=/usr/lib/llvm-12/lib \
    LIBCLANG_STATIC_PATH=/usr/lib/llvm-12/lib \
    CLANG_PATH=/usr/bin/clang-12

# Autoware 运行所需环境与上游 env.sh 保持一致。
ENV AUTOWARE_CARLA_ROOT=/opt/autoware_carla_launch \
    CARLA_MAP_NAME=Town01 \
    CARLA_MAP_PATH=/opt/autoware_carla_launch/carla_map/Town01 \
    CARLA_SIMULATOR_IP=127.0.0.1 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST \
    ROS_DOMAIN_ID=0 \
    CYCLONEDDS_URI="<CycloneDDS><Domain><Discovery><ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>1000</MaxAutoParticipantIndex></Discovery></Domain></CycloneDDS>" \
    VEHICLE_NAME=v1 \
    LIDAR_DETECTION_MODEL=centerpoint \
    CENTERPOINT_MODEL_NAME=centerpoint_tiny \
    PYTHONPATH=/opt/unicarlaads

WORKDIR /opt/autoware_carla_launch

# 复制上游源码（submodule）与 overlay 修改，并只替换需要编译的文件。
COPY autoware_carla_launch /opt/autoware_carla_launch
COPY autoware_overlay /opt/unicarla_overlay
RUN cp /opt/unicarla_overlay/zenoh_carla_bridge/src/main.rs \
        /opt/autoware_carla_launch/external/zenoh_carla_bridge/src/main.rs \
    && cp /opt/unicarla_overlay/zenoh_carla_bridge/src/bridge/vehicle_bridge.rs \
        /opt/autoware_carla_launch/external/zenoh_carla_bridge/src/bridge/vehicle_bridge.rs

# 安装 Rust 并编译两个 zenoh 组件。
# CARGO_OFFLINE=1 且提供 cargo_cache 时使用挂载的 registry/toolchains，全程离线；
# 提供 carla_prebuild 时用本地 libcarla 包替代 carla-sys 的联网下载。
ARG CARGO_OFFLINE=0
RUN --mount=type=bind,from=cargo_cache,source=.,target=/mnt/cargo-cache \
    --mount=type=bind,from=carla_prebuild,source=.,target=/mnt/carla-prebuild \
    set -e; \
    mkdir -p /opt/rust; \
    if [ -d /mnt/cargo-cache/toolchains ]; then \
        cp -a /mnt/cargo-cache/. /opt/rust/; \
    else \
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --default-toolchain stable --profile minimal; \
    fi; \
    if [ -f /mnt/carla-prebuild/libcarla_client.0.9.16-x86_64-unknown-linux-gnu.tar.zstd ]; then \
        CS_BUILD_RS=$(ls /opt/rust/registry/src/*/carla-sys-0.13.0/build.rs); \
        python3 /opt/unicarla_overlay/carla_prebuild/patch_carla_sys.py "$CS_BUILD_RS"; \
    fi; \
    if [ "${CARGO_OFFLINE}" = "1" ]; then export CARGO_NET_OFFLINE=true; fi; \
    cd /opt/autoware_carla_launch/external/zenoh_carla_bridge; \
    cargo +stable build --release; \
    rm -rf target/release/build target/release/deps \
           target/release/.fingerprint target/release/incremental; \
    cd /opt/autoware_carla_launch/external/zenoh-plugin-ros2dds; \
    cargo +stable build --release -p zenoh-bridge-ros2dds; \
    rm -rf target/release/build target/release/deps \
           target/release/.fingerprint target/release/incremental; \
    rm -rf /opt/rust

# 编译本仓库的 ROS 包（Autoware 主程序来自镜像内的 /opt/autoware）。
RUN bash -lc "source /opt/autoware/setup.bash \
    && colcon build --symlink-install --base-paths src \
       --cmake-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"

# 容器内 worker 需要 CARLA Python API 创建传感器，OpenCV 压缩/编码视频帧。
RUN python3 -m pip install --no-cache-dir --break-system-packages \
    "carla==0.9.16" \
    "numpy>=1.26,<2" \
    "opencv-python-headless==4.10.0.84"

COPY unicarla_ads /opt/unicarlaads/unicarla_ads

ENV UNICARLA_ADS_NAME=autoware \
    UNICARLA_ADS_PORT=8080

# entrypoint 会 source /opt/autoware/setup.bash 并覆盖 PYTHONPATH，这里显式补回。
CMD ["bash", "-lc", "source /opt/autoware/setup.bash && export PYTHONPATH=/opt/unicarlaads:${PYTHONPATH} && exec python3 -m unicarla_ads.worker.server"]
