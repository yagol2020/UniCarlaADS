"""随机选择起终点并运行一次 InterFuser 仿真。"""

import argparse
import json
import random
import os
import sys
from pathlib import Path

try:
    CARLA_EGG = (
        Path(__file__).resolve().parent
        / "carla_package"
        / "carla-0.9.10-py3.7-linux-x86_64.egg"
    )
    sys.path.insert(0, str(CARLA_EGG))
    import carla
except ImportError as exc:
    raise RuntimeError(
        "未找到 CARLA Python API，请先将 CARLA 0.9.10 的 PythonAPI 加入 PYTHONPATH"
    ) from exc

from service import ADS

FIXED_DELTA_SECONDS = 0.05
MAX_ROUTE_DISTANCE = 100
ARRIVAL_DISTANCE = 5.0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="CARLA 服务地址")
    parser.add_argument("--port", type=int, default=2000, help="CARLA RPC 端口")
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=1000,
        help="本次仿真的最大 tick 数",
    )
    parser.add_argument("--seed", type=int, help="随机种子")
    return parser.parse_args()


def spawn_ego(world, spawn_points):
    """在随机出生点创建 InterFuser 使用的 ego 车辆。"""
    blueprint = world.get_blueprint_library().find("vehicle.lincoln.mkz2017")
    blueprint.set_attribute("role_name", "hero")

    candidates = list(spawn_points)
    random.shuffle(candidates)
    for transform in candidates:
        ego = world.try_spawn_actor(blueprint, transform)
        if ego is not None:
            return ego, transform
    raise RuntimeError("所有 CARLA 出生点均被占用，无法创建 ego 车辆")


def choose_destination(spawn_points, start_location):
    """随机选择一个与起点距离足够近的终点。"""
    candidates = [
        transform
        for transform in spawn_points
        if transform.location.distance(start_location) <= MAX_ROUTE_DISTANCE
    ]
    if not candidates:
        raise RuntimeError("当前地图中找不到合适的随机终点")
    return random.choice(candidates)


def location_to_dict(location):
    return {"x": location.x, "y": location.y, "z": location.z}


def apply_control(ego, values):
    """将 ADS 返回的控制信号应用到 ego。"""
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

    client = carla.Client(args.host, args.port)
    client.set_timeout(20.0)
    world = client.get_world()
    original_settings = world.get_settings()

    ads = ADS("interfuser")
    ego = None
    ads_initialized = False
    try:
        # 场景只由这个外部程序推进，ADS 不负责运行阶段的 tick。
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = FIXED_DELTA_SECONDS
        settings.no_rendering_mode = False
        world.apply_settings(settings)

        spawn_points = world.get_map().get_spawn_points()
        if len(spawn_points) < 2:
            raise RuntimeError("当前地图至少需要两个出生点")

        ego, start_transform = spawn_ego(world, spawn_points)
        destination_transform = choose_destination(
            spawn_points,
            start_transform.location,
        )
        destination = destination_transform.location

        print("地图: {}".format(world.get_map().name))
        print("起点: {}".format(location_to_dict(start_transform.location)))
        print("终点: {}".format(location_to_dict(destination)))
        print("正在初始化 InterFuser...")

        ads.init()
        ads_initialized = True
        deploy_result = ads.deploy(
            ego_actor_id=ego.id,
            route=[
                location_to_dict(start_transform.location),
                location_to_dict(destination),
            ],
            carla_host=args.host,
            carla_port=args.port,
        )
        print("InterFuser 已部署，setup_frame={}".format(deploy_result["setup_frame"]))

        for _ in range(args.max_ticks):
            frame_id = world.tick()
            result = ads.step(frame_id)
            control = result["control"]
            distance = ego.get_location().distance(destination)
            print(
                "tick={} dis-to-dest={:.2f}m control={} ".format(
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
        # 先清理 ADS 传感器，再销毁其依附的 ego。
        if ads_initialized:
            try:
                video_path = ads.download_gui()
                print("GUI 视频已下载到 {}".format(video_path))
            except Exception as exc:
                print("GUI 视频下载失败: {}".format(exc))
        ads.close()
        if ego is not None:
            ego.destroy()
        world.apply_settings(original_settings)


if __name__ == "__main__":
    main()
