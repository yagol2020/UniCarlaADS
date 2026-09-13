# Autoware overlay

本目录存放需要修改、且必须编译进镜像的上游文件。Dockerfile 会在编译前
把它们覆盖到 `autoware_carla_launch` 的对应路径，保证 submodule 本身不被修改。

对应版本：

- `autoware_carla_launch` commit `86628e9`
- `external/zenoh_carla_bridge` commit `7975212`

## 修改内容

### `carla_prebuild/patch_carla_sys.py`

编译期辅助脚本。carla-sys 默认从 GitHub 下载 libcarla 预编译包，网络受限时可
用 `UNICARLA_CARLA_PREBUILD` 指向一个包含
`libcarla_client.0.9.16-x86_64-unknown-linux-gnu.tar.zstd` 的目录，Dockerfile
会把该脚本和预编译包挂载进构建阶段并替换下载逻辑。

### `zenoh_carla_bridge/src/main.rs`

- 去掉 bridge 自带的定时 tick 线程，并把主循环里的 `wait_for_tick()` 换成
  “轮询 frame 变化”。这样仿真的 tick 完全由 UniCarlaADS 宿主控制。
- 检测到宿主推进一帧后发布 `/clock`。

### `zenoh_carla_bridge/src/bridge/vehicle_bridge.rs`

- `update_carla_control()` 只计算并保存 `VehicleControl`，不再直接
  `apply_control`。
- `actuation_status` 发布的是当前指令控制量，而不是 CARLA actor 的实际控制量。

控制指令由容器内 worker 换算后通过 HTTP 返回，宿主调用 `ego.apply_control()` 执行。
