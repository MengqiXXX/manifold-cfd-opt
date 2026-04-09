"""
报告生成模块：将优化结果输出为 Markdown 报告。
Phase 1 用 LLM 生成分析文字；若 LLM 不可用则退回到模板报告。
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from evaluators.base import EvalResult


def generate_report(
    history: list[EvalResult],
    best: EvalResult | None,
    output_path: str | Path = "optimization_report.md",
    llm_client=None,
    llm_model: str | None = None,
) -> str:
    """生成优化报告，写入 Markdown 文件，返回文件路径字符串。"""
    output_path = Path(output_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    valid   = [r for r in history if r.is_valid()]
    invalid = [r for r in history if not r.is_valid()]
    objs    = [r.objective for r in valid] if valid else [math.nan]

    # 统计
    best_obj  = max(objs) if valid else math.nan
    worst_obj = min(objs) if valid else math.nan
    mean_obj  = sum(objs) / len(objs) if valid else math.nan

    is_manifold = bool(best and getattr(best.params, "logit_1", None) is not None) or any(
        getattr(r.params, "logit_1", None) is not None for r in history
    )

    lines = [
        f"# {'歧管' if is_manifold else '涡流管'}贝叶斯优化报告",
        f"",
        f"**生成时间**: {now}  ",
        f"**总评估点**: {len(history)}  |  "
        f"**有效**: {len(valid)}  |  "
        f"**失败**: {len(invalid)}",
        f"",
        f"---",
        f"",
        f"## 最优设计",
        f"",
    ]

    if best:
        if is_manifold:
            lines += [
                f"| 参数 | 值 |",
                f"|------|-----|",
                f"| logit_1 | {best.params.logit_1:.4f} |",
                f"| logit_2 | {best.params.logit_2:.4f} |",
                f"| logit_3 | {best.params.logit_3:.4f} |",
                f"| Flow CV | {best.flow_cv:.6f} |",
                f"| ΔP (Pa) | {best.pressure_drop:.2f} |",
                f"| 优化目标 | **{best.objective:.6f}** |",
                f"| 收敛状态 | {'是' if best.converged else '否'} |",
                f"| 计算耗时 | {best.runtime_s:.1f} s |",
                f"",
            ]
        else:
            lines += [
                f"| 参数 | 值 |",
                f"|------|-----|",
                f"| D (直径) | {best.params.D*1000:.2f} mm |",
                f"| L/D (长径比) | {best.params.L_D:.2f} |",
                f"| r_c (冷端比) | {best.params.r_c:.4f} |",
                f"| 优化目标 | **{best.objective:.4f}** |",
                f"| 收敛状态 | {'是' if best.converged else '否'} |",
                f"| 计算耗时 | {best.runtime_s:.1f} s |",
                f"",
            ]
    else:
        lines += ["_未找到有效设计点_", ""]

    lines += [
        f"## 统计摘要",
        f"",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 最优目标值 | {best_obj:.4f} |",
        f"| 最差目标值 | {worst_obj:.4f} |",
        f"| 平均目标值 | {mean_obj:.4f} |",
        f"| 成功率 | {len(valid)/max(len(history),1)*100:.1f}% |",
        f"",
        f"## 前10最优设计点",
        f"",
        f"| # | logit_1 | logit_2 | logit_3 | Flow CV | ΔP(Pa) | 目标值 | 耗时(s) |"
        if is_manifold
        else f"| # | D(mm) | L/D | r_c | 目标值 | 耗时(s) |",
        f"|---|---------|---------|---------|---------|-------|--------|---------|"
        if is_manifold
        else f"|---|-------|-----|-----|--------|---------|",
    ]

    top10 = sorted(valid, key=lambda r: r.objective, reverse=True)[:10]
    for i, r in enumerate(top10, 1):
        if is_manifold:
            lines.append(
                f"| {i} | {r.params.logit_1:.3f} | {r.params.logit_2:.3f} | {r.params.logit_3:.3f} | "
                f"{r.flow_cv:.6f} | {r.pressure_drop:.1f} | {r.objective:.6f} | {r.runtime_s:.1f} |"
            )
        else:
            lines.append(
                f"| {i} | {r.params.D*1000:.1f} | {r.params.L_D:.1f} | "
                f"{r.params.r_c:.3f} | {r.objective:.4f} | {r.runtime_s:.1f} |"
            )

    lines += [
        f"",
        f"## 失败案例统计",
        f"",
    ]
    status_counts: dict[str, int] = {}
    for r in invalid:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    if status_counts:
        lines += [f"| 状态 | 数量 |", f"|------|------|"]
        for k, v in sorted(status_counts.items()):
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("_无失败案例_")

    # LLM 生成分析（可选）
    if llm_client and llm_model and valid:
        try:
            if is_manifold and best:
                summary_prompt = (
                    "以下是一个歧管多出口分流优化实验的结果摘要，"
                    "请用中文写3-5句话分析优化趋势与最优点含义（流量均匀性与压降的权衡）：\n"
                    f"最优点: logit=[{best.params.logit_1:.3f},{best.params.logit_2:.3f},{best.params.logit_3:.3f}], "
                    f"Flow CV={best.flow_cv:.6f}, ΔP={best.pressure_drop:.1f}Pa, 目标值={best.objective:.6f}\n"
                    f"有效点数: {len(valid)}, 平均目标值: {mean_obj:.6f}"
                )
            else:
                summary_prompt = (
                    "以下是一个涡流管贝叶斯优化实验的结果摘要，"
                    "请用中文写3-5句话分析优化趋势和最优设计的物理意义：\n"
                    f"最优点: D={best.params.D*1000:.1f}mm, L/D={best.params.L_D:.1f}, "
                    f"r_c={best.params.r_c:.3f}, 目标值={best.objective:.4f}\n"
                    f"有效点数: {len(valid)}, 平均目标值: {mean_obj:.4f}"
                )
            response = llm_client.chat.completions.create(
                model=llm_model,
                max_tokens=250,
                temperature=0.2,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            analysis = response.choices[0].message.content or ""
            lines += ["", "## LLM 分析", "", analysis]
        except Exception as e:
            lines += ["", f"_LLM 分析失败: {e}_"]

    content = "\n".join(lines) + "\n"
    output_path.write_text(content, encoding="utf-8")
    return str(output_path)
