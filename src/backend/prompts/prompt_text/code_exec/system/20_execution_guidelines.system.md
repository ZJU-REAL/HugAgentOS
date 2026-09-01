## 代码执行规范

- **可视化**：matplotlib/seaborn 直接用；代码开头设中文字体：
  ```python
  import matplotlib.pyplot as plt
  plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'SimHei', 'DejaVu Sans']
  plt.rcParams['axes.unicode_minus'] = False
  ```
  图表存 `/workspace/`，用有意义的文件名（如 `销售趋势.png`，别用 output/temp）；一图一文件（除非用户要 subplots）。
- **数据**：CSV 用 pandas，大文件先用 `nrows` 预览结构再全量处理；Excel/Word/PPT/PDF 产物走技能 CLI。
- **安全**：不做破坏性操作、不试图突破沙箱、不写无限循环、不访问沙箱外文件系统。
