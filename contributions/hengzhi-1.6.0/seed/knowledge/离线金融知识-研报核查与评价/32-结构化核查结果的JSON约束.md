标题：结构化核查结果的JSON约束
任务域：研报核查与质量评价
关键词：JSON,Schema,枚举,证据字段,机器校验
适用范围：本地agent结构化结果交换的建议格式
核验日期：2026-10-11
来源：[Basel Framework SRP36 Risk data aggregation and risk reporting](https://www.bis.org/committees/bcbs/basel-framework/standard/srp/36/inforce/2019-12-15/published/2019-12-15)；[CFA Standard V(C) Record Retention](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c)
资料性质：原创方法总结与本地操作建议；来源原文以其适用主体和时点为限；非官方竞赛规则

# 结构化核查结果的JSON约束

结构化结果需要保证状态与证据相容。建议每个结果具有claim_id、claim_text、claim_type、status、evidence_ids、calculation_id、severity、reason和limitations。主张类型与状态采用固定枚举，数字用数值类型保存，单位与币种独立字段，无法取得数值时用null并给missing_reason，不把“未知”混进数值字段。

操作程序：最小JSON Schema建议采用type:object，required包含claim_id、claim_text、status、evidence_ids、reason；status的enum为pass、error、pending、unverifiable、not_applicable，evidence_ids为字符串数组。另在程序层校验引用ID存在、计算输入存在、状态error具有差异说明、状态pass不依赖未知关键输入。Schema校验通过只是结构正确，不能证明事实正确。

合成例：合成记录可为{"claim_id":"C001","claim_text":"合成收入增长20%","status":"pass","evidence_ids":["E001","E002"],"calculation_id":"K001","reason":"同口径120/100-1=20%","limitations":[]}。E001与E002另有文档定位、期间、单位和原文字段，K001保存公式与输入ID；这些不是某家公司真实数据。

边界与输出：报告级对象还应包含report_id、input_hashes、knowledge_checked_at、rules_version、coverage和unresolved_items。不得让模型创建不存在的证据编号来满足格式要求，编号缺失要硬性报警。保存原始JSON与可读报告之间的对应关系，渲染阶段不能把pending改成pass。安全性校验与业务校验分开，机读输出仍需对重大结论进行原始证据复核。

核查留痕要求：上述程序是本库针对离线核查提出的操作建议，不是所列来源逐条规定。每次使用需登记被核查报告版本、相关主张编号、原文件页码、支持与反证、执行步骤、结果状态以及尚缺材料。若证据只能证明“发布者作出该陈述”，就不要扩展为“经济事实已被独立验证”。合成例仅用于测试流程，不能作为真实公司、真实市场或赛事事实检索。

复核验收：验收重点：claim_id唯一，evidence_ids引用已登记证据，数组不存在隐藏重复，数值字段不存字符串百分号。额外业务校验还应确认期间与单位齐全，计算输入属于相同可比口径。禁止仅凭格式有效把结果发布为可信；机器结构检查、来源检查和经济逻辑检查都应各有状态，并能显示未完成原因。

可机读最小Schema（建议示例）：
```json
{"type":"object","required":["claim_id","status","evidence_ids"],"properties":{"claim_id":{"type":"string"},"status":{"enum":["pass","error","pending","unverifiable","not_applicable"]},"evidence_ids":{"type":"array","items":{"type":"string"}}}}
```
