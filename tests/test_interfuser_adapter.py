import unittest

from unicarla_ads.worker.interfuser_adapter import (
    InterFuserAdapter,
    _create_memory_display,
)


class InterFuserAdapterTest(unittest.TestCase):
    def test_step_requires_frame_after_setup_tick(self):
        adapter = InterFuserAdapter()
        adapter.state = "DEPLOYED"
        adapter._setup_frame = 10

        with self.assertRaisesRegex(ValueError, "请先由外部调用 world.tick"):
            adapter.step(10)

    def test_render_gui_video_requires_frames(self):
        adapter = InterFuserAdapter()

        with self.assertRaisesRegex(RuntimeError, "没有可下载的 GUI 帧"):
            adapter.render_gui_video()

    def test_memory_display_keeps_original_gui_output(self):
        frames = []

        class Display:
            def run_interface(self, input_data):
                return input_data["surface"]

        memory_display = _create_memory_display(Display, frames.append)()
        surface = object()

        self.assertIs(memory_display.run_interface({"surface": surface}), surface)
        self.assertEqual(frames, [surface])


if __name__ == "__main__":
    unittest.main()
