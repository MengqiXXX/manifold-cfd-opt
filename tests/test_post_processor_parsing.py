import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluators import post_processor


class TestPostProcessorParsing(unittest.TestCase):
    def test_to_finite_float_ok(self):
        v, err = post_processor._to_finite_float("1.25")
        self.assertEqual(v, 1.25)
        self.assertIsNone(err)

    def test_to_finite_float_non_numeric(self):
        v, err = post_processor._to_finite_float("abc")
        self.assertIsNone(v)
        self.assertEqual(err, "NON_NUMERIC")

    def test_to_finite_float_nan(self):
        v, err = post_processor._to_finite_float("nan")
        self.assertIsNone(v)
        self.assertEqual(err, "NAN_INF")

    def test_to_finite_float_inf(self):
        v, err = post_processor._to_finite_float("inf")
        self.assertIsNone(v)
        self.assertEqual(err, "NAN_INF")


if __name__ == "__main__":
    unittest.main()

