"""通过 rclpy 与容器内 Autoware 交互的最小接口。"""

import threading
import time

from autoware_adapi_v1_msgs.msg import (
    LocalizationInitializationState,
    OperationModeState,
    RouteState,
)
from autoware_adapi_v1_msgs.srv import (
    AcceptStart,
    ChangeOperationMode,
    ClearRoute,
    InitializeLocalization,
    SetRoutePoints,
)
from autoware_map_msgs.msg import LaneletMapBin
from autoware_vehicle_msgs.msg import GearCommand, VelocityReport
from geometry_msgs.msg import Pose, PoseStamped, PoseWithCovarianceStamped
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import Imu, PointCloud2
from tier4_vehicle_msgs.msg import ActuationCommandStamped


def _stamp_to_seconds(stamp):
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def yaw_to_quaternion(yaw):
    """只绕 Z 轴旋转的四元数。"""
    import math

    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


def make_pose(x, y, z, yaw):
    pose = Pose()
    pose.position.x = float(x)
    pose.position.y = float(y)
    pose.position.z = float(z)
    qx, qy, qz, qw = yaw_to_quaternion(yaw)
    pose.orientation.x = qx
    pose.orientation.y = qy
    pose.orientation.z = qz
    pose.orientation.w = qw
    return pose


class ServiceCallError(RuntimeError):
    """Autoware 服务调用失败。"""


class AutowareRosInterface:
    """订阅状态/控制话题，并提供 AD API 服务调用。"""

    # 最大兼容的 QoS：发布端更严格时也能收到。
    _LENIENT_QOS = QoSProfile(
        depth=10,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )
    # 地图话题一般是 transient_local，晚加入的订阅者需要兼容的 QoS。
    _MAP_QOS = QoSProfile(
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )

    def __init__(self, node_name="unicarla_autoware_adapter"):
        import rclpy

        if not rclpy.ok():
            rclpy.init()
        self._rclpy = rclpy
        self._condition = threading.Condition()
        self._clock_time = None
        self._actuation_cmd = None
        self._actuation_stamp = None
        self._gear = None
        self._velocity = 0.0
        self._localization_state = None
        self._route_state = None
        self._operation_mode = None
        self._seen_topics = {}

        self._node = rclpy.create_node(node_name)
        node = self._node
        node.create_subscription(
            Clock, "/clock", self._on_clock, self._LENIENT_QOS
        )
        node.create_subscription(
            ActuationCommandStamped,
            "/control/command/actuation_cmd",
            self._on_actuation_cmd,
            self._LENIENT_QOS,
        )
        node.create_subscription(
            GearCommand, "/control/command/gear_cmd", self._on_gear, self._LENIENT_QOS
        )
        node.create_subscription(
            VelocityReport,
            "/vehicle/status/velocity_status",
            self._on_velocity,
            self._LENIENT_QOS,
        )
        node.create_subscription(
            LocalizationInitializationState,
            "/api/localization/initialization_state",
            self._on_localization_state,
            self._LENIENT_QOS,
        )
        node.create_subscription(
            RouteState, "/api/routing/state", self._on_route_state, self._LENIENT_QOS
        )
        node.create_subscription(
            OperationModeState,
            "/api/operation_mode/state",
            self._on_operation_mode,
            self._LENIENT_QOS,
        )

        # 启动就绪检查用的传感器/地图话题。
        self._mark_subscription("gnss_pose", PoseStamped, "/sensing/gnss/pose")
        self._mark_subscription("imu_raw", Imu, "/sensing/imu/tamagawa/imu_raw")
        self._mark_subscription(
            "lidar_top", PointCloud2, "/sensing/lidar/top/pointcloud_raw_ex"
        )
        self._mark_subscription("vector_map", LaneletMapBin, "/map/vector_map", self._MAP_QOS)
        self._mark_subscription("pointcloud_map", PointCloud2, "/map/pointcloud_map", self._MAP_QOS)

        self.localization_client = node.create_client(
            InitializeLocalization, "/api/localization/initialize"
        )
        self.clear_route_client = node.create_client(
            ClearRoute, "/api/routing/clear_route"
        )
        self.set_route_client = node.create_client(
            SetRoutePoints, "/api/routing/set_route_points"
        )
        self.engage_client = node.create_client(
            ChangeOperationMode, "/api/operation_mode/change_to_autonomous"
        )
        self.accept_start_client = node.create_client(
            AcceptStart, "/api/motion/accept_start"
        )

        # 只保留一个 Autoware 内部订阅客户端；HTTP 线程和服务回调都能用。
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(node)
        self._spin_thread = threading.Thread(
            target=self._executor.spin, daemon=True
        )
        self._spin_thread.start()

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------
    def _on_clock(self, msg):
        with self._condition:
            self._clock_time = _stamp_to_seconds(msg.clock)
            self._condition.notify_all()

    def _on_actuation_cmd(self, msg):
        with self._condition:
            self._actuation_cmd = msg
            self._actuation_stamp = _stamp_to_seconds(msg.header.stamp)
            self._condition.notify_all()

    def _on_gear(self, msg):
        with self._condition:
            self._gear = msg.command

    def _on_velocity(self, msg):
        with self._condition:
            self._velocity = float(msg.longitudinal_velocity)
            self._seen_topics["velocity_status"] = True
            self._condition.notify_all()

    def _on_localization_state(self, msg):
        with self._condition:
            self._localization_state = msg.state
            self._condition.notify_all()

    def _on_route_state(self, msg):
        with self._condition:
            self._route_state = msg.state
            self._condition.notify_all()

    def _on_operation_mode(self, msg):
        with self._condition:
            self._operation_mode = msg.mode
            self._condition.notify_all()

    def _mark_subscription(self, name, message_type, topic, qos=None):
        def callback(_msg):
            with self._condition:
                self._seen_topics[name] = True
                self._condition.notify_all()

        self._node.create_subscription(
            message_type, topic, callback, qos or self._LENIENT_QOS
        )

    def wait_topics(self, names, timeout):
        """等待一组话题都出现过至少一条消息。"""
        with self._condition:
            self._condition.wait_for(
                lambda: all(self._seen_topics.get(name) for name in names),
                timeout=float(timeout),
            )
            return {name: bool(self._seen_topics.get(name)) for name in names}

    # ------------------------------------------------------------------
    # 查询与等待
    # ------------------------------------------------------------------
    def wait_for_service(self, client, timeout):
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if client.wait_for_service(timeout_sec=min(1.0, max(remaining, 0.01))):
                return True
        return False

    def call(self, client, request, timeout=10.0, service_name=""):
        """同步调用服务；executor 在后台线程自旋，这里轮询 future。"""
        deadline = time.monotonic() + float(timeout)
        while not client.service_is_ready():
            if time.monotonic() >= deadline:
                raise ServiceCallError("等待服务超时: {}".format(service_name))
            time.sleep(0.05)
        future = client.call_async(request)
        while not future.done():
            if time.monotonic() >= deadline:
                raise ServiceCallError("服务调用超时: {}".format(service_name))
            time.sleep(0.01)
        error = future.exception()
        if error is not None:
            raise ServiceCallError("服务调用异常 {}: {}".format(service_name, error))
        return future.result()

    def wait_clock_update(self, previous, timeout):
        """等待比 previous 更新的仿真时钟；超时返回当前值。"""
        with self._condition:
            if self._is_newer_clock(previous):
                return self._clock_time
            self._condition.wait_for(
                lambda: self._is_newer_clock(previous), timeout=float(timeout)
            )
            return self._clock_time

    def _is_newer_clock(self, previous):
        if self._clock_time is None:
            return False
        return previous is None or self._clock_time > previous + 1e-9

    def wait_actuation_cmd(self, min_stamp, timeout):
        """等待 stamp 不早于 min_stamp 的控制指令。"""
        with self._condition:
            if self._has_fresh_actuation(min_stamp):
                return self._actuation_cmd
            self._condition.wait_for(
                lambda: self._has_fresh_actuation(min_stamp), timeout=float(timeout)
            )
            if not self._has_fresh_actuation(min_stamp):
                return None
            return self._actuation_cmd

    def _has_fresh_actuation(self, min_stamp):
        if self._actuation_cmd is None or self._actuation_stamp is None:
            return False
        return self._actuation_stamp >= min_stamp - 1e-6

    def wait_localization_initialized(self, timeout):
        with self._condition:
            self._condition.wait_for(
                lambda: self._localization_state
                == LocalizationInitializationState.INITIALIZED,
                timeout=float(timeout),
            )
            return (
                self._localization_state
                == LocalizationInitializationState.INITIALIZED
            )

    def wait_route_set(self, timeout):
        with self._condition:
            self._condition.wait_for(
                lambda: self._route_state in (RouteState.SET, RouteState.ARRIVED),
                timeout=float(timeout),
            )
            return self._route_state in (RouteState.SET, RouteState.ARRIVED)

    def wait_route_unset(self, timeout):
        with self._condition:
            self._condition.wait_for(
                lambda: self._route_state in (None, RouteState.UNSET),
                timeout=float(timeout),
            )
            return self._route_state in (None, RouteState.UNSET)

    def wait_autonomous(self, timeout):
        with self._condition:
            self._condition.wait_for(
                lambda: self._operation_mode == OperationModeState.AUTONOMOUS,
                timeout=float(timeout),
            )
            return self._operation_mode == OperationModeState.AUTONOMOUS

    @property
    def latest_clock(self):
        with self._condition:
            return self._clock_time

    @property
    def latest_actuation_stamp(self):
        with self._condition:
            return self._actuation_stamp

    @property
    def gear(self):
        with self._condition:
            return self._gear

    @property
    def operation_mode(self):
        with self._condition:
            return self._operation_mode

    @property
    def route_state(self):
        with self._condition:
            return self._route_state

    @property
    def velocity(self):
        with self._condition:
            return self._velocity
