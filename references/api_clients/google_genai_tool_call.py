"""
Google GenAI 官方最新 SDK (google-genai 2.x) Tool-Calling 参考实现
适用于: Google Gemini 2.0 (Flash/Pro), Gemini 1.5 系列

涵盖特性:
1. 基于 GenerateContentConfig 与 Tool(function_declarations=...) 的声明
2. 原生 function_call parts 解析 (name, args)
3. 构建 function_response parts 传回多轮对话
4. Thought / Thinking 提取
5. 错误重试与代理设置
"""

import os
import json
import time
import random
from typing import Any
from google import genai
from google.genai import types
from google.genai.errors import APIError

def create_genai_client(api_key: str | None = None, proxy: str | None = None) -> genai.Client:
    # google-genai 允许通过 httpx 客户端或环境变量控制网络代理
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        
    return genai.Client(
        api_key=api_key or os.environ.get("GEMINI_API_KEY", "mock-key")
    )

def convert_json_schema_to_genai_tools(standard_tools: list[dict[str, Any]]) -> list[types.Tool]:
    """将标准 JSON-Schema 工具列表转换为 Google GenAI 的 types.Tool"""
    declarations = []
    for tool_def in standard_tools:
        fn = tool_def.get("function", tool_def)
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=fn.get("parameters", {})
            )
        )
    return [types.Tool(function_declarations=declarations)]

def call_gemini_with_retry(
    client: genai.Client,
    model: str,
    contents: list[Any],
    tools: list[dict[str, Any]] | None = None,
    max_retries: int = 5
) -> dict[str, Any]:
    """调用 Gemini 并解析 functionCall 与文本回复"""
    backoff = 1.0
    config_args: dict[str, Any] = {"temperature": 0.0}
    
    if tools:
        config_args["tools"] = convert_json_schema_to_genai_tools(tools)
        
    config = types.GenerateContentConfig(**config_args)
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            
            tool_calls = []
            text_parts = []
            thought = ""
            
            # 解析 response.candidates
            if response.candidates:
                candidate = response.candidates[0]
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        # 检查工具调用
                        if getattr(part, "function_call", None):
                            fc = part.function_call
                            tool_calls.append({
                                "id": f"call_{fc.name}_{int(time.time()*1000)}",
                                "name": fc.name,
                                "arguments": dict(fc.args) if fc.args else {}
                            })
                        elif getattr(part, "text", None):
                            text_parts.append(part.text)
                        
                        # 思考链提取 (若有)
                        if getattr(part, "thought", None):
                            thought = part.thought
                            
            usage = {
                "prompt_tokens": response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
                "completion_tokens": response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
                "total_tokens": response.usage_metadata.total_token_count if response.usage_metadata else 0,
            }
            
            return {
                "content": "\n".join(text_parts),
                "thought": thought,
                "tool_calls": tool_calls,
                "token_usage": usage,
                "raw_response": response
            }
            
        except APIError as e:
            if attempt == max_retries - 1:
                raise
            sleep_time = min(60.0, backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
            print(f"[Retry] Gemini APIError ({e.code}): {e.message}, waiting {sleep_time:.2f}s...")
            time.sleep(sleep_time)

def build_gemini_function_response(function_name: str, response_dict: dict[str, Any]) -> types.Part:
    """构建回传给 Gemini 的 FunctionResponse Part"""
    return types.Part.from_function_response(
        name=function_name,
        response=response_dict
    )
