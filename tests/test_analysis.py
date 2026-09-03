"""avs.analysis / avs.prompts のテスト。

aitool は呼ばない（チャンク分割・JSON 抽出・パート統合・プロンプト組み立てのみ）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.analysis import (
    chunk_with_overlap,
    collect_manifests,
    derive_basename,
    extract_json,
    merge_part_results,
    resolve_output_base,
)
from avs.prompts import (
    OBJECTIVE_PLACEHOLDER,
    apply_objective,
    assemble_prompt,
    build_hypothesis_block,
    build_tile_context,
    resolve_text_arg,
)


# --- チャンク分割 --------------------------------------------------------------


def test_chunk_with_overlap_fits_exactly():
    items = list(range(8))
    assert chunk_with_overlap(items, 8) == [items]


def test_chunk_with_overlap_single_item_over_limit():
    # max+1 -> 2チャンク。境界のタイルは両方に入る（1タイルオーバーラップ）
    items = list(range(9))
    chunks = chunk_with_overlap(items, 8)
    assert chunks == [list(range(8)), [7, 8]]
    assert chunks[0][-1] == chunks[1][0]


def test_chunk_with_overlap_multiple_chunks():
    items = list(range(10))
    chunks = chunk_with_overlap(items, 4)
    assert chunks == [[0, 1, 2, 3], [3, 4, 5, 6], [6, 7, 8, 9]]
    # 隣り合うチャンクは必ず1件だけ重なる
    for left, right in zip(chunks, chunks[1:]):
        assert left[-1] == right[0]


def test_chunk_with_overlap_max_one_has_no_overlap():
    assert chunk_with_overlap([0, 1, 2], 1) == [[0], [1], [2]]


def test_chunk_with_overlap_rejects_zero():
    with pytest.raises(RuntimeError):
        chunk_with_overlap([1, 2], 0)


# --- JSON 抽出 ----------------------------------------------------------------


def test_extract_json_with_json_fence():
    text = 'まとめました。\n```json\n{"events": [{"title": "A"}]}\n```\n以上。'
    assert extract_json(text) == {"events": [{"title": "A"}]}


def test_extract_json_with_bare_fence():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_without_fence():
    assert extract_json('  {"a": [1, 2]}  ') == {"a": [1, 2]}


def test_extract_json_invalid_raises_value_error():
    with pytest.raises(ValueError):
        extract_json("JSONではない文章です")
    with pytest.raises(ValueError):
        extract_json('```json\n{"a": 1,,}\n```')


# --- パート統合 ----------------------------------------------------------------


def test_merge_part_results_concatenates_lists_and_collects_scalars():
    parts = [
        {"summary": "前半", "events": [{"title": "A"}], "hypothesis_verdict": "confirmed"},
        {"summary": "後半", "events": [{"title": "B"}, {"title": "C"}], "hypothesis_verdict": "rejected"},
    ]
    merged = merge_part_results(parts)
    assert [event["title"] for event in merged["events"]] == ["A", "B", "C"]
    assert merged["summary_parts"] == ["前半", "後半"]
    assert merged["hypothesis_verdict_parts"] == ["confirmed", "rejected"]
    assert merged["_merged_from_parts"] == 2
    assert "summary" not in merged


def test_merge_part_results_keeps_non_dict_parts():
    merged = merge_part_results([{"events": [1]}, ["生の配列"]])
    assert merged["events"] == [1]
    assert merged["_unstructured_parts"] == [["生の配列"]]
    assert merged["_merged_from_parts"] == 2


# --- 出力パス ------------------------------------------------------------------


def test_derive_basename():
    assert derive_basename(Path("/x/cand_a_fps5/manifest.json")) == "cand_a_fps5"
    assert derive_basename(Path("/x/one.json")) == "one"


def test_resolve_output_base_defaults(tmp_path):
    manifest = tmp_path / "cand_a" / "manifest.json"
    base = resolve_output_base(manifest, None, None, multiple=False)
    assert base == (tmp_path / "cand_a_analysis").resolve()

    single = tmp_path / "one.json"
    assert resolve_output_base(single, None, None, multiple=False) == (tmp_path / "one_analysis").resolve()


def test_resolve_output_base_with_output_dir(tmp_path):
    manifest = tmp_path / "cand_a" / "manifest.json"
    base = resolve_output_base(manifest, str(tmp_path / "out"), None, multiple=True)
    assert base == (tmp_path / "out" / "cand_a_analysis").resolve()


def test_resolve_output_base_output_rejected_for_multiple(tmp_path):
    manifest = tmp_path / "cand_a" / "manifest.json"
    with pytest.raises(RuntimeError):
        resolve_output_base(manifest, None, str(tmp_path / "x.txt"), multiple=True)


# --- manifest 列挙 -------------------------------------------------------------


def test_collect_manifests_reads_notes_from_summary(tmp_path):
    manifest_a = tmp_path / "a" / "manifest.json"
    manifest_b = tmp_path / "b" / "manifest.json"
    summary = tmp_path / "batch_summary.json"
    summary.write_text(
        json.dumps(
            {
                "results": [
                    {"manifest_path": str(manifest_a), "note": "仮説A"},
                    {"manifest_path": str(manifest_b), "note": None},
                    {"status": "dry_run"},
                ]
            }
        ),
        encoding="utf-8",
    )

    pairs = collect_manifests(str(summary), [str(manifest_a)])
    # summary の note が対応付き、重複は1回だけ
    assert pairs == [(manifest_a.resolve(), "仮説A"), (manifest_b.resolve(), None)]


def test_collect_manifests_requires_input():
    with pytest.raises(RuntimeError):
        collect_manifests(None, None)


# --- プロンプト組み立て ---------------------------------------------------------

MANIFEST = {
    "extraction": {"start_sec": 3.0, "end_sec": 10.0, "fps": 2},
}
TILES = [
    {"tile_index": 0, "start_timestamp_sec": 3.0, "end_timestamp_sec": 5.5, "start_frame": 0, "end_frame": 5},
    {"tile_index": 1, "start_timestamp_sec": 6.0, "end_timestamp_sec": 8.5, "start_frame": 6, "end_frame": 11},
]
TILE_PATHS = [Path("/x/tile_000.jpg"), Path("/x/tile_001.jpg")]


def test_build_tile_context_lists_tiles():
    context = build_tile_context(MANIFEST, TILES, TILE_PATHS)
    assert "## タイル画像の読み方" in context
    assert "範囲: 3.0s - 10.0s / fps: 2 / タイル数: 2" in context
    assert "- Tile 0: tile_000.jpg (t=3.0s-5.5s, frames 0-5)" in context
    assert "- Tile 1: tile_001.jpg (t=6.0s-8.5s, frames 6-11)" in context
    assert "分割解析のパート" not in context


def test_build_tile_context_part_info():
    context = build_tile_context(MANIFEST, TILES, TILE_PATHS, part_info=(1, 3, 6.0, 8.5))
    assert "分割解析のパート 2/3" in context
    assert "6.0s - 8.5s" in context


def test_apply_objective_replaces_placeholder():
    assert apply_objective(f"目的: {OBJECTIVE_PLACEHOLDER}", "テスト目的") == "目的: テスト目的"
    # プレースホルダが無い本文はそのまま
    assert apply_objective("目的なし", "テスト目的") == "目的なし"


def test_assemble_prompt_order():
    body = f"本文 目的={OBJECTIVE_PLACEHOLDER}"
    prompt = assemble_prompt(
        body,
        "テスト目的",
        "TILE_CONTEXT",
        "追加文脈",
        "仮説メモ",
        retry_hint=True,
    )
    positions = [
        prompt.index("本文 目的=テスト目的"),
        prompt.index("TILE_CONTEXT"),
        prompt.index("## 事前の仮説"),
        prompt.index("## 追加コンテキスト"),
        prompt.index("前回の出力はJSONとして不正だった"),
    ]
    assert positions == sorted(positions)
    assert "仮説メモ" in prompt
    assert "追加文脈" in prompt
    assert build_hypothesis_block("仮説メモ") in prompt


def test_assemble_prompt_omits_optional_blocks():
    prompt = assemble_prompt("本文", "テスト目的", "TILE_CONTEXT", None, None)
    assert prompt == "本文\n\nTILE_CONTEXT"


# --- 目的・コンテキストの解決 ---------------------------------------------------


def test_resolve_text_arg_returns_literal_when_not_a_file():
    assert resolve_text_arg("テスト目的") == "テスト目的"
    assert resolve_text_arg(None) is None


def test_resolve_text_arg_reads_file(tmp_path):
    path = tmp_path / "objective.txt"
    path.write_text("  ファイルの目的\n", encoding="utf-8")
    assert resolve_text_arg(str(path)) == "ファイルの目的"
