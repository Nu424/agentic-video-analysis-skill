"""analyze_audio のテスト（動画・API 不要。バックエンドはフェイクに差し替える）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import analyze_audio
from analyze_audio import AudioOptions, run_audio_analysis
from avs.backends import MediaVideoClip

AUDIO_PAYLOAD = {
    "segments": [
        {"start_sec": 0.0, "end_sec": 3.0, "kind": "bgm", "description": "静かな音楽", "confidence": "medium"},
        {"start_sec": 5.0, "end_sec": 6.0, "kind": "sfx", "description": "短い効果音", "confidence": "low"},
    ]
}


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.usage = {
            "input_tokens": 2000,
            "output_tokens": 200,
            "reasoning_tokens": 0,
            "total_tokens": 2200,
            "cost_usd": 0.002,
            "raw": {},
        }
        self.latency_sec = 1.25
        self.model = "fake-model"
        self.backend = "gemini"
        self.retries = 0


class FakeBackend:
    name = "gemini"
    default_model = "fake-model"
    supports_video_clip = True
    supports_audio = True

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return FakeResponse(self.payloads.pop(0))


class NoAudioBackend(FakeBackend):
    name = "openrouter"
    supports_video_clip = False
    supports_audio = False


def fenced(payload: dict) -> str:
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


@pytest.fixture
def video(tmp_path) -> Path:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"not a real video")  # ffprobe は呼ばないので中身は問わない
    return path


def install(monkeypatch, backend):
    monkeypatch.setattr(analyze_audio, "get_backend", lambda *args, **kw: backend)
    return backend


def test_run_audio_analysis_writes_outputs(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    backend = install(monkeypatch, FakeBackend([fenced(AUDIO_PAYLOAD)]))

    options = AudioOptions(video=str(video), session=str(session), objective="音の把握")
    assert run_audio_analysis(options) == 0

    audio_dir = session / "audio"
    result = json.loads((audio_dir / "audio_analysis.json").read_text(encoding="utf-8"))
    assert len(result["segments"]) == 2
    assert (audio_dir / "audio_analysis.raw.txt").exists()
    assert (audio_dir / "audio_analysis.prompt.txt").exists()

    meta = json.loads((audio_dir / "audio_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "gemini"
    assert meta["usage"]["total_tokens"] == 2200
    assert meta["retries"] == 0

    record = json.loads((session / "usage.jsonl").read_text(encoding="utf-8").strip())
    assert record["name"] == "audio_analysis"
    assert record["cost_usd"] == 0.002

    # 全編を1本のクリップとして送る（範囲もサンプリングも指定しない）
    media = backend.requests[0].media
    assert len(media) == 1
    clip = media[0]
    assert isinstance(clip, MediaVideoClip)
    assert clip.start_sec == 0.0
    assert clip.end_sec is None
    assert clip.fps is None
    # プロンプトには目的とスキーマが埋まる
    prompt = backend.requests[0].prompt
    assert "音の把握" in prompt
    assert "segments" in prompt


def test_backend_without_audio_support_is_rejected(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    install(monkeypatch, NoAudioBackend([]))
    options = AudioOptions(video=str(video), session=str(session), backend="openrouter")
    with pytest.raises(RuntimeError, match="音声"):
        run_audio_analysis(options)


def test_existing_output_is_skipped_unless_forced(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    audio_dir = session / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_analysis.json").write_text('{"segments": []}', encoding="utf-8")
    backend = install(monkeypatch, FakeBackend([fenced(AUDIO_PAYLOAD)]))

    options = AudioOptions(video=str(video), session=str(session))
    assert run_audio_analysis(options) == 0
    assert backend.requests == []

    assert run_audio_analysis(AudioOptions(video=str(video), session=str(session), force=True)) == 0
    assert len(backend.requests) == 1


def test_retries_once_on_bad_json(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    backend = install(monkeypatch, FakeBackend(["JSONではない", fenced(AUDIO_PAYLOAD)]))

    assert run_audio_analysis(AudioOptions(video=str(video), session=str(session))) == 0
    assert len(backend.requests) == 2
    assert "コードフェンス付きJSON" in backend.requests[1].prompt
    meta = json.loads((session / "audio" / "audio_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["retries"] == 1
    # リトライ分の usage も合算する（合算しないと集計からトークンが抜ける）
    assert meta["usage"]["total_tokens"] == 4400
    record = json.loads((session / "usage.jsonl").read_text(encoding="utf-8").strip())
    assert record["usage"]["total_tokens"] == 4400


def test_missing_video_is_rejected(tmp_path, monkeypatch):
    install(monkeypatch, FakeBackend([]))
    options = AudioOptions(video=str(tmp_path / "none.mp4"), session=str(tmp_path))
    with pytest.raises(RuntimeError, match="動画ファイル"):
        run_audio_analysis(options)


def test_dry_run_does_not_call_backend(tmp_path, video, monkeypatch, capsys):
    session = tmp_path / "session"
    session.mkdir()
    backend = install(monkeypatch, FakeBackend([]))
    assert run_audio_analysis(AudioOptions(video=str(video), session=str(session), dry_run=True)) == 0
    assert backend.requests == []
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert not (session / "audio" / "audio_analysis.json").exists()


# --- step と --domain --------------------------------------------------------------


def test_usage_record_has_audio_step(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    install(monkeypatch, FakeBackend([fenced(AUDIO_PAYLOAD)]))
    assert run_audio_analysis(AudioOptions(video=str(video), session=str(session))) == 0

    record = json.loads((session / "usage.jsonl").read_text(encoding="utf-8").strip())
    assert record["name"] == "audio_analysis"
    assert record["step"] == "audio"
    meta = json.loads((session / "audio" / "audio_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["step"] == "audio"


def test_domain_is_injected_into_prompt(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(
        json.dumps(
            {
                "name": "テストドメイン",
                "vocabulary": {"用語A": "短い電子音が鳴ること"},
                "watchlist": ["合図の音"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",  # BOM 付きでも読める
    )
    backend = install(monkeypatch, FakeBackend([fenced(AUDIO_PAYLOAD)]))

    options = AudioOptions(video=str(video), session=str(session), domain=str(domain_path))
    assert run_audio_analysis(options) == 0

    prompt = backend.requests[0].prompt
    assert "テストドメイン" in prompt
    assert "短い電子音が鳴ること" in prompt
    assert "合図の音" in prompt


def test_prompt_file_with_bom_is_read(tmp_path, video, monkeypatch):
    session = tmp_path / "session"
    session.mkdir()
    prompt_path = tmp_path / "audio.txt"
    prompt_path.write_text("音声の本文プロンプト", encoding="utf-8-sig")
    backend = install(monkeypatch, FakeBackend([fenced(AUDIO_PAYLOAD)]))

    options = AudioOptions(video=str(video), session=str(session), prompt=str(prompt_path))
    assert run_audio_analysis(options) == 0
    sent = backend.requests[0].prompt
    assert sent.startswith("音声の本文プロンプト")
    assert "\ufeff" not in sent
