# Financial document extractor 2.0

提供 parse_pdf、extract_tables、extract_financial_fields、list_schemas、validate_financial_records 五个MCP工具，以及 cli.py。字段定义来自 schemas.json 与 engine.py。

主启动脚本自动创建独立 .venv-doc-mcp、安装 requirements.txt 并连接服务，无需手工填写开发者路径。源文件均本地解析，不主动上传文档。宿主模型若使用云端API，其接收内容由宿主应用控制。

原生PDF解析可跨平台。扫描OCR在macOS使用Apple Vision/Swift，在其他系统需要Tesseract及语言数据。缺失时显式报错，不冒充成功。审计输出写入接收方自己的 data/plugin-audit，发布包不带原有日志、测试原文或运行结果。

依赖许可证见 THIRD_PARTY.md；验证范围见 VERIFICATION.md。
