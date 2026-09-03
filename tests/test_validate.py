"""avs.validate のテスト（動画・API 不要）。

各フラグの発火条件と非発火、manifest が無いときのスキップ、confidence_adjusted、
そして CLI 相当の `run_validation`（`<name>_validated.json` と validation_report.json）を見る。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.validate import (
    FLAG_BOUNDARY,
    FLAG_DURATION_OUTLIER,
    FLAG_EVIDENCE_OUT_OF_RANGE,
    FLAG_HYPOTHESIS_REJECTED,
    FLAG_LOW_CONFIDENCE,
    FLAG_NEGATIVE_MATCH,
    FLAG_NO_CELL_EVIDENCE,
    ValidateOptions,
    build_report,
    collect_targets,
    downgrade_confidence,
    has_cell_reference,
    parse_evidence_times,
    run_validation,
    validate_analysis,
    validate_event,
    validated_path_for,
)

# manifest の範囲は [9.0, 21.0]（要求 10-20 に pad 1.0）、fps 5 -> 境界許容 0.2 秒
MANIFEST = {
    "approach": "agentic_video_frame_tiles",
    "extraction": {
        "requested_start_sec": 10.0,
        "requested_end_sec": 20.0,
        "start_sec": 9.0,
        "end_sec": 21.0,
        "pad_sec": 1.0,
        "fps": 5.0,
    },
}

ZOOM_MANIFEST = {
    "approach": "agentic_video_frame_zoom",
    "extraction": {"start_sec": 9.0, "end_sec": 21.0, "pad_sec": 0.0, "fps": None},
}

DOMAIN = {
    "name": "テストドメイン",
    "negatives": [
        {"name": "誤認されやすい事象A", "pattern": "特殊な合図", "window": [30.0, 40.0]},
        {"name": "誤認されやすい事象B", "pattern": "存在しない表示"},
    ],
}


def make_event(**overrides):
    event = {
        "start_sec": 12.0,
        "end_sec": 14.0,
        "title": "表示の変化",
        "summary": "画面右下の数値が増えた",
        "visual": ["F5 t=12.0s: 数値が 3 から 4 に変わる"],
        "confidence": "high",
    }
    event.update(overrides)
    return event


# --- 小さな部品 ---------------------------------------------------------------


def test_parse_evidence_times_supports_both_forms():
    assert parse_evidence_times(["F5 t=12.0s: なにか"]) == [12.0]
    # m:ss.s 形式（長尺のラベル）も読む
    assert parse_evidence_times(["F5 t=1:02.5 の画面"]) == [62.5]
    assert parse_evidence_times(["F5 t=20:34.5s"]) == [1234.5]
    assert parse_evidence_times(["根拠なし", None]) == []


def test_has_cell_reference():
    assert has_cell_reference(["F12 t=39.4s: 表示"]) is True
    assert has_cell_reference(["t=39.4s の画面"]) is True
    assert has_cell_reference(["F3 の画面"]) is True
    assert has_cell_reference(["画面に変化があった"]) is False
    assert has_cell_reference([]) is False


def test_downgrade_confidence():
    assert downgrade_confidence("high") == "medium"
    assert downgrade_confidence("medium") == "low"
    assert downgrade_confidence("low") == "low"


def test_validated_path_for(tmp_path):
    assert validated_path_for(tmp_path / "cand_a_analysis.json") == tmp_path / "cand_a_validated.json"
    assert validated_path_for(tmp_path / "other.json") == tmp_path / "other_validated.json"
    out = tmp_path / "sub"
    assert validated_path_for(tmp_path / "cand_a_analysis.json", out) == out / "cand_a_validated.json"


# --- フラグの発火・非発火 -------------------------------------------------------


def test_clean_event_has_no_flags():
    checked = validate_event(make_event(), MANIFEST, DOMAIN)
    assert checked["flags"] == []
    assert checked["confidence_adjusted"] == "high"


def test_no_cell_evidence_downgrades_one_step():
    checked = validate_event(make_event(visual=["画面右下の数値が増えた"]), MANIFEST, DOMAIN)
    assert checked["flags"] == [FLAG_NO_CELL_EVIDENCE]
    assert checked["confidence_adjusted"] == "medium"


def test_no_cell_evidence_not_applied_without_tile_manifest():
    # zoom manifest / manifest 無しではセルラベル系ルールを適用しない
    event = make_event(visual=["画面右下の数値が増えた"])
    assert FLAG_NO_CELL_EVIDENCE not in validate_event(event, ZOOM_MANIFEST)["flags"]
    assert validate_event(event, None)["flags"] == []


def test_evidence_out_of_range_forces_low():
    checked = validate_event(
        make_event(visual=["F5 t=12.0s: 変化", "F9 t=99.0s: 別の場面"]),
        MANIFEST,
        DOMAIN,
    )
    assert FLAG_EVIDENCE_OUT_OF_RANGE in checked["flags"]
    assert checked["evidence_times_out_of_range"] == [99.0]
    assert checked["confidence_adjusted"] == "low"


def test_evidence_inside_pad_is_not_out_of_range():
    # pad 込みの [9.0, 21.0] の内側なら発火しない
    checked = validate_event(make_event(visual=["F1 t=9.2s: 変化"]), MANIFEST)
    assert FLAG_EVIDENCE_OUT_OF_RANGE not in checked["flags"]


def test_boundary_fires_within_one_frame_interval():
    # fps 5 -> 1/5 = 0.2 秒以内なら境界扱い
    checked = validate_event(make_event(start_sec=9.1, end_sec=12.0), MANIFEST)
    assert FLAG_BOUNDARY in checked["flags"]
    checked_end = validate_event(make_event(start_sec=19.0, end_sec=20.9), MANIFEST)
    assert FLAG_BOUNDARY in checked_end["flags"]
    # 十分内側なら発火しない
    assert FLAG_BOUNDARY not in validate_event(make_event(start_sec=12.0, end_sec=14.0), MANIFEST)["flags"]


def test_duration_outlier():
    reversed_event = validate_event(make_event(start_sec=14.0, end_sec=12.0), None)
    assert FLAG_DURATION_OUTLIER in reversed_event["flags"]
    long_event = validate_event(make_event(start_sec=0.0, end_sec=40.0), None)
    assert FLAG_DURATION_OUTLIER in long_event["flags"]
    # しきい値を上げれば発火しない
    assert FLAG_DURATION_OUTLIER not in validate_event(
        make_event(start_sec=0.0, end_sec=40.0), None, None, max_event_sec=60.0
    )["flags"]


def test_low_confidence_flag_keeps_confidence():
    checked = validate_event(make_event(confidence="low"), MANIFEST)
    assert checked["flags"] == [FLAG_LOW_CONFIDENCE]
    assert checked["confidence_adjusted"] == "low"


def test_negative_match_respects_window():
    inside = validate_event(
        make_event(start_sec=32.0, end_sec=35.0, summary="特殊な合図が出た"), None, DOMAIN
    )
    assert FLAG_NEGATIVE_MATCH in inside["flags"]
    assert inside["negative_matches"] == ["誤認されやすい事象A"]
    assert inside["confidence_adjusted"] == "low"

    outside = validate_event(
        make_event(start_sec=12.0, end_sec=14.0, summary="特殊な合図が出た"), None, DOMAIN
    )
    assert FLAG_NEGATIVE_MATCH not in outside["flags"]


def test_negative_match_without_window_matches_visual():
    checked = validate_event(
        make_event(visual=["F5 t=12.0s: 存在しない表示が見える"]), MANIFEST, DOMAIN
    )
    assert FLAG_NEGATIVE_MATCH in checked["flags"]


def test_manifest_missing_skips_manifest_rules():
    event = make_event(visual=["根拠テキスト t=99.0s"], start_sec=9.0, end_sec=12.0)
    flags = validate_event(event, None)["flags"]
    assert FLAG_NO_CELL_EVIDENCE not in flags
    assert FLAG_EVIDENCE_OUT_OF_RANGE not in flags
    assert FLAG_BOUNDARY not in flags


def test_validate_analysis_keeps_events_and_marks_hypothesis():
    analysis = {
        "events": [make_event(), make_event(confidence="low")],
        "hypothesis_verdict": "rejected",
        "notes": "メモ",
    }
    validated = validate_analysis(analysis, MANIFEST, DOMAIN)
    # 削除しない・元のキーを残す
    assert len(validated["events"]) == 2
    assert validated["notes"] == "メモ"
    assert validated["flags"] == [FLAG_HYPOTHESIS_REJECTED]
    # 入力は書き換えない
    assert "flags" not in analysis["events"][0]


# --- CLI 相当 -----------------------------------------------------------------


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_run_validation_writes_validated_and_report(tmp_path, capsys):
    ranges_dir = tmp_path / "ranges"
    write_json(ranges_dir / "cand_a" / "manifest.json", MANIFEST)
    write_json(
        ranges_dir / "cand_a_analysis.json",
        {"events": [make_event(), make_event(visual=["根拠なし"])], "hypothesis_verdict": "confirmed"},
    )
    domain_path = write_json(tmp_path / "domain.json", DOMAIN)
    report_path = tmp_path / "merge" / "validation_report.json"

    options = ValidateOptions(
        analysis=[str(ranges_dir / "cand_a_analysis.json")],
        domain=str(domain_path),
        report=True,
        report_output=str(report_path),
    )
    assert run_validation(options) == 0

    validated = json.loads((ranges_dir / "cand_a_validated.json").read_text(encoding="utf-8"))
    assert len(validated["events"]) == 2
    assert validated["events"][0]["flags"] == []
    # manifest は同名ディレクトリから自動で見つかるのでセルラベル系ルールが効く
    assert validated["events"][1]["flags"] == [FLAG_NO_CELL_EVIDENCE]

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["n_events"] == 2
    assert report["flag_counts"][FLAG_NO_CELL_EVIDENCE] == 1
    entry = report["flags"][FLAG_NO_CELL_EVIDENCE][0]
    assert entry["event_index"] == 1
    assert entry["title"] == "表示の変化"
    assert entry["start_sec"] == 12.0
    assert "[完了]" in capsys.readouterr().out


def test_run_validation_from_summary(tmp_path):
    ranges_dir = tmp_path / "ranges"
    manifest_path = write_json(ranges_dir / "cand_a" / "manifest.json", MANIFEST)
    write_json(ranges_dir / "cand_a_analysis.json", {"events": [make_event()]})
    summary_path = write_json(
        ranges_dir / "batch_summary.json",
        {"results": [{"label": "cand_a", "manifest_path": str(manifest_path)}]},
    )

    options = ValidateOptions(summary=str(summary_path))
    targets = collect_targets(options)
    assert [path.name for path, _ in targets] == ["cand_a_analysis.json"]
    assert targets[0][1] == manifest_path

    assert run_validation(options) == 0
    assert (ranges_dir / "cand_a_validated.json").exists()


def test_run_validation_requires_targets():
    with pytest.raises(RuntimeError):
        run_validation(ValidateOptions())


def test_build_report_counts_top_level_flags():
    results = [
        {
            "analysis": "a_analysis.json",
            "validated": {
                "events": [{"flags": [FLAG_LOW_CONFIDENCE], "title": "t", "start_sec": 1.0}],
                "flags": [FLAG_HYPOTHESIS_REJECTED],
            },
        }
    ]
    report = build_report(results)
    assert report["flag_counts"][FLAG_LOW_CONFIDENCE] == 1
    assert report["flag_counts"][FLAG_HYPOTHESIS_REJECTED] == 1
    assert report["flags"][FLAG_HYPOTHESIS_REJECTED][0]["event_index"] is None
