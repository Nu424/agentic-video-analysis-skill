"""avs.ranges のテスト（config 合成・重なりマージ・出力パス・range オプション生成）。

動画は不要。
"""

from __future__ import annotations

import pytest

from avs.ranges import (
    make_range_options,
    merge_defaults,
    merge_range_entries,
    overlap_ratio,
    parse_timestamps_value,
    resolve_output_path,
)
from avs.tiling import TileOptions


# --- defaults 合成 -------------------------------------------------------------


def test_merge_defaults_overrides_defaults():
    config = {"defaults": {"fps": 5, "pad": 2, "frames_per_tile": 12}}
    merged = merge_defaults(config, {"label": "a", "start": 4, "end": 9, "fps": 9})
    assert merged["fps"] == 9  # range 側が勝つ
    assert merged["pad"] == 2  # defaults から継承
    assert merged["frames_per_tile"] == 12
    assert merged["label"] == "a"


def test_merge_defaults_requires_start_end():
    with pytest.raises(RuntimeError):
        merge_defaults({"defaults": {"fps": 5}}, {"label": "a", "start": 4})


def test_merge_defaults_allows_zoom_range_without_start_end():
    merged = merge_defaults({"defaults": {"fps": 5}}, {"label": "z", "timestamps": [3.5, 7.0]})
    assert merged["timestamps"] == [3.5, 7.0]
    assert "start" not in merged


# --- 重なりマージ --------------------------------------------------------------


def test_overlap_ratio_uses_shorter_span():
    # 重なり2秒 / 短い方4秒 = 0.5
    assert overlap_ratio({"start": 0, "end": 10}, {"start": 8, "end": 12}) == pytest.approx(0.5)
    assert overlap_ratio({"start": 0, "end": 4}, {"start": 4, "end": 8}) == 0.0


def test_merge_range_entries_merges_overlapping():
    ranges = [
        {"label": "a", "start": 4, "end": 9, "priority": "medium", "note": "仮説A"},
        {"label": "b", "start": 8, "end": 12, "priority": "high", "note": "仮説B"},
        {"label": "c", "start": 20, "end": 24, "priority": "low", "note": "別件"},
    ]
    merged = merge_range_entries(ranges, 0.2)
    assert len(merged) == 2

    first = merged[0]
    assert (first["start"], first["end"]) == (4.0, 12.0)
    assert first["label"] == "a_b"
    assert first["note"] == "仮説A / 仮説B"  # note は " / " で連結
    assert first["priority"] == "high"  # 高い方を採用
    assert merged[1]["label"] == "c"


def test_merge_range_entries_threshold_is_inclusive():
    ranges = [
        {"label": "a", "start": 0, "end": 10},
        {"label": "b", "start": 8, "end": 12},  # 重なり率ちょうど 0.5
    ]
    assert len(merge_range_entries(ranges, 0.5)) == 1
    assert len(merge_range_entries(ranges, 0.51)) == 2


def test_merge_range_entries_keeps_non_overlapping_and_sorts():
    ranges = [
        {"label": "late", "start": 20, "end": 24},
        {"label": "early", "start": 1, "end": 3},
    ]
    merged = merge_range_entries(ranges, 0.5)
    assert [entry["label"] for entry in merged] == ["early", "late"]


def test_merge_range_entries_deduplicates_identical_notes():
    ranges = [
        {"label": "a", "start": 0, "end": 10, "note": "同じ"},
        {"label": "a", "start": 8, "end": 12, "note": "同じ"},
    ]
    merged = merge_range_entries(ranges, 0.5)
    assert merged[0]["note"] == "同じ"
    assert merged[0]["label"] == "a"


# --- 出力パス ------------------------------------------------------------------


def test_resolve_output_path_uses_label_and_fps(tmp_path):
    config = {"output_dir": str(tmp_path / "cand")}
    merged = {"label": "cand_a", "start": 4, "end": 9, "fps": 5}
    assert resolve_output_path(config, merged) == tmp_path / "cand" / "cand_a_fps5.jpg"


def test_resolve_output_path_falls_back_to_start_end(tmp_path):
    config = {"output_dir": str(tmp_path / "cand")}
    assert resolve_output_path(config, {"start": 4, "end": 9}) == tmp_path / "cand" / "4_9_fps1.jpg"


def test_resolve_output_path_zoom_naming(tmp_path):
    config = {"output_dir": str(tmp_path / "z")}
    merged = {"label": "cand_a", "timestamps": [3.5, 7.0]}
    assert resolve_output_path(config, merged) == tmp_path / "z" / "cand_a_zoom.jpg"


def test_resolve_output_path_explicit_output_wins(tmp_path):
    config = {"output_dir": str(tmp_path / "ignored")}
    merged = {"output": str(tmp_path / "explicit.jpg"), "start": 0, "end": 1}
    assert resolve_output_path(config, merged) == tmp_path / "explicit.jpg"


# --- range オプション生成 ------------------------------------------------------


def test_make_range_options_applies_range_keys(tmp_path):
    base = TileOptions(config="ranges.json", fps=1.0, pad=0.0, frames_per_tile=12)
    merged = {"start": 4, "end": 9, "fps": 5, "pad": 2, "frames_per_tile": 6, "label": "a"}
    options = make_range_options(base, tmp_path / "v.mp4", merged, tmp_path / "out.jpg")

    assert options.config is None
    assert options.video == str(tmp_path / "v.mp4")
    assert options.output == str(tmp_path / "out.jpg")
    assert (options.start, options.end) == (4.0, 9.0)
    assert options.fps == 5
    assert options.pad == 2
    assert options.frames_per_tile == 6
    # base は書き換えない
    assert base.fps == 1.0
    assert base.config == "ranges.json"


def test_make_range_options_zoom_range_keeps_start_end(tmp_path):
    base = TileOptions(start=0.0, end=None)
    merged = {"timestamps": [3.5, 7.0], "label": "z"}
    options = make_range_options(base, tmp_path / "v.mp4", merged, tmp_path / "z.jpg")

    # zoom range は start/end を持たないので触らない
    assert options.start == 0.0
    assert options.end is None
    assert parse_timestamps_value(merged["timestamps"]) == [3.5, 7.0]


def test_parse_timestamps_value_accepts_string_and_list():
    assert parse_timestamps_value("3.5,7") == [3.5, 7.0]
    assert parse_timestamps_value([3.5, 7]) == [3.5, 7.0]
    with pytest.raises(RuntimeError):
        parse_timestamps_value([])
