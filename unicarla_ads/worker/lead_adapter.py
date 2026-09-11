"""LEAD 的被动 tick 运行适配器。"""

import os
import tempfile
import threading
import time

import numpy as np

from .frame_sensor_interface import FrameSensorInterface


class _SilentVideoWriter:
    """下载完成后占位，丢弃后续帧，避免写入已关闭的编码器。"""

    def write(self, frame):
        pass

    def release(self):
        pass


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
        # 置 UNICARLA_ADS_STEP_TIMING=1 可在 docker logs 中看到每步分段耗时。
        self._timing_enabled = os.environ.get("UNICARLA_ADS_STEP_TIMING") == "1"

    def initialize(self, agent_config=None, agent_path=None):
        """创建 LEAD Agent；模型在取得路线后于 deploy 阶段加载。"""
        del agent_path
        with self._lock:
            if self.state != "CREATED":
                raise RuntimeError("initialize 只能在 CREATED 状态调用")
            self.state = "INITIALIZING"
            try:
                # LEAD 的评估录制只有在 SAVE_PATH 存在时才会启用；视频先落在
                # 容器临时目录，download_gui 时再结束编码并读回。
                if not os.environ.get("SAVE_PATH"):
                    os.environ["SAVE_PATH"] = tempfile.mkdtemp(
                        prefix="unicarla_lead_gui_"
                    )
                os.environ.setdefault("BENCHMARK_ROUTE_ID", "unicarla")

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

                # 打开 LEAD 自带的评估录制：grid 视频由演示视角与模型输入
                # 拼接而成，相当于 InterFuser 的 GUI 画面。
                self._agent.lead_config.evaluation.apply_overrides(
                    {
                        "produce_demo_video": True,
                        "produce_grid_video": True,
                    },
                    is_user_override=True,
                )

                # 部署时先空跑几次前向，让 cudnn 的逐形状基准搜索发生在部署阶段，
                # 避免行驶阶段第一次前向阻塞数秒。
                self._warmup_agent()

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

    def _warmup_agent(self):
        """用零填充的伪输入空跑几次前向，完成 cudnn 逐形状基准与惰性初始化。"""
        import torch

        policy = self._agent.policy
        runner = self._agent.policy_runner
        config = policy.config
        features = {
            "town": self._map_name.split("/")[-1],
            "rgb": np.zeros(
                (3, config.final_image_height, config.final_image_width),
                np.float32,
            ),
            "rasterized_lidar": np.zeros(
                (1, config.lidar_height_pixel, config.lidar_width_pixel),
                np.float32,
            ),
            "radar": np.zeros(
                (
                    policy.lead_config.expert.sensor_rig.num_radar_sensors
                    * config.num_radar_points_per_sensor,
                    5,
                ),
                np.float32,
            ),
            "previous_target_point": np.zeros(2, np.float32),
            "target_point": np.zeros(2, np.float32),
            "next_target_point": np.zeros(2, np.float32),
            "speed": 0.0,
        }
        batch = policy.features_to_batch(features, runner.device)
        start = time.perf_counter()
        try:
            for _ in range(3):
                runner.forward(batch)
            torch.cuda.synchronize()
        except Exception as exc:  # 预热失败不阻塞部署，行为退回原状。
            print(
                "LEAD 模型预热失败（不影响运行）: {}".format(exc),
                flush=True,
            )
            return
        print(
            "LEAD 模型预热完成: 3 次前向共 {:.2f}s，设备 {}".format(
                time.perf_counter() - start,
                runner.device,
            ),
            flush=True,
        )

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
            # LEAD 期望 speed 为 numpy 标量（内部会调用 .item()）。
            self._sensor_interface.update_sensor(
                "speed",
                {"speed": np.float64(forward_speed)},
                frame_id,
            )

            if self._timing_enabled:
                t0 = time.perf_counter()
            sensor_data = self._sensor_interface.get_data(
                frame_id,
                self._sensor_timeout if timeout is None else float(timeout),
            )
            if self._timing_enabled:
                t1 = time.perf_counter()
            control = self._agent.run_step(sensor_data, GameTime.get_time())
            if self._timing_enabled:
                print(
                    "[timing] frame={} 传感器等待={:.3f}s 模型推理={:.3f}s".format(
                        frame_id,
                        t1 - t0,
                        time.perf_counter() - t1,
                    ),
                    flush=True,
                )
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

    def render_gui_video(self, fps=20.0):
        """结束评估视频编码，返回本次仿真的 MP4 字节。

        fps 参数仅为与 InterFuser 的下载接口保持一致；LEAD 录制时已按
        evaluation.video_fps 固定帧率，因此这里忽略该参数。
        """
        del fps
        with self._lock:
            self._close_gui_writers()
            video_path = self._find_gui_video()
            if video_path is None:
                raise RuntimeError("当前没有可下载的 LEAD 视频")
            with open(video_path, "rb") as video_file:
                return video_file.read()

    def _close_gui_writers(self):
        """关闭 ffmpeg 编码器，让视频文件写入完整尾部后再读取。"""
        if self._agent is None:
            return
        recorder = getattr(self._agent, "video_recorder", None)
        if recorder is None:
            return
        for name in (
            "grid_video_writer",
            "demo_video_writer",
            "debug_video_writer",
            "input_video_writer",
        ):
            writer = getattr(recorder, name, None)
            if writer is not None:
                writer.release()
                setattr(recorder, name, _SilentVideoWriter())

    def _find_gui_video(self):
        """按信息量从多到少返回已录制的评估视频路径。"""
        if self._agent is None:
            return None
        evaluation = self._agent.lead_config.evaluation
        if evaluation.save_path is None:
            return None
        for path in (
            evaluation.grid_video_path,
            evaluation.demo_video_path,
            evaluation.debug_video_path,
            evaluation.input_video_path,
        ):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
        return None

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
