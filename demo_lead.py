"""随机选择起终点并运行一次 LEAD 仿真。"""

import argparse
import json
import random

try:
    import carla
except ImportError as exc:
    raise RuntimeError(
        "未找到 CARLA Python API，请在 Python 3.10-3.12 环境安装 carla==0.9.16"
    ) from exc

from service import ADS

FIXED_DELTA_SECONDS = 0.05
MIN_ROUTE_DISTANCE = 30.0
MAX_ROUTE_DISTANCE = 100.0
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
    """创建与 LEAD 训练配置匹配的 ego 车辆。"""
    blueprint = world.get_blueprint_library().find("vehicle.lincoln.mkz_2020")
    blueprint.set_attribute("role_name", "hero")

    candidates = list(spawn_points)
    random.shuffle(candidates)
    for transform in candidates:
        ego = world.try_spawn_actor(blueprint, transform)
        if ego is not None:
            return ego, transform
    raise RuntimeError("所有 CARLA 出生点均被占用，无法创建 ego 车辆")


def choose_destination(spawn_points, start_location):
    """随机选择一个与起点距离适中的终点。"""
    candidates = [
        transform
        for transform in spawn_points
        if MIN_ROUTE_DISTANCE
        <= transform.location.distance(start_location)
        <= MAX_ROUTE_DISTANCE
    ]
    if not candidates:
        raise RuntimeError("当前地图中找不到合适的随机终点")
    return random.choice(candidates)


def location_to_dict(location):
    return {"x": location.x, "y": location.y, "z": location.z}


def apply_control(ego, values):
    """将 LEAD 返回的控制信号应用到 ego。"""
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
    client_version = client.get_client_version()
    server_version = client.get_server_version()
    if not client_version.startswith("0.9.16"):
        raise RuntimeError(
            "LEAD 宿主侧需要 CARLA 0.9.16 API，当前为 {}".format(client_version)
        )
    if not server_version.startswith("0.9.16"):
        raise RuntimeError(
            "LEAD 需要 CARLA 0.9.16 服务端，当前为 {}".format(server_version)
        )

    world = client.get_world()
    original_settings = world.get_settings()

    ads = ADS("lead")
    ego = None
    ads_initialized = False
    try:
        # 场景运行阶段只由这个外部程序推进。
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
        print("正在初始化 LEAD...")

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
        print("LEAD 已部署，setup_frame={}".format(deploy_result["setup_frame"]))

        for _ in range(args.max_ticks):
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
        # 先清理 LEAD 传感器，再销毁其依附的 ego。
        if ads_initialized:
            ads.close()
        if ego is not None:
            ego.destroy()
        world.apply_settings(original_settings)


if __name__ == "__main__":
    main()
