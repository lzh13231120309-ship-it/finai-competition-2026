# 衡知 金融投研 Agent

团队私有协作仓库，当前实现选题 **1 文档理解与核验、2 财报分析、4 估值推演**。网页显示中文解释，详细分析集中在一份Word。3、5、6、7尚未实现。

## 同伴第一次部署

1. 接受仓库邀请，用GitHub Desktop克隆至 **C:\FinAI\finai-competition-2026** 等纯英文路径。
2. 安装 **Python 3.13 64位**（含Python Launcher），双击 **安装环境.bat**，等待依赖与模型下载及检查完成。
3. 双击 **启动衡知.bat**，打开 <http://127.0.0.1:17904/>。

详细操作、故障排查和四人修改流程见 [衡知部署与四人协作指南 Word](docs/衡知部署与四人协作指南.docx)。部署脚本以Windows为目标；不同硬件仍需各人完成首次启动检查。

## 开发入口

主应用在`金融大赛_集成试用版`：`server/agent.py`是真实模型工具循环；`financial_agent.py`负责MinerU调度和核验；`research_agent.py`与`research_engine.py`负责财报分析与估值；`finance_delivery.py`负责一份Word交付；`web`为界面。`第一阶段_金融提取/finance_extract`保留解析工具。

环境、模型、安装日志在`runtime`，私人输入和聊天在应用`data`及提取模块`local_data`，均不提交。不要将本机总工作报告或密钥复制到仓库。

## 测试与合作

`金融测试集_100份`提供40份明确模拟材料及100份来源分组清单。60份公开PDF通过`scripts/download_public_samples.py`按原网址恢复，并验证SHA256。保留集不参与调参。

每人在`feature/任务名`分支修改，提交后发起Pull Request，另一人审查后合并。禁止强推main。单元检查与真实模型检查分别记录，小样本通过数量不等于总体准确率。

## 第三方来源

[MinerU](https://github.com/opendatalab/MinerU)用于文档解析，[Qwen3 4B GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF)用于本地工具调用。模型分别从官方来源下载，Qwen指纹在安装和检查时核验。许可证按原项目执行，本仓库不重新授权第三方成果，也未授权对外公开。
