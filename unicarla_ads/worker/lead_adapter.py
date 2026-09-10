"""LEAD 的被动 tick 运行适配器。"""

import threading
import time

from .frame_sensor_interface import FrameSensorInterface


class LeadAdapter:
    """连接一个 CARLA world，并为一个 ego 运行 LEAD。"""

    DEFAULT_CONFIG = "/opt/lead/checkpoints/resnet34_v1.5.0/seed0"

    def __init__(self):
        self.state = "CREATED"
        self.last_error = None
        self.last_frame_id = None
        self._last_result = None
        self._lock = threading.Lock()
        self._agent = None
        self._agent_wrapper = None
        self._agent_config = None
        self._sensor_interface = None
        self._client = None
        self._world = None
        self._ego = None
        self._sensor_timeout = 10.0
        self._server_version = None
        self._map_name = None
        self._setup_frame = None

    def initialize(self, agent_config=None, agent_path=None):
        """创建 LEAD Agent；模型在取得路线后于 deploy 阶段加载。"""
        del agent_path
        with self._lock:
            if self.state != "CREATED":
                raise RuntimeError("initialize 只能在 CREATED 状态调用")
            self.state = "INITIALIZING"
            try:
                from lead.evaluation.agents.transfuser.transfuser_agent import (
                    TransfuserAgent,
                )

                self._agent_config = agent_config or self.DEFAULT_CONFIG
                self._agent = TransfuserAgent("127.0.0.1", 2000, False)
                self.state = "INITIALIZED"
                self.last_error = None
                return self.status()
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)
                raise

    def deploy(self, payload):
        """绑定外部创建的 ego，加载模型并部署 LEAD 传感器。"""
        with self._lock:
            if self.state != "INITIALIZED":
                raise RuntimeError("deploy 只能在 INITIALIZED 状态调用")
            self.state = "DEPLOYING"
            try:
                import carla
                import timm
                from leaderboard.autoagents.agent_wrapper import AgentWrapper
                from leaderboard.utils.route_manipulation import interpolate_trajectory
                from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
                from srunner.scenariomanager.timer import GameTime

                self._sensor_timeout = float(payload.get("sensor_timeout", 10.0))
                self._client = carla.Client(
                    payload.get("carla_host", "127.0.0.1"),
                    int(payload.get("carla_port", 2000)),
                )
                self._client.set_timeout(10.0)
                self._server_version = self._client.get_server_version()
                self._world = self._client.get_world()
                self._map_name = self._world.get_map().name

                world_settings = self._world.get_settings()
                if not world_settings.synchronous_mode:
                    raise RuntimeError("CARLA world 必须由外部设置为同步模式")
                if not world_settings.fixed_delta_seconds:
                    raise RuntimeError(
                        "CARLA world 必须由外部设置 fixed_delta_seconds=0.05"
                    )
                if abs(float(world_settings.fixed_delta_seconds) - 0.05) > 1e-6:
                    raise RuntimeError("LEAD 要求 fixed_delta_seconds=0.05")

                ego_actor_id = int(payload["ego_actor_id"])
                self._ego = self._world.get_actor(ego_actor_id)
                if self._ego is None or not self._ego.type_id.startswith("vehicle."):
                    raise ValueError("找不到 ego 车辆 actor: {}".format(ego_actor_id))
                if self._ego.attributes.get("role_name") != "hero":
                    raise ValueError("LEAD 要求 ego 的 role_name 为 hero")

                route_points = payload.get("route", [])
                if len(route_points) < 2:
                    raise ValueError("route 至少需要两个路径点")
                locations = [
                    carla.Location(
                        x=float(point["x"]),
                        y=float(point["y"]),
                        z=float(point.get("z", 0.0)),
                    )
                    for point in route_points
                ]

                CarlaDataProvider.set_client(self._client)
                CarlaDataProvider.set_world(self._world)
                CarlaDataProvider.register_actor(self._ego, self._ego.get_transform())
                CarlaDataProvider._carla_actor_pool[self._ego.id] = self._ego
                GameTime.restart()

                gps_route, world_route = interpolate_trajectory(locations)
                if not world_route:
                    raise ValueError("无法根据给定路径点生成 CARLA 路线")
                self._agent.set_global_plan(gps_route, world_route)

                # 完整 LEAD 检查点会覆盖骨干网络权重，避免初始化时联网下载。
                original_create_model = timm.create_model

                def create_model_without_download(*args, **kwargs):
                    kwargs["pretrained"] = False
                    return original_create_model(*args, **kwargs)

                timm.create_model = create_model_without_download
                try:
                    self._agent.setup(self._agent_config)
                finally:
                    timm.create_model = original_create_model

                self._sensor_interface = FrameSensorInterface()
                self._agent.sensor_interface = self._sensor_interface
                self._agent_wrapper = AgentWrapper(self._agent)

                # LEAD 原生包装器会用 10 个 tick 预热传感器；运行阶段不主动 tick。
                self._agent_wrapper.setup_sensors(self._ego)
                self._setup_frame = self._world.get_snapshot().frame
                self._sensor_interface.discard_through(self._setup_frame)

                self.state = "DEPLOYED"
                self.last_error = None
                return self.status()
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)
                raise

    def step(self, frame_id, timeout=None):
        """读取外部已经推进完成的 frame，并返回 LEAD 控制信号。"""
        with self._lock:
            if self.state not in ("DEPLOYED", "RUNNING"):
                raise RuntimeError("step 只能在 DEPLOYED 或 RUNNING 状态调用")

            frame_id = int(frame_id)
            if self._setup_frame is not None and frame_id <= self._setup_frame:
                raise ValueError(
                    "frame_id 必须晚于部署帧 {}，请先由外部调用 world.tick()".format(
                        self._setup_frame
                    )
                )
            if frame_id == self.last_frame_id and self._last_result is not None:
                return dict(self._last_result)
            if self.last_frame_id is not None and frame_id < self.last_frame_id:
                raise ValueError("frame_id 必须单调递增")

            from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
            from srunner.scenariomanager.timer import GameTime

            snapshot = self._wait_for_snapshot(frame_id)
            GameTime.on_carla_tick(snapshot.timestamp)
            CarlaDataProvider.on_carla_tick()

            velocity = self._ego.get_velocity()
            forward = self._ego.get_transform().get_forward_vector()
            forward_speed = (
                velocity.x * forward.x
                + velocity.y * forward.y
                + velocity.z * forward.z
            )
            self._sensor_interface.update_sensor(
                "speed",
                {"speed": forward_speed},
                frame_id,
            )

            sensor_data = self._sensor_interface.get_data(
                frame_id,
                self._sensor_timeout if timeout is None else float(timeout),
            )
            control = self._agent.run_step(sensor_data, GameTime.get_time())
            result = {
                "frame_id": frame_id,
                "control": {
                    "steer": float(control.steer),
                    "throttle": float(control.throttle),
                    "brake": float(control.brake),
                    "hand_brake": bool(control.hand_brake),
                    "reverse": bool(control.reverse),
                    "manual_gear_shift": bool(control.manual_gear_shift),
                    "gear": int(control.gear),
                },
            }
            self.state = "RUNNING"
            self.last_frame_id = frame_id
            self.last_error = None
            self._last_result = result
            return dict(result)

    def _wait_for_snapshot(self, frame_id, timeout=1.0):
        """等待当前 CARLA 客户端同步到指定帧。"""
        deadline = time.monotonic() + float(timeout)
        while True:
            snapshot = self._world.get_snapshot()
            if snapshot.frame == frame_id:
                return snapshot
            if snapshot.frame > frame_id:
                raise ValueError(
                    "CARLA 已推进到 frame {}，请求的 frame {} 已被跳过，"
                    "请检查是否有其他客户端调用 world.tick()".format(
                        snapshot.frame,
                        frame_id,
                    )
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "等待 CARLA frame {} 超时，当前 frame 为 {}".format(
                        frame_id,
                        snapshot.frame,
                    )
                )
            time.sleep(0.005)

    def status(self):
        sensor_ids = []
        if self._agent_wrapper is not None:
            sensor_ids = [
                sensor.id
                for sensor in self._agent_wrapper._sensors_list
                if hasattr(sensor, "id")
            ]
        return {
            "ads": "lead",
            "state": self.state,
            "last_error": self.last_error,
            "last_frame_id": self.last_frame_id,
            "setup_frame": self._setup_frame,
            "carla_server_version": self._server_version,
            "map": self._map_name,
            "ego_actor_id": self._ego.id if self._ego is not None else None,
            "sensor_actor_ids": sensor_ids,
            "agent_config": self._agent_config,
        }

    def close(self):
        """只清理 LEAD 创建的传感器和模型，不销毁外部 ego。"""
        with self._lock:
            if self.state == "CLOSED":
                return self.status()

            cleanup_errors = []
            if self._agent_wrapper is not None:
                try:
                    self._agent_wrapper.cleanup()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if self._sensor_interface is not None:
                self._sensor_interface.close()
            if self._agent is not None:
                try:
                    self._agent.destroy()
                except Exception as exc:
                    cleanup_errors.append(str(exc))

            try:
                from srunner.scenariomanager.carla_data_provider import CarlaDataProvider

                if self._ego is not None:
                    CarlaDataProvider._carla_actor_pool.pop(self._ego.id, None)
                CarlaDataProvider.cleanup()
            except Exception as exc:
                cleanup_errors.append(str(exc))

            self._agent_wrapper = None
            self._sensor_interface = None
            self._agent = None
            self._ego = None
            self._world = None
            self._client = None
            self._setup_frame = None
            self.state = "CLOSED"
            self.last_error = "; ".join(cleanup_errors) or None
            return self.status()
