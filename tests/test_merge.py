"""avs.merge のテスト（動画・API 不要。LLM 統合はフェイクバックエンドで通す）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import avs.merge as merge_module
from avs.merge import (
    MergeOptions,
    attach_audio,
    collect_events,
    find_analysis_files,
    is_duplicate,
    label_from_path,
    merge_events,
    merge_diff,
    overlap_ratio,
    render_final_md,
    run_merge,
    union_timelines,
)


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def event(start, end, title, **overrides):
    base = {
        "start_sec": start,
        "end_sec": end,
        "title": title,
        "summary": f"{title}の説明",
        "visual": [f"F1 t={start}s: {title}"],
        "confidence": "high",
    }
    base.update(overrides)
    return base


# --- 重複判定と機械統合 ---------------------------------------------------------


def test_overlap_ratio_uses_shorter_span():
    a = {"start_sec": 9.0, "end_sec": 11.0}
    b = {"start_sec": 10.0, "end_sec": 20.0}
    # 重なり 1.0 秒 / 短い方 2.0 秒 = 0.5
    assert overlap_ratio(a, b) == pytest.approx(0.5)
    assert overlap_ratio(a, {"start_sec": 30.0, "end_sec": 31.0}) == 0.0


def test_is_duplicate_needs_both_conditions():
    a = event(9.0, 11.0, "数値の増加")
    b = event(10.0, 12.0, "数値の増加")
    assert is_duplicate(a, b) is True
    # タイトルが違えば別項目
    assert is_duplicate(a, event(10.0, 12.0, "画面全体の暗転")) is False
    # 時間が離れていれば別項目
    assert is_duplicate(a, event(30.0, 32.0, "数値の増加")) is False


def test_merge_events_merges_across_range_boundary():
    events = [
        merge_module.normalize_event(
            event(9.0, 11.0, "数値の増加", confidence="high", visual=["F1 t=9.0s: 増加"]),
            source="r1",
        ),
        merge_module.normalize_event(
            event(
                10.0,
                12.0,
                "数値の増加",
                confidence="low",
                summary="範囲をまたいで続いた数値の増加",
                visual=["F1 t=10.0s: 増加"],
                flags=["boundary"],
            ),
            source="r2",
        ),
    ]
    merged = merge_events(events)
    assert len(merged) == 1
    item = merged[0]
    # 秒数は包含、confidence は低い方
    assert (item["start_sec"], item["end_sec"]) == (9.0, 12.0)
    assert item["confidence"] == "low"
    assert item["sources"] == ["r1", "r2"]
    assert item["flags"] == ["boundary"]
    assert item["evidence"] == ["F1 t=9.0s: 増加", "F1 t=10.0s: 増加"]
    # summary は長い方
    assert item["summary"] == "範囲をまたいで続いた数値の増加"


def test_merge_events_keeps_distinct_items_sorted():
    events = [
        merge_module.normalize_event(event(20.0, 21.0, "画面の暗転"), source="r2"),
        merge_module.normalize_event(event(9.0, 11.0, "数値の増加"), source="r1"),
        merge_module.normalize_event(event(10.0, 12.0, "別の表示の点滅"), source="r1"),
    ]
    merged = merge_events(events)
    assert [item["title"] for item in merged] == ["数値の増加", "別の表示の点滅", "画面の暗転"]


# --- 収集 ----------------------------------------------------------------------


def test_label_from_path(tmp_path):
    assert label_from_path(tmp_path / "cand_a_validated.json") == "cand_a"
    assert label_from_path(tmp_path / "cand_a_analysis.json") == "cand_a"


def make_session(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    write_json(session / "session.json", {"video": "video.mp4", "steps": []})
    write_json(session / "ranges" / "r1_analysis.json", {"events": [event(9.0, 11.0, "数値の増加")]})
    write_json(
        session / "ranges" / "r1_validated.json",
        {
            "events": [dict(event(9.0, 11.0, "数値の増加"), flags=["boundary"], confidence_adjusted="medium")],
            "flags": ["hypothesis_rejected"],
        },
    )
    write_json(
        session / "ranges" / "r2_analysis.json",
        {"events": [event(10.0, 12.0, "数値の増加"), event(20.0, 21.0, "画面の暗転")]},
    )
    write_json(session / "overview" / "overview_analysis.json", {"events": [event(0.0, 30.0, "全体")]})
    return session


def test_find_analysis_files_prefers_validated(tmp_path):
    session = make_session(tmp_path)
    files = find_analysis_files(session)
    assert [path.name for path in files] == ["r1_validated.json", "r2_analysis.json"]


def test_find_analysis_files_excludes_non_detail_dirs(tmp_path):
    session = tmp_path / "session2"
    write_json(session / "session.json", {"video": "video.mp4"})
    write_json(session / "cand_a_analysis.json", {"events": []})
    write_json(session / "zooms" / "zoom_a_analysis.json", {"events": []})
    write_json(session / "overview" / "overview_analysis.json", {"events": []})
    write_json(session / "audio" / "audio_analysis.json", {"segments": []})
    files = find_analysis_files(session)
    assert [path.name for path in files] == ["cand_a_analysis.json"]


def test_collect_events_attaches_sources_and_hypothesis(tmp_path):
    session = make_session(tmp_path)
    collected = collect_events(session=session)
    assert len(collected.events) == 3
    assert collected.events[0]["sources"] == ["r1"]
    # validated の confidence_adjusted が confidence として使われる
    assert collected.events[0]["confidence"] == "medium"
    assert collected.hypothesis_rejected == ["r1"]


# --- 和集合 --------------------------------------------------------------------


def make_timeline_file(root: Path, run: str, items) -> Path:
    return write_json(root / run / "merge" / "timeline.json", {"overview": f"{run}の概要", "timeline": items})


def test_union_timelines_records_runs(tmp_path):
    a = make_timeline_file(tmp_path, "run1", [event(9.0, 11.0, "数値の増加")])
    b = make_timeline_file(
        tmp_path, "run2", [event(10.0, 12.0, "数値の増加"), event(20.0, 21.0, "画面の暗転")]
    )
    doc = union_timelines([a, b])
    assert doc["runs"] == ["run1", "run2"]
    timeline = doc["timeline"]
    assert len(timeline) == 2
    assert timeline[0]["runs"] == ["run1", "run2"]
    assert timeline[1]["runs"] == ["run2"]


def test_union_requires_two_inputs(tmp_path):
    a = make_timeline_file(tmp_path, "run1", [])
    with pytest.raises(RuntimeError):
        union_timelines([a])


# --- 音声 ----------------------------------------------------------------------


def test_attach_audio_flags_unconfirmed():
    timeline = [merge_module.normalize_event(event(10.0, 12.0, "数値の増加"), source="r1")]
    audio = {
        "segments": [
            {"start_sec": 10.5, "end_sec": 11.0, "kind": "sfx", "description": "短い効果音"},
            {"start_sec": 30.0, "end_sec": 31.0, "kind": "speech", "description": "話し声"},
        ]
    }
    combined = attach_audio(timeline, audio)
    assert [item.get("source") for item in combined] == [None, "audio", "audio"]
    overlapping = combined[1]
    unconfirmed = combined[2]
    assert overlapping["flags"] == []
    assert unconfirmed["flags"] == ["audio_unconfirmed"]
    assert unconfirmed["importance"] == "low"
    assert unconfirmed["sources"] == ["audio"]


# --- final.md -------------------------------------------------------------------


def test_render_final_md_sections():
    doc = {
        "overview": "全体の概要",
        "hypothesis_rejected": ["r3"],
        "timeline": [
            {
                "start_sec": 9.0,
                "end_sec": 12.0,
                "title": "数値の増加",
                "summary": "数値が増えた",
                "importance": "high",
                "confidence": "medium",
                "evidence": ["F1 t=9.0s: 増加"],
                "sources": ["r1", "r2"],
                "flags": ["boundary"],
            },
            {
                "start_sec": 20.0,
                "end_sec": 21.0,
                "title": "変化の乏しい区間",
                "summary": "特に変化なし",
                "importance": "low",
                "confidence": "high",
                "evidence": [],
                "sources": ["r2"],
                "flags": [],
            },
        ],
    }
    text = render_final_md(doc)
    for heading in ("## 概要", "## 見どころ候補", "## 追加確認した範囲", "## タイムライン要約", "## 注意点"):
        assert heading in text
    # 見どころ候補は importance != low だけ
    assert "### 1. 数値の増加 (9.0s - 12.0s)" in text
    assert "### 2." not in text
    # タイムライン要約には全項目
    assert "- 20.0s-21.0s [low] 変化の乏しい区間" in text
    assert "F1 t=9.0s: 増加" in text
    assert "r1, r2" in text
    assert "boundary" in text
    assert "r3" in text


# --- run_merge（--no-llm） -------------------------------------------------------


def test_run_merge_without_llm_writes_outputs(tmp_path, capsys):
    session = make_session(tmp_path)
    options = MergeOptions(session=str(session), no_llm=True, final_md=True)
    assert run_merge(options) == 0

    mechanical = json.loads((session / "merge" / "timeline_mechanical.json").read_text(encoding="utf-8"))
    timeline_doc = json.loads((session / "merge" / "timeline.json").read_text(encoding="utf-8"))
    assert len(mechanical["timeline"]) == 2
    assert timeline_doc["timeline"] == mechanical["timeline"]
    assert timeline_doc["hypothesis_rejected"] == ["r1"]
    # LLM を呼ばないので merge_diff は書かれない
    assert not (session / "merge" / "validation_report.json").exists()

    final_md = (session / "final.md").read_text(encoding="utf-8")
    assert "## 見どころ候補" in final_md
    assert "[完了]" in capsys.readouterr().out


def test_run_merge_with_audio(tmp_path):
    session = make_session(tmp_path)
    audio_path = write_json(
        session / "audio" / "audio_analysis.json",
        {"segments": [{"start_sec": 25.0, "end_sec": 26.0, "kind": "bgm", "description": "音楽"}]},
    )
    options = MergeOptions(session=str(session), no_llm=True, audio=str(audio_path))
    assert run_merge(options) == 0
    timeline = json.loads((session / "merge" / "timeline.json").read_text(encoding="utf-8"))["timeline"]
    audio_items = [item for item in timeline if item.get("source") == "audio"]
    assert len(audio_items) == 1
    assert audio_items[0]["flags"] == ["audio_unconfirmed"]


# --- LLM 統合（フェイクバックエンド） --------------------------------------------


class FakeResponse:
    def __init__(self, text: str, model: str = "fake-model"):
        self.text = text
        self.usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "reasoning_tokens": 0,
            "total_tokens": 150,
            "cost_usd": 0.001,
            "raw": {},
        }
        self.latency_sec = 0.5
        self.model = model
        self.backend = "fake"
        self.retries = 0


class FakeBackend:
    name = "fake"
    default_model = "fake-model"
    supports_video_clip = False
    supports_audio = False

    def __init__(self, payloads: list[str]):
        self.payloads = list(payloads)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        payload = self.payloads.pop(0) if self.payloads else self.payloads
        return FakeResponse(payload)


def fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def install_fake_backend(monkeypatch, payloads) -> FakeBackend:
    backend = FakeBackend(payloads)
    monkeypatch.setattr(merge_module, "get_backend", lambda *args, **kw: backend)
    return backend


def test_run_merge_with_llm_records_diff_and_usage(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    # LLM は 1 件だけ返す（もう 1 件は落ちたものとして merge_diff に載る）
    payload = {
        "overview": "統合後の概要",
        "timeline": [
            {
                "start_sec": 9.0,
                "end_sec": 12.5,
                "title": "数値の増加",
                "summary": "数値が増えた",
                "importance": "high",
                "confidence": "medium",
                "evidence": ["F1 t=9.0s: 増加"],
                "sources": ["r1", "r2"],
                "flags": ["boundary"],
            }
        ],
    }
    backend = install_fake_backend(monkeypatch, [fenced(payload)])

    options = MergeOptions(
        session=str(session), objective="テスト目的", final_md=True, llm_chunk=80
    )
    assert run_merge(options) == 0

    doc = json.loads((session / "merge" / "timeline.json").read_text(encoding="utf-8"))
    assert doc["overview"] == "統合後の概要"
    assert len(doc["timeline"]) == 1

    report = json.loads((session / "merge" / "validation_report.json").read_text(encoding="utf-8"))
    diff = report["merge_diff"]
    assert diff["n_before"] == 2
    assert diff["n_after"] == 1
    assert [item["title"] for item in diff["dropped"]] == ["画面の暗転"]
    assert diff["moved"][0]["after"] == [9.0, 12.5]

    usage_lines = (session / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(usage_lines) == 1
    record = json.loads(usage_lines[0])
    assert record["name"] == "merge"
    assert record["usage"]["total_tokens"] == 150

    prompt = backend.requests[0].prompt
    assert "テスト目的" in prompt
    assert "数値の増加" in prompt  # 機械統合の結果が context として入る
    assert (session / "final.md").exists()


def test_llm_merge_retries_once_on_bad_json(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    good = fenced({"overview": "概要", "timeline": []})
    backend = install_fake_backend(monkeypatch, ["JSONではない出力", good])

    options = MergeOptions(session=str(session))
    assert run_merge(options) == 0
    assert len(backend.requests) == 2
    # リトライ時はヒントが付く
    assert "コードフェンス付きJSON" in backend.requests[1].prompt
    usage_lines = (session / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(usage_lines) == 2


def test_llm_merge_splits_by_chunk(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    payload = fenced({"overview": "概要", "timeline": []})
    backend = install_fake_backend(monkeypatch, [payload, payload])

    options = MergeOptions(session=str(session), llm_chunk=1)
    assert run_merge(options) == 0
    # 機械統合の結果 2 件を 1 件ずつ 2 回に分けて送る
    assert len(backend.requests) == 2


def test_merge_diff_detects_added_and_moved():
    before = [merge_module.normalize_event(event(9.0, 11.0, "数値の増加"), source="r1")]
    after = [
        merge_module.normalize_event(event(9.0, 11.5, "数値の増加")),
        merge_module.normalize_event(event(30.0, 31.0, "入力に無かった項目")),
    ]
    diff = merge_diff(before, after)
    assert diff["dropped"] == []
    assert [item["title"] for item in diff["added"]] == ["入力に無かった項目"]
    assert diff["moved"][0]["before"] == [9.0, 11.0]
