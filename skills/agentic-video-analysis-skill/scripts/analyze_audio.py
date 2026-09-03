#!/usr/bin/env python
"""動画の音声を解析するCLI（動画クリップと音声を扱えるバックエンド限定 = gemini）。

**必ず全編を送る。** 範囲を絞ると、映像を見ずに音声だけから内容を捏造することがある。

  python skills/agentic-video-analysis-skill/scripts/analyze_audio.py \
    --video video.mp4 --session output/agentic_sessions/example --backend gemini

出力（<session>/audio/ 配下）:
  audio_analysis.json        segments[]{start_sec,end_sec,kind,description,confidence}
  audio_analysis.raw.txt     モデルの生出力
  audio_analysis.prompt.txt  実際に送ったプロンプト
  audio_analysis.meta.json   backend / model / step / メディア / usage / cost / latency / retries
  <session>/usage.jsonl      上記 + name="audio_analysis" を1行追記（step="audio"）

結果は `merge_analyses.py --audio <session>/audio/audio_analysis.json` で
timeline に取り込める（映像側と重ならない項目には audio_unconfirmed が付く）。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avs.backends import BACKEND_NAMES, LLMRequest, MediaVideoClip, describe_media, get_backend
from avs.common import configure_utf8_stdout, read_text_utf8, write_json
from avs.merge import extract_json
from avs.prompts import (
    AUDIO_SCHEMA,
    DEFAULT_OBJECTIVE,
    api_schema,
    assemble_prompt,
    load_domain,
    resolve_text_arg,
    step_for_prompt,
)
from avs.session import append_usage_line, build_call_meta

DEFAULT_AUDIO_BACKEND = "gemini"
OUTPUT_BASENAME = "audio_analysis"
AUDIO_DIR_NAME = "audio"


def default_prompt_path() -> Path:
    """スキル同梱の `prompts/audio.txt`。"""
    return Path(__file__).resolve().parent.parent / "prompts" / "audio.txt"


@dataclass
class AudioOptions:
    """音声解析の設定。CLI の argparse.Namespace と同じ属性名を持つ。"""

    video: str | None = None
    session: str | None = None
    backend: str = DEFAULT_AUDIO_BACKEND
    model: str | None = None
    api_key: str | None = None
    objective: str | None = None
    domain: str | None = None
    prompt: str | None = None
    output_dir: str | None = None
    strict_json: bool = False
    force: bool = False
    dry_run: bool = False

    @classmethod
    def from_namespace(cls, namespace: Any) -> "AudioOptions":
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})


def resolve_output_dir(options: AudioOptions) -> Path:
    if options.output_dir:
        return Path(options.output_dir).expanduser().resolve()
    if not options.session:
        raise RuntimeError("--session または --output-dir を指定してください")
    return Path(options.session).expanduser().resolve() / AUDIO_DIR_NAME


def run_audio_analysis(options: AudioOptions) -> int:
    if not options.video:
        raise RuntimeError("--video を指定してください")
    video_path = Path(options.video).expanduser().resolve()
    if not video_path.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video_path}")

    output_dir = resolve_output_dir(options)
    base = output_dir / OUTPUT_BASENAME
    json_path = base.with_name(f"{base.name}.json")

    if json_path.exists() and not options.force:
        print(f"[スキップ] 既に解析結果があります: {json_path}（再解析は --force）")
        return 0

    backend = get_backend(
        options.backend,
        model=options.model,
        api_key=options.api_key or ("dry-run" if options.dry_run else None),
        strict_json=options.strict_json,
    )
    if not getattr(backend, "supports_audio", False):
        raise RuntimeError(
            f"--backend {options.backend} は音声を扱えません。"
            "音声解析は --backend gemini（GEMINI_API_KEY と google-genai が必要）で実行してください"
        )

    prompt_path = Path(options.prompt).expanduser() if options.prompt else default_prompt_path()
    if not prompt_path.exists():
        raise RuntimeError(f"プロンプトファイルが見つかりません: {prompt_path}")
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE
    domain = load_domain(options.domain) if options.domain else None

    prompt = assemble_prompt(
        read_text_utf8(prompt_path),
        objective,
        tile_context="",
        context_text=None,
        note=None,
        domain=domain,
        schema=AUDIO_SCHEMA,
    )
    # 範囲を絞らず全編を送る（end_sec=None / fps=None = クリッピングもサンプリングもしない）
    media = [MediaVideoClip(video=video_path, start_sec=0.0, end_sec=None, fps=None)]

    if options.dry_run:
        print(f"[dry-run] backend={backend.name} model={options.model or backend.default_model}")
        print(f"[dry-run] メディア: {', '.join(describe_media(media))}")
        print(f"[dry-run] プロンプト先頭200字: {prompt[:200]}")
        print(f"[dry-run] 出力先: {json_path}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    base.with_name(f"{base.name}.prompt.txt").write_text(prompt, encoding="utf-8")
    print(f"[開始] 音声解析: {video_path.name}（全編） -> {json_path}")

    responses: list[Any] = []
    parsed: Any = None
    json_retries = 0
    for attempt in range(2):  # 本番 + JSON 失敗時の1回リトライ
        request = LLMRequest(
            prompt=(
                prompt
                if attempt == 0
                else prompt + "\n\n前回の出力はJSONとして不正だった。コードフェンス付きJSONのみを出力せよ。"
            ),
            media=media,
            json_schema=api_schema(AUDIO_SCHEMA),
            model=options.model,
        )
        response = backend.complete(request)
        responses.append(response)
        base.with_name(f"{base.name}.raw.txt").write_text(response.text, encoding="utf-8")
        try:
            parsed = extract_json(response.text)
        except (ValueError, TypeError):
            json_retries = attempt + 1
            parsed = None
            continue
        break

    # usage はリトライ分も含めて合算する（合算しないと集計からトークンが抜ける）
    meta = build_call_meta(
        responses,
        describe_media(media),
        prompt_chars=len(prompt),
        json_retries=json_retries,
        step=step_for_prompt(prompt_path),
    )
    write_json(base.with_name(f"{base.name}.meta.json"), meta)
    append_usage_line(options.session, {"name": OUTPUT_BASENAME, **meta})

    if parsed is None:
        raise RuntimeError(
            f"音声解析の出力をJSONとして解析できませんでした（生出力: {base.name}.raw.txt）"
        )

    write_json(json_path, parsed)
    n_segments = len(parsed.get("segments") or []) if isinstance(parsed, dict) else 0
    print(f"[完了] {n_segments}件のセグメント -> {json_path}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "動画の音声を全編まとめて解析し、audio_analysis.json を書きます。"
            "動画と音声を扱えるバックエンド（gemini）が必要です。"
        )
    )
    parser.add_argument("-v", "--video", required=True, help="入力動画パス")
    parser.add_argument(
        "--session",
        default=None,
        help="セッションディレクトリ。<session>/audio/ に出力し、usage.jsonl に記録を追記する",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="出力ディレクトリを明示する（省略時は <session>/audio）",
    )
    parser.add_argument(
        "--backend",
        choices=list(BACKEND_NAMES),
        default=DEFAULT_AUDIO_BACKEND,
        help=f"使用するバックエンド（既定: {DEFAULT_AUDIO_BACKEND}）。音声を扱えるものだけ指定できる",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="使用モデル。省略時はバックエンドの既定",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="APIキー。省略時は 環境変数 -> ./.env -> ~/.env.global の順に探す",
    )
    parser.add_argument(
        "--objective",
        default=None,
        help=f"解析の目的（テキスト or ファイルパス）。既定: {DEFAULT_OBJECTIVE}",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="ドメイン定義 JSON（domain.json）。用語・watchlist などの手引きをプロンプトに足す",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="プロンプトファイル。省略時はスキル同梱の prompts/audio.txt",
    )
    parser.add_argument(
        "--strict-json",
        action="store_true",
        help="構造化出力（response_format）を要求する",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="audio_analysis.json が既に存在しても再解析する（既定はスキップ）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="backend / model / メディア / プロンプト先頭200字 / 出力先を表示するだけで API を呼ばない",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_utf8_stdout()
    return run_audio_analysis(AudioOptions.from_namespace(parse_args(argv)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(1)
