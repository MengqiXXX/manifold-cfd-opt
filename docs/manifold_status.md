# 歧管（manifold）优化系统：进展与问题（自动更新）

更新时间：2026-04-09

## 已完成（代码侧）

- 监控面板已切换为歧管主题与参数展示
  - 标题从“涡流管”切换为“歧管”
  - 最优参数展示：`logit_1/logit_2/logit_3`，并计算 `softmax([l1,l2,l3,0]) -> w1..w4`
  - 指标展示：`Flow CV`（越小越均匀）、`ΔP`（Pa）、以及 `objective`
  - 探索散点图：`logit_1 vs logit_2`
- 监控面板默认端口调整为 `8090`（仍可通过 `MONITOR_PORT` 覆盖）
- 新增 vLLM 启动脚本（限制两张 GPU）
  - [scripts/start_vllm_qwen_2gpu.sh](file:///d:/TRAE/manifold-cfd-opt/scripts/start_vllm_qwen_2gpu.sh)
  - 默认：`CUDA_VISIBLE_DEVICES=0,1`，`--tensor-parallel-size 2`，端口 `8001`
- Agent 配置已补齐（用于服务器上跑 OpenFOAM + Qwen）
  - [config_agent_remote_openfoam.yaml](file:///d:/TRAE/manifold-cfd-opt/config_agent_remote_openfoam.yaml)
  - 本地监控用配置（LLM base_url 指向服务器）：[config_agent_remote_openfoam_local.yaml](file:///d:/TRAE/manifold-cfd-opt/config_agent_remote_openfoam_local.yaml)
- 报告生成已支持歧管（自动识别是否为 `logit_*` 参数体系）
  - 报告标题、最优参数表格、Top10 表格与 LLM 总结 prompt 均支持歧管指标（CV/ΔP/Objective）

## 当前运行状态（服务器 192.168.110.10）

### 1) Qwen(vLLM) 未跑起来（关键阻塞）

- 现象：`ss -ltnp | grep 8001` 显示 `NO_8001_LISTEN`
- 日志：`.runs/vllm_8001.log` 中出现
  - `huggingface.co ... Network is unreachable`
  - 说明 vLLM 仍在尝试从 HuggingFace 拉取 `Qwen/Qwen2.5-72B-Instruct`，服务器无外网导致启动失败，端口未监听
- 建议：
  - 用本地已存在的模型路径启动（服务器上发现 `/home/liumq/models/Qwen/Qwen2___5-72B-Instruct-AWQ`）
  - 并显式开启离线模式（避免 hub 访问），例如设置 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`
  - vLLM 启动建议（示例）：
    - `CUDA_VISIBLE_DEVICES=0,1`
    - `--tensor-parallel-size 2`
    - `--model /home/liumq/models/Qwen/Qwen2___5-72B-Instruct-AWQ`

### 2) OpenFOAM 没有真正执行（导致所有设计点 ERROR）

- 现象：Agent 日志中长期出现：
  - `有效数据点 0 < 3，回退到随机采样`
  - `连续 N 轮全部发散`
- 结果：`optimization_report_agent_remote.md` 显示
  - `总评估点 88 | 有效 0 | 失败 88`
- 服务器 case 目录存在但没有 `log.blockMesh / log.solver / postProcessing` 输出，表明 solver 链路没有跑起来。
- 进一步确认：在服务器上 `source /opt/openfoam13/etc/bashrc` 后仍然 `blockMesh=NA simpleFoam=NA`（PATH 未生效 / OpenFOAM bin 不在环境中）。
- 建议：
  - 先在服务器手动验证 OpenFOAM 工具可用：
    - `source /opt/openfoam13/etc/bashrc`
    - `which blockMesh simpleFoam`
  - 若 `which` 仍为空，需要修复 OpenFOAM 安装/环境（如 `WM_PROJECT_DIR` / `FOAM_INST_DIR` 等），否则优化无法产生有效点。

## 下一步（使系统跑稳）

1. 先把 vLLM 以本地模型路径启动成功（确保 `:8001/v1` 可访问）
2. 修复 OpenFOAM 环境，使 `blockMesh/simpleFoam/decomposePar` 可执行并产出 `postProcessing`
3. 重新启动 agent，确认至少出现少量 `status=OK` 的有效点（监控面板将开始显示 CV/ΔP/Objective 的趋势）

