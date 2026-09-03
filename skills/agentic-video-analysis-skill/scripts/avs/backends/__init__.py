"""LLM バックエンド。

`get_backend(name, model, api_key, **kw)` でインスタンスを得る。
解析ロジックは `avs.backends.base` の型だけを知り、実装差は各モジュールに閉じる。

- `openrouter`: 既定。標準ライブラリの HTTP で chat/completions を叩く（画像のみ）
- `gemini`    : google-genai の Interactions API（画像 + ネイティブ動画クリップ + 音声）
"""

from __future__ import annotations

from typing import Any

from avs.backends.base import (
    Backend,
    LLMRequest,
    LLMResponse,
    Media,
    MediaImage,
    MediaVideoClip,
    describe_media,
)

BACKEND_NAMES = ("openrouter", "gemini")


def get_backend(
    name: str,
    model: str | None = None,
    api_key: str | None = None,
    **kw: Any,
) -> Backend:
    """バックエンド名からインスタンスを作る。`kw` は各実装に渡す（strict_json など）。"""
    if name == "openrouter":
        from avs.backends.openrouter import OpenRouterBackend  # noqa: PLC0415 - 遅延 import

        return OpenRouterBackend(model=model, api_key=api_key, **kw)
    if name == "gemini":
        from avs.backends.gemini import GeminiBackend  # noqa: PLC0415 - 遅延 import

        return GeminiBackend(model=model, api_key=api_key, **kw)
    raise RuntimeError(
        f"未知のバックエンドです: {name}（指定できるのは {' | '.join(BACKEND_NAMES)}）"
    )


__all__ = [
    "BACKEND_NAMES",
    "Backend",
    "LLMRequest",
    "LLMResponse",
    "Media",
    "MediaImage",
    "MediaVideoClip",
    "describe_media",
    "get_backend",
]
