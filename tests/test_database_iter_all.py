import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluators.base import DesignParams, EvalResult
from storage.database import ResultDatabase


class TestDatabaseIterAll(unittest.TestCase):
    def test_iter_all_and_best(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.sqlite"
            db = ResultDatabase(db_path)

            ok = EvalResult(
                params=DesignParams(0.1, -0.2, 0.3),
                flow_cv=0.1,
                pressure_drop=10.0,
                converged=True,
                runtime_s=1.0,
                status="OK",
                metadata={"dp_weight": 1.0e-5, "dp_ref": 1.0},
            )
            bad = EvalResult(
                params=DesignParams(0.0, 0.0, 0.0),
                flow_cv=float("nan"),
                pressure_drop=float("nan"),
                converged=False,
                runtime_s=0.5,
                status="RUN_SOLVER_FAILED",
                metadata={},
            )

            db.save_batch([ok, bad], run_id="t1")
            all_rows = list(db.iter_all())
            self.assertEqual(len(all_rows), 2)

            best = db.get_best()
            self.assertIsNotNone(best)
            self.assertEqual(best.status, "OK")

            X, Y = db.load_training_data()
            self.assertEqual(int(X.shape[0]), 1)
            self.assertEqual(int(Y.shape[0]), 1)


if __name__ == "__main__":
    unittest.main()

