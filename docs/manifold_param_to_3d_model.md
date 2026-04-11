# 由歧管优化参数生成三维数模（工具说明）

本工具把歧管优化参数空间中的 `logit_1/logit_2/logit_3` 转成与 CFD 模板一致的几何分段（`outlet_weights/y_levels`），并生成一套**三维数模输出**：
- OpenFOAM 3D case（可直接用于 blockMesh 生成网格）
- 轻量可视化模型（OBJ/STL）
- 参数与派生量（JSON）

工具脚本：
- [generate_manifold_3d_model.py](file:///d:/TRAE/manifold-cfd-opt/scripts/generate_manifold_3d_model.py)

---

## 1) 输入与参数空间

输入：
- `logit_1, logit_2, logit_3`

派生逻辑与优化系统一致（复用 evaluator 中的实现）：
- 计算 `logits=[logit_1, logit_2, logit_3, 0.0]`
- softmax 得到 4 个出口权重 `outlet_weights`
- 将总高度 `H` 按权重累积得到分段边界 `y_levels=[0, ..., H]`

这与系统中 `DesignParams` 的定义一致：[base.py](file:///d:/TRAE/manifold-cfd-opt/evaluators/base.py)

---

## 2) 运行方式（CLI）

在 `manifold-cfd-opt` 目录下：

```bash
python scripts/generate_manifold_3d_model.py --logit1 1.2 --logit2 -0.7 --logit3 0.3 --n-cores 8 --out ./.tmp_model
```

参数：
- `--n-cores`：写入 `system/decomposeParDict` 的并行核数（模板需要）
- `--template-dir`：默认 `templates/manifold_3d`
- `--out`：输出目录（不指定则生成 `manifold_3d_model_YYYYmmdd_HHMMSS`）

---

## 3) 输出结构

输出目录中包含：
- `case/`：OpenFOAM case（从 `templates/manifold_3d` 渲染）
  - `system/blockMeshDict`：3D 分段网格（y 方向按 `y_levels` 分块）
  - `system/decomposeParDict`：并行分解（需要 `n_cores`）
- `model.obj`：可视化几何（外形盒体 + outlet 面的分段线）
- `model.stl`：可视化几何（外形盒体）
- `model.json`：输入参数与派生量（`outlet_weights/y_levels/n_cells_*`）
- `README.txt`：输出说明

---

## 4) 与“真实几何”的关系（重要）

当前 `templates/manifold_2d/manifold_3d` 的几何外形是固定的矩形通道；优化参数主要改变：
- outlet 在 y 方向的分段比例（即 outlet patch 的高度占比）
- y 方向网格分配（`n_cells_y`）

因此你会在 `model.obj` 的 outlet 面看到分段线的位置变化，而外轮廓本身不变。

如果你后续要做“真正的 CAD 三维实体（STEP/IGES）”并让参数影响外形，需要先定义：
- 参数到 CAD 轮廓/特征（圆角、扩压段、歧管分支、厚度等）的映射
- 再选择 CAD 后端（CadQuery/pythonOCC 或 gmsh OpenCASCADE）来生成实体并导出 STEP。

