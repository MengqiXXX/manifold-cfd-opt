from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluators.base import DesignParams
from evaluators.remote_openfoam_evaluator import _derive_mesh_params_3d, _render_template_dir


def _triangulate_quad(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float], d: tuple[float, float, float]):
    return [(a, b, c), (a, c, d)]


def _normal(tri: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]):
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
    ux, uy, uz = (bx - ax, by - ay, bz - az)
    vx, vy, vz = (cx - ax, cy - ay, cz - az)
    nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    n = math.sqrt(nx * nx + ny * ny + nz * nz)
    if n <= 0:
        return (0.0, 0.0, 0.0)
    return (nx / n, ny / n, nz / n)


def _write_ascii_stl(path: Path, tris: list[tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]], name: str = "manifold"):
    lines: list[str] = [f"solid {name}"]
    for tri in tris:
        nx, ny, nz = _normal(tri)
        lines.append(f"  facet normal {nx:.8e} {ny:.8e} {nz:.8e}")
        lines.append("    outer loop")
        for vx, vy, vz in tri:
            lines.append(f"      vertex {vx:.10g} {vy:.10g} {vz:.10g}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_obj(path: Path, faces: list[list[tuple[float, float, float]]], outlet_face_lines: list[tuple[tuple[float, float, float], tuple[float, float, float]]]):
    verts: list[tuple[float, float, float]] = []
    idx: dict[tuple[float, float, float], int] = {}

    def add_v(v: tuple[float, float, float]) -> int:
        if v in idx:
            return idx[v]
        verts.append(v)
        idx[v] = len(verts)
        return idx[v]

    face_vids: list[list[int]] = []
    for f in faces:
        face_vids.append([add_v(v) for v in f])

    line_vids: list[tuple[int, int]] = []
    for a, b in outlet_face_lines:
        line_vids.append((add_v(a), add_v(b)))

    out: list[str] = []
    out.append("o manifold")
    for x, y, z in verts:
        out.append(f"v {x:.10g} {y:.10g} {z:.10g}")
    out.append("g shell")
    for vids in face_vids:
        out.append("f " + " ".join(str(i) for i in vids))
    if line_vids:
        out.append("g outlet_partitions")
        for i, j in line_vids:
            out.append(f"l {i} {j}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _box_geometry(L: float, H: float, W: float):
    x0, x1 = 0.0, float(L)
    y0, y1 = 0.0, float(H)
    z0, z1 = 0.0, float(W)
    v000 = (x0, y0, z0)
    v100 = (x1, y0, z0)
    v110 = (x1, y1, z0)
    v010 = (x0, y1, z0)
    v001 = (x0, y0, z1)
    v101 = (x1, y0, z1)
    v111 = (x1, y1, z1)
    v011 = (x0, y1, z1)
    faces = [
        [v000, v100, v110, v010],
        [v001, v011, v111, v101],
        [v000, v010, v011, v001],
        [v100, v101, v111, v110],
        [v000, v001, v101, v100],
        [v010, v110, v111, v011],
    ]
    return faces


def _outlet_partition_lines(L: float, y_levels: list[float], W: float) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    x = float(L)
    z0, z1 = 0.0, float(W)
    lines: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for y in y_levels[1:-1]:
        y = float(y)
        lines.append(((x, y, z0), (x, y, z1)))
    return lines


def _dump_summary(path: Path, params: DesignParams, ctx: dict):
    d = {
        "params": {"logit_1": params.logit_1, "logit_2": params.logit_2, "logit_3": params.logit_3},
        "derived": {
            "L": ctx.get("L"),
            "H": ctx.get("H"),
            "W": ctx.get("W", ctx.get("thickness")),
            "thickness": ctx.get("thickness"),
            "outlet_count": ctx.get("outlet_count"),
            "outlet_weights": ctx.get("outlet_weights"),
            "y_levels": ctx.get("y_levels"),
            "n_cells_x": ctx.get("n_cells_x"),
            "n_cells_y": ctx.get("n_cells_y"),
            "n_cells_z": ctx.get("n_cells_z"),
        },
    }
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_args():
    p = argparse.ArgumentParser(description="Generate manifold 3D numerical model from optimization params (logits).")
    p.add_argument("--logit1", type=float, required=True)
    p.add_argument("--logit2", type=float, required=True)
    p.add_argument("--logit3", type=float, required=True)
    p.add_argument("--n-cores", type=int, default=8)
    p.add_argument("--out", type=str, default="")
    p.add_argument("--template-dir", type=str, default="templates/manifold_3d")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    params = DesignParams(args.logit1, args.logit2, args.logit3)

    ctx = _derive_mesh_params_3d(params, outlet_count=4)
    ctx["n_cores"] = int(args.n_cores)
    template_dir = (Path(__file__).resolve().parents[1] / args.template_dir).resolve()
    if not template_dir.exists():
        raise SystemExit(f"template_dir not found: {template_dir}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out).resolve() if args.out else (Path.cwd() / f"manifold_3d_model_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    case_dir = out_dir / "case"
    case_dir.mkdir(parents=True, exist_ok=True)
    _render_template_dir(template_dir, case_dir, ctx)

    W = float(ctx.get("W", ctx.get("thickness", 0.1)))
    faces = _box_geometry(L=float(ctx["L"]), H=float(ctx["H"]), W=W)
    lines = _outlet_partition_lines(L=float(ctx["L"]), y_levels=list(ctx["y_levels"]), W=W)
    _write_obj(out_dir / "model.obj", faces, lines)

    tris: list[tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]] = []
    for f in faces:
        tris.extend(_triangulate_quad(f[0], f[1], f[2], f[3]))
    _write_ascii_stl(out_dir / "model.stl", tris, name="manifold")

    _dump_summary(out_dir / "model.json", params, ctx)
    (out_dir / "README.txt").write_text(
        "\n".join(
            [
                "Generated artifacts:",
                "- case/: OpenFOAM case rendered from templates/manifold_3d",
                "- model.obj: visualization geometry (box + outlet partition lines on outlet face)",
                "- model.stl: visualization geometry (outer box only)",
                "- model.json: derived parameters (outlet_weights, y_levels, mesh counts)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(str(out_dir))


if __name__ == "__main__":
    main()
