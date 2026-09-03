"""avs.pipeline（標準ドライバ run_pipeline.py）のテスト。

実 API は呼ばない。バックエンドはプロンプト本文からステップを判別して
妥当な JSON を返すフェイクに差し替える。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from avs import pipeline as pipeline_module
from avs.backends.base import LLMResponse, MediaVideoClip
from avs.cost import make_usage
from avs.pipeline import (
    PipelineOptions,
    create_context,
    run_pipeline,
    step_overview,
    step_plan_ranges,
)
from avs.session import Session

# タイル経路 / ネイティブ経路それぞれのコンテキストから対象範囲を読む
_TILE_RANGE_RE = re.compile(r"範囲: ([0-9.]+)s - ([0-9.]+)s")
_CLIP_RANGE_RE = re.compile(r"動画全体の ([0-9.]+) 秒から ([0-9.]+) 秒")


def _prompt_range(prompt: str, fallback: tuple[float, float]) -> tuple[float, float]:
    for pattern in (_TILE_RANGE_RE, _CLIP_RANGE_RE):
        match = pattern.search(prompt)
        if match:
            return float(match.group(1)), float(match.group(2))
    return fallback


def _split(start: float, end: float, pieces: int) -> list[tuple[float, float]]:
    width = (end - start) / pieces
    return [(start + index * width, start + (index + 1) * width) for index in range(pieces)]


class FakeBackend:
    """`Backend` プロトコルを満たすフェイク。プロンプト本文でステップを見分ける。"""

    name = "fake"
    default_model = "fake-model"
    supports_audio = False

    def __init__(self, supports_video_clip: bool = False):
        self.supports_video_clip = supports_video_clip
        self.requests: list = []

    # --- ステップごとの応答 -------------------------------------------------

    def _chapters(self, start: float, end: float) -> dict:
        return {
            "chapters": [
                {
                    "label": f"chapter_{index:02d}",
                    "start_sec": round(piece_start, 2),
                    "end_sec": round(piece_end, 2),
                    "title": f"章{index}",
                    "summary": "章の要約",
                }
                for index, (piece_start, piece_end) in enumerate(_split(start, end, 3))
            ]
        }

    def _overview(self, start: float, end: float) -> dict:
        pieces = max(1, int((end - start) // 8) + (1 if (end - start) % 8 else 0))
        return {
            "summary": "全体の概要",
            "candidates": [
                {
                    "label": f"cand_{index:02d}",
                    "start_sec": round(piece_start, 2),
                    "end_sec": round(piece_end, 2),
                    "title": f"候補{index}",
                    "evidence": [f"F1 t={piece_start:.1f}s の表示"],
                    "priority": "high" if index == 0 else "low",
                    "needs_followup": True,
                    "reason": "画面に変化がありそう",
                }
                for index, (piece_start, piece_end) in enumerate(_split(start, end, pieces))
            ],
        }

    def _detail(self, start: float, end: float) -> dict:
        middle = start + (end - start) / 2
        return {
            "events": [
                {
                    "start_sec": round(start + 0.2, 2),
                    "end_sec": round(middle, 2),
                    "title": "前半の出来事",
                    "summary": "画面の表示が変化した",
                    "visual": [f"F1 t={start + 0.2:.1f}s: 表示が変化"],
                    "confidence": "high",
                    "zoom_targets": [round(start + 0.2, 2)],
                },
                {
                    "start_sec": round(middle, 2),
                    "end_sec": round(end - 0.2, 2),
                    "title": "後半の出来事",
                    "summary": "エフェクトが出た",
                    "visual": [f"F6 t={middle:.1f}s: エフェクト"],
                    "confidence": "low",
                },
            ],
            "hypothesis_verdict": "confirmed",
            "notes": "",
        }

    def _merge(self) -> dict:
        return {
            "overview": "統合した概要",
            "timeline": [
                {
                    "start_sec": 0.2,
                    "end_sec": 4.0,
                    "title": "前半の出来事",
                    "summary": "画面の表示が変化した",
                    "importance": "high",
                    "confidence": "high",
                    "evidence": ["F1 t=0.2s: 表示が変化"],
                    "sources": ["cand_00"],
                    "flags": [],
                }
            ],
        }

    def _payload(self, request) -> dict:
        prompt = request.prompt
        start, end = _prompt_range(prompt, (0.0, 30.0))
        if not request.media:  # 統合はメディアを伴わない唯一の呼び出し
            return self._merge()
        if "大きな区切り（章）" in prompt:
            return self._chapters(start, end)
        if "候補範囲" in prompt:
            return self._overview(start, end)
        if "出来事の単位" in prompt:
            return self._detail(start, end)
        raise AssertionError(f"未知のプロンプトです: {prompt[:80]}")

    def complete(self, request):
        self.requests.append(request)
        payload = json.dumps(self._payload(request), ensure_ascii=False)
        return LLMResponse(
            text=f"```json\n{payload}\n```",
            usage=make_usage(input_tokens=100, output_tokens=50, total_tokens=150, cost_usd=0.001),
            latency_sec=0.01,
            model="fake-model",
            backend="fake",
        )


@pytest.fixture
def fake_backend(monkeypatch):
    backend = FakeBackend()

    def factory(*_args, **_kw):
        return backend

    monkeypatch.setattr("avs.analysis.get_backend", factory)
    monkeypatch.setattr("avs.merge.get_backend", factory)
    monkeypatch.setattr("avs.pipeline.get_backend", factory)
    return backend


def make_options(video: Path, session: Path, **overrides) -> PipelineOptions:
    defaults = {
        "video": str(video),
        "session": str(session),
        "objective": "テスト目的",
        "jobs": 2,
    }
    defaults.update(overrides)
    return PipelineOptions(**defaults)


# --- tile 経路の通し -------------------------------------------------------------


def test_pipeline_tile_produces_all_artifacts(tmp_path, synth_video, fake_backend):
    session_dir = tmp_path / "session"
    assert run_pipeline(make_options(synth_video, session_dir)) == 0

    # WORKFLOW.md §6 の成果物
    assert (session_dir / "session.json").exists()
    assert (session_dir / "usage.jsonl").exists()
    assert (session_dir / "overview" / "overview_analysis.json").exists()
    assert (session_dir / "ranges" / "ranges.json").exists()
    assert (session_dir / "ranges" / "batch_summary.json").exists()
    assert list((session_dir / "ranges").glob("*_analysis.json"))
    assert list((session_dir / "ranges").glob("*_validated.json"))
    assert (session_dir / "merge" / "timeline_mechanical.json").exists()
    assert (session_dir / "merge" / "timeline.json").exists()
    assert (session_dir / "merge" / "validation_report.json").exists()
    assert (session_dir / "final.md").exists()

    # 全区間を隙間なく覆っている
    plan = json.loads((session_dir / "ranges" / "ranges.json").read_text(encoding="utf-8"))
    assert plan["plan"]["coverage"] == "full"
    assert plan["ranges"][0]["start"] == 0.0
    assert plan["ranges"][-1]["end"] == pytest.approx(plan["plan"]["duration_sec"])

    # session.json に各ステップが記録される
    steps = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["steps"]
    names = [step["name"] for step in steps]
    assert names == [
        "session",
        "overview",
        "plan_ranges",
        "tiling",
        "detail",
        "validate",
        "merge",
        "report",
    ]
    assert all("elapsed_sec" in step and "started_at" in step for step in steps)
    assert all(step["status"] in ("created", "ok") for step in steps)

    # usage.jsonl は 1 呼び出し 1 行
    lines = (session_dir / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(fake_backend.requests)


def test_pipeline_rerun_skips_all_steps_and_force_reruns(tmp_path, synth_video, fake_backend):
    session_dir = tmp_path / "session"
    assert run_pipeline(make_options(synth_video, session_dir)) == 0
    calls_after_first = len(fake_backend.requests)
    assert calls_after_first > 0

    # 再実行: 全ステップがスキップされ、API 呼び出しは増えない
    assert run_pipeline(make_options(synth_video, session_dir)) == 0
    assert len(fake_backend.requests) == calls_after_first

    steps = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["steps"]
    second_run = [step for step in steps if step["name"] != "session"][-7:]
    statuses = {step["name"]: step["status"] for step in second_run}
    assert statuses["overview"] == "skipped"
    assert statuses["plan_ranges"] == "skipped"
    assert statuses["tiling"] == "skipped"
    assert statuses["detail"] == "skipped"
    assert statuses["validate"] == "skipped"
    assert statuses["merge"] == "skipped"
    assert statuses["report"] == "ok"

    # --force なら再実行される
    assert run_pipeline(make_options(synth_video, session_dir, force=True)) == 0
    assert len(fake_backend.requests) > calls_after_first


def test_pipeline_records_failure_and_returns_1(tmp_path, synth_video, monkeypatch):
    session_dir = tmp_path / "session"

    def explode(*_args, **_kw):
        raise RuntimeError("バックエンドが落ちました")

    monkeypatch.setattr("avs.analysis.get_backend", explode)
    assert run_pipeline(make_options(synth_video, session_dir)) == 1

    steps = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["steps"]
    assert steps[-1]["name"] == "overview"
    assert steps[-1]["status"] == "error"
    assert "バックエンドが落ちました" in steps[-1]["error"]
    # 失敗したステップまでの成果物は残る（タイル化は済んでいる）
    assert list((session_dir / "overview").glob("**/manifest.json"))


# --- native 経路 -----------------------------------------------------------------


def test_pipeline_native_uses_video_clips(tmp_path, synth_video, monkeypatch):
    backend = FakeBackend(supports_video_clip=True)
    monkeypatch.setattr("avs.analysis.get_backend", lambda *a, **kw: backend)
    monkeypatch.setattr("avs.merge.get_backend", lambda *a, **kw: backend)
    monkeypatch.setattr("avs.pipeline.get_backend", lambda *a, **kw: backend)

    session_dir = tmp_path / "session"
    options = make_options(synth_video, session_dir, backend="gemini", input_mode="native")
    assert run_pipeline(options) == 0

    clips = [
        media
        for request in backend.requests
        for media in request.media
        if isinstance(media, MediaVideoClip)
    ]
    assert clips, "ネイティブ経路では MediaVideoClip で呼ばれるはず"
    # タイル画像は作られない
    assert not list((session_dir / "ranges").glob("**/*.jpg"))
    assert (session_dir / "overview" / "overview_analysis.json").exists()
    assert list((session_dir / "ranges").glob("*_validated.json"))
    assert (session_dir / "final.md").exists()


def test_native_with_openrouter_fails_at_startup(tmp_path, synth_video):
    options = make_options(
        synth_video, tmp_path / "session", backend="openrouter", input_mode="native"
    )
    with pytest.raises(RuntimeError, match="--backend gemini"):
        run_pipeline(options)


# --- 長尺分岐 ---------------------------------------------------------------------


def test_pipeline_long_form_uses_chapters_and_priority_coverage(
    tmp_path, synth_video, fake_backend, monkeypatch
):
    """動画長を 700 秒に見せかけ、章立て → 章ごと overview → priority coverage を確認する。"""
    monkeypatch.setattr(pipeline_module, "probe_duration_sec", lambda *_a, **_kw: 700.0)
    monkeypatch.setattr("avs.session.probe_duration_sec", lambda *_a, **_kw: 700.0)

    session_dir = tmp_path / "session"
    ctx = create_context(make_options(synth_video, session_dir))
    assert ctx.is_long_form

    overview_info = step_overview(ctx)
    assert overview_info["status"] == "ok"
    assert (session_dir / "overview" / "chapters_analysis.json").exists()

    config = json.loads(
        (session_dir / "overview" / "chapters_ranges.json").read_text(encoding="utf-8")
    )
    assert len(config["ranges"]) == 3
    for entry in config["ranges"]:
        assert (session_dir / "overview" / f"{entry['label']}_analysis.json").exists()

    overview = json.loads(
        (session_dir / "overview" / "overview_analysis.json").read_text(encoding="utf-8")
    )
    assert overview["_merged_from_chapters"] == 3
    assert overview["candidates"]
    assert all("chapter" in candidate for candidate in overview["candidates"])

    plan_info = step_plan_ranges(ctx)
    assert plan_info["coverage"] == "priority"
    plan = json.loads((session_dir / "ranges" / "ranges.json").read_text(encoding="utf-8"))
    assert plan["plan"]["duration_sec"] == 700.0
    # priority: low の範囲は --low-fps、high/medium は --detail-fps
    fps_by_priority = {entry["priority"]: entry["fps"] for entry in plan["ranges"]}
    assert fps_by_priority["low"] == ctx.options.low_fps
    assert fps_by_priority["high"] == ctx.options.detail_fps


# --- dry-run ---------------------------------------------------------------------


def test_pipeline_dry_run_does_not_call_api(tmp_path, synth_video, fake_backend, capsys):
    session_dir = tmp_path / "session"
    assert run_pipeline(make_options(synth_video, session_dir, dry_run=True)) == 0

    assert fake_backend.requests == []
    assert not (session_dir / "usage.jsonl").exists()
    assert not (session_dir / "merge" / "timeline.json").exists()
    # タイル化は行ってよい（dry-run でも実行予定のパスと件数が分かる）
    assert (session_dir / "ranges" / "ranges.json").exists()
    assert (session_dir / "ranges" / "batch_summary.json").exists()

    output = capsys.readouterr().out
    assert "[dry-run]" in output
    assert "[Step 7] レポート" in output
    assert "--dry-run を外して" in output


def test_dry_run_plan_is_regenerated_on_real_run(tmp_path, synth_video, fake_backend):
    """dry-run の仮計画（候補0件）を本番実行がそのまま使わないこと。"""
    session_dir = tmp_path / "session"
    assert run_pipeline(make_options(synth_video, session_dir, dry_run=True)) == 0
    provisional = json.loads((session_dir / "ranges" / "ranges.json").read_text(encoding="utf-8"))
    assert provisional["plan"]["dry_run"] is True
    assert all(entry["source"] == "gap" for entry in provisional["ranges"])

    assert run_pipeline(make_options(synth_video, session_dir)) == 0
    plan = json.loads((session_dir / "ranges" / "ranges.json").read_text(encoding="utf-8"))
    assert "dry_run" not in plan["plan"]
    assert any(entry["source"] == "overview" for entry in plan["ranges"])
    assert (session_dir / "final.md").exists()


# --- セッションとドメイン ------------------------------------------------------------


def test_pipeline_creates_timestamped_session_and_copies_domain(tmp_path, synth_video, fake_backend):
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(
        json.dumps({"name": "テスト", "watchlist": ["画面左下の数値表示"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    root = tmp_path / "sessions"
    options = PipelineOptions(
        video=str(synth_video), session_root=str(root), domain=str(domain_path)
    )
    ctx = create_context(options)

    assert ctx.root.parent == root.resolve()
    assert ctx.root.name.startswith(f"{synth_video.stem}_")
    assert ctx.domain_path == ctx.root / "notes" / "domain.json"
    assert json.loads(ctx.domain_path.read_text(encoding="utf-8"))["name"] == "テスト"

    data = Session(ctx.root).load()
    assert data["video"] == str(synth_video)
    assert data["domain"] == str(ctx.domain_path)
    assert data["duration_sec"] == pytest.approx(30.0, abs=1.0)


def test_pipeline_reuses_copied_domain_on_resume(tmp_path, synth_video, fake_backend):
    session_dir = tmp_path / "session"
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(json.dumps({"name": "テスト"}, ensure_ascii=False), encoding="utf-8")

    first = create_context(make_options(synth_video, session_dir, domain=str(domain_path)))
    assert first.domain_path is not None

    # --domain を省いて再開しても notes/domain.json が使われる
    second = create_context(make_options(synth_video, session_dir))
    assert second.domain_path == first.domain_path
    steps = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["steps"]
    assert steps[-1]["status"] == "resumed"
