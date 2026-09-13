import tempfile
import unittest
from pathlib import Path
from unittest import mock

from service import ADS


class RouteNormalizationTest(unittest.TestCase):
    def test_lead_uses_its_own_image(self):
        ads = ADS("lead")

        self.assertEqual(ads.image, "unicarlaads-lead:latest")
        self.assertEqual(ads.container_name, "unicarlaads-lead")

    def test_dictionary_route(self):
        route = ADS._normalize_route(
            [
                {"x": 1, "y": 2},
                {"x": 3, "y": 4, "z": 5},
            ]
        )
        self.assertEqual(
            route,
            [
                {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.0},
                {"x": 3.0, "y": 4.0, "z": 5.0, "yaw": 0.0},
            ],
        )

    def test_route_requires_two_points(self):
        with self.assertRaises(ValueError):
            ADS._normalize_route([(1, 2, 3)])


class GuiDownloadTest(unittest.TestCase):
    def test_download_gui_writes_video_to_directory(self):
        ads = ADS()
        with tempfile.TemporaryDirectory() as output_dir:
            with mock.patch.object(ads, "_request_bytes", return_value=b"video"):
                video_path = ads.download_gui(
                    output_dir=output_dir,
                    filename="test.mp4",
                )

            self.assertEqual(Path(video_path).read_bytes(), b"video")


if __name__ == "__main__":
    unittest.main()
