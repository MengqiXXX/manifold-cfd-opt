# vLLM NCCL 启动问题排查记录

**时间**: 2026-04-10  
**环境**: Ubuntu 22.04, CUDA Driver 580.126.09 (CUDA 13.0), RTX 5090 x4 (compute cap 12.0 / Blackwell)

---

## 问题描述

vLLM 0.19.0 + Qwen2.5-72B-AWQ + TP=4 启动失败，Worker 进程在 NCCL 初始化时 segfault：

```
!!!!!!! Segfault encountered !!!!!!!
  File "<unknown>", line 0, in cuMemCreate
  File "misc/cudawrap.cc", line 94, in ncclCuMemHostEnable()
  File "misc/cudawrap.cc", line 210, in ncclCuMemHostEnable()
  File "misc/cudawrap.cc", line 294, in initOnceFunc
  File "misc/cudawrap.cc", line 312, in ncclCudaLibraryInit()
  File ".../src/init.cc", line 2246, in ncclCommInitRank
RuntimeError: Engine core initialization failed. See root cause above. Failed core proc(s): {}
```

---

## 根本原因

NCCL 在 `ncclCudaLibraryInit()` 中无条件调用 `cuMemCreate` 探测 CUDA 虚拟内存管理（VMM）支持。  
RTX 5090（Blackwell, sm_120）+ CUDA 驱动 580.126.09 上，`cuMemCreate` 触发 SIGSEGV。  
这是 NCCL 与当前 CUDA 驱动的兼容性 bug，**非 vLLM 代码问题**。

---

## 历史参考

**2026-04-07 06:24 UTC 成功运行记录**（来自 `/home/liumq/log.vllm_server`）：

```
(Worker) [nccl.py:24] Found nccl from environment variable VLLM_NCCL_SO_PATH=
    /home/liumq/.local/lib/python3.10/site-packages/nvidia/nccl/lib/libnccl.so.2
(Worker) [pynccl.py:111] vLLM is using nccl==2.29.7
```

成功时使用的参数：
```bash
python3 -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 --port 8001 \
  --model /home/liumq/models/Qwen/Qwen2___5-72B-Instruct-AWQ \
  --served-model-name qwen2.5-72b \
  --tensor-parallel-size 4 \
  --max-model-len 8192 \
  --quantization awq_marlin \
  --disable-custom-all-reduce \
  --gpu-memory-utilization 0.88
```

**关键区别**：成功时 `VLLM_NCCL_SO_PATH` 已在环境变量中设置（来源不明，可能在 .bashrc 或启动脚本中），且 NCCL 2.29.7 是特定构建版本（非当前 PyPI 版本）。

---

## 已尝试的方法（均失败）

| 方法 | 结果 |
|------|------|
| `nvidia-nccl-cu12==2.29.7`（PyPI 重装） | cuMemCreate segfault |
| `nvidia-nccl-cu12==2.27.5` | segfault（同上）|
| `nvidia-nccl-cu12==2.27.7` | segfault（同上）|
| `nvidia-nccl-cu12==2.26.2` | ImportError: undefined symbol: ncclCommShrink |
| `nvidia-nccl-cu12==2.23.4` | ImportError: undefined symbol: ncclCommShrink |
| `NCCL_CUMEM_ENABLE=0` | 无效，`ncclCudaLibraryInit` 仍调用 cuMemCreate |
| `NCCL_NVLS_ENABLE=0` | 无效，同上 |
| `NCCL_CUMEM_ENABLE=0 + NCCL_NVLS_ENABLE=0` | 无效 |
| `--enforce-eager`（跳过 torch.compile）| 无效，segfault 在更早阶段 |
| `VLLM_NCCL_SO_PATH` 显式设置 | 无效（log 模式与成功时一致，但仍 segfault）|
| TP=2（仅 2 GPU）| 模型装不下，OOM |

**PyTorch 约束**：`torch 2.10.0+cu128` 要求 `nvidia-nccl-cu12==2.27.5`，而 `ncclCommShrink` 符号在 2.27.x 引入，所以最低需要 2.27.x。

---

## 当前状态

- 服务器上 NCCL 版本：`nvidia-nccl-cu12==2.27.7`（2.27.5 PyTorch 要求版本的 patch，仍 segfault）
- vLLM 进程无法启动，GPU 空闲（4x RTX 5090，每张 32 GiB 空余）
- litellm proxy 运行中（port 4000），但后端 vLLM（port 8001）未运行
- OpenFOAM 优化 agent 未启动（依赖 vLLM）

---

## 可能的解决方向（待验证）

1. **升级 PyTorch 到 nightly**（可能包含修复了 Blackwell cuMemCreate 的 NCCL）：
   ```bash
   pip install --pre torch --index-url https://download.pytorch.org/whl/nightly/cu128
   ```

2. **从 NVIDIA 开发者渠道下载 NCCL**（不走 PyPI，使用官方 CUDA 12.9 Blackwell 补丁版）：
   - https://developer.nvidia.com/nccl/nccl-download
   - 需要登录，下载 `.deb` 或 `.tar.gz`

3. **更新 CUDA 驱动**（580.126.09 → 更新版本）：
   - RTX 5090 + 驱动 580.126.09 (2026-01-07) 存在 cuMemCreate bug
   - 更新驱动可能修复，但需要重启服务器

4. **找回原始 NCCL 2.29.7 构建**：
   - 原始版本可能来自 `pip install vllm` 时的依赖链（不同 index）
   - 检查 `/home/liumq/llm_download` screen 的启动命令

5. **使用 CPU-only LLM**（绕过 GPU NCCL 问题）：
   - 配置 agent 使用 litellm proxy（port 4000）对接其他后端
   - 或暂时使用小模型（单 GPU，不需要 NCCL）

---

## 服务器关键路径

```
模型:      /home/liumq/models/Qwen/Qwen2___5-72B-Instruct-AWQ
项目:      /home/liumq/manifold-cfd-opt
vLLM 日志: /home/liumq/log.vllm_new
           /home/liumq/log.vllm_server  (历史)
启动脚本:  /home/liumq/start_vllm.sh
Screen:    vllm_server (失败后退出), litellm_proxy (port 4000), llm_download
OF 源码:   /opt/openfoam13/etc/bashrc
案例目录:  /home/liumq/manifold_cases_3d
```

---

## OpenFOAM 3D 优化 agent 启动命令（vLLM 就绪后）

```bash
cd /home/liumq/manifold-cfd-opt
python3 run_agent.py --config config_manifold_3d_remote.yaml
```
