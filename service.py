"""UniCarlaADS 的宿主侧调用入口。"""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib import error, request


class ADSRequestError(RuntimeError):
    """ADS 容器返回错误。"""


class ADS:
    """管理单个 ADS 容器并调用其 HTTP 接口。"""

    DEFAULT_IMAGES = {
        "interfuser": "unicarlaads-interfuser:latest",
        "lead": "unicarlaads-lead:latest",
        "autoware": "unicarlaads-autoware:latest",
    }

    def __init__(
        self,
        name="interfuser",
        port=8080,
        image=None,
        container_name=None,
        startup_timeout=30.0,
        volumes=None,
    ):
        if name not in self.DEFAULT_IMAGES:
            raise ValueError("当前支持的 ADS: {}".format(", ".join(self.DEFAULT_IMAGES)))

        self.name = name
        self.port = int(port)
        self.image = image or self.DEFAULT_IMAGES[name]
        self.container_name = container_name or "unicarlaads-{}".format(name)
        self.startup_timeout = float(startup_timeout)
        self.volumes = list(volumes or [])
        self.base_url = "http://127.0.0.1:{}".format(self.port)
        self._container_started = False

    def init(self, agent_config=None, initialize_timeout=300.0):
        """启动 ADS 容器并初始化自动驾驶系统。"""
        if self._container_started:
            raise RuntimeError("ADS 容器已经启动")

        command = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--init",
            "--name",
            self.container_name,
            "--network",
            "host",
            "--gpus",
            "all",
        ]
        command += self._extra_run_args()
        for volume in self.volumes:
            command += ["--volume", volume]
        command += [
            "--env",
            "UNICARLA_ADS_PORT={}".format(self.port),
            "--env",
            "UNICARLA_ADS_NAME={}".format(self.name),
            self.image,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError("启动 ADS 容器失败: {}".format(message))

        self._container_started = True
        try:
            self._wait_until_ready()
            payload = {}
            if agent_config is not None:
                payload["agent_config"] = agent_config
            return self._request(
                "POST", "/initialize", payload, timeout=float(initialize_timeout)
            )
        except Exception:
            self._stop_container()
            raise

    def initialize(self, agent_config=None, initialize_timeout=300.0):
        """兼容更直观的 initialize 命名。"""
        return self.init(
            agent_config=agent_config, initialize_timeout=initialize_timeout
        )

    def deploy(
        self,
        ego_actor_id,
        route,
        carla_host="127.0.0.1",
        carla_port=2000,
        sensor_timeout=10.0,
        timeout=120.0,
    ):
        """连接 CARLA、绑定 ego、设置路线并部署传感器。"""
        payload = {
            "carla_host": carla_host,
            "carla_port": int(carla_port),
            "ego_actor_id": int(ego_actor_id),
            "route": self._normalize_route(route),
            "sensor_timeout": float(sensor_timeout),
        }
        return self._request("POST", "/deploy", payload, timeout=float(timeout))

    def step(self, frame_id, timeout=30.0):
        """计算指定外部 CARLA tick 对应的控制信号。"""
        return self._request(
            "POST",
            "/step",
            {"frame_id": int(frame_id), "timeout": float(timeout)},
            timeout=float(timeout) + 5.0,
        )

    def status(self):
        """查询 ADS 当前状态。"""
        return self._request("GET", "/status", timeout=5.0)

    def download_gui(
        self,
        output_dir="video_download",
        filename=None,
        fps=20.0,
        timeout=300.0,
    ):
        """下载 ADS 在本次仿真中生成的 GUI 视频。"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        if filename is None:
            filename = "{}_gui_{}.mp4".format(
                self.name,
                time.strftime("%Y%m%d_%H%M%S"),
            )
        filename = str(filename)
        if Path(filename).name != filename:
            raise ValueError("filename 只能是文件名，不能包含目录")
        if not filename.lower().endswith(".mp4"):
            filename += ".mp4"

        video = self._request_bytes(
            "POST",
            "/download_gui",
            {"fps": float(fps)},
            timeout=float(timeout),
        )
        video_path = output_path / filename
        video_path.write_bytes(video)
        return str(video_path.resolve())

    def close(self):
        """清理传感器和模型，然后停止容器。"""
        response = None
        if self._container_started:
            try:
                response = self._request("POST", "/close", {}, timeout=30.0)
            except Exception:
                # 即使 HTTP 清理失败，也必须停止当前 ADS 容器。
                pass
            finally:
                self._stop_container()
        return response

    def _wait_until_ready(self):
        deadline = time.monotonic() + self.startup_timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                self._request("GET", "/health", timeout=1.0)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError("等待 ADS HTTP 服务超时: {}".format(last_error))

    def _request(self, method, path, payload=None, timeout=30.0):
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        http_request = request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error", body)
            except ValueError:
                message = body
            raise ADSRequestError(message) from exc
        except error.URLError as exc:
            raise ADSRequestError("无法连接 ADS 服务: {}".format(exc.reason)) from exc

        return json.loads(body) if body else {}

    def _request_bytes(self, method, path, payload=None, timeout=30.0):
        data = None
        headers = {"Accept": "video/mp4"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        http_request = request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(http_request, timeout=timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error", body)
            except ValueError:
                message = body
            raise ADSRequestError(message) from exc
        except error.URLError as exc:
            raise ADSRequestError("无法连接 ADS 服务: {}".format(exc.reason)) from exc

    def _stop_container(self):
        subprocess.run(
            ["docker", "stop", "--time", "10", self.container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        self._container_started = False

    def _extra_run_args(self):
        """Autoware 需要特权模式、共享内存和宿主机用户映射。"""
        if self.name != "autoware":
            return []
        args = [
            "--privileged",
            "--ipc",
            "host",
            "--shm-size=8g",
            "--ulimit",
            "memlock=33554432:33554432",
            "--env",
            "HOST_UID={}".format(os.getuid()),
            "--env",
            "HOST_GID={}".format(os.getgid()),
        ]
        # 允许宿主用环境变量关闭红绿灯识别（镜像内默认开启）或选择桥接方式。
        for name in (
            "UNICARLA_AUTOWARE_TRAFFIC_LIGHT",
            "UNICARLA_AUTOWARE_BRIDGE_MODE",
        ):
            value = os.environ.get(name)
            if value is not None:
                args += ["--env", "{}={}".format(name, value)]
        return args

    @staticmethod
    def _normalize_route(route):
        if not isinstance(route, (list, tuple)) or len(route) < 2:
            raise ValueError("route 至少需要两个路径点")

        points = []
        for point in route:
            if isinstance(point, dict):
                if "x" not in point or "y" not in point:
                    raise ValueError("每个路径点必须包含 x 和 y")
                points.append(
                    {
                        "x": float(point["x"]),
                        "y": float(point["y"]),
                        "z": float(point.get("z", 0.0)),
                        "yaw": float(point.get("yaw", 0.0)),
                    }
                )
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append(
                    {
                        "x": float(point[0]),
                        "y": float(point[1]),
                        "z": float(point[2]) if len(point) > 2 else 0.0,
                        "yaw": float(point[3]) if len(point) > 3 else 0.0,
                    }
                )
            else:
                raise ValueError("路径点必须是字典或坐标序列")
        return points

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
