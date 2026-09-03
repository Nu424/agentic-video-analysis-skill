#!/usr/bin/env python
"""プロンプト組み立て。

`assemble_prompt` が最終プロンプトを作る。構成は常にこの順:

1. プロンプト本文（`{{OBJECTIVE}}` を目的で置換したもの）
2. 入力の読み方: タイル経路は `build_tile_context`、
   ネイティブ動画クリップ経路は `build_clip_context`
3. 事前の仮説（`build_hypothesis_block`。config の note がある範囲のみ）
4. 追加コンテキスト（`--context`）
5. リトライ時のヒント
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_OBJECTIVE = "動画の見どころ候補の抽出"
OBJECTIVE_PLACEHOLDER = "{{OBJECTIVE}}"


# --- 入力テキスト（目的・コンテキスト）の解決 ---------------------------------


def resolve_text_arg(value: str | None) -> str | None:
    """値がファイルパスとして存在すればその中身を、そうでなければ値そのものを返す。"""
    if value is None:
        return None
    candidate = Path(value).expanduser()
    try:
        if candidate.exists() and candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return value


# --- プロンプト組み立て -------------------------------------------------------


def build_tile_context(
    manifest: dict[str, Any],
    tiles: list[dict[str, Any]],
    tile_paths: list[Path],
    part_info: tuple[int, int, float, float] | None = None,
) -> str:
    """タイル画像の読み方（全プロンプト共通）と、このリクエストのタイル対応を返す。"""
    extraction = manifest.get("extraction", {})
    lines = [
        "## タイル画像の読み方",
        "",
        "- 各タイルは動画から抜いたフレームを格子状に並べた画像。セルは **左→右、上→下** の順で時系列。",
        "- タイルが複数あるときは Tile 0 → Tile 1 → … の順で時刻が連続する。",
        "- 各セルには `F<番号> t=<秒>s` のラベルが付く。`<秒>` は動画内の**絶対時刻**。",
        "- 隣接セルの内容を混同しないこと。まず各タイルを1枚ずつ走査し、その後で全体を統合する。",
        "- 複数タイル・複数セルにまたがる出来事は、統合して1件として記述する。",
        "",
        "## このリクエストのタイル",
        "",
        f"範囲: {extraction.get('start_sec', '?')}s - {extraction.get('end_sec', '?')}s"
        f" / fps: {extraction.get('fps', '?')} / タイル数: {len(tile_paths)}",
    ]

    if part_info is not None:
        part_index, part_count, part_start, part_end = part_info
        lines.append(
            f"※ これは分割解析のパート {part_index + 1}/{part_count}"
            f"（この呼び出しが担当するのは {part_start:.1f}s - {part_end:.1f}s）。"
            "このパートで確認できる範囲だけを対象にすること。"
        )

    lines.append("")
    lines.append("各タイルの対応:")
    for tile, tile_path in zip(tiles, tile_paths, strict=True):
        lines.append(
            f"- Tile {tile['tile_index']}: {tile_path.name} "
            f"(t={tile['start_timestamp_sec']:.1f}s-{tile['end_timestamp_sec']:.1f}s, "
            f"frames {tile['start_frame']}-{tile['end_frame']})"
        )
    lines.append("")
    return "\n".join(lines)


def build_clip_context(start_sec: float, end_sec: float, fps: float | None) -> str:
    """ネイティブ動画クリップ（`--ranges` / `--video --start --end`）用の短いコンテキスト。

    タイル経路の `build_tile_context` に相当するもの。絶対秒で答えさせることが目的。
    """
    lines = [
        "## この映像について",
        "",
        f"- この映像は動画全体の {start_sec:.1f} 秒から {end_sec:.1f} 秒を切り出したもの。",
        (
            f"- 秒数は必ず動画全体の絶対秒（{start_sec:.1f}〜{end_sec:.1f} の範囲）で書くこと。"
            "切り出しの先頭を 0 秒としない。"
        ),
    ]
    if fps is not None:
        lines.append(f"- サンプリングは {fps} fps。")
    lines.append("")
    return "\n".join(lines)


def apply_objective(prompt_body: str, objective: str) -> str:
    if OBJECTIVE_PLACEHOLDER in prompt_body:
        return prompt_body.replace(OBJECTIVE_PLACEHOLDER, objective)
    return prompt_body


def build_hypothesis_block(note: str) -> str:
    return (
        "## 事前の仮説（全体把握での観察）\n"
        f"{note}\n\n"
        "この仮説を映像上の根拠で確認・反証し、hypothesis_verdict に回答すること。\n"
        "仮説に引きずられず、見えている事実を優先すること。"
    )


def assemble_prompt(
    prompt_body: str,
    objective: str,
    tile_context: str,
    context_text: str | None,
    note: str | None,
    retry_hint: bool = False,
) -> str:
    parts = [apply_objective(prompt_body, objective), tile_context]
    if note:
        parts.append(build_hypothesis_block(note))
    if context_text:
        parts.append(f"## 追加コンテキスト\n{context_text}")
    if retry_hint:
        parts.append("前回の出力はJSONとして不正だった。コードフェンス付きJSONのみを出力せよ。")
    return "\n\n".join(part for part in parts if part)
