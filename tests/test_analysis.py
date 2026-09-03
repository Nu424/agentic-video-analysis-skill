"""avs.analysis / avs.prompts のテスト。

実 API は呼ばない。バックエンドは `Backend` を満たすフェイクに差し替える。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avs.analysis import (
    AnalyzeOptions,
    ClipJob,
    analyze_clip,
    analyze_one,
    chunk_with_overlap,
    collect_clip_jobs,
    collect_manifests,
    derive_basename,
    extract_json,
    merge_part_results,
    resolve_output_base,
    run_analysis,
)
from avs.backends.base import LLMResponse, MediaImage, MediaVideoClip
from avs.cost import make_usage
from avs.session import Session
from avs.prompts import (
    OBJECTIVE_PLACEHOLDER,
    apply_objective,
    assemble_prompt,
    build_clip_context,
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


def test_build_clip_context_uses_absolute_seconds():
    context = build_clip_context(12.0, 20.0, 5)
    assert "12.0 秒から 20.0 秒" in context
    assert "絶対秒" in context
    assert "0 秒としない" in context
    assert "5 fps" in context
    # fps 未指定（動画全体を送る場合）は fps 行を出さない
    assert "fps" not in build_clip_context(0.0, 10.0, None)


# --- フェイクバックエンド -------------------------------------------------------

JSON_TEXT = '```json\n{"events": [{"title": "A"}]}\n```'


class FakeBackend:
    """`Backend` プロトコルを満たすテスト用バックエンド。"""

    name = "fake"
    default_model = "fake-model"
    supports_audio = False

    def __init__(self, texts: list[str] | None = None, supports_video_clip: bool = False):
        self.texts = list(texts) if texts else None
        self.supports_video_clip = supports_video_clip
        self.requests: list = []

    def complete(self, request):
        self.requests.append(request)
        text = self.texts.pop(0) if self.texts else JSON_TEXT
        return LLMResponse(
            text=text,
            usage=make_usage(
                input_tokens=1000,
                output_tokens=200,
                total_tokens=1200,
                cost_usd=0.002,
                raw={"prompt_tokens": 1000},
            ),
            latency_sec=1.5,
            model="fake-model",
            backend="fake",
            retries=1,
        )


def make_manifest(tmp_path: Path, tile_count: int = 2) -> Path:
    """タイル画像と manifest.json を作る（実画像である必要はない）。"""
    range_dir = tmp_path / "cand_a"
    range_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for index in range(tile_count):
        filename = f"tile_{index:03d}.jpg"
        (range_dir / filename).write_bytes(b"\xff\xd8\xffFAKE")
        tiles.append(
            {
                "tile_index": index,
                "filename": filename,
                "path": str(range_dir / filename),
                "start_timestamp_sec": float(index * 2),
                "end_timestamp_sec": float(index * 2 + 1.5),
                "start_frame": index * 4,
                "end_frame": index * 4 + 3,
            }
        )
    manifest_path = range_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"extraction": {"start_sec": 0.0, "end_sec": 4.0, "fps": 2}, "tiles": tiles}),
        encoding="utf-8",
    )
    return manifest_path


def default_options(**overrides) -> AnalyzeOptions:
    options = AnalyzeOptions(prompt="dummy.txt", jobs=1)
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


# --- analyze_one（タイル経路） --------------------------------------------------


def test_analyze_one_writes_analysis_meta_prompt_and_usage_jsonl(tmp_path):
    manifest_path = make_manifest(tmp_path)
    session = tmp_path / "session"
    base = tmp_path / "cand_a_analysis"
    backend = FakeBackend()
    options = default_options(session=str(session))

    report = analyze_one(
        manifest_path, options, "本文", "テスト目的", None, "仮説メモ", backend, base
    )

    assert report["parts"] == 1 and report["json_failures"] == 0
    # 画像は MediaImage として渡る
    request = backend.requests[0]
    assert [type(item) for item in request.media] == [MediaImage, MediaImage]
    assert "仮説メモ" in request.prompt

    assert json.loads((tmp_path / "cand_a_analysis.json").read_text(encoding="utf-8")) == {
        "events": [{"title": "A"}]
    }
    assert (tmp_path / "cand_a_analysis.raw.txt").read_text(encoding="utf-8") == JSON_TEXT
    assert (tmp_path / "cand_a_analysis.prompt.txt").read_text(encoding="utf-8") == request.prompt

    meta = json.loads((tmp_path / "cand_a_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "fake"
    assert meta["model"] == "fake-model"
    assert meta["media"] == ["tile_000.jpg", "tile_001.jpg"]
    assert meta["usage"]["input_tokens"] == 1000
    assert meta["cost_usd"] == 0.002
    assert meta["cost_is_estimate"] is False
    assert meta["latency_sec"] == 1.5
    assert meta["retries"] == 1  # バックエンドの通信リトライ
    assert meta["prompt_chars"] == len(request.prompt)
    assert meta["created_at"]

    lines = (session / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "cand_a_analysis"
    assert record["usage"] == meta["usage"]
    assert record["cost_usd"] == meta["cost_usd"]


def test_analyze_one_without_session_writes_no_usage_jsonl(tmp_path):
    manifest_path = make_manifest(tmp_path)
    analyze_one(
        manifest_path,
        default_options(),
        "本文",
        "目的",
        None,
        None,
        FakeBackend(),
        tmp_path / "cand_a_analysis",
    )
    assert not list(tmp_path.rglob("usage.jsonl"))


def test_analyze_one_retries_once_on_invalid_json(tmp_path):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    backend = FakeBackend(texts=["JSONではない", JSON_TEXT])
    session = tmp_path / "session"

    report = analyze_one(
        manifest_path,
        default_options(session=str(session)),
        "本文",
        "目的",
        None,
        None,
        backend,
        tmp_path / "cand_a_analysis",
    )

    assert report["json_failures"] == 0
    assert len(backend.requests) == 2
    assert "コードフェンス付きJSONのみ" in backend.requests[1].prompt
    assert (tmp_path / "cand_a_analysis.retry.prompt.txt").exists()

    meta = json.loads((tmp_path / "cand_a_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["usage"]["input_tokens"] == 2000  # 2 回分を合算
    assert meta["cost_usd"] == pytest.approx(0.004)
    assert meta["retries"] == 3  # 通信リトライ 1 + 1 と JSON リトライ 1
    # usage.jsonl は 1 パート 1 行（リトライ分は合算）
    assert len((session / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_analyze_one_splits_parts_and_writes_meta_per_part(tmp_path):
    manifest_path = make_manifest(tmp_path, tile_count=3)
    backend = FakeBackend()

    report = analyze_one(
        manifest_path,
        default_options(max_tiles_per_call=2),
        "本文",
        "目的",
        None,
        None,
        backend,
        tmp_path / "cand_a_analysis",
    )

    assert report["parts"] == 2
    assert (tmp_path / "cand_a_analysis_part00.meta.json").exists()
    assert (tmp_path / "cand_a_analysis_part01.prompt.txt").exists()
    merged = json.loads((tmp_path / "cand_a_analysis.json").read_text(encoding="utf-8"))
    assert merged["_merged_from_parts"] == 2


def test_analyze_one_raw_mode_skips_json(tmp_path):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    analyze_one(
        manifest_path,
        default_options(expect_json=False),
        "本文",
        "目的",
        None,
        None,
        FakeBackend(texts=["ただの文章"]),
        tmp_path / "cand_a_analysis",
    )
    assert (tmp_path / "cand_a_analysis.txt").read_text(encoding="utf-8") == "ただの文章"
    assert not (tmp_path / "cand_a_analysis.json").exists()


def test_analyze_one_dry_run_does_not_call_backend(tmp_path, capsys):
    manifest_path = make_manifest(tmp_path)
    backend = FakeBackend()

    analyze_one(
        manifest_path,
        default_options(dry_run=True),
        "本文",
        "目的",
        None,
        None,
        backend,
        tmp_path / "cand_a_analysis",
    )

    assert backend.requests == []
    assert not (tmp_path / "cand_a_analysis.meta.json").exists()
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "backend: fake" in out
    assert "画像 2 枚" in out


# --- ネイティブ動画クリップ経路（--ranges / --video） ----------------------------


def make_ranges_config(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    config_path = tmp_path / "ranges.json"
    config_path.write_text(
        json.dumps(
            {
                "video": str(video),
                "output_dir": str(tmp_path / "out"),
                "defaults": {"fps": 5, "pad": 1.0},
                "ranges": [
                    {"label": "cand_a", "start": 10.0, "end": 14.0, "note": "仮説A"},
                    {"label": "cand_b", "start": 0.5, "end": 4.0, "fps": 8},
                ],
            }
        ),
        encoding="utf-8",
    )
    return config_path, video


def test_collect_clip_jobs_from_ranges_applies_pad_and_defaults(tmp_path):
    config_path, video = make_ranges_config(tmp_path)
    jobs = collect_clip_jobs(default_options(ranges=str(config_path)))

    assert [job.label for job in jobs] == ["cand_a", "cand_b"]
    assert jobs[0].start_sec == 9.0 and jobs[0].end_sec == 15.0  # pad 1.0
    assert jobs[0].fps == 5 and jobs[0].note == "仮説A"
    assert jobs[1].start_sec == 0.0  # 0 未満にはしない
    assert jobs[1].fps == 8
    assert jobs[0].video == video.resolve()
    assert jobs[0].base == (tmp_path / "out" / "cand_a_analysis").resolve()


def test_collect_clip_jobs_single_video_range(tmp_path):
    _config, video = make_ranges_config(tmp_path)
    jobs = collect_clip_jobs(
        default_options(
            video=str(video), start=48.0, end=54.0, fps=10, output_dir=str(tmp_path / "out")
        )
    )
    assert len(jobs) == 1
    assert (jobs[0].start_sec, jobs[0].end_sec, jobs[0].fps) == (48.0, 54.0, 10.0)
    assert jobs[0].base.name == "range_48_54_analysis"


def test_run_analysis_ranges_creates_one_clip_per_range(tmp_path, monkeypatch):
    config_path, video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    backend = FakeBackend(supports_video_clip=True)
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    options = default_options(
        ranges=str(config_path), prompt=str(prompt), session=str(tmp_path / "session")
    )
    assert run_analysis(options) == 0

    assert len(backend.requests) == 2
    clips = [request.media[0] for request in backend.requests]
    assert all(isinstance(clip, MediaVideoClip) for clip in clips)
    assert [(clip.start_sec, clip.end_sec, clip.fps) for clip in clips] == [
        (9.0, 15.0, 5.0),
        (0.0, 5.0, 8.0),
    ]
    assert all(clip.video == video.resolve() for clip in clips)
    assert "9.0 秒から 15.0 秒" in backend.requests[0].prompt

    out = tmp_path / "out"
    assert (out / "cand_a_analysis.json").exists()
    assert (out / "cand_b_analysis.meta.json").exists()
    assert (out / "cand_a_analysis.prompt.txt").exists()
    lines = (tmp_path / "session" / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert {json.loads(line)["name"] for line in lines} == {"cand_a_analysis", "cand_b_analysis"}


def test_run_analysis_ranges_rejects_backend_without_video_clip(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: FakeBackend())

    with pytest.raises(RuntimeError, match="--backend gemini"):
        run_analysis(default_options(ranges=str(config_path), prompt=str(prompt)))


def test_run_analysis_dry_run_calls_no_api(tmp_path, monkeypatch, capsys):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    backend = FakeBackend(supports_video_clip=True)
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    assert (
        run_analysis(default_options(ranges=str(config_path), prompt=str(prompt), dry_run=True)) == 0
    )

    assert backend.requests == []
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.json"))
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "video.mp4" in out  # クリップ仕様が出る
    assert "出力先: cand_a_analysis" in out


def test_collect_clip_jobs_rejects_ranges_and_video_together(tmp_path):
    config_path, video = make_ranges_config(tmp_path)
    with pytest.raises(RuntimeError, match="同時に指定できません"):
        collect_clip_jobs(default_options(ranges=str(config_path), video=str(video)))


def test_collect_clip_jobs_rejects_colliding_output(tmp_path):
    """--output は 1 件用。複数範囲を 1 ファイルに書こうとしたら止める。"""
    config_path, video = make_ranges_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    del config["output_dir"]  # config の output_dir が無いと --output が使われる
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert video.exists()

    with pytest.raises(RuntimeError, match="出力先が衝突します"):
        collect_clip_jobs(
            default_options(ranges=str(config_path), output=str(tmp_path / "x.json"))
        )


def test_collect_clip_jobs_slugifies_unsafe_labels_but_keeps_original_label(tmp_path):
    """ファイル名は slug 化されるが、job.label（JSON 上の label）は元のまま。"""
    config_path = tmp_path / "ranges.json"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    config_path.write_text(
        json.dumps(
            {
                "video": str(video),
                "output_dir": str(tmp_path / "out"),
                "defaults": {"fps": 5},
                "ranges": [{"label": "Boss/Fight:1", "start": 0.0, "end": 2.0}],
            }
        ),
        encoding="utf-8",
    )
    jobs = collect_clip_jobs(default_options(ranges=str(config_path)))

    assert jobs[0].label == "Boss/Fight:1"  # 元のラベルはそのまま
    assert jobs[0].base == (tmp_path / "out" / "Boss_Fight_1_analysis").resolve()  # ファイル名は slug


def test_collect_clip_jobs_rejects_colliding_slugified_output(tmp_path):
    """slug 化すると衝突する別ラベルは起動時にエラーにする。"""
    config_path = tmp_path / "ranges.json"
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    config_path.write_text(
        json.dumps(
            {
                "video": str(video),
                "output_dir": str(tmp_path / "out"),
                "defaults": {"fps": 5},
                "ranges": [
                    {"label": "cand/a", "start": 0.0, "end": 2.0},
                    {"label": "cand:a", "start": 5.0, "end": 7.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="出力先が衝突します"):
        collect_clip_jobs(default_options(ranges=str(config_path)))


def test_run_analysis_rejects_mixing_tile_and_native_inputs(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr(
        "avs.analysis.build_backend", lambda options: FakeBackend(supports_video_clip=True)
    )
    manifest_path = make_manifest(tmp_path)

    with pytest.raises(RuntimeError, match="同時に指定できません"):
        run_analysis(
            default_options(
                ranges=str(config_path), manifest=[str(manifest_path)], prompt=str(prompt)
            )
        )


# --- 既存出力のスキップ / --force ------------------------------------------------


def test_analyze_one_skips_when_output_already_exists(tmp_path):
    manifest_path = make_manifest(tmp_path)
    base = tmp_path / "cand_a_analysis"
    base.with_suffix(".json").write_text('{"events": ["既存"]}', encoding="utf-8")
    backend = FakeBackend()

    report = analyze_one(manifest_path, default_options(), "本文", "目的", None, None, backend, base)

    assert report.get("skipped") is True
    assert backend.requests == []
    # 既存の内容は書き換えられない
    assert json.loads(base.with_suffix(".json").read_text(encoding="utf-8")) == {"events": ["既存"]}


def test_analyze_one_force_reanalyzes_existing_output(tmp_path):
    manifest_path = make_manifest(tmp_path)
    base = tmp_path / "cand_a_analysis"
    base.with_suffix(".json").write_text('{"events": ["既存"]}', encoding="utf-8")
    backend = FakeBackend()

    report = analyze_one(
        manifest_path, default_options(force=True), "本文", "目的", None, None, backend, base
    )

    assert not report.get("skipped")
    assert len(backend.requests) == 1
    assert json.loads(base.with_suffix(".json").read_text(encoding="utf-8")) == {
        "events": [{"title": "A"}]
    }


def test_analyze_clip_skips_when_output_already_exists(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    base = tmp_path / "out" / "cand_a_analysis"
    base.parent.mkdir(parents=True)
    base.with_suffix(".json").write_text('{"events": ["既存"]}', encoding="utf-8")
    backend = FakeBackend(supports_video_clip=True)
    job = ClipJob(label="cand_a", video=video, start_sec=0.0, end_sec=4.0, fps=5.0, note=None, base=base)

    report = analyze_clip(job, default_options(), "本文", "目的", None, backend)

    assert report.get("skipped") is True
    assert backend.requests == []


# --- 失敗隔離とレジューム（run_analysis 経由の書き戻し） -------------------------


def _write_raw_manifest(directory: Path, tile_filenames: list[str]) -> Path:
    """タイル画像を実際には作らない manifest（resolve_tile_path が失敗するように）。"""
    directory.mkdir(parents=True, exist_ok=True)
    tiles = [
        {
            "tile_index": index,
            "filename": name,
            "start_timestamp_sec": float(index * 2),
            "end_timestamp_sec": float(index * 2 + 1.5),
            "start_frame": index * 4,
            "end_frame": index * 4 + 3,
        }
        for index, name in enumerate(tile_filenames)
    ]
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps({"extraction": {"start_sec": 0.0, "end_sec": 4.0, "fps": 2}, "tiles": tiles}),
        encoding="utf-8",
    )
    return manifest_path


def test_run_analysis_continues_after_one_manifest_fails_and_writes_back_summary(
    tmp_path, monkeypatch
):
    good_manifest = make_manifest(tmp_path / "good")
    # 存在しないタイル画像を参照する manifest -> resolve_tile_path で RuntimeError
    bad_manifest = _write_raw_manifest(tmp_path / "bad" / "cand_bad", ["missing.jpg"])

    summary_path = tmp_path / "batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "results": [
                    {"manifest_path": str(good_manifest), "note": None, "label": "good"},
                    {"manifest_path": str(bad_manifest), "note": None, "label": "bad"},
                ]
            }
        ),
        encoding="utf-8",
    )

    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(summary=str(summary_path), prompt=str(prompt), output_dir=str(tmp_path / "out"))
    assert run_analysis(options) == 0  # 1件失敗だが全滅ではないので0

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    statuses = {entry["label"]: entry["analysis_status"] for entry in summary["results"]}
    assert statuses == {"good": "ok", "bad": "error"}
    bad_entry = next(entry for entry in summary["results"] if entry["label"] == "bad")
    assert "タイル画像が見つかりません" in bad_entry["analysis_error"]
    good_entry = next(entry for entry in summary["results"] if entry["label"] == "good")
    assert "analysis_error" not in good_entry


def test_run_analysis_strict_returns_1_on_partial_failure(tmp_path, monkeypatch):
    good_manifest = make_manifest(tmp_path / "good")
    bad_manifest = _write_raw_manifest(tmp_path / "bad" / "cand_bad", ["missing.jpg"])
    summary_path = tmp_path / "batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "results": [
                    {"manifest_path": str(good_manifest), "note": None, "label": "good"},
                    {"manifest_path": str(bad_manifest), "note": None, "label": "bad"},
                ]
            }
        ),
        encoding="utf-8",
    )
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        summary=str(summary_path), prompt=str(prompt), output_dir=str(tmp_path / "out"), strict=True
    )
    assert run_analysis(options) == 1


def test_run_analysis_returns_1_when_all_manifests_fail(tmp_path, monkeypatch):
    bad_manifest = _write_raw_manifest(tmp_path / "bad" / "cand_bad", ["missing.jpg"])
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        manifest=[str(bad_manifest)], prompt=str(prompt), output_dir=str(tmp_path / "out")
    )
    assert run_analysis(options) == 1


class FlakyBackend(FakeBackend):
    """指定した呼び出し回数目だけ失敗するバックエンド（--ranges の失敗隔離テスト用）。"""

    def __init__(self, fail_on_call: int, **kwargs):
        super().__init__(**kwargs)
        self.fail_on_call = fail_on_call
        self.call_count = 0

    def complete(self, request):
        self.call_count += 1
        if self.call_count == self.fail_on_call:
            raise RuntimeError("api boom")
        return super().complete(request)


def test_run_analysis_ranges_isolates_failure_and_writes_analysis_summary(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    backend = FlakyBackend(fail_on_call=1, supports_video_clip=True)
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    options = default_options(ranges=str(config_path), prompt=str(prompt))
    assert run_analysis(options) == 0  # cand_a 失敗 / cand_b 成功。全滅ではない

    summary = json.loads((tmp_path / "out" / "analysis_summary.json").read_text(encoding="utf-8"))
    statuses = {entry["label"]: entry["status"] for entry in summary["results"]}
    assert statuses == {"cand_a": "error", "cand_b": "ok"}
    failed = next(entry for entry in summary["results"] if entry["label"] == "cand_a")
    assert "api boom" in failed["error"]
    assert (tmp_path / "out" / "cand_b_analysis.json").exists()


def test_run_analysis_ranges_strict_returns_1_on_partial_failure(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    backend = FlakyBackend(fail_on_call=1, supports_video_clip=True)
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    options = default_options(ranges=str(config_path), prompt=str(prompt), strict=True)
    assert run_analysis(options) == 1


# --- --session 省略時の自動検出 ---------------------------------------------------


def test_run_analysis_auto_attaches_session_found_from_output_dir(tmp_path, monkeypatch):
    session = Session.create(tmp_path / "sess", video=str(tmp_path / "video.mp4"), backend="openrouter")
    manifest_path = make_manifest(tmp_path / "sess" / "ranges")
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        manifest=[str(manifest_path)], prompt=str(prompt), output_dir=str(tmp_path / "sess" / "ranges")
    )
    assert options.session is None
    assert run_analysis(options) == 0

    assert options.session == str(session.root)
    assert session.usage_path.exists()


# --- P3 プロンプト配線: --domain / --strict-json / {{SCHEMA}} -------------------


def test_run_analysis_domain_option_adds_domain_guide_to_prompt(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(
        json.dumps(
            {
                "name": "テストゲーム",
                "description": "テストドメインの説明",
                "watchlist": ["HPバーの増減"],
            }
        ),
        encoding="utf-8",
    )
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        domain=str(domain_path),
        output_dir=str(tmp_path / "out"),
    )
    assert run_analysis(options) == 0

    sent_prompt = backend.requests[0].prompt
    assert "## ドメインの手引き" in sent_prompt
    assert "テストドメインの説明" in sent_prompt
    assert "HPバーの増減" in sent_prompt


def test_run_analysis_without_domain_option_omits_domain_guide(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        manifest=[str(manifest_path)], prompt=str(prompt), output_dir=str(tmp_path / "out")
    )
    assert run_analysis(options) == 0
    assert "## ドメインの手引き" not in backend.requests[0].prompt


def test_run_analysis_strict_json_gates_llm_request_json_schema(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    prompt = tmp_path / "overview.txt"  # stem が既知のスキーマ名と一致する
    prompt.write_text("本文 {{SCHEMA}}", encoding="utf-8")

    backend_without = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend_without)
    options_without = default_options(
        manifest=[str(manifest_path)], prompt=str(prompt), output_dir=str(tmp_path / "out1")
    )
    assert run_analysis(options_without) == 0
    assert backend_without.requests[0].json_schema is None  # --strict-json 無しでは None

    backend_with = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend_with)
    options_with = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        output_dir=str(tmp_path / "out2"),
        strict_json=True,
    )
    assert run_analysis(options_with) == 0
    schema = backend_with.requests[0].json_schema
    assert schema is not None
    assert schema["type"] == "object"
    assert "examples" not in json.dumps(schema)  # api_schema() で examples は落ちる


def test_run_analysis_strict_json_without_known_schema_stays_none(tmp_path, monkeypatch):
    """未知のプロンプト名（自作プロンプト）では --strict-json でも json_schema は None のまま。"""
    manifest_path = make_manifest(tmp_path, tile_count=1)
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "my_custom_prompt.txt"
    prompt.write_text("本文", encoding="utf-8")

    options = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        output_dir=str(tmp_path / "out"),
        strict_json=True,
    )
    assert run_analysis(options) == 0
    assert backend.requests[0].json_schema is None


def test_run_analysis_warns_when_unknown_prompt_has_schema_placeholder(tmp_path, monkeypatch, capsys):
    manifest_path = make_manifest(tmp_path, tile_count=1)
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    prompt = tmp_path / "my_custom_prompt.txt"
    prompt.write_text("本文 {{SCHEMA}}", encoding="utf-8")

    options = default_options(
        manifest=[str(manifest_path)], prompt=str(prompt), output_dir=str(tmp_path / "out")
    )
    assert run_analysis(options) == 0

    out = capsys.readouterr().out
    assert "[警告]" in out
    assert "{{SCHEMA}}" in out
    # 未知の stem なので置換されず本文にそのまま残る
    assert "{{SCHEMA}}" in backend.requests[0].prompt


def test_run_analysis_real_prompt_schema_placeholder_is_fully_substituted(tmp_path, monkeypatch):
    """実プロンプト（overview.txt）経由で {{SCHEMA}} が置換され、プレースホルダが残らない。"""
    manifest_path = make_manifest(tmp_path, tile_count=1)
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)
    repo_root = Path(__file__).resolve().parents[1]
    prompt = repo_root / "skills" / "agentic-video-analysis-skill" / "prompts" / "overview.txt"
    assert prompt.exists()

    options = default_options(
        manifest=[str(manifest_path)], prompt=str(prompt), output_dir=str(tmp_path / "out")
    )
    assert run_analysis(options) == 0

    assert "{{" not in backend.requests[0].prompt


# --- usage.jsonl の step（§4.7。detail が other に落ちないこと） ---------------------


def test_usage_records_step_from_prompt_name(tmp_path, monkeypatch):
    """step はプロンプト名（stem）から決まる。出力名からの推測ではない。"""
    manifest_path = make_manifest(tmp_path)
    session = tmp_path / "session"
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    options = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        session=str(session),
        output_dir=str(tmp_path / "out"),
    )
    assert run_analysis(options) == 0

    record = json.loads((session / "usage.jsonl").read_text(encoding="utf-8").strip())
    # 出力名は cand_a_analysis なので、名前からのキーワード判定では other に落ちる
    assert record["name"] == "cand_a_analysis"
    assert record["step"] == "detail"
    meta = json.loads((tmp_path / "out" / "cand_a_analysis.meta.json").read_text(encoding="utf-8"))
    assert meta["step"] == "detail"


def test_usage_step_is_other_for_unknown_prompt_name(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path)
    session = tmp_path / "session"
    prompt = tmp_path / "my_custom_prompt.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: FakeBackend())

    options = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        session=str(session),
        output_dir=str(tmp_path / "out"),
    )
    assert run_analysis(options) == 0
    record = json.loads((session / "usage.jsonl").read_text(encoding="utf-8").strip())
    assert record["step"] == "other"


# --- --raw のときの summary の output（§4.5） --------------------------------------


def test_analysis_summary_output_points_to_txt_in_raw_mode(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr(
        "avs.analysis.build_backend", lambda options: FakeBackend(supports_video_clip=True)
    )

    options = default_options(ranges=str(config_path), prompt=str(prompt), expect_json=False)
    assert run_analysis(options) == 0

    summary = json.loads((tmp_path / "out" / "analysis_summary.json").read_text(encoding="utf-8"))
    outputs = {entry["label"]: entry["output"] for entry in summary["results"]}
    assert all(value.endswith(".txt") for value in outputs.values()), outputs
    assert (tmp_path / "out" / "cand_a_analysis.txt").exists()


def test_analysis_summary_output_points_to_json_by_default(tmp_path, monkeypatch):
    config_path, _video = make_ranges_config(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr(
        "avs.analysis.build_backend", lambda options: FakeBackend(supports_video_clip=True)
    )

    assert run_analysis(default_options(ranges=str(config_path), prompt=str(prompt))) == 0
    summary = json.loads((tmp_path / "out" / "analysis_summary.json").read_text(encoding="utf-8"))
    assert all(entry["output"].endswith(".json") for entry in summary["results"])


def test_batch_summary_write_back_records_analysis_output(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path)
    summary_path = tmp_path / "batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "manifest_path": str(manifest_path),
                        "label": "cand_a",
                        "output": str(tmp_path / "cand_a" / "tile.jpg"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文", encoding="utf-8")
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: FakeBackend())

    options = default_options(
        summary=str(summary_path),
        prompt=str(prompt),
        output_dir=str(tmp_path / "out"),
        expect_json=False,
    )
    assert run_analysis(options) == 0

    entry = json.loads(summary_path.read_text(encoding="utf-8"))["results"][0]
    assert entry["analysis_status"] == "ok"
    assert entry["analysis_output"].endswith("cand_a_analysis.txt")
    # タイル画像を指す output は上書きしない
    assert entry["output"].endswith("tile.jpg")


# --- BOM 付き UTF-8 の入力（§4.7） --------------------------------------------------


def test_run_analysis_reads_prompt_and_context_with_bom(tmp_path, monkeypatch):
    manifest_path = make_manifest(tmp_path)
    prompt = tmp_path / "detail.txt"
    prompt.write_text("本文プロンプト\n目的: {{OBJECTIVE}}", encoding="utf-8-sig")
    objective = tmp_path / "objective.txt"
    objective.write_text("BOM 付きの目的", encoding="utf-8-sig")
    context = tmp_path / "context.md"
    context.write_text("BOM 付きの追加情報", encoding="utf-8-sig")
    domain_path = tmp_path / "domain.json"
    domain_path.write_text(
        json.dumps({"name": "テストドメイン", "watchlist": ["数値の増減"]}, ensure_ascii=False),
        encoding="utf-8-sig",
    )
    backend = FakeBackend()
    monkeypatch.setattr("avs.analysis.build_backend", lambda options: backend)

    options = default_options(
        manifest=[str(manifest_path)],
        prompt=str(prompt),
        objective=str(objective),
        context=str(context),
        domain=str(domain_path),
        output_dir=str(tmp_path / "out"),
    )
    assert run_analysis(options) == 0

    sent = backend.requests[0].prompt
    assert "\ufeff" not in sent
    assert sent.startswith("本文プロンプト")
    assert "BOM 付きの目的" in sent
    assert "BOM 付きの追加情報" in sent
    assert "数値の増減" in sent
