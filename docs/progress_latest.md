# 最新进展与问题总结（用于同步到 GitHub）

本文档汇总近期 `manifold-cfd-opt` 的关键进展、已修复问题、当前遗留问题与下一步建议，方便团队审阅与复现。

---

## 1) 核心进展（已落地）

### 1.1 远程 OpenFOAM “闭环”可跑通（评估→后处理→入库→报告）

覆盖的链路：
- 参数采样/推荐（Sobol + BO）
- 远端 case 生成与上传
- OpenFOAM 流水线：`blockMesh → checkMesh → decomposePar → mpirun foamRun → reconstructPar`
- 后处理：从 `postProcessing/**/*.dat` 读取标量（如 outlet1Flow/outlet1P）
- 入库：SQLite `results*.sqlite`
- 报告：Markdown `optimization_report*.md`（可选 LLM 分析段）

关键入口：
- 脚本式优化闭环：[run_optimizer.py](file:///d:/TRAE/manifold-cfd-opt/run_optimizer.py)
- Agent 编排闭环（LangGraph）：[run_agent.py](file:///d:/TRAE/manifold-cfd-opt/run_agent.py) + [graph.py](file:///d:/TRAE/manifold-cfd-opt/agents/graph.py)

### 1.2 监控页面 UI/状态判定修复 + 公网只读发布支持

- 监控页改为蓝白亮色、无渐变，整体更清晰。
- “QWEN 智能诊断”与“物理仿真状态”的运行判定口径已拆分并对齐：
  - `n_running`：严格按求解进程（进程 cwd）判定
  - `n_active`：无进程时的“活跃目录”回退（recent logs / processor0）
- 增加只读模式 `MONITOR_READONLY=1`：禁用一切会触发动作的 POST 接口，前端按钮自动隐藏/禁用。
- `admin` 高风险接口默认对外隐藏（未配置 token 时返回 404）。

监控相关：
- 服务入口：[monitor/main.py](file:///d:/TRAE/manifold-cfd-opt/monitor/main.py)
- UI：[monitor/static](file:///d:/TRAE/manifold-cfd-opt/monitor/static)
- 公网部署文档与模板：[monitor/DEPLOY.md](file:///d:/TRAE/manifold-cfd-opt/monitor/DEPLOY.md) + `monitor/deploy/*`

### 1.3 新增“歧管参数 → 三维数模”工具

工具：
- [generate_manifold_3d_model.py](file:///d:/TRAE/manifold-cfd-opt/scripts/generate_manifold_3d_model.py)

输入：
- `logit_1/logit_2/logit_3`

输出：
- `case/`：渲染后的 OpenFOAM 3D case（`templates/manifold_3d`）
- `model.obj / model.stl`：轻量可视化几何（外形盒体 + outlet 分段线）
- `model.json`：派生量（`outlet_weights/y_levels/n_cells_*`）

说明文档：
- [manifold_param_to_3d_model.md](file:///d:/TRAE/manifold-cfd-opt/docs/manifold_param_to_3d_model.md)

---

## 2) 已修复问题（关键 bug / 误判）

### 2.1 远端 OpenFOAM 运行误判与进程等待问题

修复要点：
- `setsid` 启动的后台进程 wait 逻辑调整，避免 `wait: pid ... is not a child` 导致“求解已在跑但被判失败”。（runner 侧）
- 远端 OpenFOAM 环境 `bashrc` source 更稳健（失败不致命，避免 pipeline 直接中断）。

相关模块：
- [foam_runner.py](file:///d:/TRAE/manifold-cfd-opt/evaluators/foam_runner.py)

### 2.2 postProcessing 读取失败（尤其并行算例）

修复要点：
- 读取 `.dat` 时同时兼容：
  - `postProcessing/<func>/*/*.dat`
  - `processor*/postProcessing/<func>/*/*.dat`
- 修复远端 bash 命令转义/拼接导致的语法错误。

相关模块：
- [post_processor.py](file:///d:/TRAE/manifold-cfd-opt/evaluators/post_processor.py)

### 2.3 监控页“诊断未运行”与“仿真在跑”冲突

根因：
- 两个区块原先使用不同口径（严格进程 vs 目录推断），导致长期不一致。

修复：
- `cfd-status` 输出新增字段并明确含义；UI 对 `n_running/n_active` 做区分展示。

相关模块：
- [cfd_status.py](file:///d:/TRAE/manifold-cfd-opt/monitor/providers/cfd_status.py)
- [app.js](file:///d:/TRAE/manifold-cfd-opt/monitor/static/app.js)

### 2.4 Windows 下 SSH 输出解码导致的异常

修复：
- SSH 子进程输出强制 `utf-8` 解码并 `errors=replace`，避免因系统默认编码导致诊断/采集偶发崩溃。

相关模块：
- [ssh.py](file:///d:/TRAE/manifold-cfd-opt/infra/ssh.py)

---

## 3) 当前遗留问题 / 风险点（需要重点审阅）

### 3.1 目标函数权重与多目标合理性

目前目标函数为线性加权和（`objective = -flow_cv - 1e-5 * ΔP`）。需要评审：
- `flow_cv` 与 `ΔP` 的量纲/数量级是否使该权重合理？
- 是否需要归一化（例如相对基准）或改为 Pareto 多目标？

### 3.2 几何参数化表达能力

当前 manifold 的参数化主要改变 outlet 在 y 方向的分段比例（以及对应网格划分），外形是固定矩形通道。
- 若目标是“真实三维 CAD 外形可变”（扩压、圆角、歧管分支等），需要新增 CAD 参数化与实体建模链路（STEP/IGES），并决定网格生成方案（snappyHexMesh 或 gmsh/hex-dominant）。当前工具只覆盖“数模/网格分段层”。  

### 3.3 监控公网发布的安全边界

只读模式已禁用 POST 操作，但仍建议：
- 代理层（Nginx/Cloudflare）限制 `/api/*` 只允许 GET
- 明确不对公网暴露任何“执行命令/启动求解”能力

---

## 4) 下一步建议（可执行）

1) 目标函数审阅：制定 `flow_cv` 与 `ΔP` 的工程等价尺度，确定权重或切换多目标策略。  
2) 参数空间约束：将明显无效区域前置约束（几何可制造性、面积下限等），减少无效算例比例。  
3) CAD 级数模：若需要真实 3D 实体建模，选择后端（CadQuery/pythonOCC 或 gmsh-OCCT），定义特征参数并导出 STEP；并评估网格策略与计算开销。  
4) 监控只读外链：按 `monitor/DEPLOY.md` 的 Cloudflare Tunnel 方案发布，并在网关层做 GET-only 限制。  

---

## 5) 复现指南（最小闭环）

1) 运行一次最小闭环（远程 OpenFOAM）：`python scripts/run_openfoam_once.py <cfg>`  
2) 运行脚本式优化闭环：`python run_optimizer.py --config <cfg>`  
3) 运行 Agent 闭环：`python run_agent.py --config <cfg>`  
4) 启动监控：`python monitor/run_server.py`（公网发布用 `MONITOR_READONLY=1`）  

