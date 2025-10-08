"""
title: EdgeOne_AI
id: edgeone_ai
author: Besty0728
author_url: https://github.com/Besty0728
git_url: https://github.com/Besty0728/OpenWebUI-EdgeOne-AI-Gemini/blob/main/edgeone_ai.py
description:适用于OpenWebUI对接EdgeOne的AI网关（Gemini）,同时支持两个版本，支持APIKey负载均衡
version: 0.1.0
license: Apache2.0
"""
import os
import requests
import logging
import json
import threading
from pydantic import BaseModel, Field

# 官方建议的日志记录器设置
logging.basicConfig(level=os.getenv("SRC_LOG_LEVELS", "INFO"))
logger = logging.getLogger(__name__)

# --- 线程安全的全局状态管理 ---
# 由于 Pipe 类在每次请求时都会重新实例化，我们需要在模块级别存储状态。
# 我们使用一个锁 (Lock) 来确保在多线程/多进程环境中对索引的修改是原子操作，防止竞争条件。
CACHED_API_KEYS_STRING = ""
API_KEYS_LIST = []
CURRENT_KEY_INDEX = 0
KEY_LOCK = threading.Lock()
# -----------------------------

class Pipe:
    """
    与 EO Edge AI Gateway v1 API 交互的 OpenWebUI 管道。
    此版本支持 API Key 的负载均衡（轮询机制）。
    """

    class Valves(BaseModel):
        base_url: str = Field(
            default="https://ai-gateway.eo-edgefunctions7.com/v1",
            description="你的EdgeOneAI网关分配的地址/v1",
        )
        api_keys: str = Field(
            default="",
            description="输入一个或多个API Key，请用英文逗号 (,) 分隔。",
            extra={"type": "password"},
        )
        oe_key: str = Field(
            default="", 
            description="请求头 'OE-Key' 的值。", 
            extra={"type": "password"}
        )
        gateway_name: str = Field(
            default="",
            description="请求头 'OE-Gateway-Name' 的值。",
        )
        ai_provider: str = Field(
            default="gemini",
            description="请求头 'OE-AI-Provider' 的值。",
        )
        available_models: str = Field(
            default="gemini-pro,gemini-1.5-pro-latest",
            description="可用的模型列表，请用英文逗号 (,) 分隔。",
        )
        timeout: int = Field(
            default=180,
            description="API 请求的超时时间（秒）。"
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        if not self.valves.available_models:
            return []
        model_list = [model.strip() for model in self.valves.available_models.split(",") if model.strip()]
        return [{"id": model, "name": model} for model in model_list]

    def _get_next_api_key(self) -> str:
        """
        线程安全地获取下一个 API Key。
        """
        global CACHED_API_KEYS_STRING, API_KEYS_LIST, CURRENT_KEY_INDEX

        with KEY_LOCK:
            # 检查用户是否在UI中更新了Key列表
            if self.valves.api_keys != CACHED_API_KEYS_STRING:
                logger.info("API keys have been updated. Reloading...")
                API_KEYS_LIST = [key.strip() for key in self.valves.api_keys.split(",") if key.strip()]
                CACHED_API_KEYS_STRING = self.valves.api_keys
                CURRENT_KEY_INDEX = 0
            
            if not API_KEYS_LIST:
                return None
            
            # 选择当前的 key
            selected_key = API_KEYS_LIST[CURRENT_KEY_INDEX]
            
            # 更新索引，为下一次请求做准备（循环使用）
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS_LIST)
            
            logger.info(f"Using API Key at index: {CURRENT_KEY_INDEX - 1 if CURRENT_KEY_INDEX > 0 else len(API_KEYS_LIST) - 1}")
            return selected_key

    async def pipe(self, body: dict, **kwargs) -> str:
        if not all([self.valves.api_keys, self.valves.oe_key, self.valves.gateway_name]):
            return "错误：管道未配置。请确保 API Keys, OE-Key, 和 Gateway Name 均已填写。"

        try:
            # 调用负载均衡方法获取一个Key
            api_key = self._get_next_api_key()
            if not api_key:
                return "错误：没有可用的API Key。请在配置中至少填写一个API Key。"

            model_id = body.get("model", "")
            model = model_id.split(".", 1)[-1] if "." in model_id else model_id
            
            gemini_contents = []
            for message in body.get("messages", []):
                role = "model" if message.get("role") == "assistant" else "user"
                content = message.get("content", "")
                parts = []
                if isinstance(content, str):
                    parts.append({"text": content})
                elif isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            parts.append({"text": item.get("text", "")})
                if parts:
                    gemini_contents.append({"role": role, "parts": parts})
            
            if not gemini_contents:
                return "错误：无法从请求中解析出有效的对话内容。"

            url = f"{self.valves.base_url}/models/{model}:generateContent"
            # 使用动态选择的 api_key
            params = {"key": api_key}
            headers = {
                "OE-Key": self.valves.oe_key,
                "OE-Gateway-Name": self.valves.gateway_name,
                "OE-AI-Provider": self.valves.ai_provider,
                "Content-Type": "application/json",
            }
            payload = {"contents": gemini_contents}

            response = requests.post(
                url, headers=headers, params=params, json=payload, timeout=self.valves.timeout
            )
            response.raise_for_status()
            response_data = response.json()
            
            result_text = response_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return result_text.strip()
            
        except requests.exceptions.HTTPError as http_err:
            error_details = http_err.response.text
            logger.error(f"HTTP error occurred: {http_err} - Details: {error_details}")
            return f"错误: 网关返回 HTTP {http_err.response.status_code} 错误。详情: {error_details}"
        except Exception as e:
            logger.exception(f"An unexpected error occurred in pipe: {e}")
            return f"发生未知错误: {e}"
