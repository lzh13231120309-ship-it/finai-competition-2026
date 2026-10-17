# 估值契约（valuation.py 实际实现）

本文件描述 `server/valuation.py` **实际怎么算**，不是设计意图。
所有数值都用 `Decimal`，比例统一为分数（`0.2` = 20%），金额单位与输入事实一致（元）。

---

## 一、核心公式

```
nwc_days = receivable_days + inventory_days - payable_days

对 t = 1..years：
    revenue_t    = revenue_{t-1} × (1 + revenue_growth)
    EBIT_t       = revenue_t × (gross_margin - opex_ratio)
                   ── t == 1 时再减 revenue_1 × impairment_to_revenue（一次性税前损失）
    NOPAT_t      = EBIT_t × (1 - tax_rate)
    D&A_t        = revenue_t × da_to_revenue
    Capex_t      = revenue_t × capex_to_revenue
    ΔNWC_t       = revenue_t × nwc_days / 365  −  revenue_{t-1} × nwc_days / 365
    FCFF_t       = NOPAT_t + D&A_t − Capex_t − ΔNWC_t
                   ── t ≤ 2 时再乘 (1 − near_term_fcff_haircut)
                   ── 再乘 (1 − sustainable_profit_discount)

PV_explicit = Σ FCFF_t / (1 + WACC)^t
TV          = [FCFF_n × (1 + g)] / (WACC − g)          ← Gordon，用"下一年"口径
EV          = PV_explicit + TV / (1 + WACC)^n
Equity      = EV − net_debt − minority_interest + non_operating_assets
每股价值     = Equity / shares        （仅在 shares > 0 时输出）
```

几个容易被误读的点：

- **`t == 1` 的 `ΔNWC` 不是全量营运资本占用。** `prev_nwc` 初始化为
  `revenue_0 × nwc_days / 365`，所以第 1 年只计增量。这是有意的：期初营运资本已经
  在基期资产负债表里，重复计入会双重扣减 FCFF。
- **减值只落在首年，且只进 EBIT。** 它不会联动减少净债务或净资产科目。
  如果现实中的减值同时减少股东权益，需要人工判断是否在 `net_debt` / `non_operating_assets`
  里体现——引擎不会自动做。
- **`sustainable_profit_discount` 作用在整条现金流上**，包括推出终值的末年 FCFF，
  所以它对估值的影响远大于同量级的 `near_term_fcff_haircut`（后者只打前两年）。
  这个折让乘在`FCFF`上而非`EBIT`上，含义是"可分配给股东的现金流低于账面口径"。
- **股利/利息不在 FCFF 口径里。** FCFF 是全体资本提供者的现金流，
  净债务在 EV → Equity 的桥接处一次性扣除。不要重复在预测期里扣利息。

---

## 二、默认假设（`DEFAULT_ASSUMPTIONS`）

| 参数 | 默认值 | 口径与来源 |
| --- | --- | --- |
| `years` | 5 | 明确预测期。竞赛 MVP 要求 3 年，可覆盖为 3 |
| `revenue_growth` | 0.08 | 年化收入增速。引擎不自动取历史 CAGR，需人工给依据 |
| `gross_margin` | 0.30 | 可由 `metrics.gross_margin` 自动填充 |
| `opex_ratio` | 0.15 | 销售+管理+研发费用 / 收入；可由 `gross_margin − net_margin` 反推 |
| `tax_rate` | 0.15 | 有效税率，非法定税率。高新技术企业常用 15% |
| `da_to_revenue` | 0.05 | 折旧摊销 / 收入 |
| `capex_to_revenue` | 0.06 | 资本开支 / 收入 |
| `receivable_days` | 60 | 可由 `metrics.receivables_days` 自动填充 |
| `inventory_days` | 60 | 可由 `metrics.inventory_days` 自动填充 |
| `payable_days` | 60 | **引擎不自动取**，需看应付账款周转天数人工给 |
| `wacc` | 0.10 | 行业中性默认值，**不是这家公司的**。要换需给依据 |
| `terminal_growth` | 0.03 | 长期通胀+实际增长，不得超过长期 GDP 增速 |
| `impairment_to_revenue` | 0 | 一次性减值 / 首年收入，只进首年 EBIT |
| `near_term_fcff_haircut` | 0 | 前两年 FCFF 折减比例 |
| `sustainable_profit_discount` | 0 | 整条 FCFF 流的折让比例 |
| `net_debt` | 0 | 有息负债 − 货币资金；取不到会保持 0 并列入 `missing_inputs` 语义 |
| `minority_interest` | 0 | 少数股东权益，从权益价值中扣除 |
| `non_operating_assets` | 0 | 非经营性资产（如 FVTPL 金融资产），加回权益价值 |
| `shares` | None | 总股本；为 None 时不输出每股价值 |

**后三个旋钮（`impairment_to_revenue` / `near_term_fcff_haircut` /
`sustainable_profit_discount`）是"法证发现 → 估值"的唯一落点。**
它们必须真实存在于模型中，否则对应规则会被静默记为"参数缺失、无法传导"，
压力情景与基准情景数值一模一样而报告上看不出任何异常。
（实测踩过：11 条规则里有 4 条映射到不存在的参数名，压力情景完全没动静。）

---

## 三、参数的自动填充（`from_metrics`）

调用 `scenario_valuation` 时，引擎从**已核验事实**与**指标**里抽取建模起点：

| 参数 | 来源 | 条件 |
| --- | --- | --- |
| `revenue_base` | 事实 `revenue`（verified，优先合并口径、最新期） | **硬性**；缺则 `ok=false` |
| `gross_margin` | 指标 `gross_margin` 最新期 | — |
| `opex_ratio` | `gross_margin − net_margin`，下限 0.01 | 需要 `net_margin` |
| `receivable_days` | 指标 `receivables_days` 最新期 | — |
| `inventory_days` | 指标 `inventory_days` 最新期 | — |
| `net_debt` | `short_term_debt + long_term_debt + bonds_payable − cash` | 长短明细全缺时回退 `borrowings − cash` |
| `minority_interest` | 事实 `minority_equity` | — |
| `non_operating_assets` | 事实 `financial_assets_fvtpl` | — |

关于 `net_debt` 的一个关键纪律：**长短借款明细与「計息借貸」总额是两套列报习惯，
不能相加**，否则重复计债。有明细只用明细，明细全缺才回退总额科目。

`payable_days`、`wacc`、`terminal_growth`、`revenue_growth`、`tax_rate`、
`da_to_revenue`、`capex_to_revenue` 引擎**不会自动取**，保持默认值。
这是刻意的：它们要么依赖行业与市场数据，要么依赖管理层计划，
自动填会制造"看起来很确定"的假象。要在交付物里说明哪些是默认值。

---

## 四、法证发现 → 参数的传导

`RISK_TO_PARAM` 的 14 条映射见 SKILL.md 的传导表。计算方式：

```
weight = SEVERITY_WEIGHT[severity]        # critical 1.0 / high 0.7 / medium 0.4 / low 0.15
delta  = step × weight
```

然后按参数类型分三种处理：

1. `terminal_growth`：`direction > 0` 时 `g ← g − delta`，否则 `g ← g + delta`，下限 0。
2. `direction > 0`：`p ← base + |delta × max(base, 1)|`
3. `direction < 0`：`p ← base − |delta × max(base, 1)|`

**`max(base, 1)` 是理解幅度的关键**：
- 比例类参数（`wacc` = 0.10、`gross_margin` = 0.60）的 base < 1，所以 `max` 取 1，
  改动是**绝对百分点**：`delta` 直接相加。
- 天数类参数（`receivable_days` = 45）的 base > 1，所以改动是**乘性的**：
  `delta × 45`。

改完后的夹逼（clamp）：

| 参数 | 下限 | 上限 |
| --- | --- | --- |
| `gross_margin`、`opex_ratio` | 0.01 | — |
| `impairment_to_revenue`、`near_term_fcff_haircut`、`sustainable_profit_discount` | 0 | 0.9 |
| `net_debt` | 0 | — |
| `terminal_growth` | 0 | — |

上限 0.9 的理由：超过 90% 的折让在语义上已经不是"压力情景"而是"公司没了"。

### 实测幅度参考（`revenue_base = 430 亿`，基准 WACC 10%、g 3%）

| 规则 | 严重度 | 参数改动 | 效果 |
| --- | --- | --- | --- |
| `R_AR_01` | high | `receivable_days` 45 → 51.3 | +6.3 天（+14%） |
| `R_CFO_01` | high | `wacc` 0.100 → 0.1035；`g` 0.030 → 0.0265 | +0.35pp / −0.35pp |
| `R_MARGIN_01` | medium | `gross_margin` 0.600 → 0.592 | −0.8pp |
| `R_GW_01` | medium | `impairment_to_revenue` 0 → 0.012 | 首年 1.2% 收入的税前损失 |

这份参考说明**单条规则的幅度是刻意保守的**：四条规则叠加后压力情景权益价值
从 1,575.7 亿降到 1,387.6 亿（约 −11.9%）。如果交付物里压力情景只比基准低 1%–2%，
说明严重度被低估或规则命中偏少，要在报告里点出来，而不是宣称"风险对估值影响有限"。

**`severity` 标错会直接改变估值结论**，所以法证层的严重度必须逐条复核，
这也是两个技能必须串联使用的根本原因。

### 未匹配规则的两种情形（都进 `unmatched_risks`）

1. `该规则未定义估值传导路径` —— 该 `rule_id` 不在 `RISK_TO_PARAM` 里。
2. `参数 X 缺失，无法传导` —— 映射的参数为 `None` 或不在 params 里。

两类都必须在交付物里列为**待补假设**。`data_confidence` 是特例：
它被显式跳过（`continue`），表示"整体降级为参考值"，不作为数值参数参与计算。

---

## 五、拒绝计算与警告

### 硬拒绝

```python
if g >= wacc:
    # ok=False，enterprise_value=None，equity_value=None
    # 警告："永续增长率不低于 WACC，终值公式不成立；已拒绝计算，请检查假设。"
```

这是**设计上的拒绝，不是缺陷**。Gordon 模型在 `g >= WACC` 时终值趋于无穷或为负，
任何数值输出都没有意义。遇到这个警告不要绕过，回去改假设。

### 软警告

`PV_terminal / EV > 0.75` 时追加警告：
"终值占企业价值超过 75%，结论对永续假设高度敏感，必须展示敏感性矩阵。"

用默认参数（5 年、g 3%、WACC 10%、收入增速 8%）跑，终值占比通常在 **72%–76%**，
即**默认参数几乎必然触发或逼近这条警告**。这不是 bug，是 DCF 的固有性质：
明确预测期越短，终值占比越高。处理方式是把敏感性矩阵放进交付物，而不是把警告删掉。

降低终值占比的正当手段：延长 `years`、提高 `revenue_growth`、提高 WACC 与 g 的差。
不要为了"好看"而调这些参数——它们每一个都要有依据。

---

## 六、敏感性矩阵（`sensitivity`）

默认 5×5：

- WACC 轴：`wacc_center + {-2,-1,0,+1,+2} × 0.01`
- 永续增长率轴：`g_center + {-2,-1,0,+1,+2} × 0.005`

每个单元格是按对应 WACC 与 g **完整重算**的权益价值（不是线性插值）。
中心格应等于该情景的 `equity_value`（可用来验证矩阵没算错）。

实测（基准情景，中心 10% / 3% = 1,575.7 亿）：

| g \ WACC | 8% | 9% | 10% | 11% | 12% |
| --- | --- | --- | --- | --- | --- |
| 2.0% | 1,898.5 | 1,627.8 | 1,425.1 | 1,267.6 | 1,141.7 |
| 2.5% | 2,035.9 | 1,724.0 | 1,495.4 | 1,320.7 | 1,183.0 |
| 3.0% | 2,200.9 | 1,836.1 | **1,575.7** | 1,380.5 | 1,228.9 |
| 3.5% | 2,402.6 | 1,968.6 | 1,668.3 | 1,448.3 | 1,280.2 |
| 4.0% | 2,654.6 | 2,127.6 | 1,776.4 | 1,525.7 | 1,337.8 |

单位：亿元。**区间宽度 1,141.7 – 2,654.6 亿（约 2.3 倍）**，
这说明对成熟期假设敏感的公司，DCF 单点值几乎不可用——必须给区间并说明主要敏感方向。
矩阵单元格不包含 `value_per_share`（`sensitivity()` 调用 `dcf()` 时不传 shares）。

---

## 七、`overrides` 的使用纪律

`scenario_valuation(overrides={...})` 会覆盖任意驱动变量，值经 `Decimal` 转换后生效。
用于：

- 把 `wacc` 换成按 CAPM 算出的公司专属值（要写清无风险利率、β、风险溢价的来源）。
- 把 `terminal_growth` 换成长期通胀目标。
- 把 `payable_days` 换成实际应付账款周转天数。
- 把 `years` 改成 3（竞赛 MVP 要求）。

**不要把 `overrides` 当调参旋钮**。任何覆盖都要在交付物里写出"原值 → 新值 → 依据"。
覆盖不会进入 `risk_adjustments` 记录（那是有法证来源的传导），
所以人工覆盖必须自己留痕，否则报告无法复现。

---

## 八、输出字段速查

`scenario_valuation` 返回：

```
ok                    # 三情景中至少有一个算出价值
run_id / parent_run_id / forensic_run_id
findings_used         # 参与压力传导的 rule_id 列表
range                 # {equity_value_low/high/mid, text, disclaimer}
scenarios             # {base|optimistic|stress: {...}}（不含 sensitivity）
  ├ label / note / parameters / projection / valuation
  ├ risk_adjustments  # 传导明细 [{source, parameter, from, to, path}]
  └ unmatched_risks   # 未匹配规则
sensitivity_base      # 基准情景 5×5 矩阵
risk_adjustments_stress
inputs_used           # 每个建模输入的来源（期间/口径/证据 ID/推导式）
missing_inputs        # 缺失的硬性输入
complete_result       # 完整 run.json 的下载路径
```

`valuation` 子字段：`ok`、`wacc`、`terminal_growth`、`pv_explicit_fcff`、
`terminal_value`、`pv_terminal_value`、`terminal_value_share`、`enterprise_value`、
`net_debt`、`minority_interest`、`non_operating_assets`、`equity_value`、
`warnings`、以及（有 shares 时）`value_per_share` / `shares`。

`note` 与 `range.disclaimer` 明确了三件事：压力情景参数由法证发现推导而非手填、
输出是情景分布区间而不是目标价、不构成投资建议。**交付物必须保留这层表述。**
