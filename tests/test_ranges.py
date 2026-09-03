"""avs.ranges のテスト（config 合成・重なりマージ・出力パス・range オプション生成）。

動画は不要。
"""

from __future__ import annotations

import json
import re

import pytest

from avs.ranges import (
    make_range_options,
    merge_defaults,
    merge_range_entries,
    overlap_ratio,
    parse_timestamps_value,
    plan_full_coverage,
    resolve_output_path,
    run_config_mode,
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


# --- run_config_mode: 失敗隔離 ---------------------------------------------------


def test_run_config_mode_isolates_failures_and_still_writes_summary(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    summary_path = tmp_path / "batch_summary.json"
    config_path = tmp_path / "ranges.json"
    config_path.write_text(
        json.dumps(
            {
                "video": str(video),
                "output_dir": str(tmp_path / "out"),
                "defaults": {"fps": 1},
                "ranges": [
                    {"label": "a", "start": 0, "end": 2},
                    {"label": "b", "start": 5, "end": 7},
                ],
                "summary_output": str(summary_path),
            }
        ),
        encoding="utf-8",
    )

    calls: list[int] = []

    def fake_tile_one_range(options):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg boom")
        return {"output": "out.jpg", "manifest_path": str(tmp_path / "out" / "manifest.json")}

    monkeypatch.setattr("avs.ranges.tile_one_range", fake_tile_one_range)

    exit_code = run_config_mode(TileOptions(config=str(config_path)))

    assert exit_code == 0  # 1件は成功しているので全滅ではない
    assert len(calls) == 2  # b の処理まで続行された

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    statuses = {entry["label"]: entry["status"] for entry in summary["results"]}
    assert statuses == {"a": "error", "b": "ok"}
    failed = next(entry for entry in summary["results"] if entry["label"] == "a")
    assert "ffmpeg boom" in failed["error"]
    assert "manifest_path" not in failed


def test_run_config_mode_returns_1_when_all_ranges_fail(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    summary_path = tmp_path / "batch_summary.json"
    config_path = tmp_path / "ranges.json"
    config_path.write_text(
        json.dumps(
            {
                "video": str(video),
                "output_dir": str(tmp_path / "out"),
                "defaults": {"fps": 1},
                "ranges": [{"label": "a", "start": 0, "end": 2}],
                "summary_output": str(summary_path),
            }
        ),
        encoding="utf-8",
    )

    def always_fails(options):
        raise RuntimeError("boom")

    monkeypatch.setattr("avs.ranges.tile_one_range", always_fails)

    assert run_config_mode(TileOptions(config=str(config_path))) == 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["results"][0]["status"] == "error"


# --- plan_full_coverage ---------------------------------------------------------


def test_plan_full_coverage_fills_gaps_and_guarantees_bounds():
    plan = plan_full_coverage([{"start_sec": 5.0, "end_sec": 8.0}], 15.0)
    ranges = plan["ranges"]

    assert [r["label"] for r in ranges] == ["gap_00", "cand_00", "gap_01"]
    assert (ranges[0]["start"], ranges[0]["end"]) == (0.0, 5.0)
    assert (ranges[1]["start"], ranges[1]["end"]) == (5.0, 8.0)
    assert (ranges[2]["start"], ranges[2]["end"]) == (8.0, 15.0)
    assert ranges[0]["priority"] == "low" and ranges[0]["source"] == "gap"
    assert ranges[1]["source"] == "overview"
    # 先頭 start==0 / 末尾 end==duration の保証
    assert ranges[0]["start"] == 0.0
    assert ranges[-1]["end"] == plan["plan"]["duration_sec"] == 15.0


def test_plan_full_coverage_empty_candidates_covers_whole_video_with_gaps():
    plan = plan_full_coverage([], 20.0, max_range_sec=8.0)
    ranges = plan["ranges"]

    assert all(r["source"] == "gap" for r in ranges)
    assert ranges[0]["start"] == 0.0
    assert ranges[-1]["end"] == 20.0
    # 20s を 8s 上限で分割すると3片（等分）になる
    assert len(ranges) == 3
    for left, right in zip(ranges, ranges[1:]):
        assert left["end"] == pytest.approx(right["start"])


def test_plan_full_coverage_small_duration_without_candidates_is_single_gap():
    plan = plan_full_coverage([], 3.0, max_range_sec=8.0)
    assert len(plan["ranges"]) == 1
    assert plan["ranges"][0] == {
        "label": "gap_00",
        "start": 0.0,
        "end": 3.0,
        "priority": "low",
        "source": "gap",
        "fps": 5.0,
    }


def test_plan_full_coverage_splits_long_candidate_evenly():
    plan = plan_full_coverage(
        [{"start_sec": 0.0, "end_sec": 20.0, "title": "long"}], 20.0, max_range_sec=8.0
    )
    ranges = plan["ranges"]

    assert len(ranges) == 3  # ceil(20/8) == 3 等分
    assert all(r["source"] == "overview" for r in ranges)
    assert ranges[0]["start"] == 0.0
    assert ranges[-1]["end"] == 20.0
    for entry in ranges:
        assert (entry["end"] - entry["start"]) <= 8.0 + 0.01
    # 連続している（境界が一致）
    for left, right in zip(ranges, ranges[1:]):
        assert left["end"] == pytest.approx(right["start"])


def test_plan_full_coverage_merges_short_candidate_into_touching_neighbor():
    candidates = [
        {"start_sec": 5.0, "end_sec": 6.0, "priority": "low", "title": "small"},
        {"start_sec": 6.0, "end_sec": 10.0, "priority": "high", "title": "big"},
    ]
    plan = plan_full_coverage(candidates, 10.0, min_range_sec=2.0, min_gap_sec=1.0)
    ranges = plan["ranges"]

    overview_ranges = [r for r in ranges if r["source"] == "overview"]
    assert len(overview_ranges) == 1  # 短い方が隣に吸収された
    merged = overview_ranges[0]
    assert (merged["start"], merged["end"]) == (5.0, 10.0)
    assert merged["priority"] == "high"  # 高い方の優先度を採用
    assert "small" in merged["note"] and "big" in merged["note"]


def test_plan_full_coverage_leaves_isolated_short_candidate_alone():
    plan = plan_full_coverage(
        [{"start_sec": 5.0, "end_sec": 6.0}], 20.0, min_range_sec=2.0, min_gap_sec=1.0
    )
    overview_ranges = [r for r in plan["ranges"] if r["source"] == "overview"]
    assert len(overview_ranges) == 1
    assert (overview_ranges[0]["start"], overview_ranges[0]["end"]) == (5.0, 6.0)
    # 前後の隙間は gap で埋まる。先頭・末尾も保証される
    assert plan["ranges"][0]["start"] == 0.0
    assert plan["ranges"][-1]["end"] == 20.0


def test_plan_full_coverage_coverage_full_uses_detail_fps_everywhere():
    candidates = [
        {"start_sec": 2.0, "end_sec": 4.0, "priority": "low"},
        {"start_sec": 10.0, "end_sec": 12.0, "priority": "high"},
    ]
    plan = plan_full_coverage(candidates, 20.0, coverage="full", detail_fps=5.0, low_fps=1.0)
    assert all(entry["fps"] == 5.0 for entry in plan["ranges"])
    assert plan["plan"]["dropped"] == []


def test_plan_full_coverage_coverage_priority_uses_low_fps_for_low_priority():
    candidates = [
        {"start_sec": 2.0, "end_sec": 4.0, "priority": "low"},
        {"start_sec": 10.0, "end_sec": 12.0, "priority": "high"},
    ]
    plan = plan_full_coverage(
        candidates, 20.0, coverage="priority", detail_fps=5.0, low_fps=1.0, min_gap_sec=100.0
    )
    by_label = {entry["label"]: entry for entry in plan["ranges"]}
    high_entry = next(e for e in plan["ranges"] if e["priority"] == "high")
    low_entries = [e for e in plan["ranges"] if e["priority"] == "low"]
    assert high_entry["fps"] == 5.0
    assert low_entries and all(e["fps"] == 1.0 for e in low_entries)
    assert by_label  # sanity: labels are unique


def test_plan_full_coverage_coverage_high_only_drops_low_priority():
    candidates = [
        {"start_sec": 2.0, "end_sec": 4.0, "priority": "low"},
        {"start_sec": 10.0, "end_sec": 12.0, "priority": "high"},
    ]
    plan = plan_full_coverage(
        candidates, 20.0, coverage="high-only", detail_fps=5.0, min_gap_sec=100.0
    )
    assert all(entry["priority"] != "low" for entry in plan["ranges"])
    assert all(entry["fps"] == 5.0 for entry in plan["ranges"])
    dropped = plan["plan"]["dropped"]
    assert dropped and all(entry["priority"] == "low" for entry in dropped)
    assert all("fps" not in entry for entry in dropped)
    assert plan["plan"]["n_ranges"] == len(plan["ranges"])


def test_plan_full_coverage_rounds_seconds_to_two_decimals():
    plan = plan_full_coverage(
        [{"start_sec": 0.0, "end_sec": 20.0}], 20.0, max_range_sec=8.0
    )
    for entry in plan["ranges"]:
        assert entry["start"] == round(entry["start"], 2)
        assert entry["end"] == round(entry["end"], 2)
    assert plan["plan"]["duration_sec"] == round(plan["plan"]["duration_sec"], 2)


def test_plan_full_coverage_labels_are_filesystem_safe():
    candidates = [
        {"start_sec": 2.0, "end_sec": 4.0, "title": "Boss Fight! (Ch.1) ボス戦"},
    ]
    plan = plan_full_coverage(candidates, 20.0, min_gap_sec=100.0)
    label_re = re.compile(r"^(cand_\d{2}(_[a-z0-9_]+)?|gap_\d{2})$")
    for entry in plan["ranges"]:
        assert label_re.match(entry["label"]), entry["label"]
    cand = next(e for e in plan["ranges"] if e["source"] == "overview")
    assert cand["label"].startswith("cand_00_boss_fight")


def test_plan_full_coverage_video_output_dir_summary_output_are_none():
    plan = plan_full_coverage([], 10.0)
    assert plan["video"] is None
    assert plan["output_dir"] is None
    assert plan["summary_output"] is None


def test_plan_full_coverage_rejects_invalid_duration():
    with pytest.raises(RuntimeError):
        plan_full_coverage([], 0.0)


def test_plan_full_coverage_rejects_invalid_coverage():
    with pytest.raises(RuntimeError):
        plan_full_coverage([], 10.0, coverage="nonsense")


def test_plan_full_coverage_requires_start_end_keys():
    with pytest.raises(RuntimeError):
        plan_full_coverage([{"title": "no times"}], 10.0)
