"""按 CARLA frame 聚合 ADS 传感器数据。"""

import threading
import time
from collections import OrderedDict


class SensorFrameTimeout(RuntimeError):
    """等待指定帧的传感器数据超时。"""


class FrameSensorInterface:
    """实现 Leaderboard Callback 所需的最小传感器接口。"""

    def __init__(self, max_frames=8):
        self._condition = threading.Condition()
        self._sensors = {}
        self._frames = OrderedDict()
        self._discard_through = -1
        self._max_frames = max_frames
        self._closed = False

    def register_sensor(self, tag, sensor_type, sensor):
        with self._condition:
            if tag in self._sensors:
                raise ValueError("传感器 id 重复: {}".format(tag))
            self._sensors[tag] = sensor_type

    def update_sensor(self, tag, data, frame):
        frame = int(frame)
        with self._condition:
            if self._closed or frame <= self._discard_through:
                return
            if tag not in self._sensors:
                raise ValueError("未注册的传感器: {}".format(tag))

            frame_data = self._frames.setdefault(frame, {})
            frame_data[tag] = (frame, data)
            while len(self._frames) > self._max_frames:
                self._frames.popitem(last=False)
            self._condition.notify_all()

    def discard_through(self, frame):
        """丢弃部署阶段及其之前产生的数据。"""
        with self._condition:
            self._discard_through = max(self._discard_through, int(frame))
            for old_frame in list(self._frames):
                if old_frame <= self._discard_through:
                    self._frames.pop(old_frame, None)

    def get_data(self, frame, timeout):
        """等待并返回指定 frame 的全部传感器数据。"""
        frame = int(frame)
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while True:
                if self._closed:
                    raise SensorFrameTimeout("传感器接口已经关闭")

                frame_data = self._frames.get(frame, {})
                missing = sorted(set(self._sensors) - set(frame_data))
                if not missing:
                    result = dict(frame_data)
                    self._frames.pop(frame, None)
                    return result

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SensorFrameTimeout(
                        "等待 frame {} 超时，缺少传感器: {}".format(
                            frame,
                            ", ".join(missing),
                        )
                    )
                self._condition.wait(remaining)

    def close(self):
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()
