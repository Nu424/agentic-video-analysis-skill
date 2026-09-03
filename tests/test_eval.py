"""eval/score.py と eval/union_recall.py のテスト。

実 API は呼ばない。`eval/ground_truth.example.json` と、その場で作る合成 timeline
JSON だけで検証する。`eval/` は `avs` パッケージとは独立したディレクトリなので、
`tests/conftest.py`（`avs` 用に `scripts/` を sys.path へ入れる）は触らず、
このファイル内で `eval/` を sys.path に追加してから import する。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval"
GT_EXAMPLE_PATH = EVAL_DIR / "ground_truth.example.json"

if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import score  # noqa: E402
import union_recall  # noqa: E402


# --- フィクスチャ -----------------------------------------------------------------


@pytest.fixture()
def gt() -> dict[str, Any]:
    """`eval/ground_truth.example.json` をそのまま読み込んだもの。"""
    return score.load_ground_truth(GT_EXAMPLE_PATH)


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _timeline_entry(start: float, end: float, title: str, summary: str = "", evidence=None):
    return {
        "start_sec": start,
        "end_sec": end,
        "title": title,
        "summary": summary,
        "evidence": evidence or [],
        "importance": "medium",
    }


# 6つの正解 event (E01-E06) のうち E01,E03,E04 を検出し、E02,E05,E06 を見逃す
# タイムライン。誤認パターン（架空要素A）も1件混ぜてある。
PARTIAL_TIMELINE = {
    "overview": "test",
    "timeline": [
        _timeline_entry(0.5, 3.0, "読み込み画面", "起動画面が表示"),
        _timeline_entry(19.8, 20.2, "状態表示Aの数値が減少", "数値が下がる"),
        _timeline_entry(25.1, 25.3, "加速演出", "ブースト発動"),
        _timeline_entry(60.0, 61.0, "架空要素Aの出現", "架空要素Aが登場した"),
    ],
}

# E02, E05 を検出するタイムライン（PARTIAL_TIMELINE と組み合わせて和集合を測る用）
OTHER_TIMELINE = {
    "timeline": [
        _timeline_entry(4.5, 9.0, "場面紹介", "ステージ全体を俯瞰"),
        _timeline_entry(40.3, 41.0, "衝突", "障害物と接触して停止"),
    ],
}


# --- load_entries: 入力アダプタ 3 形状 -------------------------------------------


def test_load_entries_timeline_shape(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "timeline.json", PARTIAL_TIMELINE)
    entries = score.load_entries(p)
    assert len(entries) == 4
    start, end, text = entries[0]
    assert (start, end) == (0.5, 3.0)
    assert "読み込み画面" in text
    assert "起動画面が表示" in text


def test_load_entries_final_json_result_timeline_shape(tmp_path: Path) -> None:
    """移植元ツールの `final.json`（`result.timeline[]`）形状。"""
    data = {"result": {"timeline": PARTIAL_TIMELINE["timeline"]}, "totals": {"usd": 0.01}}
    p = _write_json(tmp_path / "final.json", data)
    entries = score.load_entries(p)
    assert len(entries) == 4
    assert entries[0][:2] == (0.5, 3.0)


def test_load_entries_events_shape(tmp_path: Path) -> None:
    """detail の `*_analysis.json` / `*_validated.json`（`events[]` + `visual[]`）形状。"""
    data = {
        "events": [
            {
                "start_sec": 25.1,
                "end_sec": 25.3,
                "title": "加速演出",
                "summary": "ブースト発動",
                "visual": ["F3 t=25.1s: 軌跡が伸びる"],
                "confidence": "high",
            }
        ],
        "hypothesis_verdict": "n/a",
    }
    p = _write_json(tmp_path / "cand_a_analysis.json", data)
    entries = score.load_entries(p)
    assert len(entries) == 1
    start, end, text = entries[0]
    assert (start, end) == (25.1, 25.3)
    assert "軌跡が伸びる" in text  # visual[] がテキストに連結される


def test_load_entries_unknown_shape_raises(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "bad.json", {"foo": "bar"})
    with pytest.raises(score.ScoreError):
        score.load_entries(p)


def test_load_entries_missing_start_end_raises(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "bad2.json", {"timeline": [{"title": "x"}]})
    with pytest.raises(score.ScoreError):
        score.load_entries(p)


def test_load_entries_not_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad3.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(score.ScoreError):
        score.load_entries(p)


# --- score_entries: 検出・見逃し・カテゴリ別 -------------------------------------


def test_score_entries_detect_and_miss(gt: dict[str, Any]) -> None:
    entries = [
        (float(e["start_sec"]), float(e["end_sec"]), score._entry_text(e))
        for e in PARTIAL_TIMELINE["timeline"]
    ]
    result = score.score_entries(entries, gt)
    assert set(result["detected"]) == {"E01", "E03", "E04"}
    missed_ids = {m["id"] for m in result["missed"]}
    assert missed_ids == {"E02", "E05", "E06"}
    assert result["n_detected"] == 3
    assert result["n_events"] == 6
    assert result["recall_pct"] == pytest.approx(50.0)


def test_score_entries_by_cat(gt: dict[str, Any]) -> None:
    entries = [
        (float(e["start_sec"]), float(e["end_sec"]), score._entry_text(e))
        for e in PARTIAL_TIMELINE["timeline"]
    ]
    result = score.score_entries(entries, gt)
    by_cat = result["by_cat"]
    # E01 (A) 検出, E02 (A) 見逃し -> A: 1/2
    assert by_cat["A"] == {"detected": 1, "total": 2}
    # E03 (B) 検出 -> B: 1/1
    assert by_cat["B"] == {"detected": 1, "total": 1}
    # E04 (C) 検出 -> C: 1/1
    assert by_cat["C"] == {"detected": 1, "total": 1}
    # E05, E06 (D) いずれも見逃し -> D: 0/2
    assert by_cat["D"] == {"detected": 0, "total": 2}


def test_score_entries_false_positive_counted(gt: dict[str, Any]) -> None:
    entries = [
        (float(e["start_sec"]), float(e["end_sec"]), score._entry_text(e))
        for e in PARTIAL_TIMELINE["timeline"]
    ]
    result = score.score_entries(entries, gt)
    # 「架空要素Aの出現」は negatives[0]（verdict=誤り）にマッチする
    assert result["n_false_positives"] == 1
    assert result["false_positives"][0]["name"] == "そのドメインで登場しがちだが映像には無い要素の出現"
    assert result["no_basis"] == []


def test_score_entries_no_basis_not_counted_as_false_positive(gt: dict[str, Any]) -> None:
    """verdict=根拠なし は誤認数(n_false_positives)に数えず、別集計(no_basis)に入る。"""
    entries = [(10.0, 10.5, "難易度が高い設定です")]
    result = score.score_entries(entries, gt)
    assert result["n_false_positives"] == 0
    assert result["false_positives"] == []
    assert len(result["no_basis"]) == 1
    assert result["no_basis"][0]["name"] == "難易度設定の値"


def test_score_entries_negative_window_limits_scan() -> None:
    """negatives[].window の外側にあるテキストはマッチしても対象外。"""
    gt_data = {
        "events": [
            {
                "id": "E01",
                "start": 0.0,
                "end": 1.0,
                "cat": "A",
                "name": "x",
                "evidence": "x",
                "match": {"window": [0.0, 1.0], "any": ["x"]},
            }
        ],
        "negatives": [
            {"name": "限定誤認", "pattern": "禁止語", "window": [30.0, 33.0], "verdict": "誤り"}
        ],
    }
    entries_outside = [(0.0, 1.0, "x"), (100.0, 100.5, "禁止語が出た")]
    result = score.score_entries(entries_outside, gt_data)
    assert result["n_false_positives"] == 0

    entries_inside = [(0.0, 1.0, "x"), (31.0, 31.5, "禁止語が出た")]
    result2 = score.score_entries(entries_inside, gt_data)
    assert result2["n_false_positives"] == 1


def test_score_file_multi_file(tmp_path: Path, gt: dict[str, Any]) -> None:
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    p2 = _write_json(tmp_path / "run2.json", OTHER_TIMELINE)
    r1 = score.score_file(p1, gt)
    r2 = score.score_file(p2, gt)
    assert r1["file"] == str(p1)
    assert set(r1["detected"]) == {"E01", "E03", "E04"}
    assert set(r2["detected"]) == {"E02", "E05"}


# --- ground truth の検証 ----------------------------------------------------------


def test_load_ground_truth_valid(gt: dict[str, Any]) -> None:
    assert len(gt["events"]) == 6
    assert len(gt["negatives"]) == 3
    assert gt["events"][0]["id"] == "E01"


def test_load_ground_truth_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(tmp_path / "nope.json")


def test_load_ground_truth_not_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


def test_load_ground_truth_top_level_not_object_raises(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "bad.json", [1, 2, 3])
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


def test_load_ground_truth_missing_events_raises(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "bad.json", {"negatives": []})
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


def test_load_ground_truth_event_missing_match_raises(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "bad.json",
        {
            "events": [{"id": "E01", "start": 0, "end": 1, "cat": "A", "name": "x"}],
            "negatives": [],
        },
    )
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


def test_load_ground_truth_event_bad_window_raises(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "bad.json",
        {
            "events": [
                {
                    "id": "E01",
                    "start": 0,
                    "end": 1,
                    "cat": "A",
                    "name": "x",
                    "match": {"window": [0.0], "any": ["x"]},
                }
            ],
            "negatives": [],
        },
    )
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


def test_load_ground_truth_negative_bad_verdict_raises(tmp_path: Path) -> None:
    p = _write_json(
        tmp_path / "bad.json",
        {
            "events": [
                {
                    "id": "E01",
                    "start": 0,
                    "end": 1,
                    "cat": "A",
                    "name": "x",
                    "match": {"window": [0.0, 1.0], "any": ["x"]},
                }
            ],
            "negatives": [{"name": "n", "pattern": "p", "verdict": "不明"}],
        },
    )
    with pytest.raises(score.GroundTruthError):
        score.load_ground_truth(p)


# --- 標本標準偏差 -----------------------------------------------------------------


def test_sample_stdev_matches_statistics_stdev() -> None:
    values = [50.0, 100.0, 75.0]
    assert score.sample_stdev(values) == pytest.approx(statistics.stdev(values))


def test_sample_stdev_single_value_is_zero() -> None:
    assert score.sample_stdev([42.0]) == 0.0


def test_sample_stdev_empty_is_zero() -> None:
    assert score.sample_stdev([]) == 0.0


# --- コスト集計 (usage.jsonl) ------------------------------------------------------


def test_load_usage_records_missing_returns_empty(tmp_path: Path) -> None:
    assert score.load_usage_records(tmp_path) == []


def test_load_usage_records_skips_broken_lines(tmp_path: Path) -> None:
    (tmp_path / "usage.jsonl").write_text(
        '{"name": "a"}\nnot json\n{"name": "b"}\n', encoding="utf-8"
    )
    records = score.load_usage_records(tmp_path)
    assert [r["name"] for r in records] == ["a", "b"]


def test_summarize_cost_splits_actual_and_estimated() -> None:
    records = [
        {
            "name": "overview_full_analysis",
            "usage": {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 0, "total_tokens": 1200},
            "cost_usd": 0.002,
            "cost_is_estimate": False,
            "latency_sec": 3.0,
        },
        {
            "name": "cand_a_detail_analysis",
            "usage": {"input_tokens": 2000, "output_tokens": 400, "reasoning_tokens": 50, "total_tokens": 2450},
            "cost_usd": 0.01,
            "cost_is_estimate": True,
            "latency_sec": 4.0,
        },
        {
            "name": "cand_b_detail_analysis",
            "usage": {"input_tokens": 500, "output_tokens": 50, "reasoning_tokens": 0, "total_tokens": 550},
            "cost_usd": None,
            "cost_is_estimate": False,
            "latency_sec": 1.0,
        },
    ]
    summary = score.summarize_cost(records)
    assert summary["calls"] == 3
    assert summary["tokens"] == {
        "input_tokens": 3500,
        "output_tokens": 650,
        "reasoning_tokens": 50,
        "total_tokens": 4200,
    }
    assert summary["cost_usd_actual"] == pytest.approx(0.002)
    assert summary["cost_usd_estimated"] == pytest.approx(0.01)
    assert summary["cost_usd_total"] == pytest.approx(0.012)
    assert summary["unknown_cost_calls"] == 1
    assert summary["cost_has_estimate"] is True
    assert summary["latency_sec"] == pytest.approx(8.0)
    assert set(summary["by_step"]) == {"overview", "detail"}
    assert summary["by_step"]["detail"]["calls"] == 2


def test_summarize_cost_empty_records() -> None:
    summary = score.summarize_cost([])
    assert summary["calls"] == 0
    assert summary["cost_has_estimate"] is False
    assert summary["unknown_cost_calls"] == 0


# --- CLI (main) --------------------------------------------------------------------


def test_score_main_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_json(tmp_path / "run.json", PARTIAL_TIMELINE)
    rc = score.main([str(p), "--gt", str(GT_EXAMPLE_PATH), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["n_runs"] == 1
    assert out["runs"][0]["n_detected"] == 3


def test_score_main_multi_file_reports_mean_and_sd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    p2 = _write_json(tmp_path / "run2.json", OTHER_TIMELINE)
    rc = score.main([str(p1), str(p2), "--gt", str(GT_EXAMPLE_PATH), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    recalls = [r["recall_pct"] for r in out["runs"]]
    assert out["summary"]["recall_pct_mean"] == pytest.approx(statistics.mean(recalls), abs=0.05)
    assert out["summary"]["recall_pct_sd"] == pytest.approx(statistics.stdev(recalls), abs=0.05)


def test_score_main_invalid_gt_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_gt = _write_json(tmp_path / "bad_gt.json", {"nope": True})
    p = _write_json(tmp_path / "run.json", PARTIAL_TIMELINE)
    rc = score.main([str(p), "--gt", str(bad_gt)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "エラー" in err


def test_score_main_with_session_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session_dir = tmp_path / "session1"
    session_dir.mkdir()
    (session_dir / "usage.jsonl").write_text(
        json.dumps(
            {
                "name": "overview_full_analysis",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "reasoning_tokens": 0,
                    "total_tokens": 120,
                },
                "cost_usd": 0.001,
                "cost_is_estimate": False,
                "latency_sec": 1.0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    p = _write_json(tmp_path / "run.json", PARTIAL_TIMELINE)
    rc = score.main([str(p), "--gt", str(GT_EXAMPLE_PATH), "--session", str(session_dir), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cost"]["calls"] == 1
    assert out["cost"]["cost_usd_actual"] == pytest.approx(0.001)


# --- union_recall.py ---------------------------------------------------------------


def test_detected_ids(gt: dict[str, Any]) -> None:
    entries = [
        (float(e["start_sec"]), float(e["end_sec"]), score._entry_text(e))
        for e in PARTIAL_TIMELINE["timeline"]
    ]
    ids = union_recall.detected_ids(entries, gt)
    assert ids == {"E01", "E03", "E04"}


def test_union_recall_compute_pair_union(tmp_path: Path, gt: dict[str, Any]) -> None:
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    p2 = _write_json(tmp_path / "run2.json", OTHER_TIMELINE)
    result = union_recall.compute([str(p1), str(p2)], gt)
    assert result["n_runs"] == 2
    assert result["n_events"] == 6
    # run1: E01,E03,E04 (50%), run2: E02,E05 (33.3%)
    solo_pcts = {row["file"]: row["recall_pct"] for row in result["solo"]}
    assert solo_pcts[str(p1)] == pytest.approx(50.0)
    assert solo_pcts[str(p2)] == pytest.approx(100 / 3, abs=0.1)
    # union of run1 + run2 = E01,E02,E03,E04,E05 = 5/6
    assert result["pair_union_recall_pct_mean"] == pytest.approx(500 / 6, abs=0.1)
    assert result["pair_union_recall_pct_min"] == result["pair_union_recall_pct_max"]
    # E06 は両方とも取れていない
    assert [ev["id"] for ev in result["never_detected"]] == ["E06"]


def test_union_recall_compute_single_run_has_no_pair_stats(
    tmp_path: Path, gt: dict[str, Any]
) -> None:
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    result = union_recall.compute([str(p1)], gt)
    assert result["n_runs"] == 1
    assert result["pair_union_recall_pct_mean"] is None
    assert result["pair_union_recall_pct_min"] is None
    assert result["pair_union_recall_pct_max"] is None


def test_union_recall_main_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    p2 = _write_json(tmp_path / "run2.json", OTHER_TIMELINE)
    rc = union_recall.main([str(p1), str(p2), "--gt", str(GT_EXAMPLE_PATH), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_runs"] == 2
    assert len(out["never_detected"]) == 1


def test_union_recall_main_invalid_gt_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_gt = _write_json(tmp_path / "bad_gt.json", {"nope": True})
    p1 = _write_json(tmp_path / "run1.json", PARTIAL_TIMELINE)
    rc = union_recall.main([str(p1), "--gt", str(bad_gt)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "エラー" in err
