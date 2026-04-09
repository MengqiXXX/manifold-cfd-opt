# 多Agent CFD优化系统 — 设计规格文档

**日期**: 2026-04-04
**版本**: v1.0
**策略**: 方案B — 纵向切片优先（MVP闭环 → 逐层升级）

---

## 1. 背景与目标

### 硬件资源
- GPU: 4× RTX 5090（128 GB VRAM 总计）
- CPU: 2× AMD EPYC 9754（512 线程）
- 操作系统: Linux（服务器端）

### 研究方向
热管 · 涡流管 · 高效传热结构，通过 CFD 仿真驱动几何参数/形状优化。

### 最终目标
构建多 Agent 协作系统，自动化"提交算例 → 评估结果 → 更新参数 → 再提交"优化闭环，支持：
- 阶段一：贝叶斯优化（少量连续参数，BoTorch）
- 阶段二：形状优化（高维，NSGA-II / CMA-ES + 神经网络代理）

---

## 2. 整体架构（三层）

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 决策层（GPU · LLM Agent · Qwen2.5-72B）           │
│  OrchestratorAgent → SurrogateAgent → AnomalyDetectorAgent  │
└─────────────────────────────────────────────────────────────┘
           ↓ 参数批次                        ↑ 结果/异常
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: 执行层（CPU · 评估器 · 自适应并发）               │
│  Phase 0: VortexTube.jar（Java，秒级）                       │
│  Phase 1: OpenFOAM rhoSimpleFoam（分钟级，MPI并行）          │
└─────────────────────────────────────────────────────────────┘
           ↓ 原始结果
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: 数据层（结果存储 · 代理模型 · 报告）              │
│  results.sqlite → GP / NN Surrogate → ReportAgent           │
└─────────────────────────────────────────────────────────────┘
```

**关键设计决策**：执行层通过统一的 `Evaluator` 接口与上层解耦，Phase 0 到 Phase 1 只需替换 `Evaluator` 实现，BO 循环和 Agent 层代码不变。

---

## 3. 两阶段交付计划

### Phase 0 — MVP 闭环（目标：第1周出优化结果）

**范围**：
- 单 Python 脚本 `run_optimizer.py` 驱动
- 无 LLM，无 Agent 框架，无 Redis
- 评估器：调用 `VortexTube.jar`（已编译，含 commons-math3）
- 优化器：BoTorch SingleTaskGP + qEI，批量并行

**验证目标**：
- 完成 ≥100 个设计点（Sobol 初始 16 点 + BO 迭代）
- 找到比初始随机点更优的涡流管参数组合
- 与 Java 代码内置优化器结果交叉验证

### Phase 1 — 完整系统（第2-4周）

**范围**：
- 替换评估器为 OpenFOAM CaseRunner
- 加入 LangGraph Agent 编排层
- 加入 vLLM + LiteLLM（Qwen2.5-72B-AWQ）
- 加入异常检测、通知、自动报告

---

## 4. 组件详细设计

### 4.1 统一评估器接口

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class DesignParams:
    """涡流管几何参数"""
    D: float          # 管径 (mm), 范围 [5, 30]
    L_D_ratio: float  # 长径比, 范围 [5, 20]
    r_c: float        # 冷端出口比, 范围 [0.1, 0.5]

@dataclass
class EvalResult:
    params: DesignParams
    delta_T: float        # 冷端温降 (K)，优化目标（最大化）
    pressure_drop: float  # 压降 (Pa)，约束
    converged: bool
    runtime_s: float
    metadata: dict        # 扩展字段

class Evaluator(ABC):
    @abstractmethod
    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        """并发评估一批设计点，返回结果列表"""
        ...
```

### 4.2 Phase 0 评估器：JavaEvaluator

```python
class JavaEvaluator(Evaluator):
    """调用 VortexTube.jar 评估单个设计点"""

    def __init__(self, jar_path: str, java_bin: str = "java"):
        self.jar_path = jar_path
        self.java_bin = java_bin

    def _run_single(self, params: DesignParams) -> EvalResult:
        cmd = [
            self.java_bin, "-jar", self.jar_path,
            "--D", str(params.D),
            "--LD", str(params.L_D_ratio),
            "--rc", str(params.r_c),
            "--output-json"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout)
        return EvalResult(
            params=params,
            delta_T=data["delta_T"],
            pressure_drop=data.get("pressure_drop", 0.0),
            converged=data["converged"],
            runtime_s=data["runtime_s"],
            metadata=data
        )

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        with ProcessPoolExecutor(max_workers=min(len(params_list), 32)) as pool:
            return list(pool.map(self._run_single, params_list))
```

> **注**：需要在 `VortexTubeSimulation.java` 中添加 `--output-json` 命令行参数支持，输出标准 JSON。

### 4.3 Phase 1 评估器：OpenFOAMEvaluator

```python
class OpenFOAMEvaluator(Evaluator):
    """参数化渲染 + 提交 OpenFOAM rhoSimpleFoam"""

    TEMPLATE_DIR = "templates/vortex_tube_2d/"  # Jinja2 模板目录
    CASES_BASE = "cases/"

    def _render_case(self, params: DesignParams, case_id: str) -> Path:
        """渲染 OpenFOAM 字典到独立目录"""
        case_dir = Path(self.CASES_BASE) / case_id
        # Jinja2 渲染 blockMeshDict, thermophysicalProperties, etc.
        ...
        return case_dir

    def _run_openfoam(self, case_dir: Path, n_cores: int = 16) -> EvalResult:
        """blockMesh → decomposePar → mpirun rhoSimpleFoam → reconstructPar"""
        ...

    def evaluate_batch(self, params_list: list[DesignParams]) -> list[EvalResult]:
        # 自适应并发：available_cores // cores_per_case，上限 32
        n_workers = min(len(params_list), 512 // 16, 32)
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futs = {pool.submit(self._run_openfoam, ...): p for p in params_list}
            return [f.result() for f in as_completed(futs)]
```

每个算例目录命名规范：`cases/run_{YYYYMMDD_HHMMSS}_{hash8}/`

### 4.4 优化引擎（M3）

```python
class BayesianOptimizer:
    """BoTorch SingleTaskGP + qEI，支持批量并行采样"""

    BOUNDS = torch.tensor([
        [5.0,  5.0, 0.1],   # 下界: D, L/D, r_c
        [30.0, 20.0, 0.5],  # 上界
    ])

    def __init__(self, db: ResultDatabase):
        self.db = db

    def initial_points(self, n: int = 16) -> list[DesignParams]:
        """Sobol 序列生成初始采样点"""
        sobol = SobolEngine(dimension=3, scramble=True)
        samples = sobol.draw(n)  # [n, 3] in [0,1]
        # 映射到实际参数范围
        ...

    def suggest_next_batch(self, batch_size: int = 8) -> list[DesignParams]:
        """基于当前数据拟合 GP，用 qEI 推荐下一批"""
        train_X, train_Y = self.db.load_training_data()
        model = SingleTaskGP(train_X, train_Y)
        mll = ExactMarginalLogLikelihood(model.likelihood, model)
        fit_gpytorch_mll(mll)
        qEI = qExpectedImprovement(model, best_f=train_Y.max())
        candidates, _ = optimize_acqf(
            qEI, bounds=self.BOUNDS, q=batch_size,
            num_restarts=10, raw_samples=512,
        )
        return [DesignParams(*c.tolist()) for c in candidates]
```

### 4.5 数据存储（ResultDatabase）

使用 SQLite，无外部依赖：

```sql
CREATE TABLE results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,          -- cases/ 目录名
    D           REAL, L_D_ratio REAL, r_c REAL,
    delta_T     REAL,
    pressure_drop REAL,
    converged   BOOLEAN,
    runtime_s   REAL,
    status      TEXT DEFAULT 'OK',      -- OK / DIVERGED / ANOMALY
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 4.6 Phase 1 LangGraph Agent 编排层（M4）

```
图结构（StateGraph）:

START
  └→ orchestrator_node
       ├→ [有足够数据] surrogate_node（更新GP/NN代理）
       │       └→ suggest_node（推荐下一批参数）
       ├→ [异常检测触发] anomaly_node
       │       └→ notify_node（推送通知，等待人工确认）
       └→ [达到预算/收敛] report_node → END

每个 node 是一个 LLM tool call 或纯 Python 函数
```

**状态对象**：
```python
class OptState(TypedDict):
    iteration: int
    total_budget: int
    current_best: EvalResult | None
    pending_params: list[DesignParams]
    anomaly_flag: bool
    human_confirmation: bool
    history: list[EvalResult]
```

**Orchestrator Agent 可用工具**：
- `submit_batch(params)` → 调用 Evaluator
- `get_next_batch(n)` → 调用 BayesianOptimizer
- `check_convergence()` → 判断是否达到停止条件
- `flag_anomaly(reason)` → 触发人工介入流程
- `generate_report()` → 调用 ReportAgent

**LLM 后端**：
- Phase 0: 不使用 LLM
- Phase 1: 本地 Qwen2.5-72B-AWQ via vLLM + LiteLLM（Anthropic 格式接口）

---

## 5. 异常处理策略

| 异常类型 | 检测方式 | 处理 |
|---------|---------|------|
| CFD 未收敛（Phase 1）| 残差未降至 1e-4 | 标记 DIVERGED，跳过，不纳入 GP 训练 |
| 评估超时 | subprocess timeout | 标记 TIMEOUT，计入失败次数 |
| ΔT 超出物理范围 | \|ΔT\| > 3× 当前均值 | 标记 ANOMALY，触发 Agent 分析 |
| 连续 ≥3 个 DIVERGED | 计数器 | 暂停，推送通知，等待人工确认参数空间 |
| 代理模型误差 > 阈值 | 交叉验证 RMSE | 增加 CFD 调用，减少代理主导比例 |

---

## 6. 目录结构

```
vortex_opt/
├── evaluators/
│   ├── base.py           # Evaluator ABC + DesignParams + EvalResult
│   ├── java_evaluator.py # JavaEvaluator (Phase 0)
│   └── openfoam_evaluator.py  # OpenFOAMEvaluator (Phase 1)
├── optimization/
│   ├── bayesian.py       # BayesianOptimizer (BoTorch)
│   └── evolutionary.py  # NSGA-II / CMA-ES (Phase 2)
├── agents/
│   ├── graph.py          # LangGraph StateGraph 定义
│   ├── orchestrator.py   # OrchestratorAgent tools
│   ├── surrogate.py      # SurrogateAgent
│   ├── anomaly.py        # AnomalyDetectorAgent
│   └── report.py         # ReportAgent
├── storage/
│   └── database.py       # ResultDatabase (SQLite)
├── templates/
│   └── vortex_tube_2d/   # OpenFOAM Jinja2 模板 (Phase 1)
├── cases/                # 算例运行目录 (gitignored)
├── run_optimizer.py      # Phase 0 入口脚本
├── run_agent.py          # Phase 1 入口脚本
└── config.yaml           # 参数空间、预算、并发配置
```

---

## 7. Phase 0 入口脚本逻辑

```python
# run_optimizer.py
def main():
    db = ResultDatabase("results.sqlite")
    evaluator = JavaEvaluator(jar_path="VortexTube.jar")
    optimizer = BayesianOptimizer(db)

    # 1. 初始采样
    initial = optimizer.initial_points(n=16)
    results = evaluator.evaluate_batch(initial)
    db.save_batch(results)
    print(f"初始采样完成，最优 ΔT = {max(r.delta_T for r in results):.2f} K")

    # 2. BO 迭代
    for i in range(config.n_iterations):  # 默认 10 轮
        batch = optimizer.suggest_next_batch(batch_size=config.batch_size)
        results = evaluator.evaluate_batch(batch)
        db.save_batch(results)
        best = db.get_best()
        print(f"第 {i+1} 轮完成，当前最优: D={best.params.D:.1f}, "
              f"L/D={best.params.L_D_ratio:.1f}, r_c={best.params.r_c:.2f}, "
              f"ΔT={best.delta_T:.2f} K")

    # 3. 输出报告
    db.export_csv("results.csv")
    print("优化完成，结果已保存至 results.csv")
```

---

## 8. 依赖清单

### Phase 0
```
python >= 3.10
torch >= 2.0
botorch >= 0.10
gpytorch >= 1.11
java >= 17（调用 VortexTube.jar）
```

### Phase 1（新增）
```
openfoam >= 11（MPI，已安装）
jinja2 >= 3.0
langgraph >= 0.1
langchain-anthropic >= 0.1
vllm >= 0.4（已安装）
litellm >= 1.0（已安装）
```

---

## 9. 需要对 VortexTube.jar 的修改

Phase 0 依赖 Java 程序支持命令行参数输入和 JSON 输出，需在 `VortexTubeSimulation.java` 中添加：

```java
// 新增：解析命令行参数并以 JSON 格式输出结果
if (args.length > 0 && Arrays.asList(args).contains("--output-json")) {
    double D = parseArg(args, "--D", defaultD);
    double LD = parseArg(args, "--LD", defaultLD);
    double rc = parseArg(args, "--rc", defaultRc);
    // 运行仿真...
    System.out.println(String.format(
        "{\"delta_T\": %.4f, \"pressure_drop\": %.2f, "
        + "\"converged\": %b, \"runtime_s\": %.3f}",
        result.getDeltaT(), result.getPressureDrop(),
        result.isConverged(), runtimeSeconds
    ));
}
```

---

## 10. 里程碑

| 里程碑 | 内容 | 完成标志 |
|--------|------|---------|
| M0-1 | VortexTube.jar 支持命令行+JSON输出 | `java -jar VortexTube.jar --D 10 --LD 10 --rc 0.3 --output-json` 正常输出 |
| M0-2 | JavaEvaluator + ResultDatabase 可运行 | 16点初始采样写入 SQLite |
| M0-3 | BoTorch BO 循环跑通 | 10轮迭代，ΔT 单调改善 |
| M0-4 | Phase 0 完整报告 | 与 Java 内置优化器对比，验证 BO 找到更优解 |
| M1-1 | vLLM + LiteLLM 服务启动 | API 调用 Qwen2.5 返回正确响应 |
| M1-2 | OpenFOAM 参数化模板 | 单算例从 CaseConfig 到 ΔT 全流程 <5分钟 |
| M1-3 | LangGraph Agent 闭环 | Orchestrator 自动驱动 10轮 BO，无人工干预 |
| M1-4 | 异常检测+通知 | 模拟发散案例，Agent 正确暂停并推送通知 |
