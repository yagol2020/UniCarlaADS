# UniCarlaADS

一个基于 Docker 的统一自动驾驶系统 CARLA 运行框架。当前支持 InterFuser 和 LEAD。

UniCarlaADS 不推进仿真场景：外部程序负责创建 ego、调用 `world.tick()` 并应用控制信号。ADS 容器负责加载模型、部署传感器，并根据外部产生的 frame 返回控制量。

| ADS | CARLA | ego 车辆 |
| --- | --- | --- |
| InterFuser | 0.9.10.1 | `vehicle.lincoln.mkz2017` |
| LEAD | 0.9.15 | `vehicle.lincoln.mkz_2020` |

InterFuser 镜像内使用仓库中的 `agents09101`，其 CARLA Python API 则从官方 `carlasim/carla:0.9.10.1` 镜像取得。

## InterFuser

构建 ADS 镜像：

```bash
./scripts/build_interfuser.sh
```

启动官方 CARLA 0.9.10.1 镜像：

```bash
./scripts/start_carla_0910.sh
```

该脚本使用 `DISPLAY=`、`SDL_VIDEODRIVER=offscreen` 和 `-opengl`，可在无头服务器上运行。启动时出现 `xdg-user-dir: not found` 是该官方镜像的提示，不影响 CARLA 服务。

外部程序先连接 CARLA，将 world 设置为同步模式、设置固定步长（InterFuser 默认使用 `0.05` 秒）并创建 ego，然后调用：

```python
from service import ADS

settings = world.get_settings()
settings.synchronous_mode = True
settings.fixed_delta_seconds = 0.05
world.apply_settings(settings)

ads = ADS("interfuser")
ads.init()
deploy_result = ads.deploy(
    ego_actor_id=ego.id,
    route=[
        {"x": 5.7, "y": 91.5, "z": 0.0},
        {"x": 35.0, "y": 69.2, "z": 0.0},
    ],
)

try:
    while True:
        frame_id = world.tick()
        result = ads.step(frame_id)
        control = result["control"]
        ego.apply_control(
            carla.VehicleControl(
                steer=control["steer"],
                throttle=control["throttle"],
                brake=control["brake"],
            )
        )
finally:
    video_path = ads.download_gui()
    print("GUI 视频已下载到 {}".format(video_path))
    ads.close()
```

`download_gui()` 会将仿真期间保存在容器内存中的 ADS GUI 帧编码为 MP4。默认保存到宿主机的 `video_download` 目录，也可以通过 `output_dir`、`filename` 和 `fps` 指定输出位置、文件名和帧率。该接口需要在 `close()` 前调用。

`deploy()` 内部会调用 InterFuser 原有的传感器包装器，部署阶段会产生一次 tick。`deploy()` 返回值中的 `setup_frame` 就是该帧。之后的场景推进全部由外部程序控制。

## LEAD

LEAD 使用独立镜像，并复用其仓库自带的标准 Leaderboard、ScenarioRunner 和默认 seed0 检查点。容器内使用 CARLA 0.9.15 Python API（LEAD 上游说明仅仿真运行时可用 0.9.15）。集成代码不会修改 `lead/` 源码。

构建 LEAD 镜像：

```bash
./scripts/build_lead.sh
```

首次构建需要下载 LEAD 对应的 PyTorch 2.8 CUDA 12.8 基础镜像，体积较大。启动官方 CARLA 0.9.15 镜像：

```bash
./scripts/start_carla_0915.sh
```

宿主侧示例需要 CARLA 0.9.15 Python API，可在 Python 3.7 至 3.10 环境安装：

```bash
python3.8 -m pip install carla==0.9.15
python3.8 demo_lead.py
```

LEAD 要求 world 使用同步模式和 `fixed_delta_seconds=0.05`，ego 的 `role_name` 必须为 `hero`。`deploy()` 使用 LEAD 原有传感器包装器预热传感器，因此部署阶段会产生 10 个 tick；`setup_frame` 是预热后的帧。进入运行阶段后，只有外部程序调用 `world.tick()`，LEAD 的 `step(frame_id)` 只读取该帧并返回控制信号。

`deploy()` 会同时打开 LEAD 自带的评估录制，视频内容为演示视角与模型输入拼接的 grid 视频。在 `close()` 前调用 `ads.download_gui()` 可结束视频编码，并把视频下载到宿主机的 `video_download` 目录，`demo_lead.py` 已经包含这一步。
