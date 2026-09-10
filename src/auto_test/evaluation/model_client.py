"""Observed OpenAI-compatible model calls for native evaluation cases."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from auto_test.evaluation.scoring import estimate_token_count


def chat_completions_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


@dataclass(frozen=True)
class ObservedModelResponse:
    text: str = ""
    request_id: str = ""
    http_status: int | None = None
    finish_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_source: str = "estimated"
    ttft_ms: float | None = None
    latency_ms: float | None = None
    chunks: int = 0
    error_type: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return not self.error_type and self.http_status == 200 and bool(self.text.strip())

    def metrics(self) -> dict[str, Any]:
        output_tps = None
        if self.output_tokens is not None and self.latency_ms and self.latency_ms > 0:
            output_tps = self.output_tokens / (self.latency_ms / 1000)
        return {
            "request_id": self.request_id,
            "http_status": self.http_status,
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": (
                self.input_tokens + self.output_tokens
                if self.input_tokens is not None and self.output_tokens is not None
                else None
            ),
            "token_source": self.token_source,
            "ttft_ms": self.ttft_ms,
            "latency_ms": self.latency_ms,
            "output_tokens_per_second": round(output_tps, 4) if output_tps is not None else None,
            "chunks": self.chunks,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class EvaluationModelClient:
    """Synchronous client used only inside evaluation workers, never the Web loop."""

    def __init__(self, *, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.trust_env = False

    @staticmethod
    def _usage(payload: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        try:
            normalized_input = int(input_tokens) if input_tokens is not None else None
        except (TypeError, ValueError):
            normalized_input = None
        try:
            normalized_output = int(output_tokens) if output_tokens is not None else None
        except (TypeError, ValueError):
            normalized_output = None
        return normalized_input, normalized_output

    @staticmethod
    def _estimated_input(messages: list[dict[str, str]]) -> int:
        return sum(estimate_token_count(str(item.get("content") or "")) + 4 for item in messages)

    @staticmethod
    def _error_type(exc: Exception, status: int | None = None) -> str:
        if isinstance(exc, requests.Timeout):
            return "timeout"
        if isinstance(exc, requests.ConnectionError):
            return "connection_error"
        if status == 429:
            return "http_429"
        if status is not None and 400 <= status < 500:
            return "http_4xx"
        if status is not None and status >= 500:
            return "http_5xx"
        if isinstance(exc, (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError)):
            return "invalid_response"
        return "request_error"

    def call(
        self,
        *,
        base_url: str,
        model: str,
        messages: list[dict[str, str]],
        api_key: str = "",
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 60.0,
        stream: bool = False,
    ) -> ObservedModelResponse:
        started = time.perf_counter()
        status: int | None = None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": str(model),
            "messages": messages,
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "max_tokens": max(1, int(max_tokens)),
            "stream": bool(stream),
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        response = None
        try:
            response = self.session.post(
                chat_completions_url(base_url),
                headers=headers,
                json=payload,
                timeout=max(1.0, float(timeout)),
                stream=bool(stream),
            )
            status = int(response.status_code)
            response.raise_for_status()
            if stream:
                return self._read_stream(response, messages, started)
            document = response.json()
            choice = document["choices"][0]
            content = str((choice.get("message") or {}).get("content") or "")
            input_tokens, output_tokens = self._usage(document)
            source = "api_usage" if input_tokens is not None and output_tokens is not None else "estimated"
            if input_tokens is None:
                input_tokens = self._estimated_input(messages)
            if output_tokens is None:
                output_tokens = estimate_token_count(content)
            latency_ms = (time.perf_counter() - started) * 1000
            return ObservedModelResponse(
                text=content,
                request_id=str(document.get("id") or ""),
                http_status=status,
                finish_reason=str(choice.get("finish_reason") or ""),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                token_source=source,
                ttft_ms=None,
                latency_ms=latency_ms,
                chunks=1 if content else 0,
                error_type=("empty_response" if not content.strip() else "output_truncated" if choice.get("finish_reason") == "length" else ""),
                error_message=("模型没有返回有效正文" if not content.strip() else "输出达到 Token 上限，正文不完整" if choice.get("finish_reason") == "length" else ""),
            )
        except Exception as exc:
            return ObservedModelResponse(
                http_status=status,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type=self._error_type(exc, status),
                error_message=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        finally:
            if response is not None:
                response.close()

    def _read_stream(
        self,
        response: requests.Response,
        messages: list[dict[str, str]],
        started: float,
    ) -> ObservedModelResponse:
        text_parts: list[str] = []
        first_token_at: float | None = None
        chunks = 0
        finish_reason = ""
        request_id = ""
        usage_input: int | None = None
        usage_output: int | None = None
        done = False
        response.encoding = "utf-8"
        for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                document = json.loads(data)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            request_id = request_id or str(document.get("id") or "")
            parsed_input, parsed_output = self._usage(document)
            usage_input = parsed_input if parsed_input is not None else usage_input
            usage_output = parsed_output if parsed_output is not None else usage_output
            choices = document.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = str(choice.get("finish_reason") or finish_reason)
            content = str((choice.get("delta") or {}).get("content") or "")
            if content:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                chunks += 1
                text_parts.append(content)
        completed = time.perf_counter()
        text = "".join(text_parts)
        source = "api_usage" if usage_input is not None and usage_output is not None else "estimated"
        return ObservedModelResponse(
            text=text,
            request_id=request_id,
            http_status=int(response.status_code),
            finish_reason=finish_reason,
            input_tokens=usage_input if usage_input is not None else self._estimated_input(messages),
            output_tokens=usage_output if usage_output is not None else estimate_token_count(text),
            token_source=source,
            ttft_ms=(first_token_at - started) * 1000 if first_token_at is not None else None,
            latency_ms=(completed - started) * 1000,
            chunks=chunks,
            error_type=("empty_response" if not text.strip() else "output_truncated" if finish_reason == "length" else "stream_interrupted" if not (done and finish_reason) else ""),
            error_message=("模型返回了成功状态，但没有有效正文" if not text.strip() else "输出达到 Token 上限，正文不完整" if finish_reason == "length" else "流式响应缺少正常结束标记" if not (done and finish_reason) else ""),
        )
