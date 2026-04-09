# 多Agent CFD优化系统 — 实现计划

**关联设计文档**: `2026-04-04-multi-agent-cfd-optimization-design.md`
**日期**: 2026-04-04

---

## Phase 0：MVP 闭环（目标：1周内出优化结果）

### 任务 P0-1：修改 VortexTube.jar 支持 CLI + JSON 输出

**文件**: `D:\研究院科研项目\涡流管项目\vortex tube java\vortex tube java\VortexTubeSimulation.java`

修改 `main()` 方法，检测 `--output-json` 参数：
- 解析 `--D`、`--LD`、`--rc` 命令行参数（double 类型）
- 若无参数则沿用交互式默认行为（向后兼容）
- 运行仿真后将结果以单行 JSON 输出到 stdout：
  `{"delta_T": 12.34, "pressure_drop": 5678.9, "converged": true, "runtime_s": 0.42}`
- 重新打包为 `VortexTube.jar`（Fat JAR）

验收：`java -jar VortexTube.jar --D 10 --LD 10 --rc 0.3 --output-json` 输出合法 JSON

---

### 任务 P0-2：搭建 Python 项目骨架

在服务器上创建目录 `~/vortex_opt/`，结构如下：

```
vortex_opt/
├── evaluators/
│   ├── __init__.py
│   ├── base.py          # DesignParams, EvalResult, Evaluator ABC
│   └── java_evaluator.py
├── optimization/
│   ├── __init__.py
│   └── bayesian.py
├── storage/
│   ├── __init__.py
│   └── database.py
├── run_optimizer.py
├── config.yaml
└── requirements.txt
```

`requirements.txt`:
```
torch>=2.0
botorch>=0.10
gpytorch>=1.11
pyyaml
```

---

### 任务 P0-3：实现 base.py

实现 `DesignParams`（dataclass）、`EvalResult`（dataclass）、`Evaluator`（ABC）。
严格按照设计文档第 4.1 节的字段定义。

---

### 任务 P0-4：实现 java_evaluator.py

实现 `JavaEvaluator(Evaluator)`：
- `__init__(self, jar_path, java_bin="java")`
- `_run_single(params)` — subprocess 调用，timeout=60s，解析 JSON stdout
- `evaluate_batch(params_list)` — `ProcessPoolExecutor(max_workers=min(n,32))`
- 失败时（returncode≠0 或 JSON 解析异常）返回 `EvalResult(converged=False, delta_T=NaN)`

---

### 任务 P0-5：实现 database.py

实现 `ResultDatabase`：
- `__init__(db_path)` — 自动建表（见设计文档第 4.5 节 schema）
- `save_batch(results: list[EvalResult])` — 批量插入
- `load_training_data()` — 返回 `(train_X: Tensor, train_Y: Tensor)`，只取 `converged=True` 且 `status='OK'` 的行
- `get_best()` — 返回 delta_T 最大的 EvalResult
- `export_csv(path)` — 导出所有结果

---

### 任务 P0-6：实现 bayesian.py

实现 `BayesianOptimizer`：
- `BOUNDS` 张量：D∈[5,30], L/D∈[5,20], r_c∈[0.1,0.5]
- `initial_points(n=16)` — SobolEngine + 线性映射到参数范围
- `suggest_next_batch(batch_size=8)` — SingleTaskGP + qEI + optimize_acqf
  - 若数据点 < 3，回退到随机采样（避免 GP 退化）

---

### 任务 P0-7：实现 run_optimizer.py

按设计文档第 7 节逻辑实现主循环：
1. 读取 `config.yaml`（jar_path, n_initial, n_iterations, batch_size）
2. 初始 Sobol 采样 → 评估 → 存库
3. BO 迭代 n_iterations 轮：建议 → 评估 → 存库 → 打印当前最优
4. 导出 `results.csv`
5. 打印最终最优参数和 ΔT

---

### 任务 P0-8：端到端测试与验证

在服务器上运行完整优化：
```bash
cd ~/vortex_opt
python run_optimizer.py --config config.yaml
```

预期结果：
- 16 初始点 + 10轮×8点 = 96 个设计点
- 最优 ΔT 应显著高于初始随机点均值
- 与 Java 内置优化器（VortexTubeOptimizer）最优解对比，BO 应找到相近或更优解
- 总运行时间 < 30分钟（Java 单次 < 1s，96点并发几乎瞬时）

---

## Phase 1：完整系统（第2-4周，Phase 0 验证后开始）

### 任务 P1-1：启动 vLLM + LiteLLM 服务

- 确认 Qwen2.5-72B-AWQ 模型下载完成（screen `llm_download`）
- 启动 vLLM：`bash ~/start_llm_server.sh`
- 启动 LiteLLM：`bash ~/start_litellm_proxy.sh`
- 验收：`curl http://localhost:4000/v1/messages` 返回正确响应

---

### 任务 P1-2：OpenFOAM 参数化模板

基于 `~/vortex_tube_case_comp/` 创建 Jinja2 模板集：
- `templates/vortex_tube_2d/system/blockMeshDict.j2` — 由 D, L/D 派生网格尺寸
- `templates/vortex_tube_2d/constant/thermophysicalProperties.j2`
- `templates/vortex_tube_2d/0/` — 初始条件（p, U, T, k, omega）

实现 `OpenFOAMEvaluator`（`evaluators/openfoam_evaluator.py`）：
- `render_case(params, case_id)` — Jinja2 渲染到 `cases/run_{id}/`
- `_run_openfoam(case_dir, n_cores=16)` — blockMesh + mpirun + reconstructPar
- `_extract_result(case_dir)` — 读取 postProcessing/ 提取 ΔT
- `evaluate_batch` — 自适应并发，`n_workers = min(n, 512//16, 32)`

验收：单算例 `<5分钟`，ΔT 值与 Java 模型在同一量级

---

### 任务 P1-3：LangGraph Agent 图

在 `agents/graph.py` 中定义 `StateGraph`：

节点：
- `orchestrator_node` — 决策下一步行动（LLM tool call）
- `suggest_node` — 调用 BayesianOptimizer.suggest_next_batch
- `evaluate_node` — 调用 Evaluator.evaluate_batch（此时为 OpenFOAMEvaluator）
- `anomaly_node` — 检测异常，决定是否暂停
- `notify_node` — 发送通知（微信 Webhook 或邮件）
- `report_node` — 生成优化报告

边（条件路由）：
- 正常 → suggest → evaluate → orchestrator（循环）
- 异常触发 → anomaly → notify → 等待人工确认 → orchestrator
- 达到预算/收敛 → report → END

`OptState` TypedDict 严格按照设计文档第 4.6 节定义。

---

### 任务 P1-4：Orchestrator Agent tools

在 `agents/orchestrator.py` 中实现 5 个 LangGraph tool：
- `submit_batch` — 调用 `evaluator.evaluate_batch`
- `get_next_batch` — 调用 `optimizer.suggest_next_batch`
- `check_convergence` — 检查是否达到停止条件（预算耗尽 or ΔT改善 < 0.1K/轮）
- `flag_anomaly` — 设置 `state["anomaly_flag"] = True`，记录原因
- `generate_report` — 调用 ReportAgent 生成 markdown 报告

LLM 后端：LiteLLM proxy（`claude-sonnet-4-6` → 本地 Qwen2.5-72B-AWQ）

---

### 任务 P1-5：异常检测模块

在 `agents/anomaly.py` 中实现 `AnomalyDetector`：
- 连续 ≥3 个 DIVERGED → 返回 `(True, "连续发散，建议检查参数空间边界")`
- ΔT 跳变超过当前均值 3σ → 返回 `(True, "结果异常跳变")`
- 代理误差 > 配置阈值（默认 RMSE > 2K）→ 返回 `(True, "代理模型误差过大")`

---

### 任务 P1-6：端到端 LangGraph 测试

```bash
cd ~/vortex_opt
python run_agent.py --config config.yaml --evaluator openfoam
```

验收：
- Agent 自动驱动 5 轮 BO（OpenFOAM 评估）
- 手动触发一次异常（修改参数使 CFD 发散），确认 Agent 暂停并输出通知
- 生成 markdown 优化报告

---

## 配置文件模板 (config.yaml)

```yaml
# Phase 0 配置
evaluator: java
jar_path: /home/user/VortexTube.jar
java_bin: java

# Phase 1 切换为:
# evaluator: openfoam
# openfoam_cores_per_case: 16
# openfoam_template_dir: templates/vortex_tube_2d/

# 优化参数
n_initial: 16
n_iterations: 10
batch_size: 8

# 参数空间
bounds:
  D: [5.0, 30.0]        # mm
  L_D_ratio: [5.0, 20.0]
  r_c: [0.1, 0.5]

# 停止条件
budget: 200             # 总评估次数上限
convergence_tol: 0.1    # ΔT 改善 < 0.1K 连续3轮停止

# 数据库
db_path: results.sqlite

# LLM（Phase 1）
llm_base_url: http://localhost:4000
llm_model: claude-sonnet-4-6
```

---

## 任务执行顺序

```
P0-1（修改Java）
  └→ P0-2（项目骨架）
       └→ P0-3 + P0-4 + P0-5（可并行）
            └→ P0-6（依赖P0-3+P0-5）
                 └→ P0-7（依赖全部P0）
                      └→ P0-8（验证）
                           └→ P1-1（确认LLM服务）
                                └→ P1-2 + P1-3（可并行）
                                     └→ P1-4 + P1-5（依赖P1-3）
                                          └→ P1-6（验证）
```
