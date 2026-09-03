"""avs.session のテスト（Session の create/find/record_step、レポート集計）。

実 API・実動画は不要。ffprobe が無くても Session.create は動く（duration_sec が None になるだけ）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.session import (
    Session,
    build_report,
    classify_step,
    estimate_cost,
    load_usage_records,
    scan_next_actions,
    summarize_usage,
)


# --- Session: create / find / record_step ----------------------------------------


def test_session_create_writes_session_json(tmp_path):
    session = Session.create(
        tmp_path / "sess",
        video="video.mp4",
        backend="openrouter",
        model="google/gemini-3.7-flash",
        objective="見どころ抽出",
        domain_path="notes/domain.json",
    )

    assert session.session_path.exists()
    data = json.loads(session.session_path.read_text(encoding="utf-8"))
    assert data["video"] == "video.mp4"
    assert data["backend"] == "openrouter"
    assert data["model"] == "google/gemini-3.7-flash"
    assert data["objective"] == "見どころ抽出"
    assert data["domain"] == "notes/domain.json"
    assert data["steps"] == []
    assert "created_at" in data
    # video が実在しない/ffprobeが無い場合でも作成自体は失敗しない
    assert "duration_sec" in data


def test_session_find_walks_up_from_nested_nonexistent_path(tmp_path):
    Session.create(tmp_path / "sess", video="video.mp4", backend="openrouter")

    found = Session.find(tmp_path / "sess" / "ranges" / "cand_a_analysis.json")
    assert found is not None
    assert found.root == (tmp_path / "sess").resolve()


def test_session_find_returns_none_when_not_found(tmp_path):
    assert Session.find(tmp_path / "nowhere" / "deep") is None


def test_session_record_step_appends_to_steps(tmp_path):
    session = Session.create(tmp_path / "sess", video="video.mp4", backend="openrouter")
    session.record_step("overview", output="overview/overview_analysis.json")
    session.record_step("detail", n_ranges=3)

    data = session.load()
    assert [step["name"] for step in data["steps"]] == ["overview", "detail"]
    assert data["steps"][0]["output"] == "overview/overview_analysis.json"
    assert data["steps"][1]["n_ranges"] == 3
    assert "recorded_at" in data["steps"][0]


# --- usage.jsonl 集計 ---------------------------------------------------------------


def _usage_record(**overrides):
    base = {
        "name": "cand_a_analysis",
        "backend": "fake",
        "model": "google/gemini-3.7-flash",
        "media": ["tile_000.jpg", "tile_001.jpg"],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "reasoning_tokens": 0,
            "total_tokens": 1200,
        },
        "cost_usd": 0.002,
        "cost_is_estimate": False,
        "latency_sec": 1.5,
        "retries": 0,
    }
    base.update(overrides)
    return base


def test_classify_step_matches_keywords():
    assert classify_step("overview_full_analysis") == "overview"
    assert classify_step("cand_a_detail_analysis") == "detail"
    assert classify_step("something_else") == "other"


def test_load_usage_records_skips_broken_lines_and_missing_file(tmp_path):
    assert load_usage_records(tmp_path) == []

    usage_path = tmp_path / "usage.jsonl"
    usage_path.write_text('{"name": "a"}\nnot json\n{"name": "b"}\n', encoding="utf-8")
    records = load_usage_records(tmp_path)
    assert [r["name"] for r in records] == ["a", "b"]


def test_summarize_usage_separates_actual_and_estimated_cost():
    records = [
        _usage_record(name="overview_full_analysis", cost_usd=0.002, cost_is_estimate=False),
        _usage_record(name="cand_a_detail_analysis", cost_usd=0.01, cost_is_estimate=True),
        _usage_record(name="cand_b_detail_analysis", cost_usd=None),
    ]
    summary = summarize_usage(records)

    assert summary["calls"] == 3
    assert summary["tokens"]["total_tokens"] == 3600
    assert summary["cost_usd_actual"] == 0.002
    assert summary["cost_usd_estimated"] == 0.01
    assert summary["cost_usd_total"] == pytest.approx(0.012)
    assert summary["unknown_cost_calls"] == 1
    assert summary["cost_has_estimate"] is True
    assert summary["latency_sec"] == pytest.approx(4.5)
    assert set(summary["by_step"]) == {"overview", "detail"}
    assert summary["by_step"]["detail"]["calls"] == 2


def test_summarize_usage_empty_records():
    summary = summarize_usage([])
    assert summary["calls"] == 0
    assert summary["cost_usd_total"] == 0
    assert summary["cost_has_estimate"] is False
    assert summary["by_step"] == {}


# --- 次アクション候補の走査 ----------------------------------------------------------


def test_scan_next_actions_collects_low_confidence_zoom_and_hypothesis(tmp_path):
    ranges_dir = tmp_path / "ranges"
    ranges_dir.mkdir(parents=True)
    (ranges_dir / "cand_a_analysis.json").write_text(
        json.dumps(
            {
                "events": [
                    {"title": "x", "confidence": "low", "zoom_targets": [2.4, 3.0]},
                    {"title": "y", "confidence": "high"},
                ],
                "hypothesis_verdict": "rejected",
            }
        ),
        encoding="utf-8",
    )
    (ranges_dir / "cand_b_analysis.json").write_text(
        json.dumps({"events": [{"title": "z", "confidence": "medium"}], "hypothesis_verdict": "confirmed"}),
        encoding="utf-8",
    )
    (ranges_dir / "cand_a_validated.json").write_text(
        json.dumps({"events": [{"flags": ["negative_match", "boundary"]}, {"flags": []}]}),
        encoding="utf-8",
    )
    (ranges_dir / "batch_summary.json").write_text(
        json.dumps({"results": [{"label": "gap_00", "error": "ffmpeg boom"}, {"label": "cand_a"}]}),
        encoding="utf-8",
    )

    next_actions = scan_next_actions(tmp_path)

    assert next_actions["low_confidence_count"] == 1
    assert len(next_actions["zoom_targets"]) == 2
    assert {item["timestamp_sec"] for item in next_actions["zoom_targets"]} == {2.4, 3.0}
    assert next_actions["hypothesis_rejected"] == [str(ranges_dir / "cand_a_analysis.json")]
    assert next_actions["validated_flags_count"] == 2
    assert len(next_actions["failed_ranges"]) == 1
    assert next_actions["failed_ranges"][0]["label"] == "gap_00"


def test_scan_next_actions_reads_analysis_summary_errors(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "analysis_summary.json").write_text(
        json.dumps({"results": [{"label": "cand_a", "status": "error", "error": "api boom"}]}),
        encoding="utf-8",
    )
    next_actions = scan_next_actions(tmp_path)
    assert next_actions["failed_ranges"] == [
        {"file": str(out_dir / "analysis_summary.json"), "label": "cand_a", "error": "api boom"}
    ]


def test_scan_next_actions_empty_session(tmp_path):
    next_actions = scan_next_actions(tmp_path)
    assert next_actions == {
        "low_confidence_count": 0,
        "zoom_targets": [],
        "hypothesis_rejected": [],
        "failed_ranges": [],
        "validated_flags_count": 0,
    }


# --- --estimate --------------------------------------------------------------------


def _write_ranges_json(session_dir: Path) -> None:
    ranges_dir = session_dir / "ranges"
    ranges_dir.mkdir(parents=True)
    (ranges_dir / "ranges.json").write_text(
        json.dumps(
            {
                "defaults": {"fps": 5, "frames_per_tile": 12, "pad": 1.0},
                "ranges": [
                    {"label": "cand_00", "start": 0.0, "end": 8.0},  # +pad 1*2=2 -> span10, fps5 -> 50/12
                    {"label": "gap_00", "start": 8.0, "end": 12.0, "fps": 1.0, "pad": 0.0},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_estimate_cost_returns_none_without_ranges_json(tmp_path):
    assert estimate_cost(tmp_path, []) is None


def test_estimate_cost_uses_measured_average_when_usage_available(tmp_path):
    _write_ranges_json(tmp_path)
    records = [
        {"model": "google/gemini-3.7-flash", "media": ["a.jpg", "b.jpg"], "usage": {"total_tokens": 2000}},
    ]
    estimate = estimate_cost(tmp_path, records)

    assert estimate["n_ranges"] == 2
    # cand_00: (8+2*1)*5/12 = 4.1666...
    # gap_00: (4+0)*1/12 = 0.3333... だが1範囲あたり最低1タイルとして切り上げる
    assert estimate["n_tiles_estimate"] == pytest.approx(4.1666666667 + 1.0, rel=1e-3)
    assert estimate["tokens_are_measured"] is True
    assert estimate["avg_tokens_per_tile"] == pytest.approx(1000.0, rel=1e-3)  # 2000 tokens / 2 tiles
    assert estimate["estimated_usd"] > 0


def test_estimate_cost_falls_back_to_default_tokens_when_no_usage(tmp_path):
    _write_ranges_json(tmp_path)
    estimate = estimate_cost(tmp_path, [])
    assert estimate["tokens_are_measured"] is False
    assert estimate["avg_tokens_per_tile"] == 1000.0


# --- build_report --------------------------------------------------------------------


def test_build_report_without_estimate(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    report = build_report(session_dir, do_estimate=False)
    assert report["session"] == str(session_dir.resolve())
    assert report["usage"]["calls"] == 0
    assert report["estimate"] is None


def test_build_report_with_estimate(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _write_ranges_json(session_dir)
    report = build_report(session_dir, do_estimate=True)
    assert report["estimate"] is not None
    assert report["estimate"]["n_ranges"] == 2


def test_build_report_raises_for_missing_session_dir(tmp_path):
    with pytest.raises(RuntimeError):
        build_report(tmp_path / "missing", do_estimate=False)
