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

logging.basicConfig(level=os.getenv("SRC_LOG_LEVELS", "INFO"))
logger = logging.getLogger(__name__)

# --- 线程安全的全局状态管理，用于 V1 的 API Key 负载均衡 ---
CACHED_API_KEYS_STRING = ""
API_KEYS_LIST = []
CURRENT_KEY_INDEX = 0
KEY_LOCK = threading.Lock()


class Pipe:
    """
    与 EO Edge AI Gateway 交互的 OpenWebUI 管道。
    此版本支持 V1 和 V2 API 模式切换，并为 V1 提供 API Key 负载均衡。
    """

    class Valves(BaseModel):
        # --- 通用设置 ---
        use_v2_api: bool = Field(
            default=False,
            description="启用 V2 API 模式。开启后，将使用 V2 的地址和请求格式。",
        )
        available_models: str = Field(
            default="gemini-pro,gemini-1.5-pro-latest",
            description="可用的模型列表，请用英文逗号 (,) 分隔。",
        )
        timeout: int = Field(default=180, description="API 请求的超时时间（秒）。")

        # --- V1 API 配置 ---
        base_url: str = Field(
            default="https://ai-gateway.eo-edgefunctions7.com/v1",
            description="【V1 模式】你的EdgeOneAI网关分配的地址/v1",
        )
        api_keys: str = Field(
            default="",
            description="【V1 模式】输入一个或多个API Key，用英文逗号 (,) 分隔。",
            extra={"type": "password"},
        )
        oe_key: str = Field(
            default="",
            description="【V1 模式】请求头 'OE-Key' 的值。",
            extra={"type": "password"},
        )
        gateway_name: str = Field(
            default="",
            description="【V1 模式】请求头 'OE-Gateway-Name' 的值。",
        )
        ai_provider: str = Field(
            default="gemini",
            description="【V1 模式】请求头 'OE-AI-Provider' 的值。",
        )

        # --- V2 API 配置 (占位符) ---
        v2_base_url: str = Field(
            default="", description="【V2 模式】V2 API 的基地址（未来开发使用）。"
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        if not self.valves.available_models:
            return []
        models = [
            m.strip() for m in self.valves.available_models.split(",") if m.strip()
        ]
        return [{"id": m, "name": m} for m in models]

    async def pipe(self, body: dict, **kwargs) -> str:
        """
        API 版本分发器。根据 UI 开关状态调用相应版本的处理逻辑。
        """
        if self.valves.use_v2_api:
            return await self._pipe_v2(body)
        else:
            return self._pipe_v1(body)

    # ======================================================================================
    # V1 API 实现
    # ======================================================================================
    def _get_next_api_key(self) -> str:
        global CACHED_API_KEYS_STRING, API_KEYS_LIST, CURRENT_KEY_INDEX
        with KEY_LOCK:
            if self.valves.api_keys != CACHED_API_KEYS_STRING:
                logger.info("V1 API keys have been updated. Reloading...")
                API_KEYS_LIST = [
                    k.strip() for k in self.valves.api_keys.split(",") if k.strip()
                ]
                CACHED_API_KEYS_STRING = self.valves.api_keys
                CURRENT_KEY_INDEX = 0

            if not API_KEYS_LIST:
                return None
            key = API_KEYS_LIST[CURRENT_KEY_INDEX]
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS_LIST)
            logger.info(
                f"Using V1 API Key index: {CURRENT_KEY_INDEX - 1 if CURRENT_KEY_INDEX > 0 else len(API_KEYS_LIST) - 1}"
            )
            return key

    def _pipe_v1(self, body: dict) -> str:
        if not all(
            [self.valves.api_keys, self.valves.oe_key, self.valves.gateway_name]
        ):
            return "错误：V1 模式未配置。请确保 API Keys, OE-Key, 和 Gateway Name 均已填写。"

        try:
            api_key = self._get_next_api_key()
            if not api_key:
                return "错误：没有可用的V1 API Key。"

            model = body.get("model", "").split(".", 1)[-1]
            messages = body.get("messages", [])

            contents = []
            for msg in messages:
                role = "model" if msg.get("role") == "assistant" else "user"
                content = msg.get("content", "")
                parts = (
                    [
                        {"text": item["text"]}
                        for item in content
                        if isinstance(content, list) and item.get("type") == "text"
                    ]
                    or [{"text": content}]
                    if isinstance(content, str)
                    else []
                )
                if parts:
                    contents.append({"role": role, "parts": parts})

            if not contents:
                return "错误：无法解析对话内容。"

            url = f"{self.valves.base_url}/models/{model}:generateContent"
            params = {"key": api_key}
            headers = {
                "OE-Key": self.valves.oe_key,
                "OE-Gateway-Name": self.valves.gateway_name,
                "OE-AI-Provider": self.valves.ai_provider,
                "Content-Type": "application/json",
            }
            payload = {"contents": contents}

            response = requests.post(
                url,
                headers=headers,
                params=params,
                json=payload,
                timeout=self.valves.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )

        except requests.exceptions.HTTPError as e:
            details = e.response.text
            logger.error(f"V1 HTTP error: {e} - Details: {details}")
            return f"错误: V1 网关返回 HTTP {e.response.status_code}。详情: {details}"
        except Exception as e:
            logger.exception(f"Unexpected V1 error: {e}")
            return f"V1 发生未知错误: {e}"

    # ======================================================================================
    # V2 API 实现 (占位符)
    # ======================================================================================
    async def _pipe_v2(self, body: dict) -> str:
        """
        这是为 V2 API 预留的处理方法。目前它只返回一个提示信息。
        您未来可以在这里添加与 V2 端点交互的完整逻辑。
        """
        logger.warning("V2 API mode was called, but it is not yet implemented.")

        # 示例：未来您可能会在这里检查 V2 的特定配置
        if not self.valves.v2_base_url:
            return "错误：V2 模式已启用，但 V2 的基地址 (v2_base_url) 未配置。"

        return "提示：V2 API 模式目前正在开发中，尚未实现。请在管道配置中关闭 '启用 V2 API 模式' 开关以使用 V1 版本。"
