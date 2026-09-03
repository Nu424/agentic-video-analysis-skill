#!/usr/bin/env python
"""プロンプト組み立てと、各プロンプトの JSON スキーマ。

このモジュールは3つの役割を持つ。

1. **プロンプト組み立て**: `assemble_prompt` が最終プロンプトを作る。構成は常にこの順:

   1. プロンプト本文（`{{OBJECTIVE}}` を目的で、`{{SCHEMA}}` をスキーマ例で置換したもの）
   2. 入力の読み方: タイル経路は `build_tile_context`、
      ネイティブ動画クリップ経路は `build_clip_context`
   3. 事前の仮説（`build_hypothesis_block`。config の note がある範囲のみ）
   4. ドメインの手引き（`build_domain_block`。`--domain` がある場合のみ）
   5. 追加コンテキスト（`--context`）
   6. リトライ時のヒント

2. **JSON スキーマ**: `SCHEMAS` に各プロンプトのスキーマを Python dict で持つ（唯一の正）。
   プロンプトのテキストに書かれた JSON 例は `render_schema_example` が **この dict から生成**して
   `{{SCHEMA}}` に埋め込むので、テキストと dict が二重管理にならない。
   API に構造化出力を要求するときは `api_schema()` で注釈キーワード（`examples`）を落として渡す。

3. **ドメイン定義**: `load_domain` が `domain.json` を読んで検証し、
   `build_domain_block` / `build_importance_block` がプロンプト用のテキストに整形する。
   スキル本体は特定ドメインを知らない。ドメイン知識は必ずこのファイル経由で外から入る。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_OBJECTIVE = "動画の見どころ候補の抽出"
OBJECTIVE_PLACEHOLDER = "{{OBJECTIVE}}"
SCHEMA_PLACEHOLDER = "{{SCHEMA}}"

# スキーマに置く注釈キーワード（プロンプト例の生成にだけ使い、API には送らない）
ANNOTATION_KEYS = ("examples",)


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


# --- JSON スキーマ ------------------------------------------------------------
#
# 各プロパティの `examples` はプロンプトに埋め込む JSON 例の値になる（`render_schema_example`）。
# `required` は主要キーだけにし、`additionalProperties` は付けない
# （モデルが余計なキーを足しても壊れないようにするため）。


def _string(example: str, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "examples": [example]}
    if description:
        node["description"] = description
    return node


def _number(example: float, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "number", "examples": [example]}
    if description:
        node["description"] = description
    return node


def _boolean(example: bool, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "boolean", "examples": [example]}
    if description:
        node["description"] = description
    return node


def _enum(values: list[str], example: str, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "enum": values, "examples": [example]}
    if description:
        node["description"] = description
    return node


def _string_array(example: list[str], description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string"},
        "examples": [example],
    }
    if description:
        node["description"] = description
    return node


def _number_array(example: list[float], description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "array",
        "items": {"type": "number"},
        "examples": [example],
    }
    if description:
        node["description"] = description
    return node


def _bounds(example: list[float], description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
        "description": description,
        "examples": [example],
    }


CONFIDENCE_VALUES = ["high", "medium", "low"]
PRIORITY_VALUES = ["high", "medium", "low"]
IMPORTANCE_VALUES = ["high", "medium", "low"]
VERDICT_VALUES = ["confirmed", "partially", "rejected", "n/a"]
AUDIO_KIND_VALUES = ["bgm", "speech", "sfx", "other"]


OVERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": _string("動画全体の概要(2〜4文)"),
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": _string("candidate_a", "英数字とアンダースコアだけの識別子"),
                    "start_sec": _number(38.0, "開始の最良推定（絶対秒）"),
                    "end_sec": _number(46.0, "終了の最良推定（絶対秒）"),
                    "start_bounds": _bounds(
                        [36.0, 38.0], "[未発生を確認した直前フレームの時刻, 発生を確認した時刻]"
                    ),
                    "end_bounds": _bounds(
                        [44.0, 46.0], "[継続を確認した最後の時刻, 終了を確認した時刻]"
                    ),
                    "title": _string("短いタイトル"),
                    "evidence": _string_array(["F19 t=38.0s に◯◯が見える"]),
                    "priority": _enum(PRIORITY_VALUES, "high"),
                    "needs_followup": _boolean(True),
                    "reason": _string("次段で確認すべき理由（詳細解析に仮説として渡される）"),
                },
                "required": ["label", "start_sec", "end_sec", "title", "priority"],
            },
        },
    },
    "required": ["summary", "candidates"],
}


DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": _number(39.0, "開始の最良推定（絶対秒）"),
                    "end_sec": _number(44.2, "終了の最良推定（絶対秒）"),
                    "start_bounds": _bounds(
                        [38.8, 39.0], "[未発生を確認した直前フレームの時刻, 発生を確認した時刻]"
                    ),
                    "end_bounds": _bounds(
                        [44.2, 44.4], "[継続を確認した最後の時刻, 終了を確認した時刻]"
                    ),
                    "title": _string("短いタイトル"),
                    "summary": _string("何が起きたか"),
                    "visual": _string_array(
                        ["F5 t=39.0s: 画面に◯◯"], "根拠。セルラベルと、その時刻の画面に何が映っているか"
                    ),
                    "confidence": _enum(CONFIDENCE_VALUES, "high"),
                    "zoom_targets": _number_array(
                        [39.4], "判読できず高解像度で確認したい時刻（絶対秒）"
                    ),
                },
                "required": ["start_sec", "end_sec", "title", "summary", "confidence"],
            },
        },
        "hypothesis_verdict": _enum(VERDICT_VALUES, "confirmed", "仮説が無い場合は n/a"),
        "notes": _string("誤認しやすい点など"),
    },
    "required": ["events", "hypothesis_verdict"],
}


REFINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start_sec": _number(38.8),
        "end_sec": _number(44.6),
        "evidence": _string_array(["F3 t=38.8s で開始を確認"]),
        "suggested_pad_before_sec": _number(1.0),
        "suggested_pad_after_sec": _number(0.5),
        "needs_more_review": _boolean(False),
        "remaining_questions": _string_array([]),
    },
    "required": ["start_sec", "end_sec", "evidence"],
}


ZOOM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp_sec": _number(38.5, "ラベルに書かれた絶対秒"),
                    "readable_text": _string_array(["画面上で判読できた文字列"]),
                    "details": _string("細部の観察"),
                    "unreadable": _string_array(["判読を試みたが読めなかった要素"]),
                },
                "required": ["timestamp_sec"],
            },
        }
    },
    "required": ["frames"],
}


MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": _string("動画全体の概要(2〜4文)"),
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": _number(39.0),
                    "end_sec": _number(44.2),
                    "title": _string("短いタイトル"),
                    "summary": _string("何が起きたか"),
                    "importance": _enum(IMPORTANCE_VALUES, "high", "目的への寄与で付ける"),
                    "confidence": _enum(CONFIDENCE_VALUES, "medium", "入力より格上げしない"),
                    "evidence": _string_array(["F5 t=39.0s: 画面に◯◯"]),
                    "sources": _string_array(["candidate_a"], "統合元の範囲ラベル"),
                    "flags": _string_array(["boundary"], "入力から引き継ぐ"),
                },
                "required": ["start_sec", "end_sec", "title", "summary", "importance"],
            },
        },
    },
    "required": ["overview", "timeline"],
}


AUDIO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_sec": _number(12.0),
                    "end_sec": _number(18.5),
                    "kind": _enum(AUDIO_KIND_VALUES, "speech"),
                    "description": _string("聞こえた内容"),
                    "confidence": _enum(CONFIDENCE_VALUES, "medium"),
                },
                "required": ["start_sec", "end_sec", "kind", "description"],
            },
        }
    },
    "required": ["segments"],
}


CHAPTERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": _string("chapter_01", "英数字とアンダースコアだけの識別子"),
                    "start_sec": _number(0.0),
                    "end_sec": _number(180.0),
                    "title": _string("短いタイトル"),
                    "summary": _string("この章で何が続いているか(1〜2文)"),
                },
                "required": ["label", "start_sec", "end_sec", "title"],
            },
        }
    },
    "required": ["chapters"],
}


SCHEMAS: dict[str, dict[str, Any]] = {
    "overview": OVERVIEW_SCHEMA,
    "detail": DETAIL_SCHEMA,
    "refine": REFINE_SCHEMA,
    "zoom": ZOOM_SCHEMA,
    "merge": MERGE_SCHEMA,
    "audio": AUDIO_SCHEMA,
    "chapters": CHAPTERS_SCHEMA,
}


def schema_for_prompt(prompt_path: str | Path) -> dict[str, Any] | None:
    """プロンプトファイル名（拡張子を除いた stem）から既定スキーマを引く。

    未知の stem（ユーザー自作プロンプト）では None を返す。
    戻り値は `examples` を含む注釈付きのスキーマ（プロンプト例の生成に使う）。
    API に渡すときは `api_schema()` を通すこと。
    """
    stem = Path(prompt_path).stem
    return SCHEMAS.get(stem)


def api_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """API へ送る用に注釈キーワード（`examples`）を落としたコピーを返す。"""
    if schema is None:
        return None
    return _strip_annotations(copy.deepcopy(schema))


def _strip_annotations(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_annotations(value)
            for key, value in node.items()
            if key not in ANNOTATION_KEYS
        }
    if isinstance(node, list):
        return [_strip_annotations(item) for item in node]
    return node


def _example_value(node: Any) -> Any:
    """スキーマの1ノードから、例として埋め込む値を作る。"""
    if not isinstance(node, dict):
        return None
    examples = node.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    enum_values = node.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]
    node_type = node.get("type")
    if node_type == "object":
        return {
            key: _example_value(value) for key, value in node.get("properties", {}).items()
        }
    if node_type == "array":
        items = node.get("items")
        return [_example_value(items)] if isinstance(items, dict) else []
    if node_type == "integer":
        return 0
    if node_type == "number":
        return 0.0
    if node_type == "boolean":
        return True
    if node_type == "string":
        return node.get("description") or "文字列"
    return None


# 短いスカラー配列は1行にまとめる（`[38.8, 39.0]` のような見た目にするため）
_MULTILINE_ARRAY_RE = re.compile(r"\[\n(?:[^\[\]{}\n]*\n)+?\s*\]")
_INLINE_ARRAY_MAX_LEN = 60


def _compact_short_arrays(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        lines = match.group(0).splitlines()
        inner = " ".join(line.strip() for line in lines[1:-1])
        candidate = f"[{inner}]"
        return candidate if len(candidate) <= _INLINE_ARRAY_MAX_LEN else match.group(0)

    return _MULTILINE_ARRAY_RE.sub(replace, text)


def render_schema_example(schema: dict[str, Any]) -> str:
    """スキーマから、プロンプトに埋め込む読みやすい JSON 例（インデント2）を作る。"""
    dumped = json.dumps(_example_value(schema), ensure_ascii=False, indent=2)
    return _compact_short_arrays(dumped)


def apply_schema(prompt_body: str, schema: dict[str, Any] | None) -> str:
    """本文の `{{SCHEMA}}` をスキーマ例で置換する。

    プレースホルダが無い、またはスキーマが無い場合は本文をそのまま返す
    （ユーザー自作プロンプトを許すため、未知の stem はエラーにしない）。
    """
    if schema is None or SCHEMA_PLACEHOLDER not in prompt_body:
        return prompt_body
    return prompt_body.replace(SCHEMA_PLACEHOLDER, render_schema_example(schema))


# --- ドメイン定義 -------------------------------------------------------------


class DomainError(ValueError):
    """`domain.json` の読み込み・検証エラー（メッセージは日本語）。"""


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DomainError(f"ドメイン定義の {field} は文字列で書いてください（実際: {type(value).__name__}）")
    return value


def load_domain(path: str | Path) -> dict[str, Any]:
    """`domain.json` を読み込んで形式を検証する。エラーメッセージは日本語。

    検証するのは形式だけで、内容（ドメイン知識そのもの）には一切関与しない。
    """
    domain_path = Path(path).expanduser()
    if not domain_path.exists():
        raise DomainError(f"ドメイン定義ファイルが見つかりません: {domain_path}")
    try:
        raw = domain_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainError(f"ドメイン定義ファイルを読めません: {domain_path}: {exc}") from exc
    try:
        domain = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainError(
            f"ドメイン定義のJSONを解析できません: {domain_path} ({exc.lineno}行目: {exc.msg})"
        ) from exc

    validate_domain(domain)
    return domain


def validate_domain(domain: Any) -> dict[str, Any]:
    """ドメイン定義の形式を検証する。問題があれば `DomainError` を投げる。"""
    if not isinstance(domain, dict):
        raise DomainError("ドメイン定義はオブジェクト（{ ... }）で書いてください")

    for field in ("name", "description", "hud_notes", "importance_rubric"):
        if field in domain and domain[field] is not None:
            _require_str(domain[field], field)

    watchlist = domain.get("watchlist")
    if watchlist is not None:
        if not isinstance(watchlist, list):
            raise DomainError("ドメイン定義の watchlist は文字列の配列で書いてください")
        for index, item in enumerate(watchlist):
            _require_str(item, f"watchlist[{index}]")

    vocabulary = domain.get("vocabulary")
    if vocabulary is not None:
        if not isinstance(vocabulary, dict):
            raise DomainError(
                'ドメイン定義の vocabulary は {"用語": "映像上でどう見えるか"} の形で書いてください'
            )
        for key, value in vocabulary.items():
            _require_str(value, f"vocabulary[{key!r}]")

    negatives = domain.get("negatives")
    if negatives is not None:
        if not isinstance(negatives, list):
            raise DomainError("ドメイン定義の negatives は配列で書いてください")
        for index, entry in enumerate(negatives):
            _validate_negative(entry, index)

    return domain


def _validate_negative(entry: Any, index: int) -> None:
    where = f"negatives[{index}]"
    if not isinstance(entry, dict):
        raise DomainError(f"ドメイン定義の {where} はオブジェクト（{{ ... }}）で書いてください")
    if "name" not in entry:
        raise DomainError(f"ドメイン定義の {where} に name がありません")
    _require_str(entry["name"], f"{where}.name")
    if "reason" in entry and entry["reason"] is not None:
        _require_str(entry["reason"], f"{where}.reason")

    pattern = entry.get("pattern")
    if pattern is not None:
        _require_str(pattern, f"{where}.pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DomainError(
                f"ドメイン定義の {where}.pattern が正規表現として不正です: {pattern!r} ({exc})"
            ) from exc

    window = entry.get("window")
    if window is not None:
        if not isinstance(window, list) or len(window) != 2:
            raise DomainError(f"ドメイン定義の {where}.window は [開始秒, 終了秒] の2要素で書いてください")
        for value in window:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DomainError(f"ドメイン定義の {where}.window の要素は数値で書いてください")
        if window[0] > window[1]:
            raise DomainError(f"ドメイン定義の {where}.window は 開始秒 <= 終了秒 にしてください")


def build_domain_block(domain: dict[str, Any] | None) -> str:
    """ドメイン定義をプロンプト末尾に付けるテキストに整形する。

    `hud_notes / watchlist / vocabulary` を「## ドメインの手引き」、
    `negatives[].name` を「## 誤認されやすい事象」として出す。
    書ける内容が何も無ければ空文字を返す（`assemble_prompt` が空ブロックを落とす）。
    `importance_rubric` は merge 用なので `build_importance_block` に分けてある。
    """
    if not domain:
        return ""

    guide: list[str] = []
    description = domain.get("description")
    if description:
        guide.append(str(description))

    hud_notes = domain.get("hud_notes")
    if hud_notes:
        guide.append(f"画面上の状態表示の読み方: {hud_notes}")

    watchlist = domain.get("watchlist") or []
    if watchlist:
        guide.append("注視すべき変化:")
        guide.extend(f"- {item}" for item in watchlist)

    vocabulary = domain.get("vocabulary") or {}
    if vocabulary:
        guide.append("用語（映像上でどう見えるか）:")
        guide.extend(f"- {term}: {meaning}" for term, meaning in vocabulary.items())

    negatives = [entry.get("name") for entry in (domain.get("negatives") or []) if entry.get("name")]

    blocks: list[str] = []
    if guide:
        name = domain.get("name")
        heading = "## ドメインの手引き" + (f"（{name}）" if name else "")
        blocks.append(
            "\n".join(
                [
                    heading,
                    "",
                    "以下はこの種の動画についての外部知識。**映像上で確認できたことだけを書く**という原則は変わらない。",
                    "",
                    *guide,
                ]
            )
        )
    if negatives:
        blocks.append(
            "\n".join(
                [
                    "## 誤認されやすい事象（映像で明確に確認できた場合のみ書く）",
                    "",
                    "次の事象は、実際には起きていないのに起きたと書かれやすい。",
                    "映像上の根拠を挙げられないなら書かないこと。",
                    "",
                    *[f"- {item}" for item in negatives],
                ]
            )
        )
    return "\n\n".join(blocks)


def build_importance_block(domain: dict[str, Any] | None) -> str:
    """`importance_rubric` を merge プロンプト用のブロックに整形する。無ければ空文字。"""
    if not domain:
        return ""
    rubric = domain.get("importance_rubric")
    if not rubric:
        return ""
    return f"## 重要度の基準\n\n{rubric}"


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
    domain: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> str:
    """最終プロンプトを組み立てる。

    `domain` / `schema` を省略したときの出力は、これらが無かった頃と完全に同一。
    """
    body = apply_schema(apply_objective(prompt_body, objective), schema)
    parts = [body, tile_context]
    if note:
        parts.append(build_hypothesis_block(note))
    if domain:
        parts.append(build_domain_block(domain))
    if context_text:
        parts.append(f"## 追加コンテキスト\n{context_text}")
    if retry_hint:
        parts.append("前回の出力はJSONとして不正だった。コードフェンス付きJSONのみを出力せよ。")
    return "\n\n".join(part for part in parts if part)
