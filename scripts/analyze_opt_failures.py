from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FailureSample:
    status: str
    logit_1: float
    logit_2: float
    logit_3: float
    remote_case: str | None
    failure: dict | None
    postprocess: dict | None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=2000)
    return p.parse_args()


def _load_samples(db_path: Path, limit: int) -> list[FailureSample]:
    con = sqlite3.connect(str(db_path))
    rows = con.execute(
        "select logit_1,logit_2,logit_3,status,metadata from results where converged=0 limit ?",
        (int(limit),),
    ).fetchall()
    con.close()
    out: list[FailureSample] = []
    for logit_1, logit_2, logit_3, status, meta_s in rows:
        try:
            md = json.loads(meta_s) if meta_s else {}
        except Exception:
            md = {}
        out.append(
            FailureSample(
                status=str(status),
                logit_1=float(logit_1),
                logit_2=float(logit_2),
                logit_3=float(logit_3),
                remote_case=md.get("remote_case"),
                failure=md.get("failure") if isinstance(md.get("failure"), dict) else None,
                postprocess=md.get("postprocess") if isinstance(md.get("postprocess"), dict) else None,
            )
        )
    return out


def main() -> None:
    args = _parse_args()
    db = Path(args.db)
    samples = _load_samples(db, limit=args.limit)

    by_status = Counter(s.status for s in samples)
    print("failure_status_counts:")
    for k, v in by_status.most_common():
        print(f"  {k}: {v}")

    missing_counter = Counter()
    post_key_counter = Counter()
    post_status_counter = Counter()
    post_diag_counter = Counter()

    for s in samples:
        if s.failure and isinstance(s.failure.get("missing"), list):
            for m in s.failure["missing"]:
                missing_counter[str(m)] += 1
        if s.postprocess:
            for k, v in s.postprocess.items():
                post_key_counter[str(k)] += 1
                if isinstance(v, dict):
                    post_status_counter[f"{k}:{v.get('status')}"] += 1
                    diag = v.get("diag")
                    if diag:
                        post_diag_counter[f"{k}:{diag}"] += 1

    if missing_counter:
        print("missing_fields_top:")
        for k, v in missing_counter.most_common(20):
            print(f"  {k}: {v}")

    if post_status_counter:
        print("postprocess_status_top:")
        for k, v in post_status_counter.most_common(20):
            print(f"  {k}: {v}")

    if post_diag_counter:
        print("postprocess_diag_top:")
        for k, v in post_diag_counter.most_common(20):
            print(f"  {k}: {v}")

    if samples:
        print("example_rows:")
        for s in samples[:5]:
            print(
                json.dumps(
                    {
                        "status": s.status,
                        "params": [s.logit_1, s.logit_2, s.logit_3],
                        "remote_case": s.remote_case,
                        "failure": s.failure,
                    },
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    main()

