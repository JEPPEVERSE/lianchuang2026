"""Configured OpenAI-compatible chat endpoint; secrets never enter artifacts."""

import json
import os
import sys
import time
from pathlib import Path
from urllib import error, parse, request

from .contracts import validate


class ModelError(RuntimeError):
    pass


class TruncatedOutput(ModelError):
    """Retrying the same prompt with the same budget cannot repair truncation."""
    pass


def load_env(path: Path = Path(".env")) -> None:
    """Read simple KEY=value lines without expanding variables or executing code."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("LLM_") or key == "DEEPSEEK_API_KEY":
            os.environ.setdefault(key, value.strip().strip("\"'"))


class ChatModel:
    def __init__(self, base_url=None, model=None, api_key=None, timeout=None):
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("LLM_MODEL", "deepseek-flash")
        self.api_key = api_key if api_key is not None else (os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", ""))
        self.timeout = float(timeout or os.getenv("LLM_TIMEOUT", "90"))
        if not self.base_url or not self.model:
            raise ModelError("请配置 LLM_BASE_URL 和 LLM_MODEL（可写入本项目 .env）")
        url = parse.urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.query or url.fragment:
            raise ModelError("LLM_BASE_URL 必须是无凭据、无查询参数的 HTTP(S) API 根地址")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ModelError("非本机模型服务必须使用 HTTPS")

    def complete(self, system: str, payload: dict, schema: dict, correction=None):
        if parse.urlsplit(self.base_url).hostname == "api.deepseek.com" and not self.api_key:
            raise ModelError("缺少 DeepSeek 密钥：请在 .env 填写 LLM_API_KEY 或 DEEPSEEK_API_KEY")
        message = {"input": payload, "output_schema": schema}
        if correction:
            message["previous_validation_error"] = correction
        role = "analysis" if "kind" in payload else "qa"
        default_budget = "8192" if role == "analysis" else "2048"
        budget = int(os.getenv(f"LLM_{role.upper()}_MAX_TOKENS") or os.getenv("LLM_MAX_TOKENS") or default_budget)
        if budget <= 0:
            raise ModelError("输出 token 预算必须为正整数")
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(message, ensure_ascii=False)}],
                "max_tokens": budget}
        thinking = os.getenv("LLM_THINKING")
        if thinking is None and parse.urlsplit(self.base_url).hostname == "api.deepseek.com":
            thinking = "disabled"
        if thinking is not None:
            if thinking not in ("enabled", "disabled"):
                raise ModelError("LLM_THINKING 必须是 enabled 或 disabled")
            body["thinking"] = {"type": thinking}
            if thinking == "enabled":
                body["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "low")
        if os.getenv("LLM_JSON_MODE", "1") == "1":
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = request.Request(self.base_url.rstrip("/") + "/chat/completions",
                              data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            started = time.monotonic()
            # Refuse redirects so an endpoint cannot forward the authorization header.
            opener = request.build_opener(NoRedirect())
            with opener.open(req, timeout=self.timeout) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ModelError("模型响应过大")
            result = json.loads(raw)
            choice = result["choices"][0]
            usage = result.get("usage", {})
            self.last_stats = {"role": role, "elapsed_seconds": round(time.monotonic() - started, 2),
                               "prompt_tokens": usage.get("prompt_tokens"),
                               "completion_tokens": usage.get("completion_tokens"),
                               "reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens"),
                               "finish_reason": choice.get("finish_reason"), "max_tokens": budget,
                               "thinking": thinking or "provider_default"}
            if os.getenv("LLM_LOG_STATS", "1") == "1":
                print("[model] " + json.dumps(self.last_stats, ensure_ascii=False), file=sys.stderr, flush=True)
            if choice.get("finish_reason") == "length":
                raise TruncatedOutput(f"{role} 输出达到 {budget} token 上限被截断；"
                                      "未按原预算重试。请关闭思考、调整角色输出预算或减小分析批次")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("模型未返回 JSON 文本")
            return json.loads(content)
        except error.HTTPError as exc:
            raise ModelError(f"模型 HTTP 请求失败（状态码 {exc.code}）") from None
        except (error.URLError, TimeoutError):
            raise ModelError("模型连接失败或超时；请检查服务配置") from None
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型响应格式不符合 chat/completions 协议") from exc


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def structured(model, system, payload, schema, check):
    """One correction attempt for invalid output; transport failures are explicit."""
    correction = None
    for attempt in range(2):
        try:
            output = model.complete(system, payload, schema, correction)
            validate(output, schema)
            check(output)
            return output
        except ValueError as exc:
            correction = str(exc)
            if attempt:
                raise ModelError(f"模型结果两次校验失败：{correction}") from exc
    raise AssertionError("unreachable")
