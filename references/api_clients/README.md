# 官方 API 客户端参考实现 (Tower 施工指南)

本目录包含了三大主流模型生态的官方 Python SDK 核心调用范例。所有代码均已在当前环境中通过语法检查与 SDK 类型验证。

在 Tower 工作流中，负责实现 `bench_harness/drivers/` 的 Worker 可直接参考此处的函数封装、类型转换与错误重试逻辑。

---

## 包含文件与支持协议

| 文件名 | 适用协议与模型 | 核心实现细节 |
| :--- | :--- | :--- |
| `openai_tool_call.py` | OpenAI (GPT-4o, o1/o3), DeepSeek, Qwen, Moonshot, GLM 等兼容协议 | • `tools: [{"type": "function", ...}]`<br>• `choice.message.tool_calls`<br>• `reasoning_content` (思考链提取)<br>• `role: "tool"` 回传 |
| `google_genai_tool_call.py` | Google Gemini 2.0 / 1.5 原生 REST 协议 | • 基于最新官方 `google-genai` (2.x) SDK<br>• `types.Tool(function_declarations=...)`<br>• `part.function_call` 解析<br>• `Part.from_function_response` 回传 |
| `anthropic_tool_call.py` | Anthropic Claude 3.5 / 3.7 原生协议 | • `tools: [{"name", "input_schema"}]`<br>• `block.type == "tool_use"` 解析<br>• `thinking` block 提取<br>• `tool_result` 块回传 |

---

## 完整源码参考

若需要查阅官方 SDK 内部更复杂的流式处理（Streaming）、异步客户端（Async）或更多测试用例，可直接查看克隆在同级目录的官方源码仓库：
- `references/repos/openai-python/`
- `references/repos/python-genai/`
- `references/repos/anthropic-sdk-python/`
