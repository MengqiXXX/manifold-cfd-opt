#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI


REPO = Path(__file__).resolve().parent
LLM_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8001/v1")
LLM_KEY = os.getenv("LLM_API_KEY", "dummy")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-72b")
FOAM_SRC = os.getenv("FOAM_SOURCE", "/opt/openfoam13/etc/bashrc")

TEST_DIR = REPO / "of_cases" / "test_gmsh"
ANGLE_DEG = float(os.getenv("WEDGE_ANGLE_DEG", "1.0"))


def _clean_md(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_+-]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def sh(cmd: str, cwd: Path, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
        executable="/bin/bash",
    )
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    return r.returncode, out


def ask_geo(client: OpenAI, params: dict, last_error: str | None) -> str:
    hint = ""
    if last_error:
        hint = f"\n\n上一次尝试失败日志（截断）：\n```\n{last_error[-1200:]}\n```"

    skeleton = r"""
SetFactory("OpenCASCADE");
R = ...; L = ...; r0 = R*1e-3; angle = angle_deg*Pi/180; lc = ...;

Point(1) = {r0, 0, 0, lc};
Point(2) = {R,  0, 0, lc};
Point(3) = {R,  0, L, lc};
Point(4) = {r0, 0, L, lc};

Line(1) = {1, 2}; // z=0  (coldEnd after rotation)
Line(2) = {2, 3}; // r=R  (wall)
Line(3) = {3, 4}; // z=L  (hotEnd after rotation)
Line(4) = {4, 1}; // r=r0 (axis)

Line Loop(10) = {1, 2, 3, 4};
Plane Surface(11) = {10};

Transfinite Line {1,3} = 40;
Transfinite Line {2,4} = 12;
Transfinite Surface {11};
Recombine Surface {11};

out[] = Extrude {{0,0,1}, {0,0,0}, angle} { Surface{11}; Layers{1}; Recombine; };
// out[0]=back surface, out[1]=volume, out[2..5]=lateral surfaces from lines 1..4 (order follows the loop)
Physical Surface("front")   = {11};
Physical Surface("back")    = {out[0]};
Physical Volume("fluid")    = {out[1]};
Physical Surface("coldEnd") = {out[2]};
Physical Surface("wall")    = {out[3]};
Physical Surface("hotEnd")  = {out[4]};
Physical Surface("axis")    = {out[5]};
""".strip()

    prompt = f"""
你是 Gmsh 专家。请为 OpenFOAM 13 生成一个可转换的 Gmsh .geo（OpenCASCADE）脚本，用于“涡流管”简化几何的 3D 楔形体网格。

几何与网格要求：
- 轴向为 z，径向为 x，楔向为 y，围绕 z 轴旋转生成楔形体
- 外半径 R = {params["R"]} m，长度 L = {params["L"]} m
- 楔角 = {params["angle_deg"]} deg（建议用旋转 Extrude）
- 为避免轴线退化导致的负体积：使用一个很小的内半径 r0（例如 r0 = R*1e-3），并将其命名为 axis 面
- 目标：生成单一流体体积，能用 gmshToFoam 正确识别物理面名称
- 物理分组必须包含（名字严格匹配）：
  - Physical Volume(\"fluid\")
  - Physical Surface(\"coldEnd\")  (z=0 端面)
  - Physical Surface(\"hotEnd\")   (z=L 端面)
  - Physical Surface(\"wall\")     (外壁面)
  - Physical Surface(\"axis\")     (内壁面，r=r0)
  - Physical Surface(\"front\")    (楔形面 1)
  - Physical Surface(\"back\")     (楔形面 2)
- 网格尽量使用结构化/半结构化（Transfinite + Recombine 优先），总单元数量控制在 5e4 以内
- 不要硬编码 Extrude 产生的面/体编号；必须用 out[] 返回数组来引用（更稳）
- 输出必须是纯 .geo 内容，不要解释文字，不要 Markdown

可用参数：
R={params["R"]}, L={params["L"]}, angle_deg={params["angle_deg"]}, lc={params["lc"]}
{hint}

推荐骨架（可在此基础上完善，但必须满足上面物理面命名与绕 z 轴旋转）：
{skeleton}
""".strip()

    r = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1800,
        temperature=0.1,
    )
    return _clean_md(r.choices[0].message.content or "")


def main() -> int:
    D = float(os.getenv("D", "0.025"))
    L_D = float(os.getenv("L_D", "12.0"))
    R = D / 2.0
    L = L_D * D

    params = {
        "D": D,
        "L_D": L_D,
        "R": round(R, 6),
        "L": round(L, 6),
        "angle_deg": ANGLE_DEG,
        "lc": float(os.getenv("LC", str(max(D / 30.0, 0.0005)))),
    }

    TEST_DIR.mkdir(parents=True, exist_ok=True)
    (TEST_DIR / "system").mkdir(parents=True, exist_ok=True)
    control_dict = TEST_DIR / "system" / "controlDict"
    if not control_dict.exists():
        control_dict.write_text(
            "FoamFile\n"
            "{\n"
            "    format      ascii;\n"
            "    class       dictionary;\n"
            "    location    \"system\";\n"
            "    object      controlDict;\n"
            "}\n"
            "\n"
            "application     rhoSimpleFoam;\n"
            "startFrom       startTime;\n"
            "startTime       0;\n"
            "stopAt          endTime;\n"
            "endTime         1;\n"
            "deltaT          1;\n"
            "writeControl    timeStep;\n"
            "writeInterval   1;\n"
            "purgeWrite      0;\n"
            "writeFormat     ascii;\n"
            "writePrecision  6;\n"
            "writeCompression off;\n"
            "timeFormat      general;\n"
            "timePrecision   6;\n"
            "runTimeModifiable true;\n",
            encoding="utf-8",
        )

    client = OpenAI(base_url=LLM_URL, api_key=LLM_KEY)

    last = None
    for attempt in range(3):
        geo = ask_geo(client, params, last)
        geo_path = TEST_DIR / "mesh.geo"
        geo_path.write_text(geo, encoding="utf-8")

        rc, out = sh("gmsh -3 mesh.geo -format msh2 -o mesh.msh", TEST_DIR, timeout=120)
        if rc != 0:
            last = out
            continue

        sh("rm -rf constant/polyMesh", TEST_DIR, timeout=30)
        rc2, out2 = sh(f"source {FOAM_SRC} && gmshToFoam mesh.msh", TEST_DIR, timeout=120)
        if rc2 != 0:
            last = out2
            continue

        bnd = TEST_DIR / "constant/polyMesh/boundary"
        if bnd.exists():
            btxt = bnd.read_text(encoding="utf-8", errors="replace")
            required = {"coldEnd", "hotEnd", "wall", "axis", "front", "back"}
            found = set(re.findall(r"^\s*([A-Za-z0-9_]+)\s*\n\s*\{", btxt, flags=re.M))

            face_counts = {
                m.group(1): int(m.group(2))
                for m in re.finditer(r"^\s*([A-Za-z0-9_]+)\s*\n\s*\{[\s\S]*?\bnFaces\s+(\d+);", btxt, flags=re.M)
            }
            missing = sorted(required - found)
            empty_required = sorted([k for k in required if face_counts.get(k, 0) == 0])

            if "defaultFaces" in found or missing or empty_required:
                last = "boundary issue: " + ", ".join(
                    [f"found={sorted(found)}"]
                    + ([f"missing={missing}"] if missing else [])
                    + ([f"empty={empty_required}"] if empty_required else [])
                )
                continue

        rc3, out3 = sh(f"source {FOAM_SRC} && checkMesh -allTopology -allGeometry", TEST_DIR, timeout=180)
        bad = ("FOAM FATAL" in out3) or ("Zero or negative cell volume detected" in out3)
        if rc3 != 0 or bad:
            last = out3
            continue

        print(json.dumps({"ok": True, "case": str(TEST_DIR), "params": params}, ensure_ascii=False))
        return 0

    print(json.dumps({"ok": False, "case": str(TEST_DIR), "params": params, "error": (last or "")[-2000:]}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
