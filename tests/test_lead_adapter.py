import unittest

from unicarla_ads.worker.lead_adapter import LeadAdapter


class LeadAdapterTest(unittest.TestCase):
    def test_step_requires_frame_after_setup_tick(self):
        adapter = LeadAdapter()
        adapter.state = "DEPLOYED"
        adapter._setup_frame = 20

        with self.assertRaisesRegex(ValueError, "请先由外部调用 world.tick"):
            adapter.step(20)

    def test_default_checkpoint(self):
        adapter = LeadAdapter()

        self.assertIn("resnet34_v1.5.0/seed0", adapter.DEFAULT_CONFIG)

    def test_render_gui_video_requires_recording(self):
        adapter = LeadAdapter()

        with self.assertRaisesRegex(RuntimeError, "没有可下载的 LEAD 视频"):
            adapter.render_gui_video()


if __name__ == "__main__":
    unittest.main()
