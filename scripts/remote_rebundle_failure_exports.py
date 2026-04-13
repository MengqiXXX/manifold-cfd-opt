from __future__ import annotations

import argparse
import json
import math
import shlex
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.ssh import SSHConfig, ssh_exec


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.110.10")
    p.add_argument("--user", default="liumq")
    p.add_argument("--export-root", required=True)
    return p.parse_args()


def _run(cfg: SSHConfig, bash_script: str, timeout: int = 120) -> None:
    cmd = "bash -lc " + shlex.quote(bash_script)
    rc, out, err = ssh_exec(cfg, cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"rc={rc}\n{out}\n{err}")


def _softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    ex = [math.exp(x - m) for x in xs]
    s = sum(ex)
    return [v / s for v in ex]


def _model_json(logits: list[float]) -> str:
    l1, l2, l3 = (float(logits[0]), float(logits[1]), float(logits[2]))
    w = _softmax([l1, l2, l3, 0.0])
    H = 0.2
    y_levels = [0.0]
    acc = 0.0
    for wi in w:
        acc += wi
        y_levels.append(H * acc)
    return json.dumps(
        {
            "params": {"logit_1": l1, "logit_2": l2, "logit_3": l3},
            "derived": {"outlet_weights": w, "y_levels": y_levels},
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    args = _parse_args()
    cfg = SSHConfig(host=args.host, user=args.user)
    root = args.export_root.rstrip("/")

    list_cmd = f"ls -1 {shlex.quote(root)} | grep -E '^[0-9]{{6}}_' || true"
    rc, out, err = ssh_exec(cfg, "bash -lc " + shlex.quote(list_cmd), timeout=30)
    cases = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]

    for tag in cases:
        d = f"{root}/{tag}"
        rc, out, err = ssh_exec(cfg, "bash -lc " + shlex.quote(f"cat {shlex.quote(d)}/pick.json"), timeout=30)
        pick = json.loads(out)
        remote_case = pick.get("remote_case")
        logits = pick.get("logits") or [0, 0, 0]
        model = _model_json(logits)
        _run(cfg, f"printf %s {shlex.quote(model + '\\n')} > {shlex.quote(d)}/model.json", timeout=30)

        if remote_case:
            bundle = f"{d}/case_bundle.tgz"
            tar_script = (
                f"cd {shlex.quote(remote_case)}; "
                f"rm -f {shlex.quote(bundle)}; "
                f"tar -czf {shlex.quote(bundle)} system constant 0 log.* 2>/dev/null; "
                f"ls -lh {shlex.quote(bundle)}"
            )
            rc, out, err = ssh_exec(cfg, "bash -lc " + shlex.quote(tar_script), timeout=120)
    print(root)


if __name__ == "__main__":
    main()
