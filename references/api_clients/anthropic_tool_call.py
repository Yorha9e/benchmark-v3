"""
Anthropic Claude 官方 Python SDK (anthropic 0.x/1.x) Tool-Calling 参考实现
适用于: Claude 3.5 Sonnet / Haiku / Opus, Claude 3.7

涵盖特性:
1. tools 参数定义 (name, description, input_schema)
2. tool_use 内容块解析 (id, name, input)
3. 扩展思考块 (thinking block) 提取
4. 构建 tool_result 内容块传回多轮对话
5. 错误重试与 HTTP 代理配置
"""

import os
import json
import time
import random
from typing import Any
import anthropic
from anthropic import Anthropic, APIConnectionError, RateLimitError, InternalServerError

def create_anthropic_client(api_key: str | None = None, proxy: str | None = None) -> Anthropic:
    import httpx
    http_client = httpx.Client(proxy=proxy) if proxy else None
    return Anthropic(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "mock-key"),
        http_client=http_client,
        max_retries=0, # 由 Harness 统一管理
    )

def convert_json_schema_to_anthropic_tools(standard_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将标准工具定义转换为 Anthropic tools 格式 (input_schema)"""
    anthropic_tools = []
    for t in standard_tools:
        fn = t.get("function", t)
        anthropic_tools.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}})
        })
    return anthropic_tools

def call_claude_with_retry(
    client: Anthropic,
    model: str,
    messages: list[dict[str, Any]],
    system_prompt: str = "",
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    max_retries: int = 5
) -> dict[str, Any]:
    """调用 Claude 并提取文本、思考链和 tool_use 块"""
    backoff = 1.0
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system_prompt:
        kwargs["system"] = system_prompt
    if tools:
        kwargs["tools"] = convert_json_schema_to_anthropic_tools(tools)
        
    for attempt in range(max_retries):
        try:
            response = client.messages.create(**kwargs)
            
            tool_calls = []
            text_parts = []
            thought = ""
            
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.input
                    })
                elif block.type == "thinking":
                    thought = getattr(block, "thinking", "") or ""
                    
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens
            }
            
            return {
                "content": "\n".join(text_parts),
                "thought": thought,
                "tool_calls": tool_calls,
                "token_usage": usage,
                "raw_response": response
            }
            
        except (RateLimitError, InternalServerError, APIConnectionError) as e:
            if attempt == max_retries - 1:
                raise
            sleep_time = min(60.0, backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
            print(f"[Retry] Anthropic error ({type(e).__name__}): {e}, waiting {sleep_time:.2f}s...")
            time.sleep(sleep_time)

def build_anthropic_tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    """构建回传给 Claude 的 user 角色中的 tool_result 内容块"""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error
            }
        ]
    }
