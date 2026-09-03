#!/usr/bin/env python
"""OpenRouter バックエンド（既定）。

`POST https://openrouter.ai/api/v1/chat/completions` を標準ライブラリの
`urllib.request` だけで叩く（新規依存を増やさない）。

- 画像は data URI（`data:image/jpeg;base64,...`）の `image_url` ブロックとして送る
- `"usage": {"include": true}` を付け、レスポンスの `usage.cost` を実コストとして使う
- `json_schema` があり strict_json のときだけ `response_format` を送る
  （既定はコードフェンス抽出 + 1 回リトライ。モデル非依存で動く）
- 429 / 5xx / 通信エラー / タイムアウトは指数バックオフ（1, 2, 4 秒）で最大 3 回
- 動画クリップは非対応（`--backend gemini` を使う）
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from avs.backends.base import LLMRequest, LLMResponse, MediaImage, MediaVideoClip
from avs.common import resolve_api_key
from avs.cost import make_usage

API_URL = "https://openrouter.ai/api/v1/chat/completions"
ENV_NAME = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "google/gemini-3.7-flash"
DEFAULT_TIMEOUT_SEC = 300
MAX_ATTEMPTS = 3
BACKOFF_SEC = (1, 2, 4)

# OpenRouter のランキング表示に使われる任意のヘッダ
HTTP_REFERER = "https://github.com/Nu424/agentic-video-analysis-skill"
X_TITLE = "agentic-video-analysis-skill"

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def guess_mime(path: Path) -> str:
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "image/jpeg")


def image_data_uri(path: Path) -> str:
    payload = Path(path).read_bytes()
    return f"data:{guess_mime(path)};base64,{base64.b64encode(payload).decode('ascii')}"


class OpenRouterBackend:
    """OpenRouter の chat/completions を叩くバックエンド。"""

    name = "openrouter"
    default_model = DEFAULT_MODEL
    supports_video_clip = False
    supports_audio = False

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        strict_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        max_attempts: int = MAX_ATTEMPTS,
        **_ignored: Any,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.strict_json = strict_json
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.api_key = resolve_api_key(ENV_NAME, explicit=api_key, backend=self.name)

    # --- リクエスト組み立て ---------------------------------------------------

    def build_payload(self, request: LLMRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        for item in request.media:
            if isinstance(item, MediaVideoClip):
                raise RuntimeError(
                    "openrouter バックエンドは動画クリップ非対応です。"
                    "ネイティブ動画入力には --backend gemini を使ってください"
                )
            if not isinstance(item, MediaImage):
                raise RuntimeError(f"openrouter が扱えないメディアです: {type(item).__name__}")
            content.append(
                {"type": "image_url", "image_url": {"url": image_data_uri(item.path)}}
            )

        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [{"role": "user", "content": content}],
            "usage": {"include": True},
        }
        if request.json_schema and self.strict_json:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "analysis",
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        return payload

    # --- 実行 ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self.build_payload(request)
        started = time.time()
        data, retries = self._post_with_retry(payload)
        latency = time.time() - started

        text = _extract_text(data)
        usage = _normalize_usage(data.get("usage") or {})
        return LLMResponse(
            text=text,
            usage=usage,
            latency_sec=round(latency, 3),
            model=data.get("model") or payload["model"],
            backend=self.name,
            retries=retries,
        )

    def _post_with_retry(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": HTTP_REFERER,
            "X-Title": X_TITLE,
        }
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"OpenRouter がエラーを返しました: {data['error']}")
                return data, attempt
            except urllib.error.HTTPError as error:
                detail = _read_error_body(error)
                if error.code != 429 and error.code < 500:
                    raise RuntimeError(
                        f"OpenRouter 呼び出しに失敗しました (HTTP {error.code}): {detail}"
                    ) from error
                last_error = RuntimeError(f"HTTP {error.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = RuntimeError(f"{type(error).__name__}: {error}")

            if attempt + 1 < self.max_attempts:
                time.sleep(BACKOFF_SEC[min(attempt, len(BACKOFF_SEC) - 1)])

        raise RuntimeError(
            f"OpenRouter 呼び出しが {self.max_attempts} 回失敗しました: {last_error}"
        )


def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")[:2000]
    except Exception:  # noqa: BLE001 - エラー本文が読めなくても報告は続ける
        return str(error)


def _extract_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not choices:
        raise RuntimeError(f"OpenRouter の応答に choices がありません: {json.dumps(data)[:2000]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # content が配列で返るモデルもあるので text パートを連結する
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    details = raw.get("completion_tokens_details") or {}
    return make_usage(
        input_tokens=raw.get("prompt_tokens") or 0,
        output_tokens=raw.get("completion_tokens") or 0,
        reasoning_tokens=details.get("reasoning_tokens") or 0,
        total_tokens=raw.get("total_tokens") or 0,
        cost_usd=raw.get("cost"),
        raw=raw,
    )
