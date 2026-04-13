from __future__ import annotations

import argparse
from pathlib import Path


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--foam", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--size", default="1400x800")
    return p.parse_args()


def _render(foam_path: Path, out_dir: Path, size: tuple[int, int]) -> None:
    from paraview.simple import (
        ColorBy,
        GetActiveViewOrCreate,
        GetColorTransferFunction,
        GetOpacityTransferFunction,
        GetScalarBar,
        OpenFOAMReader,
        ResetCamera,
        SaveScreenshot,
        Show,
        Slice,
        UpdatePipeline,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    r = OpenFOAMReader(FileName=str(foam_path))
    r.MeshRegions = ["internalMesh"]
    r.CellArrays = ["U", "p"]
    UpdatePipeline(proxy=r)

    view = GetActiveViewOrCreate("RenderView")
    view.ViewSize = [int(size[0]), int(size[1])]
    view.Background = [0.06, 0.07, 0.09]

    sl = Slice(Input=r)
    sl.SliceType = "Plane"
    sl.SliceType.Origin = [0.5, 0.1, 0.0]
    sl.SliceType.Normal = [0.0, 0.0, 1.0]
    UpdatePipeline(proxy=sl)

    disp = Show(sl, view)
    disp.Representation = "Surface"

    ResetCamera(view)

    ColorBy(disp, ("POINTS", "U", "Magnitude"))
    lut_u = GetColorTransferFunction("U")
    pwf_u = GetOpacityTransferFunction("U")
    lut_u.RescaleTransferFunctionToDataRange(True, False)
    pwf_u.RescaleTransferFunctionToDataRange(True, False)
    sb_u = GetScalarBar(lut_u, view)
    sb_u.Title = "|U|"
    sb_u.ComponentTitle = ""
    SaveScreenshot(str(out_dir / "velocity.png"), view, ImageResolution=[int(size[0]), int(size[1])])

    ColorBy(disp, ("POINTS", "p"))
    lut_p = GetColorTransferFunction("p")
    pwf_p = GetOpacityTransferFunction("p")
    lut_p.RescaleTransferFunctionToDataRange(True, False)
    pwf_p.RescaleTransferFunctionToDataRange(True, False)
    sb_p = GetScalarBar(lut_p, view)
    sb_p.Title = "p"
    sb_p.ComponentTitle = ""
    SaveScreenshot(str(out_dir / "pressure.png"), view, ImageResolution=[int(size[0]), int(size[1])])


def main() -> None:
    args = _parse()
    foam = Path(args.foam).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    w, h = (int(x) for x in str(args.size).lower().split("x", 1))
    _render(foam, out, (w, h))


if __name__ == "__main__":
    main()

