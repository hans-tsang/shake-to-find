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
    def test_frame_size_and_hotspot(self):
        data, w, h, hx, hy = main.CursorArt().frame(160)
        self.assertEqual((w, h), (200, 200))  # canvas = cursor size * 40/32
        self.assertEqual(len(data), 200 * 200 * 4)
        self.assertEqual((hx, hy), (30, 30))

    def test_ripple_frame_is_centred_on_tip(self):
        art = main.CursorArt()
        data, w, h, hx, hy = main.compose_ripples(art, 160, [0.0], 0.2, main.DEFAULT_CONFIG)
        self.assertEqual((hx, hy), (w // 2, h // 2))
        self.assertGreater(w, 2 * 160 * main.DEFAULT_CONFIG["ripple_max_radius"])
        self.assertEqual(len(data), w * h * 4)


class SpringTest(unittest.TestCase):
    def run_spring(self, zeta, steps=300):
        x, v, peak = 1.0, 0.0, 1.0
        for _ in range(steps):
            x, v = main.spring_step(x, v, 5.0, 260.0, zeta, 0.01)
            peak = max(peak, x)
        return x, peak

    def test_underdamped_overshoots_then_settles(self):
        x, peak = self.run_spring(0.45)
        self.assertGreater(peak, 5.5)
        self.assertAlmostEqual(x, 5.0, places=2)

    def test_critically_damped_does_not_overshoot(self):
        x, peak = self.run_spring(1.0)
        self.assertLessEqual(peak, 5.0 + 1e-6)
        self.assertAlmostEqual(x, 5.0, places=2)

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
