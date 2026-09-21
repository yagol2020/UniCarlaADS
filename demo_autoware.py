"""随机选择起终点并运行一次 Autoware 仿真。"""

import argparse
import json
import math
import os
import random
import time
from pathlib import Path

try:
    import carla
except ImportError as exc:
    raise RuntimeError(
        "未找到 CARLA Python API，请在 Python 3.10-3.12 环境安装 carla==0.9.16"
    ) from exc

from service import ADS

FIXED_DELTA_SECONDS = 0.05
MIN_ROUTE_DISTANCE = 20.0
MAX_ROUTE_DISTANCE = 80.0
ARRIVAL_DISTANCE = 5.0
DEFAULT_ASSETS = Path(__file__).resolve().parent / "autoware_carla_launch"
COVERAGE_IMAGE = "unicarlaads-autoware:coverage"
DEFAULT_COVERAGE_PORT = 8081


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="CARLA 服务地址")
    parser.add_argument("--port", type=int, default=2000, help="CARLA RPC 端口")
    parser.add_argument(
        "--assets-dir",
        default=os.environ.get("UNICARLA_AUTOWARE_ASSETS", str(DEFAULT_ASSETS)),
        help="包含 autoware_data/ 与 carla_map/ 的目录",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="开启渲染并录制视频（红绿灯识别也需要渲染；默认 no_rendering_mode）",
    )
    parser.add_argument(
        "--bridge",
        choices=("ros2dds", "rmw-zenoh"),
        default="ros2dds",
        help="CARLA 数据桥接方式：ros2dds（zenoh-bridge-ros2dds）或 rmw-zenoh",
    )
    parser.add_argument(
        "--vehicle-name",
        default="v1",
        help="Autoware 车辆名，ego 的 role_name 为 autoware_<name>",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=2000,
        help="本次仿真的最大 tick 数",
    )
    parser.add_argument(
        "--deploy-timeout",
        type=float,
        default=600.0,
        help="deploy 的 HTTP 超时（秒），Autoware 预热较慢",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="使用 gcov 插桩的覆盖率镜像 {}，结束时保存覆盖率".format(COVERAGE_IMAGE),
    )
    parser.add_argument(
        "--coverage-port",
        type=int,
        default=DEFAULT_COVERAGE_PORT,
        help="覆盖率服务端口，仅 --coverage 时生效",
    )
    parser.add_argument("--seed", type=int, help="随机种子")
    return parser.parse_args()


def spawn_ego(world, vehicle_name, spawn_points):
    """创建 Autoware 使用的 ego 车辆（Tesla Model 3）。"""
    blueprint = world.get_blueprint_library().find("vehicle.tesla.model3")
    blueprint.set_attribute("role_name", "autoware_{}".format(vehicle_name))

    candidates = list(spawn_points)
    random.shuffle(candidates)
    for transform in candidates:
        ego = world.try_spawn_actor(blueprint, transform)
        if ego is not None:
            return ego, transform
    raise RuntimeError("所有 CARLA 出生点均被占用，无法创建 ego 车辆")


def _angle_diff(a, b):
    """两个弧度角的差值，范围 (-pi, pi]。"""
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def choose_destination(spawn_points, start_transform):
    """优先选择车头前方、距离适中的出生点作为终点。

    Town01 的 lanelet 单向车道较多，选到反向道路会绕很远才能到达。
    """
    start_location = start_transform.location
    start_yaw = math.radians(start_transform.rotation.yaw)
    candidates = []
    fallback = []
    for transform in spawn_points:
        distance = transform.location.distance(start_location)
        if distance < MIN_ROUTE_DISTANCE or distance > MAX_ROUTE_DISTANCE:
            continue
        fallback.append(transform)
        delta_x = transform.location.x - start_location.x
        delta_y = transform.location.y - start_location.y
        bearing = math.atan2(delta_y, delta_x)
        if abs(_angle_diff(bearing, start_yaw)) > math.radians(60.0):
            continue
        if abs(_angle_diff(math.radians(transform.rotation.yaw), start_yaw)) > math.radians(30.0):
            continue
        candidates.append(transform)
    if not candidates:
        candidates = fallback
    if not candidates:
        raise RuntimeError("当前地图中找不到合适的随机终点")
    return random.choice(candidates)


def route_point(transform):
    return {
        "x": transform.location.x,
        "y": transform.location.y,
        "z": transform.location.z,
        "yaw": transform.rotation.yaw,
    }


def apply_control(ego, values):
    """将 Autoware 返回的控制信号应用到 ego。"""
    control = carla.VehicleControl()
    control.steer = values["steer"]
    control.throttle = values["throttle"]
    control.brake = values["brake"]
    control.hand_brake = values["hand_brake"]
    control.reverse = values["reverse"]
    control.manual_gear_shift = values["manual_gear_shift"]
    control.gear = values["gear"]
    ego.apply_control(control)


def main():
    args = parse_args()
    random.seed(args.seed)
    # 容器启动时读取该环境变量选择桥接方式。
    os.environ["UNICARLA_AUTOWARE_BRIDGE_MODE"] = args.bridge

    if not args.render and os.environ.get("UNICARLA_AUTOWARE_TRAFFIC_LIGHT", "1") == "1":
        print("提示: 红绿灯识别已启用，但未开启渲染，交通灯相机没有图像；如需识别请加 --render")

    assets_dir = Path(args.assets_dir).resolve()
    data_dir = assets_dir / "autoware_data"
    map_dir = assets_dir / "carla_map"
    map_file = map_dir / "Town01" / "lanelet2_map.osm"
    if not data_dir.is_dir() or not map_file.is_file():
        raise RuntimeError(
            "{} 下缺少 autoware_data/ 或 carla_map/Town01/，"
            "请先运行 scripts/download_autoware_assets.sh，"
            "或用 --assets-dir 指向已有数据目录".format(assets_dir)
        )

    client = carla.Client(args.host, args.port)
    client.set_timeout(60.0)
    client_version = client.get_client_version()
    server_version = client.get_server_version()
    if not client_version.startswith("0.9.16"):
        raise RuntimeError(
            "Autoware 宿主侧需要 CARLA 0.9.16 API，当前为 {}".format(client_version)
        )
    if not server_version.startswith("0.9.16"):
        raise RuntimeError(
            "Autoware 需要 CARLA 0.9.16 服务端，当前为 {}".format(server_version)
        )

    # Autoware 当前只提供 Town01 的地图数据。
    world = client.load_world("Town01")
    original_settings = world.get_settings()

    ads = ADS(
        "autoware",
        image=COVERAGE_IMAGE if args.coverage else None,
        coverage_port=args.coverage_port if args.coverage else None,
        volumes=[
            "{}:{}".format(data_dir, "/opt/autoware_carla_launch/autoware_data"),
            "{}:{}".format(map_dir, "/opt/autoware_carla_launch/carla_map"),
        ],
    )
    ego = None
    ads_initialized = False
    try:
        # 场景运行阶段只由这个外部程序推进。
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = FIXED_DELTA_SECONDS
        settings.no_rendering_mode = not args.render
        world.apply_settings(settings)

        spawn_points = world.get_map().get_spawn_points()
        if len(spawn_points) < 2:
            raise RuntimeError("当前地图至少需要两个出生点")

        ego, start_transform = spawn_ego(world, args.vehicle_name, spawn_points)
        destination_transform = choose_destination(
            spawn_points,
            start_transform,
        )
        destination = destination_transform.location

        print("地图: {}".format(world.get_map().name))
        print("起点: {}".format(route_point(start_transform)))
        print("终点: {}".format(route_point(destination_transform)))
        print("正在初始化 Autoware（首次启动可能需要数分钟）...")

        ads.init(initialize_timeout=900.0)
        ads_initialized = True
        deploy_result = ads.deploy(
            ego_actor_id=ego.id,
            route=[route_point(start_transform), route_point(destination_transform)],
            carla_host=args.host,
            carla_port=args.port,
            timeout=args.deploy_timeout,
        )
        print(
            "Autoware 已部署，setup_frame={}, engaged={}".format(
                deploy_result["setup_frame"],
                deploy_result.get("engaged"),
            )
        )

        last_tick_wall = None
        for _ in range(args.max_ticks):
            # Autoware 是异步栈，按 fixed_delta 的实时节奏推进，避免仿真跑太快。
            if last_tick_wall is not None:
                sleep_time = FIXED_DELTA_SECONDS - (time.monotonic() - last_tick_wall)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            last_tick_wall = time.monotonic()

            frame_id = world.tick()
            result = ads.step(frame_id)
            control = result["control"]
            distance = ego.get_location().distance(destination)
            print(
                "tick={} dis-to-dest={:.2f}m control={}".format(
                    frame_id,
                    distance,
                    json.dumps(control, ensure_ascii=False, sort_keys=True),
                ),
                flush=True,
            )
            apply_control(ego, control)

            if distance <= ARRIVAL_DISTANCE:
                print("ego 已到达终点，距离终点 {:.2f} 米".format(distance))
                break
        else:
            print("达到最大 tick 数，仿真结束")
    except KeyboardInterrupt:
        print("收到中断，正在结束仿真")
    finally:
        # 录制需要渲染模式，先下载视频再清理传感器。
        if ads_initialized and args.render:
            try:
                video_path = ads.download_gui()
                print("视频已下载到 {}".format(video_path))
            except Exception as exc:
                print("视频下载失败: {}".format(exc))
        # 覆盖率保存会停止 Autoware 触发 gcov 落盘，必须在视频下载之后。
        if ads_initialized and args.coverage:
            try:
                coverage_path = ads.download_coverage()
                print("覆盖率已保存到 {}".format(coverage_path))
            except Exception as exc:
                print("覆盖率保存失败: {}".format(exc))
        if ads_initialized:
            ads.close()
        if ego is not None:
            ego.destroy()
        world.apply_settings(original_settings)


if __name__ == "__main__":
    main()
