#!/usr/bin/env python
"""Gemini バックエンド（google-genai の Interactions API）。

画像に加えて **ネイティブ動画クリップ**（区間 + fps + 解像度）と音声を扱える。

実装上の注意（別リポジトリの検証で判明したもの）:

- `client.interactions.create()` はキーワード引数で呼ぶ（dict を渡すと失敗する）
- `response_mime_type` と `response_format` は併用不可。スキーマは `response_format` だけに渡す
- `start_offset` / `end_offset` は `"48.00s"` 形式の**文字列**
- `agentic` processing は使わない（検証で否定済み）
- アップロードは Files API。約 48 時間で失効するので、キャッシュ再利用の前に
  `files.get` で ACTIVE を確認する

`google-genai` は遅延 import する（未導入でも他バックエンドは動く）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from avs.backends.base import LLMRequest, LLMResponse, MediaImage, MediaVideoClip
from avs.backends.openrouter import guess_mime
from avs.common import resolve_api_key
from avs.cost import make_usage

ENV_NAME = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.7-flash"
UPLOAD_POLL_SEC = 2.0
UPLOAD_TIMEOUT_SEC = 600.0
HASH_CHUNK_SIZE = 1024 * 1024  # 1MB チャンクでストリーミング読み込み

_upload_lock = threading.Lock()

# アップロードキャッシュのエントリとして最低限必要なキー
_REQUIRED_CACHE_KEYS = ("name", "uri", "mime_type", "video")


def hash_file_content(path: Path) -> str:
    """ファイル内容の SHA-1（1MB チャンクでストリーミング）。キャッシュキーに使う。"""
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _import_genai() -> Any:
    try:
        from google import genai  # noqa: PLC0415 - 遅延 import
    except ImportError as error:
        raise RuntimeError(
            "gemini バックエンドには google-genai が必要です。"
            "`pip install google-genai`（または `uv add google-genai`）で導入してください"
        ) from error
    return genai


def format_offset(seconds: float) -> str:
    """Files API の offset 表記（"48.00s" 形式の文字列）。"""
    return f"{float(seconds):.2f}s"


def build_processing(clip: MediaVideoClip) -> dict[str, Any] | None:
    """クリップ指定から processing ブロックを作る。区間指定が無ければ None（動画全体）。"""
    if clip.fps is None and clip.end_sec is None and not clip.start_sec:
        return None
    processing: dict[str, Any] = {"type": "static"}
    if clip.fps is not None:
        processing["fps"] = clip.fps
    if clip.start_sec or clip.end_sec is not None:
        processing["start_offset"] = format_offset(clip.start_sec or 0.0)
    if clip.end_sec is not None:
        processing["end_offset"] = format_offset(clip.end_sec)
    return processing


class GeminiBackend:
    """google-genai の Interactions API を叩くバックエンド。"""

    name = "gemini"
    default_model = DEFAULT_MODEL
    supports_video_clip = True
    supports_audio = True

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        strict_json: bool = False,
        cache_dir: str | Path | None = None,
        client: Any | None = None,
        **_ignored: Any,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.strict_json = strict_json
        self._client = client
        self._api_key = api_key
        # アップロードキャッシュ。--session があればセッション配下に置く
        self.cache_dir = (
            Path(cache_dir).expanduser()
            if cache_dir
            else Path(tempfile.gettempdir()) / "avs_uploads"
        )

    # --- クライアント ---------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            genai = _import_genai()
            key = resolve_api_key(ENV_NAME, explicit=self._api_key, backend=self.name)
            self._client = genai.Client(api_key=key)
        return self._client

    # --- アップロード（セッション内キャッシュ） --------------------------------

    def upload(self, video: Path) -> dict[str, Any]:
        video = Path(video).expanduser().resolve()
        digest = hash_file_content(video)
        cache_path = self.cache_dir / f"{digest}.json"

        with _upload_lock:
            info = self._read_cache(cache_path)
            if info is not None and self._is_active(info.get("name")):
                return info

            uploaded = self.client.files.upload(file=str(video))
            uploaded = self._wait_active(uploaded)
            info = {
                "name": uploaded.name,
                "uri": uploaded.uri,
                "mime_type": uploaded.mime_type,
                "video": str(video),
            }
            self._write_cache(cache_path, info)
            return info

    def _read_cache(self, cache_path: Path) -> dict[str, Any] | None:
        """キャッシュを読む。壊れている／形式不正ならキャッシュミス扱いで None を返す。"""
        if not cache_path.exists():
            return None
        try:
            info = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(info, dict) or not all(info.get(key) for key in _REQUIRED_CACHE_KEYS):
            return None
        return info

    def _write_cache(self, cache_path: Path, info: dict[str, Any]) -> None:
        """一時ファイルに書いてから `os.replace` で原子的に置き換える。"""
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(cache_path.parent), prefix=f".{cache_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(info, ensure_ascii=False, indent=2))
            os.replace(tmp_name, cache_path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise

    def _is_active(self, name: str | None) -> bool:
        if not name:
            return False
        try:
            info = self.client.files.get(name=name)
        except Exception:  # noqa: BLE001 - 失効・削除は再アップロードで回復する
            return False
        state = getattr(info, "state", None)
        return bool(state) and getattr(state, "name", str(state)) == "ACTIVE"

    def _wait_active(self, uploaded: Any) -> Any:
        deadline = time.time() + UPLOAD_TIMEOUT_SEC
        while True:
            state = getattr(uploaded, "state", None)
            state_name = getattr(state, "name", str(state)) if state else None
            if state_name == "ACTIVE":
                return uploaded
            if state_name and state_name != "PROCESSING":
                raise RuntimeError(f"Files API のアップロードに失敗しました: state={state_name}")
            if time.time() > deadline:
                raise RuntimeError("Files API のアップロードが完了しませんでした（タイムアウト）")
            time.sleep(UPLOAD_POLL_SEC)
            uploaded = self.client.files.get(name=uploaded.name)

    # --- リクエスト組み立て ---------------------------------------------------

    def build_input(self, request: LLMRequest) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for item in request.media:
            if isinstance(item, MediaImage):
                data = Path(item.path).read_bytes()
                blocks.append(
                    {
                        "type": "image",
                        "mime_type": guess_mime(item.path),
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
            elif isinstance(item, MediaVideoClip):
                info = self.upload(item.video)
                block: dict[str, Any] = {
                    "type": "video",
                    "uri": info["uri"],
                    "mime_type": info["mime_type"],
                }
                processing = build_processing(item)
                if processing is not None:
                    block["processing"] = processing
                    if item.resolution:
                        block["resolution"] = item.resolution
                blocks.append(block)
            else:
                raise RuntimeError(f"gemini が扱えないメディアです: {type(item).__name__}")

        blocks.append({"type": "text", "text": request.prompt})
        return blocks

    # --- 実行 ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        blocks = self.build_input(request)
        model = request.model or self.model

        kwargs: dict[str, Any] = {"model": model, "input": blocks}
        if request.json_schema:
            # response_mime_type とは併用しない（400 になる）
            kwargs["response_format"] = request.json_schema

        started = time.time()
        interaction = self.client.interactions.create(**kwargs)
        latency = time.time() - started

        return LLMResponse(
            text=interaction.output_text or "",
            usage=_normalize_usage(getattr(interaction, "usage", None)),
            latency_sec=round(latency, 3),
            model=model,
            backend=self.name,
            retries=0,
        )


def _normalize_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        raw: dict[str, Any] = {}
    elif hasattr(usage, "model_dump"):
        raw = {key: value for key, value in usage.model_dump().items() if value is not None}
    elif isinstance(usage, dict):
        raw = {key: value for key, value in usage.items() if value is not None}
    else:
        raw = {key: value for key, value in vars(usage).items() if value is not None}

    return make_usage(
        input_tokens=(raw.get("total_input_tokens") or 0) + (raw.get("total_tool_use_tokens") or 0),
        output_tokens=raw.get("total_output_tokens") or 0,
        reasoning_tokens=raw.get("total_thought_tokens") or 0,
        total_tokens=raw.get("total_tokens") or 0,
        cost_usd=None,  # Gemini は課金額を返さないので cost.py の単価表で概算する
        raw=raw,
    )
