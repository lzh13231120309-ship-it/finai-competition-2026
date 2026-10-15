# 工具入口与状态检查

本技能面向本地金融 Agent 的估值入口。以下为集成约定，不证明当前会话已暴露这些工具。

- `build_valuation_model(run_id?, forensic_run_id?, filenames?, industry?, overrides?, market?, forecast?, monte_carlo?, reverse_dcf?, peers?, equity_model?, allow_illustrative_model?, allow_estimated_shares?, export_formats?)`：优先 `run_id` 复用已核验财务运行。`filenames` 只在没有可复用运行时使用；附加对象的字段以工具 schema 为准。
- `read_valuation_model(run_id, section, offset, limit)`：读取建模后的具体结果及分页窗口。先读诊断/摘要，再读用户需要的假设、情景、可比或模拟部分。
- `export_financial_report(...)`：按实际 schema 传运行 ID 与导出格式；若建模入口已返回所需导出文件，则先核对这些文件，避免重复生成。
- CLI：`python -m server.valuation_cli build --run analysis.json --options options.json --output folder`。项目虚拟环境可用时使用其 Python；只有帮助输出确认该子命令存在，才能称为可执行入口。

## 避免低效回环

数据诊断有明确缺口 → 回读相应证据或列待补项；参数假设改了 → 新建运行。仅需补读结果页、导出文件、解释已有结论 → 复用既有运行。工具失败时先读具体错误；参数/schema 不存在时修正入口，而不是多轮重复扫描整份财报。

若只有旧版 `scenario_valuation` 可用，明确说明它的真实模型范围；可以按用户授权继续该范围，但不能把旧结果称为已完成多阶段、银行 RI、反向求解或模拟。单纯能启动工具不是估值通过诊断的证明。

## 市场数据辅助入口

若 `get_valuation_market_data` 实际存在，可传 `symbol`、可选 `benchmark`、`period`（`1y/2y/5y`）、`price_file`、`benchmark_file` 与 `currency`。工具可能从已授权行情源取得价格或回报并计算 OLS β；内网/离线场景可改用已有 CSV 快照。以返回值的观察日、来源、回报频率和计算样本为准，不能把文件修改时间当报价时间。

价格和 β 不代表完整资本成本：无风险利率、ERP、前瞻债务成本及债务权重仍需有来源输入。快照太旧、交易日不齐、币种不明或股票/基准区间错配时列明限制，不能把历史回归 β 称作当前精确风险参数。

`market-input-template.json` 是 **market 对象模板**，数值均为空；放入 options 的 `market` 键，填写或删除无需的空字段后使用。每字段来源会保留为 `field_sources`；顶层统一日期/来源只能在逐字段来源足够时汇总，不能用一条网站首页掩盖不同日期与来源。

## 当前引擎字段（生成技能时核对，本地版本可能更新）

- `market`：`risk_free_rate/equity_risk_premium/beta/cost_of_debt/debt_weight` 与来源、日期、币种齐备才推导 CAPM/WACC。`country_risk_premium` 为额外、单独说明的国家风险输入。数值比例用分数。
- `forecast`：`years` 为 5–15 的整数；`paths` 可给各年 `revenue_growth/gross_margin/opex_ratio/tax_rate/da_to_revenue/capex_to_revenue/receivable_days/inventory_days/payable_days`，长度等于 years；`stable` 设渐变终点；`terminal_roic` 为稳态回报假设。
- `monte_carlo`：`samples/seed/variables`，变量形状为 `{low, mode, high}`；默认独立三角分布。可用 `correlations` 对称变量映射或按 variables 顺序的方阵；矩阵须对角1、范围[-1,1]且正定。Gaussian copula 使用潜在正态相关性，并非经济驱动或现金流的 Pearson 相关性；非对称/非正定输入返回诊断。
- `reverse_dcf`：`parameter` 可为 `revenue_growth/gross_margin/wacc`，`market_equity_value` 或 `price` 与 `shares` 提供目标，`low/high` 指定区间。检查 `status/ok` 和估值匹配误差。
- 银行模型对应 `equity_model`，当前内核需要 `book_equity/net_income/cost_of_equity/roe/payout_ratio/terminal_roe`。工具 schema 未暴露该对象时先报告接口缺项；不将其偷偷塞入无关 overrides。
- `peers` 行：`company/industry/currency/scope/period/as_of/source/market_cap/enterprise_value/net_income/ebitda/revenue/book_equity`。目标公司的分母应从同年已核验事实取得。缺值留空，是否可用由诊断判断。

当前版本可生成普通企业三表预测，是否配平须检查 three_statements.ok 与勾稽结果；未通过时不得宣称完整。银行剩余收益路径若缺资本与信用风险情景，不能声称已完成银行资本约束建模；三个标签使用相同股权参数时应直接说明未形成差异情景。

`allow_illustrative_model` 只表示在未解决数据问题下明确允许示意计算，不把未核验事实变成已核验；`allow_estimated_shares` 的收益/EPS推导可能是加权平均股数，需与发行总股本分开标注。这些字段可用时也应保持真实数据质量声明。

## 离线 CLI 已核对入口

在项目目录执行 `python -m server.valuation_cli --help` 查看 `build/market`。例如：

```text
python -m server.valuation_cli build --run analysis.json --options options.json --output model-output --findings forensic.json --formats xlsx,docx,json,md
python -m server.valuation_cli market --symbol DEMO --benchmark INDEX --price-file stock.csv --benchmark-file index.csv --currency CNY --output market.json
```

`--findings` 可为风险对象列表或包含 findings 的法证运行；带 parent_run_id 时必须与分析运行对应。行情可选 `--as-of YYYY-MM-DD` 指定观察日上界。第二个例子使用本地 CSV，代码是示例标识，不是已取得真实行情。

模型目录保存完整 `run.json` 和选定格式报告，stdout 返回运行 ID、状态与导出路径；市场子命令返回 JSON，可用 `--output` 保存。模型受阻仍保留诊断，退出2；参数/输入错误退出2，导出失败退出1。输出目录已有 `run.json` 时要求换新目录，避免覆盖旧运行。CLI 内部估值使用当前引擎，不能将它宣传为完整三张预测报表或自动监管资本模型。
