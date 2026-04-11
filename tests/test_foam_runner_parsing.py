import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluators import foam_runner


class TestFoamRunnerParsing(unittest.TestCase):
    def test_checkmesh_ok(self):
        ok, diag = foam_runner._parse_checkmesh("Mesh OK.")
        self.assertTrue(ok)
        self.assertIsNone(diag)

    def test_checkmesh_failed(self):
        ok, diag = foam_runner._parse_checkmesh("Failed 1 checks")
        self.assertFalse(ok)
        self.assertIsNotNone(diag)

    def test_checkmesh_missing_ok_is_failure(self):
        ok, diag = foam_runner._parse_checkmesh("CheckMesh completed\nEnd")
        self.assertFalse(ok)
        self.assertIsNotNone(diag)

    def test_solver_diverged(self):
        msg = foam_runner._parse_solver_failure("FOAM FATAL ERROR: something")
        self.assertIsNotNone(msg)

    def test_solver_no_diverge(self):
        msg = foam_runner._parse_solver_failure("End\nExecutionTime = 12 s")
        self.assertIsNone(msg)


if __name__ == "__main__":
    unittest.main()
