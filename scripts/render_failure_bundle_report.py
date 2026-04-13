from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--bundle-dir", required=True, help="Directory containing pick.json/model.json/structure.txt")
    p.add_argument("--out", default="", help="Output HTML path (default: <bundle-dir>/report.html)")
    return p.parse_args()


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return p.read_text(encoding="utf-8", errors="replace")


def _read_json(p: Path) -> dict:
    return json.loads(_read_text(p))


def _fmt(x) -> str:
    if isinstance(x, float):
        return f"{x:.6g}"
    return str(x)


def _svg_bar(values: list[tuple[str, float]], width: int = 520, height: int = 140) -> str:
    pad = 16
    bar_w = max(12, int((width - 2 * pad) / max(1, len(values)) - 10))
    max_v = max([v for _, v in values] + [1e-9])
    out = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    out.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="#d0d7de"/>')
    x = pad
    for label, v in values:
        h = int((height - 2 * pad - 18) * (v / max_v))
        y = height - pad - 18 - h
        out.append(f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" fill="#0969da" opacity="0.85"/>')
        out.append(f'<text x="{x + bar_w/2:.2f}" y="{height - pad - 4}" text-anchor="middle" font-size="12" fill="#24292f">{html.escape(label)}</text>')
        out.append(f'<text x="{x + bar_w/2:.2f}" y="{y - 4}" text-anchor="middle" font-size="12" fill="#24292f">{v:.4f}</text>')
        x += bar_w + 10
    out.append("</svg>")
    return "\n".join(out)


def _svg_segments(y_levels: list[float], width: int = 520, height: int = 110) -> str:
    pad = 16
    usable_h = height - 2 * pad
    y0 = pad
    x0 = 40
    x1 = width - pad
    out = [f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    out.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" stroke="#d0d7de"/>')
    out.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0+usable_h}" stroke="#24292f" stroke-width="1"/>')
    if not y_levels or y_levels[-1] <= 0:
        out.append("</svg>")
        return "\n".join(out)
    H = float(y_levels[-1])
    for i, y in enumerate(y_levels):
        yy = y0 + usable_h * (y / H)
        out.append(f'<line x1="{x0}" y1="{yy:.2f}" x2="{x1}" y2="{yy:.2f}" stroke="#d0d7de" stroke-width="1"/>')
        out.append(f'<text x="{x0-6}" y="{yy+4:.2f}" text-anchor="end" font-size="12" fill="#24292f">{y:.4f}</text>')
        if 0 < i < len(y_levels) - 1:
            out.append(f'<text x="{x1}" y="{yy-4:.2f}" text-anchor="end" font-size="12" fill="#0969da">split {i}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    args = _parse_args()
    bundle_dir = Path(args.bundle_dir).resolve()
    out_path = Path(args.out).resolve() if args.out else (bundle_dir / "report.html")

    pick_path = bundle_dir / "pick.json"
    model_path = bundle_dir / "model.json"
    structure_path = bundle_dir / "structure.txt"
    solver_grep_path = bundle_dir / "solver_grep.txt"

    pick = _read_json(pick_path) if pick_path.exists() else {}
    model = _read_json(model_path) if model_path.exists() else {}
    structure = _read_text(structure_path) if structure_path.exists() else ""
    solver_grep = _read_text(solver_grep_path) if solver_grep_path.exists() else ""

    logits = pick.get("logits") or []
    status = pick.get("status") or ""
    remote_case = pick.get("remote_case") or ""
    failure = pick.get("failure") or {}

    derived = model.get("derived") if isinstance(model.get("derived"), dict) else {}
    outlet_weights = derived.get("outlet_weights") if isinstance(derived.get("outlet_weights"), list) else []
    y_levels = derived.get("y_levels") if isinstance(derived.get("y_levels"), list) else []

    bar = ""
    if outlet_weights and all(isinstance(x, (int, float)) for x in outlet_weights):
        bar = _svg_bar([(f"o{i+1}", float(v)) for i, v in enumerate(outlet_weights)])
    seg = ""
    if y_levels and all(isinstance(x, (int, float)) for x in y_levels):
        seg = _svg_segments([float(x) for x in y_levels])

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"Failure Bundle Report - {bundle_dir.name}"

    def kv_table(d: dict) -> str:
        rows = []
        for k, v in d.items():
            rows.append(f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(_fmt(v))}</td></tr>")
        return "<table class='kv'>" + "\n".join(rows) + "</table>"

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,Microsoft YaHei,system-ui; margin: 24px; color: #24292f; }}
    h1 {{ font-size: 18px; margin: 0 0 6px; }}
    .meta {{ color:#57606a; font-size: 13px; margin-bottom: 14px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .card {{ border:1px solid #d0d7de; border-radius: 10px; padding: 14px; background: #fff; }}
    .kv td {{ border-bottom: 1px solid #f0f2f4; padding: 6px 8px; vertical-align: top; font-size: 13px; }}
    .kv td:first-child {{ width: 160px; color:#57606a; }}
    pre {{ white-space: pre-wrap; word-break: break-word; font-size: 12px; margin: 0; }}
    .mono {{ font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
    .full {{ grid-column: 1 / -1; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="meta">生成时间：{html.escape(now)} · bundle_dir：<span class="mono">{html.escape(str(bundle_dir))}</span></div>

  <div class="grid">
    <div class="card">
      <div style="font-weight:600; margin-bottom:8px;">基本信息</div>
      {kv_table({
        "status": status,
        "remote_case": remote_case,
        "logits": json.dumps(logits, ensure_ascii=False),
        "failure.kind": (failure.get("kind") if isinstance(failure, dict) else ""),
        "failure.stage": (failure.get("stage") if isinstance(failure, dict) else ""),
        "failure.missing": json.dumps((failure.get("missing") if isinstance(failure, dict) else None), ensure_ascii=False),
      })}
    </div>

    <div class="card">
      <div style="font-weight:600; margin-bottom:8px;">数模派生（outlet_weights）</div>
      {bar if bar else "<div style='color:#57606a;font-size:13px;'>model.json 未包含 outlet_weights</div>"}
    </div>

    <div class="card full">
      <div style="font-weight:600; margin-bottom:8px;">数模派生（y_levels 分段）</div>
      {seg if seg else "<div style='color:#57606a;font-size:13px;'>model.json 未包含 y_levels</div>"}
    </div>

    <div class="card full">
      <div style="font-weight:600; margin-bottom:8px;">网格/目录结构（structure.txt）</div>
      <pre class="mono">{html.escape(structure)}</pre>
    </div>

    <div class="card full">
      <div style="font-weight:600; margin-bottom:8px;">求解/后处理线索（solver_grep.txt）</div>
      <pre class="mono">{html.escape(solver_grep)}</pre>
    </div>
  </div>
</body>
</html>
"""

    out_path.write_text(html_doc, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()

