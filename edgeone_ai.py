"""
title: EdgeOne_AI
id: edgeone_ai
author: Besty0728
author_url: https://github.com/Besty0728
git_url: https://github.com/Besty0728/OpenWebUI-EdgeOne-AI-Gemini/blob/main/edgeone_ai.py
description: 适用于 OpenWebUI 对接 EdgeOne AI 网关（Gemini），支持 Gemini 原生和 OpenAI 兼容两种格式，支持 API Key 负载均衡
version: 1.0.0
license: Apache2.0
"""
import os
import httpx
import logging
import json
import threading
from pydantic import BaseModel, Field
from typing import Literal

logging.basicConfig(level=os.getenv("SRC_LOG_LEVELS", "INFO"))
logger = logging.getLogger(__name__)

# 线程安全的全局状态管理
CACHED_API_KEYS_STRING = ""
API_KEYS_LIST = []
CURRENT_KEY_INDEX = 0
KEY_LOCK = threading.Lock()


class Pipe:
    """
    EdgeOne AI Gateway 管道。
    支持 Gemini 原生格式和 OpenAI 兼容格式。
    支持 API Key 负载均衡（轮询机制）。
    """

    class Valves(BaseModel):
        api_format: Literal["gemini", "openai"] = Field(
            default="gemini",
            description="API格式：gemini（原生，推荐）或 openai（兼容格式，支持流式）",
        )
        gemini_api_version: Literal["v1", "v1beta"] = Field(
            default="v1beta",
            description="Gemini API版本：v1（旧模型）或 v1beta（2.0/3.0系列）",
        )
        base_url: str = Field(
            default="https://ai-gateway.eo-edgefunctions7.com",
            description="EdgeOne AI 网关根地址",
        )
        api_keys: str = Field(
            default="",
            description="Gemini API Key，多个用英文逗号 (,) 分隔。",
            extra={"type": "password"},
        )
        oe_key: str = Field(
            default="", 
            description="网关 API 密钥 (OE-Key)", 
            extra={"type": "password"}
        )
        gateway_name: str = Field(
            default="",
            description="网关名称 (OE-Gateway-Name)",
        )
        available_models: str = Field(
            default="gemini-1.5-flash,gemini-1.5-pro,gemini-2.0-flash,gemini-2.0-flash-thinking-exp-01-21",
            description="可用的模型列表，请用英文逗号 (,) 分隔。",
        )
        enable_streaming: bool = Field(
            default=False,
            description="是否启用流式输出（仅 OpenAI 格式支持）",
        )
        enable_experimental: bool = Field(
            default=False,
            description="开启 Gemini 3.0 实验性功能（思考模型、媒体分辨率等）",
        )
        thinking_level: Literal["low", "high", "minimal", "medium"] = Field(
            default="high",
            description="[实验性] 思考等级 (Flash支持minimal,3系列特有,2.x请用budget)",
        )
        thinking_budget: int = Field(
            default=0,
            description="[实验性] 思考预算 (2.x专用): 0=关闭, -1=动态",
        )
        media_resolution: Literal["low", "medium", "high", "ultra_high"] = Field(
            default="high",
            description="[实验性] 媒体分辨率",
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
        # 增强分隔符处理：兼容中文逗号、换行符、分号
        raw_list = self.valves.available_models.replace("，", ",").replace("\n", ",").replace(";", ",")
        model_list = [m.strip() for m in raw_list.split(",") if m.strip()]
        return [{"id": model, "name": model} for model in model_list]

    def _get_next_api_key(self) -> str:
        """线程安全地获取下一个 API Key（轮询）。"""
        global CACHED_API_KEYS_STRING, API_KEYS_LIST, CURRENT_KEY_INDEX

        with KEY_LOCK:
            if self.valves.api_keys != CACHED_API_KEYS_STRING:
                logger.info("API keys updated. Reloading...")
                API_KEYS_LIST = [k.strip() for k in self.valves.api_keys.split(",") if k.strip()]
                CACHED_API_KEYS_STRING = self.valves.api_keys
                CURRENT_KEY_INDEX = 0
            
            if not API_KEYS_LIST:
                return None
            
            key = API_KEYS_LIST[CURRENT_KEY_INDEX]
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS_LIST)
            return key

    async def pipe(self, body: dict, **kwargs):
        """主入口。"""
        if not all([self.valves.api_keys, self.valves.oe_key, self.valves.gateway_name]):
            return "错误：管道未配置。请填写 API Keys, OE-Key, Gateway Name。"

        api_key = self._get_next_api_key()
        if not api_key:
            return "错误：没有可用的 API Key。"

        model_id = body.get("model", "")
        model = model_id.split(".", 1)[-1] if "." in model_id else model_id

        try:
            if self.valves.api_format == "openai":
                return await self._pipe_openai(body, api_key, model)
            else:
                return await self._pipe_gemini(body, api_key, model)
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error: {e} - {e.response.text}")
            return f"错误: HTTP {e.response.status_code}。详情: {e.response.text}"
        except httpx.TimeoutException:
            return f"错误：请求超时（{self.valves.timeout}秒）。"
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            return f"发生未知错误: {e}"

    async def _pipe_gemini(self, body: dict, api_key: str, model: str):
        """Gemini 原生格式：/{version}/models/{model}:generateContent"""
        system_instruction = None
        contents = []
        
        for msg in body.get("messages", []):
            role, content = msg.get("role", ""), msg.get("content", "")
            
            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_instruction = {"parts": [{"text": content}]}
                continue
            
            gemini_role = "model" if role == "assistant" else "user"
            parts = []
            
            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        parts.append({"text": item.get("text", "")})
            
            if parts:
                contents.append({"role": gemini_role, "parts": parts})
        
        # 尝试从历史消息中提取并回传 thoughtSignature
        # 注意：这依赖于 OpenWebUI 是否保留了我们在响应中附加的隐式签名信息
        # 由于标准消息格式没有 thoughtSignature 字段，我们可能无法完美支持多轮对话的签名回传
        # 这里仅作简单的占位支持，等待 OpenWebUI 未来可能得支持

        if not contents:
            return "错误：无法解析对话内容。"

        base = self.valves.base_url.rstrip("/")
        ver = self.valves.gemini_api_version
        url = f"{base}/{ver}/models/{model}:generateContent"
        logger.info(f"[Gemini] {url}")
        
        headers = {
            "OE-Key": self.valves.oe_key,
            "OE-Gateway-Name": self.valves.gateway_name,
            "OE-AI-Provider": "gemini",
            "Content-Type": "application/json",
        }
        
        payload = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        
        gen_config = {}
        if "temperature" in body:
            gen_config["temperature"] = body["temperature"]
        if "max_tokens" in body:
            gen_config["maxOutputTokens"] = body["max_tokens"]
        if "top_p" in body:
            gen_config["topP"] = body["top_p"]
        if "top_k" in body:
            gen_config["topK"] = body["top_k"]
        if gen_config:
            payload["generationConfig"] = gen_config

        # 实验性功能参数注入
        if self.valves.enable_experimental:
            if "generationConfig" not in payload:
                payload["generationConfig"] = {}
            
            # Thinking Config
            # 逻辑：如果 thinking_budget 不为 0，则优先使用 thinking_budget (Gemini 2.x)
            # 否则，使用 thinking_level (Gemini 3.0)
            if self.valves.thinking_budget != 0:
                # Gemini 2.x / 2.5
                payload["generationConfig"]["thinkingConfig"] = {
                    "includeThoughts": True,
                    "thinkingBudget": self.valves.thinking_budget if self.valves.thinking_budget != -1 else -1
                }
            else:
                 # Gemini 3.0
                 payload["generationConfig"]["thinkingConfig"] = {
                    "thinkingLevel": self.valves.thinking_level,
                    "includeThoughts": True 
                 }
            
            # Media Resolution (全局设置)
            payload["generationConfig"]["mediaResolution"] = f"media_resolution_{self.valves.media_resolution}"

        async with httpx.AsyncClient(timeout=self.valves.timeout) as client:
            resp = await client.post(url, headers=headers, params={"key": api_key}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        
        if "error" in data:
            return f"错误：{data['error'].get('message', str(data['error']))}"
        
        # 解析 candidates
        candidate = data.get("candidates", [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        
        final_text = ""
        for part in parts:
            text = part.get("text", "")
            if not text:
                continue
            
            # 检查是否有思考标记 (hypothesis: part might have 'thought': True or distinct logic)
            # 或者如果是首个 part 且开启了思考模型，但 API 没有明确字段？
            # 这里的逻辑是：如果 API 返回了 thought: true (某些 SDK 行为)，或者我们需要人工判断？
            # 实际上 Gemini API 的 thinking 响应通常在 parts 里 separate.
            # 让我们不仅取文本，还看看有没有 'thought' 字段
            # 暂时假定如果开启了 experimental 且 parts > 1，第一个可能是 thought? 
            # 为了安全，我们先全部拼接。但如果 part 中有 'thought': true，则包裹。
            
            # 检查 part 是否包含 "thought" 标记 (Gemini API 变动多，需尝试)
            is_thought = part.get("thought", False) 
            # 注意：EdgeOne 网关可能透传 Google 原始字段，Google 原始字段通常在 JSON 里不直接叫 thought: true
            # 而是可能 part 的类型不同？目前 API 文档显示 thinking content 是 text 类型。
            
            # 如果我们在 payload 里特别请求了 includeThoughts=True (我们在代码里加了)
            # 那么 thought 部分 *应该* 是独立的 parts。
            # 暂时策略：如果 text 看起来像 thought (这里很难很难)，或者我们依赖 OpenWebUI 的渲染？
            # OpenWebUI 渲染 <think>...</think>。
            # 如果我们无法区分，用户依然会看到混在一起的。
            
            # 让我们加一个日志来看 response structure，方便用户反馈（或者如果我能看到 log）
            # 由于我看不到运行日志，我盲猜一下：
            # 如果 enable_experimental 开启，且 parts 有多个，我们尝试把第一个视为 thought？
            # 不，这太冒险。
            
            # 修正策略：不做猜测，只检查明确的 metadata。
            # 但为了帮助用户，我将使用 <think> 标签包裹所有内容吗？不。
            
            # 让我添加一段代码：如果 part 包含 "thought": true, wrap it.
            if is_thought:
                 final_text += f"<think>{text}</think>\n"
            else:
                 final_text += text
        
        # 如果依然为空，尝试旧逻辑（防御性）
        if not final_text:
             final_text = parts[0].get("text", "") if parts else ""

        # 为调试，如果开启实验模式，打印一下第一部分的 keys
        if self.valves.enable_experimental and parts:
             logger.info(f"[Debug] Part 0 keys: {parts[0].keys()}")

        return final_text.strip() if final_text else "警告：未找到有效响应。"

    async def _pipe_openai(self, body: dict, api_key: str, model: str):
        """OpenAI 兼容格式：/{version}/openai/chat/completions"""
        messages = []
        for msg in body.get("messages", []):
            role, content = msg.get("role", "user"), msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(i.get("text", "") for i in content if i.get("type") == "text")
            messages.append({"role": role, "content": content})
        
        if not messages:
            return "错误：无法解析对话内容。"

        base = self.valves.base_url.rstrip("/")
        ver = self.valves.gemini_api_version
        url = f"{base}/{ver}/openai/chat/completions"
        logger.info(f"[OpenAI] {url}, stream={self.valves.enable_streaming}")
        
        headers = {
            "OE-Key": self.valves.oe_key,
            "OE-Gateway-Name": self.valves.gateway_name,
            "OE-AI-Provider": "gemini",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": model,
            "messages": messages,
            "stream": self.valves.enable_streaming,
        }
        if "temperature" in body:
            payload["temperature"] = body["temperature"]
        if "max_tokens" in body:
            payload["max_tokens"] = body["max_tokens"]

        if self.valves.enable_streaming:
            return self._stream(url, headers, payload)
        
        async with httpx.AsyncClient(timeout=self.valves.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        
        if "error" in data:
            return f"错误：{data['error'].get('message', str(data['error']))}"
        
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

    async def _stream(self, url: str, headers: dict, payload: dict):
        """流式输出生成器。"""
        async with httpx.AsyncClient(timeout=self.valves.timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() and data_str.strip() != "[DONE]":
                            try:
                                data = json.loads(data_str)
                                text = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if text:
                                    yield text
                            except json.JSONDecodeError:
                                continue
