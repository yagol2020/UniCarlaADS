"""Autoware 的被动 tick 运行适配器。

控制回路：Autoware 输出 actuation_cmd，本适配器换算成 CARLA
VehicleControl 返回给宿主，由宿主 apply_control。Bridge 侧的 Rust
代码经过 overlay 裁剪，只计算不再写入 CARLA。
"""

import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import numpy as np

from .autoware_ros import AutowareRosInterface, ServiceCallError, make_pose

AUTOWARE_ROOT = os.environ.get("AUTOWARE_CARLA_ROOT", "/opt/autoware_carla_launch")
VEHICLE_NAME = os.environ.get("VEHICLE_NAME", "v1")
EGO_ROLE_NAME = "autoware_{}".format(VEHICLE_NAME)
# 覆盖率镜像里插桩构建树的路径；不存在说明当前不是覆盖率镜像。
COVERAGE_BUILD_ROOT = os.environ.get(
    "UNICARLA_AUTOWARE_COVERAGE_BUILD", "/opt/autoware_gcov/build"
)

SAFE_STOP = {
    "steer": 0.0,
    "throttle": 0.0,
    "brake": 1.0,
    "hand_brake": False,
    "reverse": False,
    "manual_gear_shift": False,
    "gear": 0,
}


class _ManagedProcess:
    """带日志文件的子进程，退出时按进程组清理。"""

    def __init__(self, name, command, log_dir, env=None):
        self.name = name
        self.log_path = os.path.join(log_dir, "{}.log".format(name))
        self._log_file = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            command,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    def alive(self):
        return self.process.poll() is None

    def tail(self, lines=20):
        try:
            with open(self.log_path, "rb") as log_file:
                content = log_file.read().decode("utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self, grace=5.0):
        """SIGINT 结束进程组，超时后 SIGKILL；grace 可放大以等待优雅退出。"""
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
                self.process.wait(timeout=float(grace))
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except OSError:
                    pass
        self._log_file.close()


class AutowareAdapter:
    """启动 Autoware 全栈，并把控制指令转换成宿主可执行的形式。"""

    def __init__(self):
        self.state = "CREATED"
        self.last_error = None
        self.last_frame_id = None
        self._last_result = None
        self._lock = threading.Lock()
        self._ros = None
        self._processes = []
        self._client = None
        self._world = None
        self._host = None
        self._port = None
        self._ego = None
        self._sensors = []
        self._video_camera = None
        self._video_frames = []
        self._video_lock = threading.Lock()
        self._video_enabled = False
        self._max_video_frames = int(
            os.environ.get("UNICARLA_AUTOWARE_VIDEO_MAX_FRAMES", "6000")
        )
        self._map_name = None
        self._server_version = None
        self._setup_frame = None
        self._steer_curve_x = None
        self._steer_curve_y = None
        self._tau = 0.2
        self._prev_steer_output = 0.0
        self._prev_steer_time = None
        self._last_step_clock = None
        self._engaged = False
        self._started = False
        self._last_engage_time = 0.0
        self._last_accept_time = 0.0
        self._tick_thread = None
        self._tick_stop = None
        self._tick_error = None
        self._map_recovery_delay = float(
            os.environ.get("UNICARLA_AUTOWARE_MAP_RECOVERY_DELAY", "30")
        )
        self._startup_timeout = float(
            os.environ.get("UNICARLA_AUTOWARE_STARTUP_TIMEOUT", "600")
        )
        self._warmup_seconds = float(
            os.environ.get("UNICARLA_AUTOWARE_WARMUP_SECONDS", "60")
        )
        self._warmup_interval = float(
            os.environ.get("UNICARLA_AUTOWARE_WARMUP_INTERVAL", "0.1")
        )
        self._strict = os.environ.get("UNICARLA_AUTOWARE_STRICT") == "1"
        self._y_flip = os.environ.get("UNICARLA_AUTOWARE_Y_FLIP", "1") == "1"
        self._traffic_light = (
            os.environ.get("UNICARLA_AUTOWARE_TRAFFIC_LIGHT", "1") == "1"
        )
        self._bridge_mode = (
            os.environ.get("UNICARLA_AUTOWARE_BRIDGE_MODE", "ros2dds").strip().lower()
        )
        if self._bridge_mode not in ("ros2dds", "rmw-zenoh"):
            raise RuntimeError(
                "不支持的桥接方式 {}，可选 ros2dds / rmw-zenoh".format(self._bridge_mode)
            )

    def _apply_bridge_env(self):
        """rmw-zenoh 模式：本进程的 rclpy 也走 zenoh，与 bridge 的 v1/ 前缀对齐。"""
        if self._bridge_mode != "rmw-zenoh":
            return
        os.environ["RMW_IMPLEMENTATION"] = "rmw_zenoh_cpp"
        os.environ["ZENOH_SESSION_CONFIG_URI"] = os.path.join(
            AUTOWARE_ROOT, "config/RMW_ZENOH_SESSION_CONFIG.json5"
        )
        os.environ["ZENOH_CONFIG_OVERRIDE"] = 'namespace="{}"'.format(VEHICLE_NAME)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def initialize(self, agent_config=None, agent_path=None):
        """启动 bridge、Autoware，并等待 AD API 服务就绪。"""
        del agent_config, agent_path
        with self._lock:
            if self.state != "CREATED":
                raise RuntimeError("initialize 只能在 CREATED 状态调用")
            self.state = "INITIALIZING"
            try:
                # rmw_zenoh 的 RMW 会话要连 bridge 的 7447，先起进程再建 ROS 接口。
                self._apply_bridge_env()
                self._start_processes()
                self._ros = AutowareRosInterface()
                self._ensure_carla_connected()
                # Autoware 的 use_sim_time 节点需要 /clock，地图加载也依赖进程定期 tick。
                self._start_tick_driver()
                try:
                    self._wait_autoware_ready()
                    self._wait_map_ready()
                finally:
                    self._stop_tick_driver()
                self.state = "INITIALIZED"
                self.last_error = None
                return self.status()
            except Exception as exc:
                self.state = "ERROR"
                self.last_error = str(exc)
                self._stop_processes()
                raise

    def _start_processes(self):
        log_dir = os.environ.get("UNICARLA_AUTOWARE_LOG_DIR", "/tmp/unicarla_autoware")
        os.makedirs(log_dir, exist_ok=True)
        env = dict(os.environ)
        env["AUTOWARE_CARLA_ROOT"] = AUTOWARE_ROOT
        env.setdefault("CARLA_SIMULATOR_IP", "127.0.0.1")

        bridge = os.path.join(
            AUTOWARE_ROOT,
            "external/zenoh_carla_bridge/target/release/zenoh_carla_bridge",
        )
        if not os.path.exists(bridge):
            raise RuntimeError("缺少已编译的 zenoh bridge，请先构建镜像")

        if self._bridge_mode == "rmw-zenoh":
            # bridge 自己就是 zenoh router，Autoware 用 rmw_zenoh 直连 7447。
            bridge_mode = "rmw-zenoh"
            bridge_config = "rmw-zenoh-carla-bridge-conf.json5"
        else:
            bridge_mode = "ros2"
            bridge_config = "zenoh-carla-bridge-conf.json5"

        # ZENOH_CONFIG_OVERRIDE 只给 RMW 会话，bridge 按自己的配置文件启动。
        bridge_env = dict(env)
        bridge_env.pop("ZENOH_CONFIG_OVERRIDE", None)
        self._processes.append(
            _ManagedProcess(
                "zenoh_carla_bridge",
                [
                    bridge,
                    "--mode",
                    bridge_mode,
                    "--zenoh-listen",
                    "tcp/0.0.0.0:7447",
                    "--zenoh-config",
                    os.path.join(AUTOWARE_ROOT, "config", bridge_config),
                    "--carla-address",
                    env.get("CARLA_SIMULATOR_IP", "127.0.0.1"),
                ],
                log_dir,
                env=bridge_env,
            )
        )
        # 先让 zenoh_carla_bridge 监听 7447。
        time.sleep(2.0)

        if self._bridge_mode == "ros2dds":
            ros2dds = os.path.join(
                AUTOWARE_ROOT,
                "external/zenoh-plugin-ros2dds/target/release/zenoh-bridge-ros2dds",
            )
            if not os.path.exists(ros2dds):
                raise RuntimeError("缺少已编译的 zenoh bridge，请先构建镜像")
            self._processes.append(
                _ManagedProcess(
                    "zenoh_bridge_ros2dds",
                    [
                        ros2dds,
                        "-n",
                        "/{}".format(VEHICLE_NAME),
                        "-d",
                        os.environ.get("ROS_DOMAIN_ID", "0"),
                        "-c",
                        os.path.join(
                            AUTOWARE_ROOT, "config/zenoh-bridge-ros2dds-conf.json5"
                        ),
                        "-e",
                        "tcp/127.0.0.1:7447",
                    ],
                    log_dir,
                    env=env,
                )
            )

        launch_command = (
            "source /opt/autoware/setup.bash"
            " && source {root}/install/setup.bash"
            " && exec ros2 launch autoware_carla_launch autoware_zenoh.launch.xml"
            " use_traffic_light_recognition:={traffic_light}"
            " manual_control:=false rviz:=false"
        ).format(
            root=AUTOWARE_ROOT,
            traffic_light="true" if self._traffic_light else "false",
        )
        self._processes.append(
            _ManagedProcess(
                "autoware",
                ["bash", "-lc", launch_command],
                log_dir,
                env=env,
            )
        )

    def _wait_autoware_ready(self):
        deadline = time.monotonic() + self._startup_timeout
        clients = (
            ("localization/initialize", self._ros.localization_client),
            ("routing/clear_route", self._ros.clear_route_client),
            ("routing/set_route_points", self._ros.set_route_client),
            ("operation_mode/change_to_autonomous", self._ros.engage_client),
        )
        for name, client in clients:
            while not client.service_is_ready():
                self._ensure_processes_alive()
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "等待 Autoware 服务 {} 超时，日志: {}".format(
                            name, self._process_logs()
                        )
                    )
                time.sleep(1.0)

    def _ensure_processes_alive(self):
        for process in self._processes:
            if not process.alive():
                raise RuntimeError(
                    "{} 进程已退出，日志: {}".format(process.name, process.tail(30))
                )

    def _ensure_carla_connected(self, host="127.0.0.1", port=2000):
        """建立 CARLA 连接；启动阶段用它 tick，deploy 阶段复用。"""
        import carla

        if self._client is None or self._port != int(port) or self._host != host:
            self._client = carla.Client(host, int(port))
            self._client.set_timeout(20.0)
            self._world = self._client.get_world()
            self._host = host
            self._port = int(port)

    def _start_tick_driver(self):
        """后台按 fixed_delta 节奏 tick，供 Autoware 启动与 deploy 使用。"""
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return
        if self._world is None:
            return
        interval = max(
            0.001, float(self._world.get_settings().fixed_delta_seconds or 0.05)
        )
        self._tick_error = None
        self._tick_stop = threading.Event()

        def tick_loop():
            while not self._tick_stop.is_set():
                try:
                    self._world.tick()
                except Exception as exc:
                    self._tick_error = str(exc)
                    self._tick_stop.set()
                    break
                self._tick_stop.wait(interval)

        self._tick_thread = threading.Thread(
            target=tick_loop, name="unicarla-autoware-tick", daemon=True
        )
        self._tick_thread.start()

    def _stop_tick_driver(self):
        if self._tick_stop is not None:
            self._tick_stop.set()
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=3.0)
        self._tick_thread = None
        self._tick_stop = None
        if self._tick_error is not None:
            error = self._tick_error
            self._tick_error = None
            raise RuntimeError("Autoware tick 线程失败: {}".format(error))

    def _wait_sensing_ready(self, timeout):
        """等待 bridge 把 ego 传感器数据送到 Autoware。"""
        names = ("velocity_status", "gnss_pose", "imu_raw", "lidar_top")
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            self._ensure_processes_alive()
            seen = self._ros.wait_topics(names, timeout=1.0)
            if all(seen.values()):
                return
        raise RuntimeError(
            "等待 Autoware 传感器数据超时: {}，日志: {}".format(
                seen, self._process_logs()
            )
        )

    def _wait_map_ready(self, timeout=None):
        """等待 lanelet2/点云地图发布；必要时手动补加载 lanelet2。"""
        timeout = self._startup_timeout if timeout is None else timeout
        deadline = time.monotonic() + float(timeout)
        pointcloud_seen_at = None
        recovery_requested = False
        while time.monotonic() < deadline:
            self._ensure_processes_alive()
            if self._ros.wait_topics(("vector_map",), timeout=1.0)["vector_map"]:
                return
            seen = self._ros.wait_topics(("pointcloud_map",), timeout=0.1)
            if seen["pointcloud_map"] and pointcloud_seen_at is None:
                pointcloud_seen_at = time.monotonic()
            if (
                pointcloud_seen_at is not None
                and not recovery_requested
                and time.monotonic() - pointcloud_seen_at > self._map_recovery_delay
            ):
                recovery_requested = True
                self._recover_lanelet_map()
        raise RuntimeError("等待 Autoware 地图加载超时，日志: {}".format(self._process_logs()))

    def _recover_lanelet_map(self):
        """上游偶发 lanelet2 loader 未随点云 loader 加载，手动 load 组件兜底。"""
        map_name = os.environ.get("CARLA_MAP_NAME", "Town01")
        map_path = "{}/carla_map/{}/lanelet2_map.osm".format(AUTOWARE_ROOT, map_name)
        command = (
            "source /opt/autoware/setup.bash"
            " && source {root}/install/setup.bash"
            " && ros2 component load --no-daemon --spin-time 3"
            " /map/map_container autoware_map_loader"
            " autoware::map_loader::Lanelet2MapLoaderNode"
            " --node-name lanelet2_map_loader --node-namespace /map"
            " -p allow_unsupported_version:=true"
            " -p center_line_resolution:=5.0"
            " -p use_waypoints:=true"
            " -p lanelet2_map_path:={map_path}"
            " -r output/lanelet2_map:=vector_map"
        ).format(root=AUTOWARE_ROOT, map_path=map_path)
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=60.0,
            )
            print(
                "Autoware lanelet 地图恢复: returncode={} {}".format(
                    result.returncode, (result.stdout + result.stderr)[-300:]
                ),
                flush=True,
            )
        except Exception as exc:
            print("Autoware lanelet 地图恢复失败: {}".format(exc), flush=True)

    def deploy(self, payload):
        """绑定外部 ego、创建传感器、初始化定位与路线并预热。"""
        with self._lock:
            if self.state != "INITIALIZED":
                raise RuntimeError("deploy 只能在 INITIALIZED 状态调用")
            self.state = "DEPLOYING"
            try:
                self._ensure_carla_connected(
                    payload.get("carla_host", "127.0.0.1"),
                    int(payload.get("carla_port", 2000)),
                )
                self._server_version = self._client.get_server_version()
                self._world = self._client.get_world()
                self._map_name = self._world.get_map().name

                settings = self._world.get_settings()
                if not settings.synchronous_mode:
                    raise RuntimeError("CARLA world 必须由外部设置为同步模式")

                ego_actor_id = int(payload["ego_actor_id"])
                self._ego = self._world.get_actor(ego_actor_id)
                if self._ego is None or not self._ego.type_id.startswith("vehicle."):
                    raise ValueError("找不到 ego 车辆 actor: {}".format(ego_actor_id))
                if self._ego.attributes.get("role_name") != EGO_ROLE_NAME:
                    raise ValueError(
                        "Autoware 要求 ego 的 role_name 为 {}".format(EGO_ROLE_NAME)
                    )

                route = payload.get("route") or []
                if len(route) < 2:
                    raise ValueError("route 至少需要两个路径点")

                self._setup_sensors(self._ego)
                self._setup_video_camera(self._ego)
                self._cache_steering_curve()
                # 与 carla_agent 一致：开启扫掠车轮碰撞。
                physics = self._ego.get_physics_control()
                physics.use_sweep_wheel_collision = True
                self._ego.apply_physics_control(physics)

                self._start_tick_driver()
                try:
                    self._wait_sensing_ready(timeout=90.0)
                    self._initialize_localization_with_retry(timeout=60.0)
                    self._warmup_until(self._ros_localization_ready, timeout=30.0)
                    self._set_route(route)
                    self._warmup_until(self._ros_route_ready, timeout=20.0)
                    self._warmup_until(self._ready_to_drive, timeout=30.0)
                finally:
                    self._stop_tick_driver()

                self._setup_frame = self._world.get_snapshot().frame
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
        """等待本帧 Autoware 控制指令，换算成 CARLA VehicleControl。"""
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

            timeout = 30.0 if timeout is None else float(timeout)
            target = self._ros.wait_clock_update(
                self._last_step_clock, timeout=min(timeout, 2.0)
            )
            if target is not None:
                self._last_step_clock = target

            if not self._engaged:
                # 未进入自动驾驶前不执行 Autoware 控制。
                self._try_engage()
                self._try_accept_start()
                if not self._engaged:
                    return self._record_result(frame_id, SAFE_STOP, None)
            if not self._started:
                # Autoware 起步前需要 accept_start，但不阻塞控制转发。
                self._try_accept_start()

            if target is None:
                return self._record_result(frame_id, SAFE_STOP, None)

            command = self._ros.wait_actuation_cmd(target, timeout)
            if command is None:
                if self._strict:
                    raise RuntimeError("等待 Autoware 控制指令超时")
                return self._record_result(frame_id, SAFE_STOP, None)

            control, info = self._convert_control(command)
            self.state = "RUNNING"
            return self._record_result(frame_id, control, info)

    def _record_result(self, frame_id, control, info):
        result = {"frame_id": frame_id, "control": control}
        if info is not None:
            result["autoware"] = info
        self.last_frame_id = frame_id
        self.last_error = None
        self._last_result = result
        return dict(result)

    # ------------------------------------------------------------------
    # Autoware 初始化
    # ------------------------------------------------------------------
    def _map_pose(self, location, yaw_degrees):
        """CARLA 坐标 -> Autoware 地图坐标（地图 y 轴与 CARLA 相反）。"""
        x = float(location.x)
        y = float(location.y)
        z = float(location.z)
        yaw = math.radians(float(yaw_degrees))
        if self._y_flip:
            return x, -y, z, -yaw
        return x, y, z, yaw

    def _initialize_localization(self):
        ego_transform = self._ego.get_transform()
        map_x, map_y, map_z, map_yaw = self._map_pose(
            ego_transform.location, ego_transform.rotation.yaw
        )
        from autoware_adapi_v1_msgs.srv import InitializeLocalization
        from geometry_msgs.msg import PoseWithCovarianceStamped

        request = InitializeLocalization.Request()
        pose = PoseWithCovarianceStamped()
        pose.header.frame_id = "map"
        pose.pose.pose = make_pose(map_x, map_y, map_z, map_yaw)
        # 给 NDT 一个宽松但合理的先验。
        pose.pose.covariance[0] = 0.25
        pose.pose.covariance[7] = 0.25
        pose.pose.covariance[14] = 0.25
        pose.pose.covariance[35] = 0.01
        request.pose.append(pose)
        return self._ros.call(
            self._ros.localization_client,
            request,
            timeout=10.0,
            service_name="/api/localization/initialize",
        )

    def _initialize_localization_with_retry(self, timeout):
        """定位服务可能因为缺少数据被拒绝，配合 tick 重试。"""
        deadline = time.monotonic() + float(timeout)
        last_error = None
        while time.monotonic() < deadline:
            if self._ros_localization_ready():
                return
            try:
                response = self._initialize_localization()
                if response.status.success:
                    return
                last_error = response.status.message
            except ServiceCallError as exc:
                last_error = str(exc)
            time.sleep(self._warmup_interval)
        raise RuntimeError("初始化 Autoware 定位失败: {}".format(last_error))

    def _set_route(self, route, timeout=60.0):
        from autoware_adapi_v1_msgs.srv import ClearRoute, SetRoutePoints

        request = SetRoutePoints.Request()
        request.header.frame_id = "map"
        goal_point = route[-1]
        goal_yaw = goal_point.get("yaw", 0.0)
        goal_x, goal_y, goal_z, _ = self._map_pose(
            _LocationProxy(goal_point), goal_yaw
        )
        request.goal = make_pose(goal_x, goal_y, goal_z, _map_yaw(goal_yaw, self._y_flip))
        request.option.allow_goal_modification = True

        waypoints = []
        for point in route[1:-1]:
            point_yaw = point.get("yaw", 0.0)
            point_x, point_y, point_z, _ = self._map_pose(
                _LocationProxy(point), point_yaw
            )
            waypoints.append(
                make_pose(
                    point_x, point_y, point_z, _map_yaw(point_yaw, self._y_flip)
                )
            )
        request.waypoints = waypoints

        # 定位刚就绪时 Route API 可能还没准备好，clear/set 会有竞态。
        deadline = time.monotonic() + float(timeout)
        last_error = "超时"
        while time.monotonic() < deadline:
            try:
                self._ros.call(
                    self._ros.clear_route_client,
                    ClearRoute.Request(),
                    timeout=10.0,
                    service_name="/api/routing/clear_route",
                )
            except ServiceCallError as exc:
                last_error = str(exc)
                time.sleep(0.5)
                continue
            self._ros.wait_route_unset(2.0)
            try:
                response = self._ros.call(
                    self._ros.set_route_client,
                    request,
                    timeout=15.0,
                    service_name="/api/routing/set_route_points",
                )
            except ServiceCallError as exc:
                last_error = str(exc)
                time.sleep(0.5)
                continue
            if response.status.success:
                return
            last_error = response.status.message
            time.sleep(0.5)
        raise RuntimeError("设置 Autoware 路线失败: {}".format(last_error))

    def _try_engage(self):
        now = time.monotonic()
        if self._engaged or now - self._last_engage_time < 2.0:
            return
        self._last_engage_time = now
        from autoware_adapi_v1_msgs.srv import ChangeOperationMode

        try:
            response = self._ros.call(
                self._ros.engage_client,
                ChangeOperationMode.Request(),
                timeout=5.0,
                service_name="/api/operation_mode/change_to_autonomous",
            )
        except ServiceCallError:
            return
        if response.status.success:
            self._engaged = True

    def _try_accept_start(self):
        """Autoware 进入 AUTONOMOUS 后还需要 accept_start 才会真正起步。"""
        if not self._engaged or self._started:
            return
        now = time.monotonic()
        if now - self._last_accept_time < 2.0:
            return
        self._last_accept_time = now
        from autoware_adapi_v1_msgs.srv import AcceptStart

        try:
            response = self._ros.call(
                self._ros.accept_start_client,
                AcceptStart.Request(),
                timeout=5.0,
                service_name="/api/motion/accept_start",
            )
        except ServiceCallError:
            return
        if response.status.success:
            self._started = True

    def _ready_to_drive(self):
        """预热阶段持续尝试进入自动驾驶，直到 Autoware 开始输出控制。"""
        if not self._engaged:
            self._try_engage()
        if self._engaged and not self._started:
            self._try_accept_start()
        return self._engaged and self._ros.latest_actuation_stamp is not None

    def _ros_localization_ready(self):
        return self._ros.wait_localization_initialized(0.0)

    def _ros_route_ready(self):
        return self._ros.wait_route_set(0.0)

    def _warmup_until(self, predicate, timeout):
        """等待条件成立；此时 tick 由 _tick_thread 驱动。"""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(self._warmup_interval)
        return predicate()

    # ------------------------------------------------------------------
    # 传感器
    # ------------------------------------------------------------------
    def _setup_sensors(self, ego):
        """按 carla_agent 的配置部署 Autoware 所需传感器。"""
        import carla
        from carla import AttachmentType

        library = self._world.get_blueprint_library()

        gnss_bp = library.find("sensor.other.gnss")
        gnss_bp.set_attribute("role_name", "ublox")
        for attribute in (
            "noise_alt_stddev",
            "noise_lat_stddev",
            "noise_lon_stddev",
            "noise_alt_bias",
            "noise_lat_bias",
            "noise_lon_bias",
        ):
            gnss_bp.set_attribute(attribute, "0.0")
        self._sensors.append(
            self._world.spawn_actor(
                gnss_bp,
                carla.Transform(carla.Location(x=0.0, y=0.0, z=2.4)),
                attach_to=ego,
            )
        )

        imu_bp = library.find("sensor.other.imu")
        imu_bp.set_attribute("role_name", "tamagawa")
        for attribute in (
            "noise_accel_stddev_x",
            "noise_accel_stddev_y",
            "noise_accel_stddev_z",
            "noise_gyro_stddev_x",
            "noise_gyro_stddev_y",
            "noise_gyro_stddev_z",
        ):
            imu_bp.set_attribute(attribute, "0.0")
        self._sensors.append(
            self._world.spawn_actor(
                imu_bp,
                carla.Transform(
                    carla.Location(x=0.0, y=0.0, z=2.4),
                    carla.Rotation(yaw=270.0),
                ),
                attach_to=ego,
            )
        )

        lidar_bp = library.find("sensor.lidar.ray_cast")
        lidar_bp.set_attribute("role_name", "top")
        lidar_bp.set_attribute("range", "100")
        lidar_bp.set_attribute("rotation_frequency", "20")
        lidar_bp.set_attribute("channels", "64")
        lidar_bp.set_attribute("upper_fov", "10")
        lidar_bp.set_attribute("lower_fov", "-30")
        lidar_bp.set_attribute("points_per_second", "1200000")
        lidar_bp.set_attribute("atmosphere_attenuation_rate", "0.004")
        lidar_bp.set_attribute("dropoff_general_rate", "0.45")
        lidar_bp.set_attribute("dropoff_intensity_limit", "0.8")
        lidar_bp.set_attribute("dropoff_zero_intensity", "0.4")
        self._sensors.append(
            self._world.spawn_actor(
                lidar_bp,
                carla.Transform(
                    carla.Location(x=0.0, y=0.0, z=2.4),
                    carla.Rotation(yaw=270.0),
                ),
                attach_to=ego,
            )
        )

        # 交通灯相机供红绿灯识别使用。
        # no_rendering_mode 下相机无法出图，软件渲染（lavapipe）还会拖慢其他传感器。
        if not self._traffic_light:
            return
        if self._world.get_settings().no_rendering_mode:
            print(
                "警告: 已启用红绿灯识别，但 no_rendering_mode 下交通灯相机没有图像，"
                "请用 --render 运行才能识别红绿灯",
                flush=True,
            )
            return

        extent = ego.bounding_box.extent
        camera_bp = library.find("sensor.camera.rgb")
        camera_bp.set_attribute("role_name", "traffic_light")
        self._sensors.append(
            self._world.spawn_actor(
                camera_bp,
                carla.Transform(
                    carla.Location(
                        x=0.8 * (extent.x + 0.5),
                        y=0.0,
                        z=1.3 * (extent.z + 0.5),
                    )
                ),
                attach_to=ego,
                attachment_type=AttachmentType.Rigid,
            )
        )

    def _setup_video_camera(self, ego):
        """部署录制专用的第三人称相机，与 Autoware 的传感器互不影响。

        no_rendering_mode 下相机无法出图，默认只在渲染模式启用；
        可用 UNICARLA_AUTOWARE_VIDEO_CAMERA=0/1 强制关闭或开启。
        """
        import carla
        from carla import AttachmentType

        setting = os.environ.get("UNICARLA_AUTOWARE_VIDEO_CAMERA")
        if setting is None:
            enabled = not self._world.get_settings().no_rendering_mode
        else:
            enabled = setting == "1"
        self._video_enabled = enabled
        if not enabled:
            return

        extent = ego.bounding_box.extent
        library = self._world.get_blueprint_library()
        camera_bp = library.find("sensor.camera.rgb")
        camera_bp.set_attribute("role_name", "unicarla_video")
        camera_bp.set_attribute("image_size_x", "800")
        camera_bp.set_attribute("image_size_y", "600")
        self._video_camera = self._world.spawn_actor(
            camera_bp,
            carla.Transform(
                carla.Location(
                    x=-(2.0 * extent.x + 1.5),
                    y=0.0,
                    z=1.5 * extent.z + 1.5,
                ),
                carla.Rotation(pitch=-15.0),
            ),
            attach_to=ego,
            attachment_type=AttachmentType.Rigid,
        )
        self._video_camera.listen(self._capture_video_frame)
        self._sensors.append(self._video_camera)

    def _capture_video_frame(self, image):
        """把相机帧压缩为 JPEG 存内存，帧数超过上限时丢弃最早的帧。"""
        import cv2

        frame = np.frombuffer(image.raw_data, dtype=np.uint8).reshape(
            (image.height, image.width, 4)
        )
        success, encoded = cv2.imencode(
            ".jpg",
            frame[:, :, :3],
            [int(cv2.IMWRITE_JPEG_QUALITY), 90],
        )
        if not success:
            return
        with self._video_lock:
            self._video_frames.append(encoded.tobytes())
            if len(self._video_frames) > self._max_video_frames:
                del self._video_frames[0]

    def _cache_steering_curve(self):
        curve = self._ego.get_physics_control().steering_curve
        self._steer_curve_x = np.array([point.x for point in curve], dtype=np.float64)
        self._steer_curve_y = np.array([point.y for point in curve], dtype=np.float64)

    # ------------------------------------------------------------------
    # 控制换算（与桥接 Rust 逻辑保持一致）
    # ------------------------------------------------------------------
    def _convert_control(self, command):
        from autoware_vehicle_msgs.msg import GearCommand

        accel = float(command.actuation.accel_cmd)
        brake = float(command.actuation.brake_cmd)
        steer_cmd = float(command.actuation.steer_cmd)
        gear = self._ros.gear

        reverse = False
        hand_brake = False
        if gear == GearCommand.REVERSE:
            reverse = True
        elif gear == GearCommand.PARK:
            accel = 0.0
            brake = 0.0
            steer_cmd = 0.0
            hand_brake = True

        velocity = self._ego.get_velocity()
        speed = abs(float(velocity.x))
        if len(self._steer_curve_x) >= 2:
            max_steer_ratio = float(
                np.interp(speed, self._steer_curve_x, self._steer_curve_y)
            )
        else:
            max_steer_ratio = 1.0

        timestamp = float(
            command.header.stamp.sec + command.header.stamp.nanosec * 1e-9
        )
        steer_norm = self._first_order_steering(-steer_cmd, timestamp)
        steer = steer_norm * max_steer_ratio

        control = {
            "steer": float(steer),
            "throttle": float(accel),
            "brake": float(brake),
            "hand_brake": bool(hand_brake),
            "reverse": bool(reverse),
            "manual_gear_shift": False,
            "gear": 0,
        }
        info = {
            "accel_cmd": accel,
            "brake_cmd": brake,
            "steer_cmd": steer_cmd,
            "speed": speed,
            "engaged": self._engaged,
        }
        return control, info

    def _first_order_steering(self, steer_input, now_ts):
        out = self._prev_steer_output
        if self._prev_steer_time is not None:
            dt = max(now_ts - self._prev_steer_time, 0.0)
            if dt > 0.0:
                out = self._prev_steer_output + (
                    steer_input - self._prev_steer_output
                ) * (dt / (self._tau + dt))
        else:
            out = steer_input
        self._prev_steer_output = out
        self._prev_steer_time = now_ts
        return out

    # ------------------------------------------------------------------
    # 视频导出
    # ------------------------------------------------------------------
    def render_gui_video(self, fps=20.0):
        """把录制相机缓存的帧编码为 MP4 字节。"""
        fps = float(fps)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps 必须是正数")

        if not self._video_enabled:
            raise RuntimeError(
                "Autoware 未启用视频录制：需要以 --render 运行，"
                "且未设置 UNICARLA_AUTOWARE_VIDEO_CAMERA=0"
            )
        with self._video_lock:
            if not self._video_frames:
                raise RuntimeError("当前没有可下载的视频帧")
            frames = list(self._video_frames)

        return self._encode_video(frames, fps)

    @staticmethod
    def _encode_video(frames, fps):
        """把 JPEG 帧序列编码为 MP4 字节。"""
        import cv2

        first_frame = cv2.imdecode(
            np.frombuffer(frames[0], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if first_frame is None:
            raise RuntimeError("无法读取视频帧")

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
                    raise RuntimeError("无法创建视频编码器")

                for encoded in frames:
                    frame = cv2.imdecode(
                        np.frombuffer(encoded, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    )
                    if frame is None:
                        raise RuntimeError("无法读取视频帧")
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

    # ------------------------------------------------------------------
    # 覆盖率导出
    # ------------------------------------------------------------------
    def coverage_enabled(self):
        """当前镜像是否带 gcov 插桩构建树。"""
        return os.path.isdir(COVERAGE_BUILD_ROOT)

    def save_coverage(self):
        """停止 Autoware 触发 gcov 落盘，返回 lcov 归档的 tar.gz 字节。

        libgcov 只在进程退出时写 .gcda，因此这里先 SIGINT 优雅结束 Autoware
        （coverage 镜像无性能要求，等待时间放宽），再运行 lcov/genhtml。
        需在视频下载之后调用。
        """
        with self._lock:
            if not self.coverage_enabled():
                raise RuntimeError(
                    "当前 Autoware 镜像未开启覆盖率编译，"
                    "请先运行 scripts/build_autoware_coverage.sh"
                )
            if self.state != "CLOSED":
                self._stop_processes(grace=30.0)
            return self._render_coverage()

    def _render_coverage(self):
        """在容器内用 lcov 采集行/分支覆盖并打包为 tar.gz。"""
        work_dir = tempfile.mkdtemp(prefix="unicarla_coverage_")
        try:
            script = (
                "set -e; cd {work}; "
                "lcov --capture --directory {build} --output-file raw.info "
                "--branch-coverage --ignore-errors mismatch,gcov,source --quiet; "
                # 只保留插桩源码，排除 CMake 探针等构建期计数；extract 需显式带
                # --branch-coverage，否则会丢弃分支数据。
                "lcov --extract raw.info '*/autoware_universe/*' '*/autoware_core/*' "
                "--branch-coverage --ignore-errors unused "
                "--output-file extracted.info --quiet; "
                # 去掉编译器插入的异常路径分支（BRDA 的 e 块）。lcov 2.0 自带的
                # --filter branch / geninfo_no_exception_branch 在该版本会把同行的
                # 真实分支一起删掉，这里自行过滤并让 summary/genhtml 重新统计。
                "python3 {filter} extracted.info coverage.info; "
                "lcov --summary coverage.info --branch-coverage > summary.txt 2>&1; "
                "genhtml coverage.info --output-directory html "
                "--branch-coverage --ignore-errors source --quiet; "
                "tar -czf coverage.tar.gz coverage.info summary.txt html"
            ).format(
                work=work_dir,
                build=COVERAGE_BUILD_ROOT,
                filter=os.path.join(os.path.dirname(__file__), "coverage_filter.py"),
            )
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=1800.0,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "采集覆盖率失败: {}".format(
                        (result.stdout + result.stderr)[-500:]
                    )
                )
            archive_path = os.path.join(work_dir, "coverage.tar.gz")
            with open(archive_path, "rb") as archive:
                return archive.read()
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 状态与清理
    # ------------------------------------------------------------------
    def status(self):
        return {
            "ads": "autoware",
            "state": self.state,
            "last_error": self.last_error,
            "last_frame_id": self.last_frame_id,
            "setup_frame": self._setup_frame,
            "carla_server_version": self._server_version,
            "map": self._map_name,
            "ego_actor_id": self._ego.id if self._ego is not None else None,
            "sensor_actor_ids": [sensor.id for sensor in self._sensors],
            "engaged": self._engaged,
            "started": self._started,
            "operation_mode": self._ros.operation_mode if self._ros is not None else None,
            "route_state": self._ros.route_state if self._ros is not None else None,
            "gear": self._ros.gear if self._ros is not None else None,
            "sim_time": self._ros.latest_clock if self._ros is not None else None,
            "velocity": self._ros.velocity if self._ros is not None else None,
            "video_enabled": self._video_enabled,
            "video_frame_count": len(self._video_frames),
            "coverage_enabled": self.coverage_enabled(),
        }

    def _process_logs(self, lines=15):
        return "; ".join(
            "{}: {}".format(process.name, process.tail(lines))
            for process in self._processes
        )

    def _stop_processes(self, grace=5.0):
        for process in reversed(self._processes):
            process.stop(grace=grace)
        self._processes = []

    def close(self):
        """清理传感器与子进程，不销毁外部 ego。"""
        with self._lock:
            if self.state == "CLOSED":
                return self.status()
            for sensor in self._sensors:
                try:
                    sensor.stop()
                    sensor.destroy()
                except Exception:
                    pass
            self._sensors = []
            self._video_camera = None
            with self._video_lock:
                self._video_frames.clear()
            self._stop_processes()
            self._ego = None
            self._world = None
            self._client = None
            self._setup_frame = None
            self.state = "CLOSED"
            return self.status()


class _LocationProxy:
    """让 _map_pose 同时兼容 carla.Location 和 route 字典。"""

    def __init__(self, point):
        self.x = float(point["x"])
        self.y = float(point["y"])
        self.z = float(point.get("z", 0.0))


def _map_yaw(yaw_degrees, flip):
    yaw = math.radians(float(yaw_degrees))
    return -yaw if flip else yaw
