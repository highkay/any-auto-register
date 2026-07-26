import unittest

from core.human_mouse import tremor_offsets, windmouse_path


class HumanMouseTest(unittest.TestCase):
    def test_windmouse_reaches_target(self):
        path = windmouse_path(0, 0, 200, 100)
        self.assertGreater(len(path), 5)
        self.assertAlmostEqual(path[-1][0], 200, delta=0.01)
        self.assertAlmostEqual(path[-1][1], 100, delta=0.01)

    def test_tremor_length(self):
        tre = tremor_offsets(20, seed=1)
        self.assertEqual(len(tre), 20)


if __name__ == "__main__":
    unittest.main()
