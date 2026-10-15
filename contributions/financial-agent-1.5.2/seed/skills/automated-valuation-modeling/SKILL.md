---
name: automated-valuation-modeling
description: 基于已核验财报构建多阶段 FCFF、银行剩余收益或可比公司估值模型，复核市场输入和终值，提供反向 DCF、可复现模拟及可编辑 Excel；用户要求自动估值、DCF、银行股权估值或估值工作簿时使用。
---

# 自动化估值建模

将历史财务证据、市场观察和未来假设分别记录，交付可复算模型、诊断与可编辑工作簿。方法资料见 [sources.md](references/sources.md)；方法不是某家公司的证据。

## 执行入口与节奏

先查看当前工具清单或 CLI 帮助，确认真实可用接口。技能文件出现工具名不等于工具已接入；发现缺项时说明具体缺项并继续可完成部分。

1. 优先复用同一公司的既有财务分析 `run_id`。没有才调用实际可用的 `analyze_financial_report`；已有法证运行可直接引用，不为建模重复扫描。不同公司的运行不能混合。
2. 读取摘要与数据诊断；按 [readiness-checklist.md](references/readiness-checklist.md) 判断数据是否足以建模，按 [model-selection.md](references/model-selection.md) 选模型。仅对缺失或矛盾的字段回读对应证据。
需要价格/β时，可使用实际存在的 `get_valuation_market_data`，或读取已有本地 CSV 快照；它不自动提供有依据的无风险利率、ERP 或债务成本。市场入口与快照时效见 [tool-contract.md](references/tool-contract.md)。

3. 若存在 `build_valuation_model`，用已核对输入一次提交：`run_id`、可选 `forensic_run_id`，以及本轮所需的 `industry`、`overrides`、`market`、`forecast`、`monte_carlo`、`reverse_dcf`、`peers`、可选 `equity_model`、`export_formats`。没有分析运行时才传 `filenames`。字段以工具实际 schema 为准。
4. 若存在 `read_valuation_model(run_id, section, offset, limit)`，读取诊断、假设来源、主要估值和用户要求的情景；按 `next_offset/has_more` 继续对应部分。不要因摘要缺一列就重建整个模型。诊断指出真实数据缺口时先补证据；参数修订才创建新运行。
   可用章节除 `summary/projection/scenarios/assumption_sources/checks/sensitivity/reverse_dcf/monte_carlo/peers/options` 外，还包括：`three_statements`（该情景的平衡三表与勾稽结果）、`fcff_cross_check`（三表反推 FCFF 与驱动法逐项对账）、`discount_rate_detail`（折现率来源分层与参数归因）、`method_triangle`（方法加权区间与离散度）、`scenario_distinctness`（三情景是否真的互不相同）、`stress_diagnostic`、`financing_rates`、`terminal_policy_checks`。以实际返回为准。
5. 使用实际可用的 `export_financial_report` 或建模工具返回的导出结果。核对文件存在、可打开，并检查 Excel 输入区和公式，再给用户文件链接。

本地 CLI 若已实现，可运行 `python -m server.valuation_cli build --run analysis.json --options options.json --output folder`。先用 `--help` 确认参数；CLI 不可用时，不反复试探目录或编写替代实现绕过项目入口。入口细节见 [tool-contract.md](references/tool-contract.md)。

## 三类来源

- **公司事实**：保持原期间、币种、合并/母公司口径、证据 ID；待复核事实不能自动升级为已核验。
- **市场观察**：价格、利率、β、ERP、同行倍数记录观察日、来源、币种和方法；历史数据保留其真实日期。联网能力不能证明已取得行情。取不到就留空或接受明确标注的用户假设，不能写成“最新市场数据”。
- **建模假设**：增长、利润率收敛、资本效率、长期税率、稳态 ROIC、终值增长、分布范围均列明理由与覆盖记录。基于历史校准也仍是预测假设。模板见 [market-input-template.json](references/market-input-template.json)。

## 决策与诊断

- 工商企业用 FCFF/WACC，衔接经营增长、利润率、资本开支与营运资本；渐变期过渡到稳态。终值用稳态下一年现金流，并校验 `再投资率 = g / ROIC`；`WACC <= g` 不生成有效 Gordon 终值。
- 银行优先剩余收益（RI）或在适用且工具支持时使用 DDM；缺普通股账面权益、收益/分红路径或资本约束时返回数据缺口，不能切回通用工商 FCFF 给出确定估值。
- 可比估值核对各行日期、币种、期间、口径和分母。PE/PB 为股权倍数，EV/EBITDA、EV/Sales 为企业价值倍数；样本筛除和可比性判断留痕。
- 反向 DCF 在其他参数固定的条件下求一个隐含参数；报告搜索范围、求解结果与残差。无解或多解按诊断展示，不能同时反推多变量再宣称唯一市场预期。
- Monte Carlo 写明分布、边界、相关性、样本量及 seed；使用 Gaussian copula 时说明其矩阵为潜在正态相关性，不是经济变量的 Pearson 相关性；分位数是所设假设体系的模拟结果，不是涨跌概率或实证置信区间。非法样本数量及处理方式必须披露。
- 法证压力必须逐条连接风险证据、参数改动和现金流变化。严重度只是规则输入，不是舞弊概率；避免同一风险同时下调现金流又任意抬高折现率而重复计量。

## 配平三表与结论稳健性（引擎支持时必查）

驱动法只沿一条线外推经营变量、算出 FCFF，不生成资产负债表。引擎若支持逐年三表联动，就必须把它当成交付的一部分而不是可选装饰——"可验证"和"金融逻辑"两项评分靠它。

- **五项勾稽逐项报数值，不能只说"已配平"。** 读 `three_statements.checks`：资产=负债+权益、资产负债表现金=现金流量表期末现金、留存收益滚存、非流动经营资产滚存、现金非负。把每一项的**最大残差**写进报告（正常应为 0 元或浮点量级）。任何一项 `ok=false` 时，该情景的估值结论不得使用，只能写"模型未通过自检"。
- **报平衡模式与计息口径。** `mode=cash_plug` 表示现金是被动平衡项（净现金公司，增长靠内生现金），`debt_plug` 表示有息负债被动平衡（缺口靠借款）。`interest_basis` 为 `average_balance` 时，融资与利息互为因果、由迭代求解——这正是压力情景下利息被低估的常见来源，不要写成"按期末余额简单计息"。
- **FCFF 交叉验证要报差异归因。** `fcff_cross_check` 把三表反推的 FCFF 与驱动法逐项对账。核心口径（应收/存货/应付）从第 2 年起应逐位一致；第 1 年差异来自期初营运资本口径（驱动法用"收入×周转天数"反推、周转天数按平均余额计算，三表用期末时点值）。把差异**归因到具体科目**，而不是笼统写"口径不同"。
- **折现率必须分层交代来源。** 读 `discount_rate_detail`：`statement_calibrated=false` 时必须写"折现率全部来自默认假设，未经财报校准"；为 true 时逐项区分财报实算 / 财报派生 / 外部输入 / 默认值。`attribution` 里外部参数合计占比超 50% 时，写明"折现率主要由外部参数决定，核验后才能定稿"——这把研究力气导向正确的地方，比笼统说"WACC 敏感"有用得多。
- **终值占比超阈值必须展开。** `terminal_policy_checks.terminal_value_share=false`（终值占企业价值超 75%）时，展示敏感性并说明稳定期依据，不能只给一个 DCF 数字。`terminal_growth_within_economy=false` 是逻辑错误（永续增速超过长期名义 GDP），必须改假设而非解释。
- **方法分歧大时禁止给单点。** `method_triangle.dispersion_alert=true` 时，结论必须是"按方法分布的价值区间"，说明各方法适用条件与差异来源；取中位数当结论、或由此推出目标价，都属越界。
- **三情景必须真的互不相同。** `scenario_distinctness` 里 `equity_value_differs_from_base=false` 的情景不得当作有效对照。压力情景通常是两层：证据驱动的传导 + 结构性情景兜底（扫描无发现时兜底保证压力情景仍成立）。`stress_diagnostic.evidence_backed=false` 时必须写明"压力情景完全由结构性情景假设构成"，不得说成由证据链推出。`floor_applied` 里 `binding=false` 的项是"证据已覆盖、兜底让位"，不要重复计一次。
- **利息不能为零。** 三表计息依赖 `financing_rates` 的存款收益率与借款成本。该字段缺失或为 0 时，说明利润表被系统性低估（现金充裕的公司尤其明显），必须写明而不是把 0 当作已计息。

## 交付

优先提供模型工作簿、完整运行结果和短说明：模型选择、关键价值区间、市场观察日期、最大敏感项、阻塞/待复核项目。每股价值依赖可靠股数和权益桥接；两者缺失时保留企业价值或条件估值。工具未输出或未通过检查的模块不能列作“已完成”。

Excel 内至少能找到历史来源、假设、预测、折现、EV→权益桥接、敏感性和诊断；具体页签以实际输出为准。保留公式与数值精度，说明缓存未重算的限制。模型区间不能自然推导为目标价、买卖建议或预期收益。

## 交付前结论校验

新模型的 `valuation_review` 会列默认驱动、权益桥接中未核实的零值假设、补数建议，以及已提供同行快照与 DCF 的方法差异。若真实接口支持，用 `read_valuation_model(section="valuation_review")` 查看；它是条件诊断，不是置信概率。`conditional` 不是计算失败，也不表示关键输入已被核验。不要为追求通过标记重复运行模型。

同行差异阈值 50% 是本项目的诊断规则，非学术硬约束。同行倍数相关，不应被计为多个独立概率证据；EV-only 结果不能与股权价值直接比较。账面净资产仅作会计参照，不是资产评估、清算价值或价值下限。项目预设方法权重不是统计校准结果，不能作为独立目标价依据。

三表预测是否可用以该运行的 `model_plan.balanced_three_statements`、`three_statements.ok` 与勾稽结果为准，不能因某版本提供该模块就声称所有公司的预测都通过。
