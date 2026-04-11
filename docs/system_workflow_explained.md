# 优化系统工作方式详解（端到端、含每环节输入/输出与合理性分析点）

本文档面向“逐环节审阅合理性”的目的，按端到端数据流拆解系统的每个模块：**输入是什么、输出是什么、关键不变量是什么、常见失败模式是什么、为什么这样设计（合理性分析点）**。文末提供“审阅清单”。

本文以 `manifold-cfd-opt` 为主（歧管多出口），但很多模块可复用于 `vortex-cfd-opt`。

---

## 1. 总览：系统在做什么

目标：在给定参数空间内搜索几何/设计参数，使目标函数最大化。

在歧管项目中：
- **设计参数**：`(logit_1, logit_2, logit_3)`（见 [DesignParams](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)）
- **CFD 输出指标**：`flow_cv`（流量均匀性，越小越好）和 `pressure_drop`（压降，越小越好）
- **目标函数**：目前实现为 `objective = -flow_cv - 1e-5 * pressure_drop`（见 [EvalResult.objective](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)）

系统循环：
1) 采样/推荐一批设计参数（Sobol 或 Bayesian Optimization）  
2) 远端生成 OpenFOAM case 并运行求解  
3) 后处理提取指标（postProcessing/*.dat）  
4) 计算目标并入库（SQLite）  
5) 用历史数据拟合代理模型并推荐下一批（BO）  
6) 可选：异常检测、报告生成、监控展示

对应入口：
- “脚本式 BO 循环”：[run_optimizer.py](file:///d:/TRAE/manifold-cfd-opt/run_optimizer.py)  
- “Agent 编排循环（LangGraph）”：[run_agent.py](file:///d:/TRAE/manifold-cfd-opt/run_agent.py) + [agents/graph.py](file:///d:/TRAE/manifold-cfd-opt/agents/graph.py)

---

## 2. 数据对象与不变量

### 2.1 设计参数：DesignParams

定义：[evaluators/base.py](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)

字段：
- `logit_1, logit_2, logit_3: float`

不变量：
- 目前对参数范围的约束主要来自优化器的 `bounds` 配置（见 [BayesianOptimizer](file:///d:/TRAE/manifold-cfd-opt/optimization/bayesian.py)）；`DesignParams` 本身不做 clamp。

合理性分析点：
- `logit_*` 命名暗示其可能经过 sigmoid/softmax 映射到几何参数。审阅时建议确认：模板渲染时如何把 logit 映射为几何（见“3. 模板与 case 生成”）。
- 若参数空间实际是有硬物理约束的（例如通道宽度必须为正、总流道面积必须受限），建议将这些约束前置为：采样时的约束优化或无效区域拒绝采样策略。

### 2.2 评估结果：EvalResult

定义：[evaluators/base.py](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)

字段（核心）：
- `flow_cv: float`（越小越好）
- `pressure_drop: float`（越小越好）
- `converged: bool`
- `status: str`（OK / MESH_FAILED / SOLVER_TIMEOUT / ...）
- `metadata: dict`（保存远端路径、日志尾部、失败原因等）
- `objective: float`（由 `flow_cv` 与 `pressure_drop` 计算）

不变量：
- `is_valid()` 仅在 `converged == True` 且指标均为有限数时返回 True（见 [is_valid](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)）。
- 优化器只应基于 `valid` 历史做拟合；无效点只用于统计与异常检测。

合理性分析点：
- 当前目标函数是线性加权和：`-flow_cv - 1e-5*dp`。审阅要点：权重是否反映工程等价尺度？是否需要归一化或 Pareto 多目标？
- `objective` 越大越好（因为是负号），因此 BO 与 report 的 “best = max(objective)” 逻辑一致。

---

## 3. 模板与 case 生成（参数 → OpenFOAM 输入文件）

核心点：把 `DesignParams` 映射为 OpenFOAM case 目录结构（system/constant/0 等），并确保每次评估互相隔离。

在远程评估器中：
- 实现：[RemoteOpenFOAMEvaluator](file:///d:/TRAE/manifold-cfd-opt/evaluators/remote_openfoam_evaluator.py)
- 模板目录（默认）：`templates/manifold_2d`（见配置）

典型输出：
- 远端 case 根目录：`{remote_base}/manifold_<timestamp>_<seed>/case`
- case 内日志：`log.blockMesh / log.checkMesh / log.decomposePar / log.solver / log.reconstructPar`

不变量：
- 每个设计点必须对应唯一 case 目录（避免并发相互覆盖）。
- 模板必须完整生成求解所需的最小文件集（system/controlDict, fvSchemes, fvSolution, blockMeshDict, decomposeParDict, 0/*, constant/*）。

合理性分析点：
- 模板渲染的确定性：同样的参数应生成同样的 case（除非显式引入随机扰动）。
- 目录隔离策略：本系统以“每次评估一个新目录”为主，便于溯源；代价是磁盘增长，需要清理策略（可作为后续改进点）。

---

## 4. 远端执行：OpenFOAM 运行流水线

### 4.1 运行步骤（pipeline）

当前流水线逻辑集中在：
- 运行器：[OpenFOAMRunner.run_remote](file:///d:/TRAE/manifold-cfd-opt/evaluators/foam_runner.py)

顺序：
1) `blockMesh`（网格生成）
2) `checkMesh`（网格质量检查）
3) `decomposePar -force`（并行划分）
4) `mpirun -np N foamRun -solver <solver> -parallel`（并行求解）
5) `reconstructPar -latestTime`（合并最新时间步）

输出：
- `RunResult(status=OK|... , logs={step: tail}, details={...})`
- `details` 中含 case_dir、pid、杀进程结果等

不变量：
- 一旦超过 `timeout_s`，必须停止远端进程组，避免僵尸计算占用资源（见 kill 逻辑）。
- `RUNNING/DONE` 判定要以进程/标志文件为准，不能只靠日志是否写入（日志写入可能缓慢）。

合理性分析点：
- 将“运行/监控/超时终止”封装为 runner，避免 evaluator/optimizer 层关心 OpenFOAM 细节。
- 这里使用“后台 pipeline + 轮询 tail”的方式，降低 SSH 连接不稳定带来的单次长连接风险。
- 建议审阅并确认 solver 的选择与并行策略（`foamRun -solver ...` 是否是你想要的求解器族）。

### 4.2 失败模式分类（状态码）

运行器会把失败归类为：
- MESH_FAILED / CHECKMESH_FAILED / DECOMP_FAILED / SOLVER_FAILED / SOLVER_TIMEOUT / SOLVER_DIVERGED / RECONSTRUCT_FAILED / SSH_ERROR

合理性分析点：
- 状态码分类的粒度足够支持“统计失败原因占比”与“异常检测策略”。
- 如果你需要做自动修复（例如自动调整网格划分或松弛因子），这些状态码是触发策略的入口。

---

## 5. 后处理：提取指标（flow_cv, ΔP）

实现：
- [read_latest_surface_field_value](file:///d:/TRAE/manifold-cfd-opt/evaluators/post_processor.py)  

机制：
- 从 `postProcessing/<func_name>/*/*.dat` 或 `processor*/postProcessing/<func_name>/*/*.dat` 找最新 `.dat`
- 取最后一行最后一列作为标量值（awk）

输出：
- `ScalarReadResult(value, status, diag, source_path, logs)`

不变量：
- 指标必须是有限数（非 NaN/Inf）才可进入有效解。
- dat 文件的路径与格式是一个“隐式契约”：模板的 functionObject 或求解器必须产生相同布局。

合理性分析点：
- 采用“读 postProcessing 结果文件”而非解析 solver log，更稳健、更可追溯。
- 若 functionObject 版本差异导致输出目录不一致，应将输出契约固化（模板内固定 function 名与输出路径）。

---

## 6. 入库：ResultDatabase（SQLite）

实现：[storage/database.py](file:///d:/TRAE/manifold-cfd-opt/storage/database.py)

写入点：
- 脚本式优化：`db.save_batch(results, run_id=...)`（见 [run_optimizer.py](file:///d:/TRAE/manifold-cfd-opt/run_optimizer.py)）
- Agent：`evaluate_node` 内写入（见 [agents/graph.py](file:///d:/TRAE/manifold-cfd-opt/agents/graph.py)）

数据内容：
- params（3 个 logit）
- flow_cv / pressure_drop / objective / converged / runtime_s / status
- metadata（包含远端路径、日志尾部、后处理诊断等）

不变量：
- 数据库是“真相源”（single source of truth）：报告与监控都应从 DB 或实时 provider 推导，不应另存一份“影子状态”。

合理性分析点：
- SQLite 对单机/轻量项目足够；并发写入量大时需要明确写入策略（本系统以 batch 保存为主）。
- metadata 允许保留诊断信息，便于事后追溯；但需要注意敏感信息脱敏（例如不要写入密钥）。

---

## 7. 优化器：Sobol + Bayesian Optimization（代理模型）

实现：[optimization/bayesian.py](file:///d:/TRAE/manifold-cfd-opt/optimization/bayesian.py)

### 7.1 初始采样（Sobol）

入口：
- `optimizer.initial_points(n)`（见 [run_optimizer.py](file:///d:/TRAE/manifold-cfd-opt/run_optimizer.py)）

输出：
- `n` 个 DesignParams（覆盖空间、低相关）

合理性分析点：
- Sobol 适合在 BO 前快速探索空间，降低代理模型先验偏差。
- 审阅重点：bounds 是否覆盖你关心的几何变化范围？是否含明显无效区域？

### 7.2 代理模型与采集函数

当前优化器使用高斯过程（GP）做代理模型并推荐下一批（细节在 bayesian.py 内）。

输出：
- `suggest_next_batch()` → `batch_size` 个 DesignParams

合理性分析点：
- GP 对低维（3 维 logit）很合适；如果未来扩展到高维，需要考虑替代模型（RF/NGBoost/NN、或 BO with embeddings）。
- 批量 BO 的实现方式（一次推荐多个）会影响探索-利用平衡；需要审阅其 acquisition 的批量策略是否合理（例如 qEI / Kriging Believer）。

---

## 8. 编排：两条“闭环”路径

### 8.1 脚本式闭环（run_optimizer.py）

文件：[run_optimizer.py](file:///d:/TRAE/manifold-cfd-opt/run_optimizer.py)

特点：
- 直观、易读：Sobol → 循环 BO → CSV 导出
- 无异常暂停/人工确认/报告自动生成（仅导出 CSV）

适用：
- 快速验证闭环可用性
- 小规模实验与调参

### 8.2 Agent 编排闭环（LangGraph）

文件：
- 入口：[run_agent.py](file:///d:/TRAE/manifold-cfd-opt/run_agent.py)
- 图定义：[agents/graph.py](file:///d:/TRAE/manifold-cfd-opt/agents/graph.py)

图结构（代码内注释也有）：
`orchestrator → evaluate → anomaly → (notify?) → orchestrator ... → report`

输出：
- DB 持续写入
- 结束时生成 Markdown 报告（`report_path`）

合理性分析点：
- 把“决策逻辑、异常逻辑、报告逻辑”显式节点化，使系统更容易扩展（例如接入邮件/IM 通知）。
- `notify_node` 在非 TTY 环境自动放行（`human_confirmed=True`），保证无人值守时不会卡住。

---

## 9. 异常检测：规则引擎（非 LLM）

实现：[agents/anomaly.py](file:///d:/TRAE/manifold-cfd-opt/agents/anomaly.py)

当前规则：
1) 连续若干轮“全发散” → 异常
2) 目标值 z-score 跳变过大 → 异常

输出：
- `(is_anomaly, reason)`，触发后会走 notify 流程

合理性分析点：
- 规则引擎可解释性强，适合“工程系统先落地”。
- 可扩展方向：把“网格质量指标”“残差收敛曲线特征”等纳入异常检测，降低误报/漏报。

---

## 10. 大模型（LLM）的作用：目前是“解释与诊断”，不是“优化决策核心”

系统里 LLM 主要承担两类任务：

### 10.1 报告文字分析（可选）

实现：[agents/report.py](file:///d:/TRAE/manifold-cfd-opt/agents/report.py)

行为：
- 若配置了 `llm_client + llm_model` 且有有效点，会调用 LLM 生成 “3-5 句趋势与权衡分析” 写入报告
- 若 LLM 不可用，会生成模板报告（不影响优化过程）

输出：
- `optimization_report*.md`（含统计、Top10、失败分布、可选 LLM 分析段）

合理性分析点：
- 这是“解释层”的可选增强，失败不会影响数值闭环，风险可控。
- 审阅重点：LLM 输出是否会被误当作决策依据？（目前不会）

### 10.2 监控页上的“QWEN 智能诊断”（运维/排障建议）

实现：[monitor/providers/qwen_diagnose.py](file:///d:/TRAE/manifold-cfd-opt/monitor/providers/qwen_diagnose.py)

行为：
- 通过 SSH 采集服务器状态（进程、case 目录、日志尾部等）
- 把状态摘要喂给 LLM，要求输出：原因分析 / Debug 计划 / 建议命令

输出：
- 诊断卡片内容（analysis/plan/action_cmd）

合理性分析点：
- 该模块本质是“专家系统 UI + LLM 语言生成器”。真实执行仍由你控制（只读模式会禁用触发）。
- 风险点：LLM 可能给出有副作用的命令；生产环境建议只允许“白名单命令模板”，或只展示不执行。

---

## 11. 监控系统（Web）

监控服务：FastAPI + 静态前端 + WebSocket（状态推送）。

入口：
- FastAPI app：[monitor/main.py](file:///d:/TRAE/manifold-cfd-opt/monitor/main.py)
- 静态前端：[monitor/static](file:///d:/TRAE/manifold-cfd-opt/monitor/static)

关键接口：
- `/api/cfd-status`：实时/缓存的仿真状态（见 [cfd_status.py](file:///d:/TRAE/manifold-cfd-opt/monitor/providers/cfd_status.py)）
- `/api/qwen-diagnose`：诊断（见 [qwen_diagnose.py](file:///d:/TRAE/manifold-cfd-opt/monitor/providers/qwen_diagnose.py)）

只读发布：
- 通过 `MONITOR_READONLY=1` 禁用 POST 操作接口（见 [monitor/main.py](file:///d:/TRAE/manifold-cfd-opt/monitor/main.py)）

合理性分析点：
- 监控不参与优化决策，只提供可观测性与运维入口，降低“黑箱感”。
- 公网暴露风险：必须禁用高危接口并加边界保护（只读 + 代理层限制 GET）。

---

## 12. 配置项索引（审阅时的抓手）

脚本式（run_optimizer.py）常用配置：
- `template_dir / cases_base`
- `ssh_host / ssh_user / ssh_port / remote_base`
- `n_initial / n_iterations / batch_size`
- `db_path / csv_output`
- `n_cores / timeout_s / foam_source`

Agent（run_agent.py）常用配置：
- `batch_size / n_initial / n_iterations / budget`
- `convergence_tol / convergence_patience`
- `anomaly_diverge_streak / anomaly_jump_sigma`
- `report_path`
- `llm_base_url / llm_model / llm_api_key`

---

## 13. 审阅清单（逐环节合理性核对）

### 参数与约束
- bounds 是否覆盖真实可行域？是否包含大量无效区域？
- 是否需要显式约束（几何可制造性、截面积守恒、壁厚下限等）？

### 目标函数
- `flow_cv` 与 `pressure_drop` 的量纲与数量级是否匹配权重？
- 是否应该做归一化、对数变换或多目标（Pareto）？

### CFD 流水线
- 网格质量阈值是否合理（checkMesh 的 OK 判定）？
- 求解器/湍流模型/边界条件是否符合物理？
- 超时策略是否会误杀长算例？是否需要 adaptive timeout？

### 后处理与指标契约
- `postProcessing` 输出路径/文件格式是否稳定？是否跨 OF 版本？
- 指标取“最后一行最后一列”是否总是正确？是否需要更严格解析？

### 数据与可追溯性
- metadata 是否记录了足够复现的信息（case_dir、日志尾部、关键参数）？
- 是否有磁盘清理与数据归档策略？

### BO 合理性
- 初始点数是否足够覆盖空间？
- acquisition 策略是否适合 batch？是否过度 exploitation？

### LLM 作用边界
- LLM 输出是否只用于解释/建议，而不直接改变数值链路？
- 公网只读是否确保无法触发有副作用操作？

