#!/usr/bin/env python
"""バックエンド共通のデータ構造とプロトコル。

解析ロジック（`avs.analysis`）はここで定義した型だけを知り、
OpenRouter / Gemini の差分は各バックエンド実装に閉じ込める。

- `MediaImage`     : ローカルの静止画（タイル画像）。base64 で送る
- `MediaVideoClip` : 動画の区間（gemini バックエンドのみ）
- `LLMRequest`     : 1 回の呼び出し（プロンプト + メディア + 任意のスキーマ）
- `LLMResponse`    : 応答本文と正規化済み usage
- `Backend`        : `complete()` を持つプロトコル
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Union


@dataclass
class MediaImage:
    """JPEG/PNG などの静止画。base64 で送る。"""

    path: Path

    def describe(self) -> str:
        return Path(self.path).name


@dataclass
class MediaVideoClip:
    """動画の区間。gemini バックエンドのみが扱える。

    音声解析のように動画全体をそのまま渡したいときは
    `start_sec=0.0, end_sec=None, fps=None`（= 区間指定なし）にする。
    """

    video: Path
    start_sec: float = 0.0
    end_sec: float | None = None
    fps: float | None = None
    resolution: str = "high"  # "low" | "high"

    def describe(self) -> str:
        end = "end" if self.end_sec is None else f"{self.end_sec:.2f}s"
        fps = "全フレーム" if self.fps is None else f"fps={self.fps}"
        return f"{Path(self.video).name} [{self.start_sec:.2f}s-{end}] {fps} res={self.resolution}"


Media = Union[MediaImage, MediaVideoClip]


@dataclass
class LLMRequest:
    prompt: str
    media: list[Media] = field(default_factory=list)
    json_schema: dict[str, Any] | None = None  # 構造化出力を要求する場合
    model: str | None = None  # None なら backend の既定


@dataclass
class LLMResponse:
    text: str
    usage: dict[str, Any]  # 正規化キーは avs.cost.make_usage を参照
    latency_sec: float
    model: str
    backend: str
    retries: int = 0  # 通信リトライ回数（成功までに追加で費やした試行数）


class Backend(Protocol):
    name: str
    default_model: str
    supports_video_clip: bool
    supports_audio: bool

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def describe_media(media: list[Media]) -> list[str]:
    """meta.json / dry-run 表示用に、メディアを人間可読な文字列にする。"""
    return [item.describe() for item in media]
