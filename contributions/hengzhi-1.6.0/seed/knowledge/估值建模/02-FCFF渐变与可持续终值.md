# FCFF 渐变与可持续终值

检索日期：2026-10-10；本项目原创方法笔记，不含个股预测。

显性期先预测收入、毛利率、费用率、税率、D&A、Capex 和营运资本。经营驱动逐年趋向可解释的稳态，而不是把单个历史增速永久外推。当前本地模型是经营驱动预测，不自动生成三张完整勾稽预测报表。

```text
EBIT_t = revenue_t × (gross_margin_t − operating_expense_ratio_t)
NOPAT_t = EBIT_t × (1 − tax_rate_t)
FCFF_t = NOPAT_t + D&A_t − Capex_t − ΔNWC_t
稳定期 reinvestment_rate = g / ROIC
terminal_FCFF_{n+1} = NOPAT_{n+1} × (1 − g / ROIC)
TV_n = terminal_FCFF_{n+1} / (WACC_terminal − g)
```

稳定期增长要有再投资支撑；稳定回报、资本成本与长期增长需共同解释。[NYU：终值与超额收益](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/termvalueexreturns.htm)

本项目复核：终值在预测末年折现；不把末年异常资本开支直接永久延续。`WACC≤g` 时拒绝有效 Gordon 终值；`g/ROIC>1` 提示本模型稳态不成立。逐项展示终值占比、敏感性和权益桥接，不能为了降低终值占比随意修改增长或折现率。货币单位与名义/实际口径一致。

源码对照：[FinanceToolkit intrinsic_model](https://github.com/JerBouma/FinanceToolkit/blob/a232ddf84d4bb2da17e5385b7862873b6943c2a8/financetoolkit/models/intrinsic_model.py)。其基础 DCF 为固定增速现金流投影，多阶段驱动路径属于本项目的额外实现。
