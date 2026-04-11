"""
结果数据库：SQLite 存储所有设计点评估结果。

无外部依赖，使用 Python 标准库 sqlite3。
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Iterator

import torch

from evaluators.base import DesignParams, EvalResult

_PARAM_MINS = [-2.0, -2.0, -2.0]
_PARAM_MAXS = [2.0, 2.0, 2.0]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    logit_1       REAL    NOT NULL,
    logit_2       REAL    NOT NULL,
    logit_3       REAL    NOT NULL,
    flow_cv       REAL,
    pressure_drop REAL    DEFAULT 0,
    objective     REAL,
    converged     INTEGER DEFAULT 0,
    runtime_s     REAL    DEFAULT 0,
    status        TEXT    DEFAULT 'OK',
    metadata      TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


class ResultDatabase:
    def __init__(self, db_path: str | Path = "results.sqlite"):
        self.db_path = Path(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.execute(_CREATE_SQL)
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(results)").fetchall()}
            if "metadata" not in cols:
                conn.execute("ALTER TABLE results ADD COLUMN metadata TEXT")
            conn.commit()
        finally:
            conn.close()

    def save_batch(self, results: list[EvalResult], run_id: str = "") -> None:
        rows = [
            (
                run_id,
                r.params.logit_1,
                r.params.logit_2,
                r.params.logit_3,
                r.flow_cv,
                r.pressure_drop,
                (r.objective if math.isfinite(float(r.objective)) else None),
                int(r.converged),
                r.runtime_s,
                r.status,
                json.dumps(r.metadata or {}, ensure_ascii=False),
            )
            for r in results
        ]
        conn = self._conn()
        try:
            conn.executemany(
                """INSERT INTO results
                   (run_id, logit_1, logit_2, logit_3, flow_cv,
                    pressure_drop, objective, converged, runtime_s, status, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def load_training_data(self) -> tuple[torch.Tensor, torch.Tensor]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT logit_1, logit_2, logit_3, objective
                   FROM results
                   WHERE converged=1 AND status='OK'
                     AND objective IS NOT NULL
                     AND objective = objective"""
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return (
                torch.empty(0, 3, dtype=torch.double),
                torch.empty(0, 1, dtype=torch.double),
            )

        X_raw = torch.tensor([[r["logit_1"], r["logit_2"], r["logit_3"]] for r in rows], dtype=torch.double)
        Y_raw = torch.tensor([[r["objective"]] for r in rows], dtype=torch.double)

        mins = torch.tensor(_PARAM_MINS, dtype=torch.double)
        maxs = torch.tensor(_PARAM_MAXS, dtype=torch.double)
        X_norm = (X_raw - mins) / (maxs - mins)
        return X_norm, Y_raw

    def count(self) -> int:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        finally:
            conn.close()

    def count_valid(self) -> int:
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM results WHERE converged=1 AND status='OK' AND objective IS NOT NULL AND objective = objective"
            ).fetchone()[0]
        finally:
            conn.close()

    def iter_all(self, run_id: str | None = None, limit: int | None = None) -> Iterator[EvalResult]:
        sql = "SELECT * FROM results"
        args: list[object] = []
        if run_id:
            sql += " WHERE run_id=?"
            args.append(run_id)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        conn = self._conn()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        for row in rows:
            yield self._row_to_eval(row)

    def load_all(self, run_id: str | None = None, limit: int | None = None) -> list[EvalResult]:
        return list(self.iter_all(run_id=run_id, limit=limit))

    def _row_to_eval(self, row: sqlite3.Row) -> EvalResult:
        metadata: dict = {}
        try:
            if row["metadata"]:
                metadata = json.loads(row["metadata"])
        except Exception:
            metadata = {}
        return EvalResult(
            params=DesignParams(logit_1=row["logit_1"], logit_2=row["logit_2"], logit_3=row["logit_3"]),
            flow_cv=row["flow_cv"] if row["flow_cv"] is not None else float("nan"),
            pressure_drop=row["pressure_drop"],
            converged=bool(row["converged"]),
            runtime_s=row["runtime_s"],
            status=row["status"],
            metadata=metadata,
        )

    def get_best(self) -> EvalResult | None:
        conn = self._conn()
        try:
            row = conn.execute(
                """SELECT *
                   FROM results
                   WHERE converged=1 AND status='OK' AND objective IS NOT NULL AND objective = objective
                   ORDER BY objective DESC
                   LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return self._row_to_eval(row)

    def export_csv(self, csv_path: str | Path = "results.csv") -> None:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM results ORDER BY id").fetchall()
        finally:
            conn.close()

        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys() if rows else [])
            for r in rows:
                writer.writerow(list(r))
