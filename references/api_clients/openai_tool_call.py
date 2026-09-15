"""
OpenAI / OpenAI-Compatible Tool-Calling 官方参考实现
适用于: OpenAI (GPT-4o, o1/o3), DeepSeek (V3/R1), Qwen, Moonshot/Kimi, GLM 等

涵盖特性:
1. 标准 tools / function 声明定义
2. 模型思考链 (reasoning_content) 兼容提取
3. 多工具并发调用 (parallel tool calling) 解析
4. 工具执行结果组装 (role="tool", tool_call_id=...)
5. 自适应超时与指数退避重试 (429/5xx)
"""

import os
import json
import time
import random
from typing import Any
import openai
from openai import OpenAI, APIConnectionError, RateLimitError, InternalServerError

def create_openai_client(api_key: str | None = None, base_url: str | None = None, proxy: str | None = None) -> OpenAI:
    import httpx
    http_client = httpx.Client(proxy=proxy) if proxy else None
    return OpenAI(
        api_key=api_key or os.environ.get("OPENAI_API_KEY", "mock-key"),
        base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
        http_client=http_client,
        max_retries=0, # 由 Harness 显式管理重试
    )

# 标准 Tool 声明模板
SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read content from a file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"}
                },
                "required": ["path"]
            }
        }
    }
]

def call_with_retry(client: OpenAI, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, max_retries: int = 5) -> dict[str, Any]:
    """带指数退避和抖动的标准 API 调用封装"""
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
                
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message
            
            # 提取思考链 (DeepSeek R1 / o1 / o3 风格)
            reasoning_content = getattr(message, "reasoning_content", "") or ""
            
            # 提取工具调用
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                    })
                    
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            
            return {
                "content": message.content or "",
                "thought": reasoning_content,
                "tool_calls": tool_calls,
                "token_usage": usage,
                "raw_message": message
            }
            
        except (RateLimitError, InternalServerError, APIConnectionError) as e:
            if attempt == max_retries - 1:
                raise
            sleep_time = min(60.0, backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
            print(f"[Retry] Transient error: {type(e).__name__} ({e}), waiting {sleep_time:.2f}s...")
            time.sleep(sleep_time)

def build_tool_result_message(tool_call_id: str, tool_name: str, result_content: str) -> dict[str, Any]:
    """构建回传给模型的工具执行响应消息"""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_content
    }
