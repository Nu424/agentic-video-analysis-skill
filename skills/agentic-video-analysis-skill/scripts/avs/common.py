#!/usr/bin/env python
"""avs パッケージ全体が共有するユーティリティ。

- 標準出力のUTF-8化 (`configure_utf8_stdout`)
- subprocess 実行とエラー整形 (`run_command`)
- ffprobe による動画長・サイズ取得 (`probe_duration_sec` / `probe_video_size`)
- パス用の float 整形 (`format_float_for_path`)
- ラベル用のタイムスタンプ整形 (`format_timestamp`)
- フォント読み込み (`load_font`)
- JSON 読み書き (`read_json` / `write_json`)
- APIキーの解決 (`resolve_api_key`)
- ラベルのファイル名 slug 化 (`safe_slug`)
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import ImageFont


def configure_utf8_stdout() -> None:
    """標準出力・標準エラーをUTF-8にする。

    Windows のコンソール既定は cp932 で、日本語やUnicode記号を含むログが
    UnicodeEncodeError で落ちることがある。全CLIの冒頭で呼ぶ。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def run_command(args: list[str], label: str) -> str:
    """subprocess を実行し、失敗時は整形したエラーを送出して stdout を返す。"""
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{args[0]} が見つかりません。ffmpeg/ffprobeをインストールしてください") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"{label} に失敗しました (exit {result.returncode}): {stderr}")

    return (result.stdout or "").strip()


def probe_duration_sec(video_path: Path) -> float:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        "ffprobe",
    )
    try:
        duration = float(output)
    except ValueError as exc:
        raise RuntimeError(f"動画の長さを取得できませんでした: {video_path}") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"動画の長さが不正です: {duration}")
    return duration


def probe_video_size(video_path: Path) -> tuple[int, int]:
    output = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        "ffprobe (size)",
    )
    try:
        width, height = [int(value) for value in output.split("x")]
    except ValueError as exc:
        raise RuntimeError(f"動画サイズを取得できませんでした: {output}") from exc
    return width, height


def format_float_for_path(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", "_")


def format_timestamp(sec: float, long_form: bool = False) -> str:
    """ラベル用の時刻文字列を返す。

    long_form=False: `38.0` -> "38.0s"
    long_form=True : `1234.5` -> "20:34.5" / `38.0` -> "0:38.0"
    """
    if not long_form:
        return f"{sec:.1f}s"
    minutes = int(sec // 60)
    remainder = sec - minutes * 60
    return f"{minutes}:{remainder:04.1f}"


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


# --- APIキーの解決 -------------------------------------------------------------

# バックエンド名 -> 必要な環境変数名。エラーメッセージで対応を明示するために使う。
BACKEND_ENV_NAMES = {
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _parse_env_file(path: Path, env_name: str) -> str | None:
    """`KEY=value` 形式のファイルから 1 つのキーを読む。

    `export ` 接頭辞と `"value"` / `'value'` のクォートを剥がす
    （`~/.env.global` はクォート付きで書かれていることがある）。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export ").strip()
        if not stripped.startswith(f"{env_name}="):
            continue
        value = stripped.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return value
    return None


def resolve_api_key(
    env_name: str,
    explicit: str | None = None,
    backend: str | None = None,
    required: bool = True,
) -> str | None:
    """APIキーを 環境変数 -> カレントの `.env` -> `~/.env.global` の順に探す。

    `explicit`（CLI の --api-key）があればそれを最優先する。
    見つからず required なら、バックエンドとキー名の対応を明示して RuntimeError。
    """
    if explicit:
        return explicit

    value = os.environ.get(env_name)
    if value:
        return value

    for candidate in (Path.cwd() / ".env", Path.home() / ".env.global"):
        if candidate.exists():
            value = _parse_env_file(candidate, env_name)
            if value:
                return value

    if not required:
        return None
    target = f"--backend {backend}" if backend else env_name
    raise RuntimeError(
        f"{target} には {env_name} が必要です。"
        "環境変数、カレントディレクトリの .env、~/.env.global の順に探しましたが見つかりませんでした"
    )


# --- ラベルのファイル名 slug 化 -------------------------------------------------

# `\ / : * ? " < > |` と制御文字（0x00-0x1F, 0x7F）を `_` に置換する
_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

# Windows の予約ファイル名（大小無視）。拡張子を除いた完全一致で判定する
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}

_SAFE_SLUG_MAX_LEN = 80


def safe_slug(label: str, fallback: str) -> str:
    """ラベルからファイル名の一部として安全な slug を作る。

    - 英数字・日本語などの文字はそのまま保つ
    - `\\/:*?"<>|` と制御文字は `_` に置換
    - 先頭末尾の空白とドットを除去
    - Windows 予約名（CON, PRN, AUX, NUL, COM1-9, LPT1-9。大小無視）は末尾に `_` を付ける
    - 除去の結果空になったら `fallback` を使う
    - 長さ 80 で切る
    """
    text = "" if label is None else str(label)
    text = _UNSAFE_FILENAME_CHARS_RE.sub("_", text)
    text = text.strip(" .")
    if text.upper() in _WINDOWS_RESERVED_NAMES:
        text = f"{text}_"
    if not text:
        text = fallback
    return text[:_SAFE_SLUG_MAX_LEN]


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
