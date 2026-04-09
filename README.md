# manifold-cfd-opt

歧管（manifold）多出口流量分配优化：使用贝叶斯优化在参数化几何/开口空间内搜索，使：

- 流量均匀性（Flow Uniformity）最大化：等价于最小化各出口质量流量的变异系数 `cv = std(m_dot)/mean(m_dot)`
- 压力损失（Pressure Drop）最小化：`ΔP = p_inlet_avg - mean(p_outlet_i_avg)`

## 运行方式（远程 OpenFOAM）

1. 配置 SSH 与 OpenFOAM 环境（服务器上需可运行 `blockMesh / simpleFoam / decomposePar`）
2. 编辑 [config_remote_openfoam.yaml](file:///d:/TRAE/manifold-cfd-opt/config_remote_openfoam.yaml)
3. 启动优化：

```bash
cd manifold-cfd-opt
python run_optimizer.py --config config_remote_openfoam.yaml
```

## 监控面板（本地）

默认端口为 `8090`（可用 `MONITOR_PORT` 覆盖）：

```bash
cd manifold-cfd-opt
MONITOR_HOST=127.0.0.1 MONITOR_PORT=8090 python monitor/run_server.py
```

## 参数化空间（当前模板）

当前 `templates/manifold_2d` 使用 4 个出口开口高度的 softmax 参数化：

- BO 输入：`logit_1, logit_2, logit_3`
- 内部映射：`softmax([logit_1, logit_2, logit_3, 0]) -> [w1,w2,w3,w4]`
- 输出几何：右侧边界被分割为 4 个 outlet patch，其高度比例为 `w1..w4`（和为 1）

该参数化等价于“用控制点定义出口开口分布曲线”的一种稳定实现方式，天然满足正值与归一化约束，适合 BO。

## 模板与指标

- OpenFOAM 模板：[templates/manifold_2d](file:///d:/TRAE/manifold-cfd-opt/templates/manifold_2d)
- 结果提取：通过 `surfaceFieldValue` functionObject 输出各 outlet 的 `sum(phi)` 与 `areaWeightedAverage(p)`，优化器读取最后一次写出的值计算 `cv` 与 `ΔP`。
