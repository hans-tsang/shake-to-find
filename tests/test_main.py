import unittest

import main


def drive(detector, strokes, step_px, samples_per_stroke, dt=0.01):
    t, x, hit = 0.0, 0, False
    for i in range(strokes):
        for _ in range(samples_per_stroke):
            x += step_px if i % 2 == 0 else -step_px
            t += dt
            hit |= detector.feed(t, x, 1.0)
    return hit


class ShakeDetectorTest(unittest.TestCase):
    def setUp(self):
        self.det = main.ShakeDetector(dict(main.DEFAULT_CONFIG))

    def test_fast_shake_triggers(self):
        self.assertTrue(drive(self.det, strokes=6, step_px=30, samples_per_stroke=10))

    def test_slow_wiggle_does_not_trigger(self):
        self.assertFalse(drive(self.det, strokes=8, step_px=4, samples_per_stroke=25))

    def test_fast_one_way_motion_does_not_trigger(self):
        self.assertFalse(drive(self.det, strokes=1, step_px=40, samples_per_stroke=60))


class CursorArtTest(unittest.TestCase):
    def test_frame_size_and_format(self):
        data, size, hot = main.CursorArt().frame(160)
        self.assertEqual(size, 160)
        self.assertEqual(len(data), 160 * 160 * 4)
        self.assertEqual(hot, 10)


class StartupTest(unittest.TestCase):
    def setUp(self):
        self._orig = main.RUN_NAME
        main.RUN_NAME = "ShakeToFindUnitTest"  # never touch the real entry

    def tearDown(self):
        main.set_startup(False)
        main.RUN_NAME = self._orig

    def test_enable_and_disable(self):
        main.set_startup(True)
        self.assertTrue(main.is_startup_enabled())
        self.assertEqual(main.get_startup_value(), main.startup_command())
        main.set_startup(False)
        self.assertFalse(main.is_startup_enabled())
        main.set_startup(False)  # disabling twice is harmless


if __name__ == "__main__":
    unittest.main()
