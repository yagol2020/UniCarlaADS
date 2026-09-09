"""InterFuser 的被动 tick 运行适配器。"""

import importlib.util
import math
import os
import tempfile
import threading

from .frame_sensor_interface import FrameSensorInterface


def _create_memory_display(display_class, frame_callback):
    """复用 ADS 原有 GUI，并将生成的画面交给内存回调。"""

    class MemoryDisplay(display_class):
        def run_interface(self, input_data):
            surface = super().run_interface(input_data)
            frame_callback(surface)
            return surface

    return MemoryDisplay


class InterFuserAdapter:
    """连接一个 CARLA world，并为一个 ego 运行 InterFuser。"""

    DEFAULT_AGENT = "/opt/interfuser/leaderboard/team_code/interfuser_agent.py"
    DEFAULT_CONFIG = "/opt/interfuser/leaderboard/team_code/interfuser_config.py"

    def __init__(self):
        self.state = "CREATED"
        self.last_error = None
        self.last_frame_id = None
        self._last_result = None
        self._lock = threading.Lock()
        self._agent = None
        self._agent_wrapper = None
        self._sensor_interface = None
        self._client = None
        self._world = None
        self._ego = None
        self._sensor_timeout = 10.0
        self._server_version = None
        self._map_name = None
        self._setup_frame = None
        self._gui_frames = []

    def initialize(self, agent_config=None, agent_path=None):
        """加载 InterFuser 模型，但暂不连接 CARLA。"""
        with self._lock:
            if self.state != "CREATED":
                raise RuntimeError("initialize 只能在 CREATED 状态调用")
            self.state = "INITIALIZING"
            try:
                os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
                module = self._load_agent_module(agent_path or self.DEFAULT_AGENT)
                module.SAVE_PATH = None
                module.DisplayInterface = _create_memory_display(
                    module.DisplayInterface,
                    self._capture_gui_frame,
                )

                # 完整检查点会覆盖骨干网络权重，避免模型构造时重复联网下载 ResNet。
                from timm.models import resnet

                resnet.default_cfgs["resnet50d"]["url"] = ""
                agent_class = getattr(module, module.get_entry_point())
                self._agent = agent_class(agent_config or self.DEFAULT_CONFIG)
                self.state = "INITIALIZED"
                self.last_error = None
                return self.status()
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)
                raise

    def deploy(self, payload):
        """绑定外部创建的 ego，设置路线并创建传感器。"""
        with self._lock:
            if self.state != "INITIALIZED":
                raise RuntimeError("deploy 只能在 INITIALIZED 状态调用")
            self.state = "DEPLOYING"
            try:
                import carla
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
                        "CARLA world 必须由外部设置 fixed_delta_seconds，例如 0.05"
                    )

                ego_actor_id = int(payload["ego_actor_id"])
                self._ego = self._world.get_actor(ego_actor_id)
                if self._ego is None or not self._ego.type_id.startswith("vehicle."):
                    raise ValueError("找不到 ego 车辆 actor: {}".format(ego_actor_id))

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
                CarlaDataProvider.register_actor(self._ego)
                GameTime.restart()

                gps_route, world_route = interpolate_trajectory(self._world, locations)
                if not world_route:
                    raise ValueError("无法根据给定路径点生成 CARLA 路线")
                self._agent.set_global_plan(gps_route, world_route)

                self._sensor_interface = FrameSensorInterface()
                self._agent.sensor_interface = self._sensor_interface
                self._agent_wrapper = AgentWrapper(self._agent)

                # InterFuser 原包装器在部署传感器后 tick 一次；运行阶段不再主动 tick。
                self._agent_wrapper.setup_sensors(self._ego)
                self._setup_frame = self._world.get_snapshot().frame
                self._sensor_interface.discard_through(self._setup_frame)

                self.state = "DEPLOYED"
                self.last_error = None
                result = self.status()
                result["setup_frame"] = self._setup_frame
                return result
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)
                raise

    def step(self, frame_id, timeout=None):
        """读取外部已经推进完成的 frame，并返回控制信号。"""
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

            snapshot = self._world.get_snapshot()
            if snapshot.frame != frame_id:
                raise ValueError(
                    "CARLA 当前 frame 为 {}，请求的是 {}".format(
                        snapshot.frame,
                        frame_id,
                    )
                )

            GameTime.on_carla_tick(snapshot.timestamp)
            CarlaDataProvider.on_carla_tick()

            # speedometer 是 Leaderboard 的伪传感器，这里按外部 frame 显式生成，避免其独立线程错过当前帧。
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

    def render_gui_video(self, fps=20.0):
        """将内存中的 GUI 帧编码为 MP4。"""
        fps = float(fps)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps 必须是正数")

        with self._lock:
            if not self._gui_frames:
                raise RuntimeError("当前没有可下载的 GUI 帧")
            frames = list(self._gui_frames)

        return self._encode_video(frames, fps)

    def status(self):
        sensor_ids = []
        if self._agent_wrapper is not None:
            sensor_ids = [
                sensor.id
                for sensor in self._agent_wrapper._sensors_list
                if hasattr(sensor, "id")
            ]
        return {
            "ads": "interfuser",
            "state": self.state,
            "last_error": self.last_error,
            "last_frame_id": self.last_frame_id,
            "setup_frame": self._setup_frame,
            "carla_server_version": self._server_version,
            "map": self._map_name,
            "ego_actor_id": self._ego.id if self._ego is not None else None,
            "sensor_actor_ids": sensor_ids,
            "gui_frame_count": len(self._gui_frames),
        }

    def close(self):
        """只清理 ADS 创建的传感器和模型，不销毁外部 ego。"""
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
            self._gui_frames.clear()
            self.state = "CLOSED"
            self.last_error = "; ".join(cleanup_errors) or None
            return self.status()

    def _capture_gui_frame(self, surface):
        """将 GUI 画面压缩后保存在内存中。"""
        if surface is None:
            return

        import cv2

        frame = cv2.cvtColor(surface, cv2.COLOR_RGB2BGR)
        success, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 90],
        )
        if not success:
            raise RuntimeError("GUI 帧编码失败")
        self._gui_frames.append(encoded.tobytes())

    @staticmethod
    def _encode_video(frames, fps):
        """把 JPEG 帧序列编码为 MP4 字节。"""
        import cv2
        import numpy as np

        first_frame = cv2.imdecode(
            np.frombuffer(frames[0], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if first_frame is None:
            raise RuntimeError("无法读取 GUI 帧")

        height, width = first_frame.shape[:2]
        file_descriptor, video_path = tempfile.mkstemp(suffix=".mp4")
        os.close(file_descriptor)
        try:
            writer = cv2.VideoWriter(
                video_path,
                cv2.VideoWriter_fourcc(*"mp4v"),
                fps,
                (width, height),
            )
            try:
                if not writer.isOpened():
                    raise RuntimeError("无法创建 GUI 视频编码器")

                for encoded in frames:
                    frame = cv2.imdecode(
                        np.frombuffer(encoded, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if frame is None:
                        raise RuntimeError("无法读取 GUI 帧")
                    if frame.shape[:2] != (height, width):
                        frame = cv2.resize(frame, (width, height))
                    writer.write(frame)
            finally:
                writer.release()

            with open(video_path, "rb") as video_file:
                return video_file.read()
        finally:
            if os.path.exists(video_path):
                os.remove(video_path)

    @staticmethod
    def _load_agent_module(agent_path):
        spec = importlib.util.spec_from_file_location("unicarla_interfuser_agent", agent_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载 InterFuser agent: {}".format(agent_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
