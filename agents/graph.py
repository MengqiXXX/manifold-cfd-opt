"""
LangGraph 优化编排图。

图结构：
  START → orchestrator_node
    ├─(suggest)→ evaluate_node → save_node → anomaly_node
    │               ├─(anomaly)→ notify_node → [等待确认] → orchestrator_node
    │               └─(ok)→ orchestrator_node
    └─(done)→ report_node → END
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, TypedDict

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluators.base import DesignParams, EvalResult, Evaluator
from optimization.bayesian import BayesianOptimizer
from storage.database import ResultDatabase

from .anomaly import AnomalyDetector
from .report import generate_report


# ──────────────────────────────────────────────
# 状态定义
# ──────────────────────────────────────────────

class OptState(TypedDict):
    iteration:           int
    total_budget:        int
    batch_size:          int
    current_best:        EvalResult | None
    pending_params:      list[DesignParams]
    last_results:        list[EvalResult]
    history:             list[EvalResult]
    anomaly_flag:        bool
    anomaly_reason:      str
    human_confirmed:     bool
    convergence_count:   int
    convergence_tol:     float
    convergence_patience: int
    done:                bool
    report_path:         str


# ──────────────────────────────────────────────
# 图节点
# ──────────────────────────────────────────────

def make_orchestrator_node(optimizer: BayesianOptimizer):
    """决策节点：决定下一步行动。"""
    def orchestrator_node(state: OptState) -> OptState:
        s = dict(state)

        # 检查是否已完成
        total_evaluated = len(s["history"])
        if total_evaluated >= s["total_budget"] or s["done"]:
            s["done"] = True
            return s

        # 检查收敛
        if s["current_best"] is not None and len(s["history"]) > s["batch_size"]:
            prev_objs = [r.objective for r in s["history"][:-s["batch_size"]] if r.is_valid()]
            curr_objs = [r.objective for r in s["history"][-s["batch_size"]:] if r.is_valid()]
            if prev_objs and curr_objs:
                prev_best = max(prev_objs)
                curr_best = max(curr_objs)
                if curr_best - prev_best < s["convergence_tol"]:
                    s["convergence_count"] += 1
                    if s["convergence_count"] >= s["convergence_patience"]:
                        print(f"  [Agent] 收敛：连续 {s['convergence_count']} 轮改善 < {s['convergence_tol']}")
                        s["done"] = True
                        return s
                else:
                    s["convergence_count"] = 0

        # 推荐下一批
        print(f"  [Agent] 第 {s['iteration']+1} 轮，已评估 {total_evaluated}/{s['total_budget']}")
        next_params = optimizer.suggest_next_batch()
        s["pending_params"] = next_params
        s["iteration"] += 1
        return s

    return orchestrator_node


def make_evaluate_node(evaluator: Evaluator, db: ResultDatabase):
    """评估节点：并发运行 CFD/Java。"""
    def evaluate_node(state: OptState) -> OptState:
        s = dict(state)
        params = s["pending_params"]
        if not params:
            return s

        print(f"  [Agent] 评估 {len(params)} 个设计点...")
        results = evaluator.evaluate_batch(params)
        s["last_results"] = results
        s["history"] = s["history"] + results

        try:
            run_id = f"agent_{int(s.get('iteration', 0)):03d}"
        except Exception:
            run_id = "agent"
        db.save_batch(results, run_id=run_id)

        # 更新最优
        valid = [r for r in results if r.is_valid()]
        if valid:
            best_new = max(valid, key=lambda r: r.objective)
            if s["current_best"] is None or best_new.objective > s["current_best"].objective:
                s["current_best"] = best_new
                print(f"  [Agent] 新最优: {best_new.params!r}  obj={best_new.objective:.4f}")

        s["pending_params"] = []
        return s

    return evaluate_node


def make_anomaly_node(detector: AnomalyDetector):
    """异常检测节点。"""
    def anomaly_node(state: OptState) -> OptState:
        s = dict(state)
        is_anomaly, reason = detector.check(s["last_results"], s["history"])
        s["anomaly_flag"]   = is_anomaly
        s["anomaly_reason"] = reason
        if is_anomaly:
            print(f"  [Agent] ⚠ 异常检测: {reason}")
        return s

    return anomaly_node


def notify_node(state: OptState) -> OptState:
    """通知节点：打印异常信息（Phase 1 扩展为微信/邮件推送）。"""
    s = dict(state)
    print(f"\n{'='*50}")
    print(f"  [通知] 检测到异常，优化已暂停")
    print(f"  原因: {s['anomaly_reason']}")
    print(f"  请检查后在终端输入 'y' 确认继续，或 'n' 停止:")
    print(f"{'='*50}")
    try:
        if not sys.stdin.isatty():
            s["human_confirmed"] = True
        else:
            ans = input("继续? [y/n]: ").strip().lower()
            s["human_confirmed"] = (ans == "y")
    except EOFError:
        s["human_confirmed"] = True
    if not s["human_confirmed"]:
        s["done"] = True
    s["anomaly_flag"] = False  # 重置
    return s


def make_report_node(db: ResultDatabase, report_path: str, llm_client=None, llm_model: str | None = None):
    """报告节点：生成最终 Markdown 报告。"""
    def report_node(state: OptState) -> OptState:
        s = dict(state)
        path = generate_report(
            history=s["history"],
            best=s["current_best"],
            output_path=report_path,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        s["report_path"] = path
        print(f"  [Agent] 报告已生成: {path}")
        return s

    return report_node


# ──────────────────────────────────────────────
# 路由函数
# ──────────────────────────────────────────────

def route_after_orchestrator(state: OptState) -> Literal["evaluate_node", "report_node"]:
    if state["done"]:
        return "report_node"
    return "evaluate_node"


def route_after_anomaly(state: OptState) -> Literal["notify_node", "orchestrator_node"]:
    if state["anomaly_flag"]:
        return "notify_node"
    return "orchestrator_node"


def route_after_notify(state: OptState) -> Literal["orchestrator_node", "report_node"]:
    if state["done"]:
        return "report_node"
    return "orchestrator_node"


# ──────────────────────────────────────────────
# 构建图
# ──────────────────────────────────────────────

def build_opt_graph(
    evaluator: Evaluator,
    optimizer: BayesianOptimizer,
    db: ResultDatabase,
    cfg: dict,
    llm_client=None,
):
    """构建并返回可运行的 LangGraph StateGraph。"""
    from langgraph.graph import StateGraph, END

    detector = AnomalyDetector(
        diverge_streak=cfg.get("anomaly_diverge_streak", 3),
        jump_sigma=cfg.get("anomaly_jump_sigma", 3.0),
    )
    report_path = cfg.get("report_path", "optimization_report.md")
    llm_model = cfg.get("llm_model")

    # 绑定依赖
    orchestrator = make_orchestrator_node(optimizer)
    evaluate     = make_evaluate_node(evaluator, db)
    anomaly      = make_anomaly_node(detector)
    report       = make_report_node(db, report_path, llm_client, llm_model)

    graph = StateGraph(OptState)
    graph.add_node("orchestrator_node", orchestrator)
    graph.add_node("evaluate_node",     evaluate)
    graph.add_node("anomaly_node",      anomaly)
    graph.add_node("notify_node",       notify_node)
    graph.add_node("report_node",       report)

    graph.set_entry_point("orchestrator_node")

    graph.add_conditional_edges(
        "orchestrator_node",
        route_after_orchestrator,
        {"evaluate_node": "evaluate_node", "report_node": "report_node"},
    )
    graph.add_edge("evaluate_node", "anomaly_node")
    graph.add_conditional_edges(
        "anomaly_node",
        route_after_anomaly,
        {"notify_node": "notify_node", "orchestrator_node": "orchestrator_node"},
    )
    graph.add_conditional_edges(
        "notify_node",
        route_after_notify,
        {"orchestrator_node": "orchestrator_node", "report_node": "report_node"},
    )
    graph.add_edge("report_node", END)

    return graph.compile()


def make_initial_state(cfg: dict) -> OptState:
    batch_size = cfg.get("batch_size", 8)
    n_initial = cfg.get("n_initial", 0)
    n_iterations = cfg.get("n_iterations")
    budget = cfg.get("budget")
    if budget is None and n_iterations is not None:
        budget = int(n_initial) + int(n_iterations) * int(batch_size)
    return OptState(
        iteration=0,
        total_budget=int(budget if budget is not None else 200),
        batch_size=int(batch_size),
        current_best=None,
        pending_params=[],
        last_results=[],
        history=[],
        anomaly_flag=False,
        anomaly_reason="",
        human_confirmed=False,
        convergence_count=0,
        convergence_tol=cfg.get("convergence_tol", 0.001),
        convergence_patience=cfg.get("convergence_patience", 3),
        done=False,
        report_path="",
    )
