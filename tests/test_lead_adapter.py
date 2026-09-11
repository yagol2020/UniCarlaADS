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


if __name__ == "__main__":
    unittest.main()
