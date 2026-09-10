import threading
import time
import unittest

from unicarla_ads.worker.frame_sensor_interface import (
    FrameSensorInterface,
    SensorFrameTimeout,
)


class FrameSensorInterfaceTest(unittest.TestCase):
    def test_collects_exact_frame(self):
        interface = FrameSensorInterface()
        interface.register_sensor("camera", "sensor.camera.rgb", object())
        interface.register_sensor("speed", "sensor.speedometer", object())

        def publish():
            time.sleep(0.01)
            interface.update_sensor("camera", "image", 12)
            interface.update_sensor("speed", {"speed": 1.0}, 12)

        thread = threading.Thread(target=publish)
        thread.start()
        data = interface.get_data(12, timeout=1.0)
        thread.join()

        self.assertEqual(data["camera"], (12, "image"))
        self.assertEqual(data["speed"], (12, {"speed": 1.0}))

    def test_discards_setup_frame(self):
        interface = FrameSensorInterface()
        interface.register_sensor("camera", "sensor.camera.rgb", object())
        interface.update_sensor("camera", "old", 4)
        interface.discard_through(4)

        with self.assertRaises(SensorFrameTimeout):
            interface.get_data(4, timeout=0.01)


if __name__ == "__main__":
    unittest.main()
