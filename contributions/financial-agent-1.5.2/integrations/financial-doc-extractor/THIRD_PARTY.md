# 第三方依赖与来源

本插件依赖以下开源库，部署时按 requirements.txt 安装并遵循各自许可证。表中版本用于接口验证，不代表接收方已安装。

| 项目 | 本次验证版本 | 用途 | 许可证/来源 |
|---|---|---|---|
| MCP Python SDK | 1.23.1 | MCP 服务、工具和 Prompt、协议测试 | MIT；https://github.com/modelcontextprotocol/python-sdk |
| pdfplumber | 0.11.10 | 原生 PDF 表格、坐标及单元格 | MIT；https://github.com/jsvine/pdfplumber |
| PyMuPDF | 1.28.2 | PDF 文本、页面渲染、Tesseract OCR 入口 | 包元数据标注 AGPL-3.0 / Artifex 商业双许可证；https://github.com/pymupdf/PyMuPDF |
| Apple Vision / Swift | 当前 macOS SDK | 可选本地中英文 OCR | Apple 系统组件；https://developer.apple.com/documentation/vision/recognizing-text-in-images |
| Tesseract 语言数据 | 本次未配置 | 可选跨平台 OCR | 使用者自行安装的版本需另行记录；https://github.com/tesseract-ocr/tessdata |

相关接口文档：

- 表格提取策略与限制：https://github.com/jsvine/pdfplumber#extracting-tables
- PyMuPDF OCR：https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html

依赖许可证不自动决定你自己代码的许可证。本次未替你选择项目许可证，也未打包第三方模型权重。参赛模型及其版本、调用方式和原项目其他参考来源需由实际使用情况补齐。
