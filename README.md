# Forecaster

页面：https://debidong.github.io/forecaster/

一页静态网页。

- **daily**：10 年期美债收益率（`DGS10`）、布伦特油价（`DCOILBRENTEU`）、投资级利差（`BAMLC0A0CM`），来自圣路易斯联储（FRED）。
- **quarterly**：微软、甲骨文、亚马逊、Alphabet 的未交付合同金额，以及英伟达应收账款周转天数，来自美国证监会（SEC EDGAR）的 `10-Q` 与 `10-K`。

每个工作日由 GitHub Actions 运行 `scripts/update.py` 自动拉取最新公开数据；有新数字时提交并发布到 GitHub Pages。
