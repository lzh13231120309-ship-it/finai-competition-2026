# 模型选择与输入边界

检索日期：2026-10-10。下面的路由是本项目对公开方法的实现建议；必须检查工具当前支持范围。

| 对象/任务 | 主要路径 | 关键输入 | 缺失时处理 |
| --- | --- | --- | --- |
| 一般工商企业 | 多阶段 FCFF | 收入与经营利润、D&A、Capex、NWC、税率、资本成本、权益桥接 | 报告缺口；用户假设单独列示，不能冒充历史事实 |
| 银行 | 普通股剩余收益 RI | 普通股期初账面权益、未来 ROE/净利润、分红及资本约束、Ke | 阻止完整银行估值，保留数据就绪分析 |
| 稳定分红金融企业 | DDM（若工具支持） | 分红可持续性、利润与派息、资本留存、Ke | 没有资本约束或适用派息路径时不假定分红等于 FCFE |
| 同行定价比较 | PE、PB、EV/EBITDA、EV/Sales | 有来源的各行市场值与同期间分母、可比性说明 | 排除冲突样本或仅列观察；不捏造同行中位数 |
| 市场价格隐含要求 | 反向 DCF | 有日期的股价/市值、权益桥接、已固定其他假设 | 没价格不能反向；无解不扩边界追求“成功” |
| 输入不确定性 | 条件性模拟 | 明示分布、合理边界、相关性和随机 seed | 有情景但无数据分布时称假设模拟，不称实证概率 |

## 方法约束

FCFF 用 WACC，股权现金流与 RI/DDM 用 Ke。对比公司不仅看行业标签，还核对增长、风险、利润率、资本强度、会计政策及资本结构。[NYU：可比差异控制](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/controldifferences.htm)

银行的债务和营运资本具有不同经济含义，通常直接估值股权；RI 关注 ROE 超过 Ke 的部分，DDM 关注可分配且不损害资本要求的现金流。[NYU：金融服务企业估值](https://pages.stern.nyu.edu/~adamodar/pdfiles/papers/finfirm.pdf)

成长与再投资成对输入；稳态长期增长、回报与折现不能只选择其中两个。[NYU：终值与超额收益](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/termvalueexreturns.htm)
