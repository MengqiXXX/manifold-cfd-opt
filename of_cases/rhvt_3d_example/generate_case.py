#!/usr/bin/env python3
"""
生成涡流管 3D OpenFOAM case（从 templates/vortex_tube_3d/ 渲染）
用法：python generate_case.py [--output /path/to/case]
依赖：pip install jinja2
参考：
  REF1 Prajit/FOSSEE, OpenFOAM 3D Vortex Tube Case Study
  REF2 Burazer et al., Thermal Science 2017, TSCI160223195B
"""
import math, shutil, argparse
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

# ─── 物理/几何参数（修改这里来生成不同工况）────────────────────────────────
PARAMS = dict(
    # 几何（单位 m）
    D      = 0.020,    # 管内径
    L      = 0.200,    # 管长 (L/D = 10)
    r_c    = 0.005,    # 冷端孔径 (r_c/R = 0.5)
    # 网格
    n_c    = 8,        # 周向格数（每外块）
    n_r    = 10,       # 径向格数（外块，向壁面加密）
    n_z    = 60,       # 轴向格数
    r_grading = 4,     # 径向加密比（壁面侧格更密）
    # 边界条件（单位：Pa, K, m/s）
    p_in   = 400000,   # 入口滞止压力 [Pa] (4 bar)
    p_cold = 100000,   # 冷端背压 [Pa] (1 bar)
    p_hot  = 100000,   # 热端背压 [Pa] (1 bar)
    T_in   = 300.0,    # 入口温度 [K]
    U_theta = 100.0,   # 切向入口速度 [m/s] (~Ma 0.29 @ 300K)
    U_axial =  30.0,   # 轴向入口速度 [m/s]
    # 湍流（REF1: 从低强度开始）
    turb_intensity = 0.05,   # 湍流强度 5%
    # 时间控制
    t_end          = 0.01,   # 模拟时长 [s]
    write_interval = 0.002,  # 写出间隔 [s]
    max_co         = 0.3,    # 最大 Courant 数（REF1 建议保守起步）
    # 并行
    n_cores = 4,
)


def compute_derived(p: dict) -> dict:
    """计算派生几何量和湍流参数"""
    R  = p["D"] / 2
    r_c = p["r_c"]
    a  = r_c / math.sqrt(2)          # 内方格半宽
    R2 = R  / math.sqrt(2)           # 外顶点投影半径

    # 湍流参数
    U_ref = p["U_theta"]
    I     = p["turb_intensity"]
    k_in  = 1.5 * (I * U_ref) ** 2
    D_h   = 2 * (R - r_c)            # 入口环形液压直径
    L_t   = 0.07 * D_h               # 湍流积分长度尺度
    omega_in = math.sqrt(k_in) / (0.09 ** 0.25 * L_t)

    return {**p, "R": R, "a": a, "R2": R2,
            "k_in": round(k_in, 2),
            "omega_in": round(omega_in, 0),
            "mix_length": round(L_t, 6)}


def render(template_dir: Path, output_dir: Path, params: dict):
    env = Environment(loader=FileSystemLoader(str(template_dir)),
                      keep_trailing_newline=True)
    for tmpl_path in sorted(template_dir.rglob("*.j2")):
        rel = tmpl_path.relative_to(template_dir)
        out = output_dir / rel.with_suffix("")   # 去掉 .j2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(env.get_template(str(rel)).render(**params))
        print(f"  rendered → {out.relative_to(output_dir)}")

    # 复制非模板文件
    for f in sorted(template_dir.rglob("*")):
        if f.is_file() and not f.suffix == ".j2":
            rel = f.relative_to(template_dir)
            dst = output_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            print(f"  copied  → {dst.relative_to(output_dir)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=str(Path(__file__).parent))
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent.parent
    tmpl = repo / "templates" / "vortex_tube_3d"
    out  = Path(args.output)
    params = compute_derived(PARAMS)

    print(f"渲染 3D 涡流管 case → {out}")
    print(f"  R={params['R']*1000:.1f}mm  L={params['L']*1000:.0f}mm  "
          f"r_c={params['r_c']*1000:.1f}mm  "
          f"p_in={params['p_in']/1e5:.1f}bar  T={params['T_in']}K")
    render(tmpl, out, params)
    print("完成。运行步骤：")
    print(f"  cd {out}")
    print("  blockMesh && checkMesh")
    print("  decomposePar")
    print("  mpirun -np 4 rhoPimpleFoam -parallel | tee log.rhoPimpleFoam")
    print("  reconstructPar")
