#!/usr/bin/env python3
"""Qwen 逐文件生成 3D 涡流管 OpenFOAM 模板。在服务器 192.168.110.10 上运行。
每次 API 调用 < 2000 tokens，适配 vLLM max-model-len=8192。
"""
import re, subprocess, sys
from pathlib import Path
from openai import OpenAI

LLM   = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="dummy")
MODEL = "qwen2.5-72b"
OF13  = "/opt/openfoam13/etc/bashrc"
TMPL  = Path("/home/liumq/vortex_opt/templates/vortex_tube_3d")


def ask(prompt: str, max_tokens: int = 1800) -> str:
    r = LLM.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens, temperature=0.1,
    )
    return r.choices[0].message.content.strip()


def shell(cmd, cwd="/home/liumq", timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       cwd=cwd, timeout=timeout, executable="/bin/bash")
    return r.returncode, r.stdout, r.stderr


def clean(text):
    """Strip markdown code fences."""
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text.strip())
    return text


COMMON = (
    "OpenFOAM 13, 3D vortex tube (Ranque-Hilsch). "
    "Jinja2 template vars: {{ D }} tube diameter(m), {{ R }}=D/2, {{ L }}=L_D*D tube length, "
    "{{ r_c }} cold outlet inner radius(m), {{ n_cores }} MPI procs, {{ max_iter }} iterations, "
    "{{ n_r }} radial cells, {{ n_z }} axial cells. "
    "Air, compressible, steady. Inlet: p=500000Pa T=300K tangential U=100m/s. "
    "Cold/hot outlet: p=101325Pa. Solver: rhoSimpleFoam. "
    "Output ONLY the file content with FoamFile header. No explanation."
)

# Each entry: (relative_path, specific_instructions)
FILES = [
    ("system/blockMeshDict",
     "3D cylindrical O-grid blocking: cylinder radius {{ R }}, length {{ L }}. "
     "Patches: inlet (tangential slit mid-wall), coldEnd (z=0 r<{{ r_c }}), "
     "hotEnd (z={{ L }} outer ring), wall (outer cylinder), axis (r=0). "
     "Use n_r radial and n_z axial cells. Include merge and grading."),

    ("system/controlDict",
     "rhoSimpleFoam steady. startTime 0, endTime {{ max_iter }}, deltaT 1, "
     "writeControl timeStep, writeInterval {{ (max_iter // 5)|int }}, purgeWrite 2. "
     "Functions: surfaceFieldValue for coldEnd/hotEnd/inlet averaging T and p; "
     "solverInfo for p U T k omega residuals every 50 steps."),

    ("system/fvSchemes",
     "rhoSimpleFoam compressible steady. "
     "ddtSchemes: steadyState. gradSchemes: Gauss linear. "
     "divSchemes: Gauss linearUpwind grad(U) for div(phi,U), Gauss upwind for others. "
     "laplacianSchemes: Gauss linear corrected. interpolationSchemes: linear."),

    ("system/fvSolution",
     "SIMPLE solver block. p: PCG GAMG tolerance 1e-6. U h k omega: PBiCGStab DILU tolerance 1e-6. "
     "SIMPLE: nNonOrthogonalCorrectors 2. "
     "relaxationFactors: p 0.3, U 0.7, h 0.5, k 0.5, omega 0.5."),

    ("system/decomposeParDict",
     "numberOfSubdomains {{ n_cores }}. method simple. "
     "simpleCoeffs: n (1 1 {{ n_cores }}), delta 0.001."),

    ("constant/thermophysicalProperties",
     "hePsiThermo, pureMixture, sutherland transport, janaf thermo, perfectGas EOS, specie. "
     "molWeight 28.9, standard JANAF air coefficients, Sutherland: As=1.458e-06 Ts=110.4."),

    ("constant/turbulenceProperties",
     "simulationType RAS; RAS { RASModel kOmegaSST; turbulence on; printCoeffs on; }"),

    ("0/p",
     "dimensions [1 -1 -2 0 0 0 0]. internalField uniform 300000. "
     "inlet: zeroGradient. coldEnd: fixedValue 101325. hotEnd: fixedValue 101325. "
     "wall: zeroGradient. axis: empty."),

    ("0/T",
     "dimensions [0 0 0 1 0 0 0]. internalField uniform 300. "
     "inlet: fixedValue 300. coldEnd: inletOutlet inletValue 300. "
     "hotEnd: inletOutlet inletValue 300. wall: zeroGradient. axis: empty."),

    ("0/U",
     "dimensions [0 1 -1 0 0 0 0]. internalField uniform (0 0 0). "
     "inlet: fixedValue (0 100 0) representing tangential jet. "
     "coldEnd: pressureInletOutletVelocity (0 0 0). "
     "hotEnd: pressureInletOutletVelocity (0 0 0). "
     "wall: noSlip. axis: empty."),

    ("0/k",
     "dimensions [0 2 -2 0 0 0 0]. internalField uniform 1.0. "
     "inlet: fixedValue 1.0. coldEnd/hotEnd: inletOutlet 1.0. "
     "wall: kqRWallFunction 1.0. axis: empty."),

    ("0/omega",
     "dimensions [0 0 -1 0 0 0 0]. internalField uniform 1000. "
     "inlet: fixedValue 1000. coldEnd/hotEnd: inletOutlet 1000. "
     "wall: omegaWallFunction 1000. axis: empty."),
]

TMPL.mkdir(parents=True, exist_ok=True)

print("Generating OpenFOAM files with Qwen...", flush=True)
for fname, spec in FILES:
    print(f"  {fname}...", end=" ", flush=True)
    prompt = f"{COMMON}\n\nFile: {fname}\n{spec}"
    try:
        content = clean(ask(prompt))
        dst = TMPL / (fname + ".j2")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        print(f"OK ({len(content)} chars)", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", flush=True)

print("\nAll files written. Testing blockMesh...", flush=True)

# Render test case
from jinja2 import Environment, FileSystemLoader
D, L_D, r_c_ratio = 0.025, 12.0, 0.30
R = D / 2; L = L_D * D; r_c = r_c_ratio * R
ctx = dict(D=D, R=R, L=L, r_c=r_c, r_c_ratio=r_c_ratio,
           n_r=20, n_z=60, n_cores=8, max_iter=50,
           T_in=300.0, p_in=500000.0, p_cold=101325.0,
           U_theta=100.0, k_in=1.0, omega_in=1000.0)

env_j = Environment(loader=FileSystemLoader(str(TMPL)), keep_trailing_newline=True)
test = Path("/home/liumq/vortex_opt/of_cases/test_3d")
test.mkdir(parents=True, exist_ok=True)

for tf in TMPL.rglob("*.j2"):
    rel = tf.relative_to(TMPL)
    dst = test / rel.with_suffix("")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.write_text(env_j.get_template(str(rel).replace("\\", "/")).render(**ctx))
    except Exception as e:
        print(f"  render error {rel}: {e}", flush=True)

rc, out, err = shell(f"source {OF13} && blockMesh", cwd=str(test))
if rc == 0:
    cells = re.search(r"nCells:\s*(\d+)", out)
    print(f"blockMesh OK — {cells.group(1) if cells else '?'} cells", flush=True)
else:
    combined = (out + err)[-2000:]
    print(f"blockMesh FAILED:\n{combined[-800:]}", flush=True)
    print("\nQwen fixing blockMeshDict...", flush=True)
    bmd_path = test / "system/blockMeshDict"
    bmd_text = bmd_path.read_text()[:1500] if bmd_path.exists() else "(missing)"
    fix = clean(ask(
        f"Fix this OpenFOAM 13 blockMeshDict for a 3D cylinder D={D}m L={L}m.\n"
        f"Error:\n{combined[-600:]}\n\nCurrent blockMeshDict:\n{bmd_text}\n\n"
        "Output ONLY the corrected blockMeshDict content."
    ))
    (TMPL / "system/blockMeshDict.j2").write_text(fix)
    (test / "system/blockMeshDict").write_text(
        re.sub(r"\{\{.*?\}\}", "0.025", fix)
    )
    rc2, out2, err2 = shell(f"source {OF13} && blockMesh", cwd=str(test))
    cells2 = re.search(r"nCells:\s*(\d+)", out2)
    if rc2 == 0 and cells2:
        print(f"After Qwen fix: OK — {cells2.group(1)} cells", flush=True)
    else:
        print(f"Still failed:\n{(out2+err2)[-500:]}", flush=True)

print("\nSetup complete.")
print("Start optimizer: screen -dmS optimizer bash -c 'cd ~/vortex_opt && python3 run_server_agent.py'")
