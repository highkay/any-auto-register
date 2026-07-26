import unittest

from services.vision_solver.schema import CaptchaSpec, load_preset
from services.vision_solver.vision import parse_answer_index, parse_pick_list
from services.vision_solver.github_puzzle import detect_variant


class VisionSolverUnitTest(unittest.TestCase):
    def test_parse_pick_list(self):
        self.assertEqual(parse_pick_list("foo PICK=[1,3,4] bar"), [1, 3, 4])

    def test_parse_answer(self):
        self.assertEqual(parse_answer_index("reason\nANSWER=2"), 2)

    def test_load_preset(self):
        spec = load_preset("hcaptcha")
        self.assertIsInstance(spec, CaptchaSpec)
        self.assertEqual(spec.mode, "canvas_grid")

    def test_detect_variant(self):
        self.assertEqual(detect_variant("rotate the image"), "rotate")
        self.assertEqual(detect_variant("put in correct order"), "sequence")


if __name__ == "__main__":
    unittest.main()
