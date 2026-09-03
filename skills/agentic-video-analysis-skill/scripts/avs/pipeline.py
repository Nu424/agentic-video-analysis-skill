#!/usr/bin/env python
"""標準ドライバ（`run_pipeline.py` の本体）。

docs/WORKFLOW.md §1 の Step 0〜7 をそのまま実装する。プリミティブ CLI を
subprocess で呼ぶのではなく、`avs/` の関数を直接呼ぶ。

| Step | 処理 | 主な出力 |
|------|------|---------|
| 0 | セッション作成（`ffprobe` で長さ取得、`notes/domain.json` にコピー） | `session.json` |
| 1 | 全体把握（長尺は章立て → 章ごと overview） | `overview/overview_analysis.json` |
| 2 | 範囲計画（全区間カバー） | `ranges/ranges.json` |
| 3 | タイル化（tile 入力のみ） | `ranges/<label>/`, `ranges/batch_summary.json` |
| 4 | 詳細解析 | `ranges/<label>_analysis.json` |
| 5 | バリデーション | `ranges/<label>_validated.json`, `merge/validation_report.json` |
| 6 | 統合 | `merge/timeline_mechanical.json`, `merge/timeline.json`, `final.md` |
| 7 | レポートと次アクション提案 | 標準出力（`usage.jsonl` 集計） |

各ステップは**主要出力が既にあればスキップ**するので、途中で止まっても同じコマンドで
再開できる（`--force` で全ステップ再実行）。1 ステップが失敗したらそこで停止し、
それまでの成果物と `session.json` の記録を残して終了コード 1 を返す。
"""

from __future__ import annotations

import dataclasses
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avs.analysis import AnalyzeOptions, run_analysis
from avs.backends import get_backend
from avs.common import probe_duration_sec, read_json, safe_slug, write_json
from avs.merge import (
    FINAL_MD_FILENAME,
    MECHANICAL_FILENAME,
    TIMELINE_FILENAME,
    MergeOptions,
    run_merge,
)
from avs.prompts import DEFAULT_OBJECTIVE, load_domain, resolve_text_arg
from avs.ranges import (
    DEFAULT_FULL_COVERAGE_MAX_SEC,
    plan_full_coverage,
    resolve_coverage,
    run_config_mode,
)
from avs.session import SESSION_FILENAME, Session, build_report, render_report_text
from avs.tiling import TileOptions, tile_one_range
from avs.validate import (
    REPORT_FILENAME,
    ValidateOptions,
    analysis_path_for_manifest,
    run_validation,
    validated_path_for,
)

DEFAULT_SESSION_ROOT = "output/agentic_sessions"
DEFAULT_BACKEND = "openrouter"
DEFAULT_INPUT_MODE = "tile"
INPUT_MODES = ("tile", "native")

DEFAULT_OVERVIEW_FPS = 1.0
DEFAULT_DETAIL_FPS = 5.0
DEFAULT_LOW_FPS = 1.0
DEFAULT_CHAPTERS_FPS = 0.15
DEFAULT_MAX_RANGE_SEC = 8.0
DEFAULT_PAD = 1.0
DEFAULT_FRAMES_PER_TILE = 12
DEFAULT_JOBS = 4
DEFAULT_OVERLAP_THRESHOLD = 0.5

OVERVIEW_DIR_NAME = "overview"
RANGES_DIR_NAME = "ranges"
MERGE_DIR_NAME = "merge"
NOTES_DIR_NAME = "notes"

OVERVIEW_ANALYSIS_FILENAME = "overview_analysis.json"
CHAPTERS_ANALYSIS_FILENAME = "chapters_analysis.json"
CHAPTERS_CONFIG_FILENAME = "chapters_ranges.json"
CHAPTERS_SUMMARY_FILENAME = "chapters_summary.json"
RANGES_FILENAME = "ranges.json"
BATCH_SUMMARY_FILENAME = "batch_summary.json"
DOMAIN_COPY_FILENAME = "domain.json"

STATUS_LABELS = {
    "ok": "完了",
    "skipped": "スキップ",
    "dry_run": "dry-run",
    "error": "失敗",
}


def log(message: str) -> None:
    print(message, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prompt_path(name: str) -> Path:
    """スキル同梱の `prompts/<name>.txt`。"""
    return Path(__file__).resolve().parents[2] / "prompts" / f"{name}.txt"


# --- オプション ------------------------------------------------------------------


@dataclass
class PipelineOptions:
    """ドライバの設定。CLI の argparse.Namespace と同じ属性名を持つ。"""

    video: str | None = None
    objective: str | None = None
    domain: str | None = None
    backend: str = DEFAULT_BACKEND
    input_mode: str = DEFAULT_INPUT_MODE
    session: str | None = None
    session_root: str = DEFAULT_SESSION_ROOT
    model: str | None = None
    overview_fps: float = DEFAULT_OVERVIEW_FPS
    detail_fps: float = DEFAULT_DETAIL_FPS
    low_fps: float = DEFAULT_LOW_FPS
    chapters_fps: float = DEFAULT_CHAPTERS_FPS
    coverage: str = "auto"
    max_range_sec: float = DEFAULT_MAX_RANGE_SEC
    pad: float = DEFAULT_PAD
    frames_per_tile: int = DEFAULT_FRAMES_PER_TILE
    jobs: int = DEFAULT_JOBS
    full_coverage_max_sec: float = DEFAULT_FULL_COVERAGE_MAX_SEC
    no_llm_merge: bool = False
    strict_json: bool = False
    force: bool = False
    dry_run: bool = False

    @classmethod
    def from_namespace(cls, namespace: Any) -> "PipelineOptions":
        known = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})

    @property
    def is_native(self) -> bool:
        return self.input_mode == "native"


@dataclass
class PipelineContext:
    """1 回の実行で全ステップが共有する状態。"""

    options: PipelineOptions
    video: Path
    duration_sec: float
    objective: str
    session: Session
    domain_path: Path | None = None

    @property
    def root(self) -> Path:
        return self.session.root

    @property
    def overview_dir(self) -> Path:
        return self.root / OVERVIEW_DIR_NAME

    @property
    def ranges_dir(self) -> Path:
        return self.root / RANGES_DIR_NAME

    @property
    def merge_dir(self) -> Path:
        return self.root / MERGE_DIR_NAME

    @property
    def ranges_config_path(self) -> Path:
        return self.ranges_dir / RANGES_FILENAME

    @property
    def batch_summary_path(self) -> Path:
        return self.ranges_dir / BATCH_SUMMARY_FILENAME

    @property
    def overview_analysis_path(self) -> Path:
        return self.overview_dir / OVERVIEW_ANALYSIS_FILENAME

    @property
    def is_long_form(self) -> bool:
        """長尺（章立て分岐に入る）か。"""
        return self.duration_sec > self.options.full_coverage_max_sec


# --- Step 0: セッション ------------------------------------------------------------


def _check_backend_input_combination(options: PipelineOptions) -> None:
    """矛盾する `--input` / `--backend` の組み合わせを起動時に弾く。"""
    if options.input_mode not in INPUT_MODES:
        raise RuntimeError(f"--input は {' | '.join(INPUT_MODES)} のいずれかです: {options.input_mode}")
    if not options.is_native:
        return
    # supports_video_clip を見るためだけに作るので、APIキーはダミーでよい（呼び出しはしない）
    backend = get_backend(options.backend, model=options.model, api_key="pipeline-precheck")
    if not backend.supports_video_clip:
        raise RuntimeError(
            f"--input native は動画クリップ非対応のバックエンド（{options.backend}）では使えません。"
            "native は --backend gemini が必要です"
        )


def resolve_session_root(options: PipelineOptions, video: Path) -> Path:
    if options.session:
        return Path(options.session).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(options.session_root).expanduser() / f"{video.stem}_{timestamp}"
    return root.resolve()


def _prepare_domain(options: PipelineOptions, root: Path) -> Path | None:
    """`--domain` を `notes/domain.json` にコピーする。省略時は既存のコピーを使う。"""
    notes_dir = root / NOTES_DIR_NAME
    copy_path = notes_dir / DOMAIN_COPY_FILENAME
    if options.domain:
        source = Path(options.domain).expanduser().resolve()
        load_domain(source)  # 形式が壊れていればここで止める
        notes_dir.mkdir(parents=True, exist_ok=True)
        if source != copy_path.resolve():
            shutil.copyfile(source, copy_path)
        return copy_path
    if copy_path.exists():
        return copy_path
    return None


def create_context(options: PipelineOptions) -> PipelineContext:
    """Step 0: 動画とオプションを検証し、セッションを作る（既存なら再開）。"""
    if not options.video:
        raise RuntimeError("--video を指定してください")
    video = Path(options.video).expanduser().resolve()
    if not video.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video}")
    _check_backend_input_combination(options)

    duration_sec = probe_duration_sec(video)
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE

    root = resolve_session_root(options, video)
    root.mkdir(parents=True, exist_ok=True)
    resumed = (root / SESSION_FILENAME).exists()
    domain_path = _prepare_domain(options, root)

    if resumed:
        session = Session(root=root)
    else:
        session = Session.create(
            root,
            video=str(video),
            backend=options.backend,
            model=options.model,
            objective=objective,
            domain_path=str(domain_path) if domain_path else None,
        )

    log(f"[Step 0] セッション: {root}" + ("（再開）" if resumed else "（新規）"))
    log(
        f"[Step 0] 動画: {video} / 長さ {duration_sec:.1f}s"
        f" / backend={options.backend} / model={options.model or '（バックエンド既定）'}"
        f" / 入力={options.input_mode}"
    )
    log(f"[Step 0] 目的: {objective}")
    log(f"[Step 0] ドメイン定義: {domain_path if domain_path else '（なし）'}")
    if options.dry_run:
        log("[Step 0] dry-run: API は呼びません（タイル化は実行します）")

    session.record_step(
        "session",
        step=0,
        status="resumed" if resumed else "created",
        started_at=_now(),
        elapsed_sec=0.0,
        video=str(video),
        duration_sec=round(duration_sec, 3),
        input_mode=options.input_mode,
        backend=options.backend,
        model=options.model,
        objective=objective,
        domain=str(domain_path) if domain_path else None,
        dry_run=options.dry_run,
        force=options.force,
    )
    return PipelineContext(
        options=options,
        video=video,
        duration_sec=duration_sec,
        objective=objective,
        session=session,
        domain_path=domain_path,
    )


# --- 解析オプションの組み立て -------------------------------------------------------


def _analyze_options(ctx: PipelineContext, prompt: str, **overrides: Any) -> AnalyzeOptions:
    options = ctx.options
    base = AnalyzeOptions(
        prompt=str(prompt_path(prompt)),
        objective=ctx.objective,
        domain=str(ctx.domain_path) if ctx.domain_path else None,
        backend=options.backend,
        model=options.model,
        strict_json=options.strict_json,
        session=str(ctx.root),
        force=options.force,
        dry_run=options.dry_run,
        jobs=1,
    )
    return dataclasses.replace(base, **overrides)


def _run_analysis_step(options: AnalyzeOptions, label: str) -> None:
    if run_analysis(options) != 0:
        raise RuntimeError(f"{label}の解析に失敗しました（詳細は上のログと .raw.txt を参照）")


# --- Step 1: 全体把握 -------------------------------------------------------------


def step_overview(ctx: PipelineContext) -> dict[str, Any]:
    """全編（長尺は章ごと）を低 fps で見て候補範囲を出す。"""
    output = ctx.overview_analysis_path
    if output.exists() and not ctx.options.force:
        return {"status": "skipped", "output": str(output)}

    ctx.overview_dir.mkdir(parents=True, exist_ok=True)
    if ctx.is_long_form:
        log(
            f"  動画長 {ctx.duration_sec:.1f}s > {ctx.options.full_coverage_max_sec:g}s のため"
            "、章立て → 章ごとの全体把握に分岐します"
        )
        return _overview_long_form(ctx, output)
    return _overview_short_form(ctx, output)


def _overview_short_form(ctx: PipelineContext, output: Path) -> dict[str, Any]:
    options = ctx.options
    fps = options.overview_fps
    if options.is_native:
        log(f"  全編をネイティブ動画クリップで解析: fps={fps:g} -> {output}")
        analyze_options = _analyze_options(
            ctx,
            "overview",
            video=str(ctx.video),
            start=0.0,
            end=None,
            fps=fps,
            output=str(output),
        )
    else:
        tile_output = ctx.overview_dir / "full.jpg"
        log(f"  全編タイル化: fps={fps:g} -> {tile_output}")
        result = tile_one_range(
            TileOptions(
                video=str(ctx.video),
                start=0.0,
                end=None,
                fps=fps,
                output=str(tile_output),
                frames_per_tile=options.frames_per_tile,
                quiet_warnings=True,
            )
        )
        log(f"  タイル {result['tile_count']} 枚 / フレーム {result['frame_count']} 枚")
        analyze_options = _analyze_options(
            ctx, "overview", manifest=[result["manifest_path"]], output=str(output)
        )

    _run_analysis_step(analyze_options, "全体把握")
    if options.dry_run:
        return {"status": "dry_run", "output": str(output)}

    data = read_json(output) if output.exists() else {}
    n_candidates = len(data.get("candidates") or []) if isinstance(data, dict) else 0
    log(f"  候補範囲: {n_candidates} 件 -> {output}")
    return {"status": "ok", "output": str(output), "n_candidates": n_candidates}


def _chapter_labels(chapters: list[dict[str, Any]]) -> list[str]:
    """章のラベルをファイル名として安全・一意にする。"""
    labels: list[str] = []
    seen: set[str] = set()
    for index, chapter in enumerate(chapters):
        fallback = f"chapter_{index:02d}"
        label = safe_slug(str(chapter.get("label") or fallback), fallback)
        if label in seen:
            label = f"{label}_{index:02d}"
        seen.add(label)
        labels.append(label)
    return labels


def _build_chapter_config(ctx: PipelineContext, chapters: list[dict[str, Any]]) -> dict[str, Any]:
    """章を範囲とする config（tile / native 双方が読む形式）を作る。"""
    options = ctx.options
    labels = _chapter_labels(chapters)
    entries: list[dict[str, Any]] = []
    for label, chapter in zip(labels, chapters, strict=True):
        start = max(0.0, float(chapter.get("start_sec", 0.0)))
        end = min(ctx.duration_sec, float(chapter.get("end_sec", ctx.duration_sec)))
        if end <= start:
            log(f"  [警告] 章 {label} の秒数が不正なので除外します: {start}-{end}")
            continue
        entry: dict[str, Any] = {
            "label": label,
            "start": round(start, 2),
            "end": round(end, 2),
            "output": str(ctx.overview_dir / f"{label}.jpg"),
        }
        note = " / ".join(
            str(part) for part in (chapter.get("title"), chapter.get("summary")) if part
        )
        if note:
            entry["note"] = note
        entries.append(entry)
    if not entries:
        raise RuntimeError("章立ての結果から有効な章を作れませんでした（chapters[] を確認してください）")
    return {
        "video": str(ctx.video),
        "output_dir": str(ctx.overview_dir),
        "defaults": {
            "fps": options.overview_fps,
            "pad": 0.0,
            "frames_per_tile": options.frames_per_tile,
        },
        "ranges": entries,
        "summary_output": str(ctx.overview_dir / CHAPTERS_SUMMARY_FILENAME),
    }


def _overview_long_form(ctx: PipelineContext, output: Path) -> dict[str, Any]:
    options = ctx.options
    chapters_output = ctx.overview_dir / CHAPTERS_ANALYSIS_FILENAME

    # 1) 章立て
    if not chapters_output.exists() or options.force:
        if options.is_native:
            log(f"  章立て（ネイティブ動画クリップ）: fps={options.chapters_fps:g} -> {chapters_output}")
            chapters_options = _analyze_options(
                ctx,
                "chapters",
                video=str(ctx.video),
                start=0.0,
                end=None,
                fps=options.chapters_fps,
                output=str(chapters_output),
            )
        else:
            tile_output = ctx.overview_dir / "chapters.jpg"
            log(f"  章立てタイル化: fps={options.chapters_fps:g} -> {tile_output}")
            result = tile_one_range(
                TileOptions(
                    video=str(ctx.video),
                    start=0.0,
                    end=None,
                    fps=options.chapters_fps,
                    output=str(tile_output),
                    frames_per_tile=options.frames_per_tile,
                    quiet_warnings=True,
                )
            )
            chapters_options = _analyze_options(
                ctx, "chapters", manifest=[result["manifest_path"]], output=str(chapters_output)
            )
        _run_analysis_step(chapters_options, "章立て")
    else:
        log(f"  章立て: 既存を再利用 {chapters_output}")

    if options.dry_run and not chapters_output.exists():
        log("  [dry-run] 章立ての結果が無いため、章ごとの全体把握はスキップします")
        return {"status": "dry_run", "output": str(output)}

    chapters_data = read_json(chapters_output)
    chapters = chapters_data.get("chapters") or [] if isinstance(chapters_data, dict) else []
    if not chapters:
        raise RuntimeError(f"章立ての結果に chapters がありません: {chapters_output}")
    log(f"  章: {len(chapters)} 件")

    # 2) 章ごとの全体把握
    config = _build_chapter_config(ctx, chapters)
    config_path = ctx.overview_dir / CHAPTERS_CONFIG_FILENAME
    write_json(config_path, config)
    labels = [entry["label"] for entry in config["ranges"]]

    if options.is_native:
        log(f"  章ごとの全体把握（ネイティブ）: {len(labels)} 章 / fps={options.overview_fps:g}")
        chapter_options = _analyze_options(
            ctx,
            "overview",
            ranges=str(config_path),
            output_dir=str(ctx.overview_dir),
            jobs=options.jobs,
        )
    else:
        log(f"  章ごとのタイル化: {len(labels)} 章 / fps={options.overview_fps:g}")
        if run_config_mode(
            TileOptions(
                config=str(config_path),
                frames_per_tile=options.frames_per_tile,
                quiet_warnings=True,
            )
        ) != 0:
            raise RuntimeError("章ごとのタイル化に全て失敗しました")
        chapter_options = _analyze_options(
            ctx,
            "overview",
            summary=str(ctx.overview_dir / CHAPTERS_SUMMARY_FILENAME),
            output_dir=str(ctx.overview_dir),
            jobs=options.jobs,
        )
    _run_analysis_step(chapter_options, "章ごとの全体把握")

    if options.dry_run:
        return {"status": "dry_run", "output": str(output), "n_chapters": len(labels)}

    # 3) 章ごとの候補を連結して overview_analysis.json にする
    candidates: list[dict[str, Any]] = []
    summaries: list[str] = []
    merged_from = 0
    for label in labels:
        path = ctx.overview_dir / f"{label}_analysis.json"
        if not path.exists():
            log(f"  [警告] 章の解析結果がありません: {path}")
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            log(f"  [警告] 章の解析結果がオブジェクトではありません: {path}")
            continue
        merged_from += 1
        for candidate in data.get("candidates") or []:
            if isinstance(candidate, dict):
                candidates.append({**candidate, "chapter": label})
        summary = data.get("summary")
        if isinstance(summary, str) and summary:
            summaries.append(summary)
    if merged_from == 0:
        raise RuntimeError("章ごとの全体把握の結果が 1 件も読めませんでした")

    write_json(
        output,
        {
            "summary": " / ".join(summaries),
            "candidates": candidates,
            "chapters": chapters,
            "_merged_from_chapters": merged_from,
        },
    )
    log(f"  候補範囲: {len(candidates)} 件（{merged_from} 章分を連結） -> {output}")
    return {
        "status": "ok",
        "output": str(output),
        "n_candidates": len(candidates),
        "n_chapters": merged_from,
    }


# --- Step 2: 範囲計画 -------------------------------------------------------------


def is_provisional_plan(path: Path) -> bool:
    """dry-run で候補なしのまま作られた仮の計画か（本番実行では作り直す）。"""
    if not path.exists():
        return False
    try:
        plan = read_json(path)
    except (OSError, ValueError):
        return False
    return bool(isinstance(plan, dict) and (plan.get("plan") or {}).get("dry_run"))


def step_plan_ranges(ctx: PipelineContext) -> dict[str, Any]:
    """overview の候補から全区間カバーの `ranges/ranges.json` を作る。"""
    options = ctx.options
    output = ctx.ranges_config_path
    if output.exists() and not options.force and not is_provisional_plan(output):
        return {"status": "skipped", "output": str(output)}

    overview_path = ctx.overview_analysis_path
    candidates: list[dict[str, Any]] = []
    provisional = False
    if overview_path.exists():
        data = read_json(overview_path)
        if isinstance(data, dict):
            candidates = [item for item in (data.get("candidates") or []) if isinstance(item, dict)]
    elif options.dry_run:
        provisional = True
        log("  [dry-run] 全体把握の結果が無いため、候補 0 件（全区間を gap で埋める）で仮に計画します")
    else:
        raise RuntimeError(f"全体把握の結果がありません: {overview_path}")

    coverage = resolve_coverage(options.coverage, ctx.duration_sec, options.full_coverage_max_sec)
    plan = plan_full_coverage(
        candidates,
        ctx.duration_sec,
        max_range_sec=options.max_range_sec,
        coverage=coverage,
        detail_fps=options.detail_fps,
        low_fps=options.low_fps,
        pad=options.pad,
    )
    plan["video"] = str(ctx.video)
    plan["output_dir"] = str(ctx.ranges_dir)
    plan["summary_output"] = str(ctx.batch_summary_path)
    plan["defaults"]["frames_per_tile"] = options.frames_per_tile
    if provisional:
        # 本番実行では作り直す（dry-run の仮計画をそのまま使わせない）
        plan["plan"]["dry_run"] = True

    ctx.ranges_dir.mkdir(parents=True, exist_ok=True)
    write_json(output, plan)
    if ctx.batch_summary_path.exists():
        # 前の計画で作った batch_summary は無効なので消す（Step 3 が作り直す）
        ctx.batch_summary_path.unlink()
        log(f"  古い {BATCH_SUMMARY_FILENAME} を削除しました（範囲計画を作り直したため）")
    dropped = len(plan["plan"]["dropped"])
    log(
        f"  coverage={coverage} / 候補 {len(candidates)} 件"
        f" -> 範囲 {plan['plan']['n_ranges']} 件"
        + (f"（high-only で除外 {dropped} 件）" if dropped else "")
    )
    log(f"  ranges: {output}")
    return {
        "status": "ok",
        "output": str(output),
        "coverage": coverage,
        "n_ranges": plan["plan"]["n_ranges"],
    }


# --- Step 3: タイル化 -------------------------------------------------------------


def _summary_has_error(summary_path: Path) -> bool:
    try:
        data = read_json(summary_path)
    except (OSError, ValueError):
        return True
    results = data.get("results") or [] if isinstance(data, dict) else []
    return any(result.get("status") == "error" for result in results if isinstance(result, dict))


def step_tiling(ctx: PipelineContext) -> dict[str, Any]:
    """`ranges.json` の全範囲を一括タイル化する（tile 入力のみ）。"""
    options = ctx.options
    if options.is_native:
        log("  ネイティブ入力のためタイル化は不要です")
        return {"status": "skipped", "reason": "native"}

    summary_path = ctx.batch_summary_path
    config_path = ctx.ranges_config_path
    plan_is_newer = (
        summary_path.exists()
        and config_path.exists()
        and config_path.stat().st_mtime > summary_path.stat().st_mtime
    )
    if (
        summary_path.exists()
        and not options.force
        and not plan_is_newer  # 範囲計画が作り直されていたらタイル化もやり直す
        and not _summary_has_error(summary_path)
    ):
        return {"status": "skipped", "output": str(summary_path)}

    exit_code = run_config_mode(
        TileOptions(
            config=str(ctx.ranges_config_path),
            merge_overlaps=True,
            overlap_threshold=DEFAULT_OVERLAP_THRESHOLD,
            frames_per_tile=options.frames_per_tile,
            quiet_warnings=True,
        )
    )
    if exit_code != 0:
        raise RuntimeError("タイル化に全て失敗しました")

    data = read_json(summary_path)
    results = data.get("results") or []
    errors = [result for result in results if result.get("status") == "error"]
    log(f"  タイル化: {len(results) - len(errors)}/{len(results)} 件成功 -> {summary_path}")
    return {
        "status": "ok",
        "output": str(summary_path),
        "n_ranges": len(results),
        "n_errors": len(errors),
    }


# --- Step 4: 詳細解析 -------------------------------------------------------------


def expected_analysis_paths(ctx: PipelineContext) -> list[Path]:
    """このセッションで作られるはずの `<label>_analysis.json` を列挙する。"""
    if ctx.options.is_native:
        config_path = ctx.ranges_config_path
        if not config_path.exists():
            return []
        config = read_json(config_path)
        paths = []
        for index, entry in enumerate(config.get("ranges") or []):
            fallback = f"range_{index:02d}"
            slug = safe_slug(str(entry.get("label") or fallback), fallback)
            paths.append(ctx.ranges_dir / f"{slug}_analysis.json")
        return paths

    summary_path = ctx.batch_summary_path
    if not summary_path.exists():
        return []
    summary = read_json(summary_path)
    paths = []
    for result in summary.get("results") or []:
        manifest_path = result.get("manifest_path")
        if manifest_path:
            paths.append(analysis_path_for_manifest(Path(manifest_path)))
    return paths


def step_detail(ctx: PipelineContext) -> dict[str, Any]:
    """範囲ごとに `detail.txt` で出来事を悉皆列挙する。"""
    options = ctx.options
    expected = expected_analysis_paths(ctx)
    if expected and not options.force and all(path.exists() for path in expected):
        return {"status": "skipped", "n_ranges": len(expected)}

    if options.is_native:
        analyze_options = _analyze_options(
            ctx,
            "detail",
            ranges=str(ctx.ranges_config_path),
            output_dir=str(ctx.ranges_dir),
            jobs=options.jobs,
        )
    else:
        analyze_options = _analyze_options(
            ctx,
            "detail",
            summary=str(ctx.batch_summary_path),
            jobs=options.jobs,
        )
    log(f"  詳細解析: 対象 {len(expected)} 件 / jobs={options.jobs} / fps は範囲ごと（既定 {options.detail_fps:g}）")
    _run_analysis_step(analyze_options, "詳細")

    if options.dry_run:
        return {"status": "dry_run", "n_ranges": len(expected)}
    done = [path for path in expected if path.exists()]
    log(f"  解析結果: {len(done)}/{len(expected)} 件")
    return {"status": "ok", "n_ranges": len(expected), "n_analyzed": len(done)}


# --- Step 5: バリデーション --------------------------------------------------------


def step_validate(ctx: PipelineContext) -> dict[str, Any]:
    """フラグ付け（削除はしない）と `merge/validation_report.json` の作成。"""
    options = ctx.options
    report_path = ctx.merge_dir / REPORT_FILENAME
    analyses = [path for path in expected_analysis_paths(ctx) if path.exists()]

    if options.dry_run:
        log(f"  [dry-run] 検証予定: {len(analyses)} 件 -> {report_path}")
        return {"status": "dry_run", "n_targets": len(analyses)}

    if not analyses:
        raise RuntimeError("検証対象の解析結果がありません（Step 4 の結果を確認してください）")

    validated = [validated_path_for(path, None) for path in analyses]
    if report_path.exists() and not options.force and all(path.exists() for path in validated):
        return {"status": "skipped", "output": str(report_path)}

    ctx.merge_dir.mkdir(parents=True, exist_ok=True)
    validate_options = ValidateOptions(
        summary=str(ctx.batch_summary_path) if not options.is_native else None,
        analysis=[str(path) for path in analyses] if options.is_native else None,
        domain=str(ctx.domain_path) if ctx.domain_path else None,
        report=True,
        report_output=str(report_path),
        session=str(ctx.root),
    )
    if run_validation(validate_options) != 0:
        raise RuntimeError("バリデーションに失敗しました")
    return {"status": "ok", "output": str(report_path), "n_targets": len(analyses)}


# --- Step 6: 統合 -----------------------------------------------------------------


def step_merge(ctx: PipelineContext) -> dict[str, Any]:
    """機械統合 → LLM 統合 → `final.md`。"""
    options = ctx.options
    timeline_path = ctx.merge_dir / TIMELINE_FILENAME
    final_md_path = ctx.root / FINAL_MD_FILENAME

    if options.dry_run:
        log(f"  [dry-run] 統合予定 -> {ctx.merge_dir / MECHANICAL_FILENAME} / {timeline_path} / {final_md_path}")
        log(f"  [dry-run] LLM 統合: {'しない（--no-llm-merge）' if options.no_llm_merge else 'する（1 回以上の呼び出し）'}")
        return {"status": "dry_run"}

    if timeline_path.exists() and final_md_path.exists() and not options.force:
        return {"status": "skipped", "output": str(timeline_path)}

    merge_options = MergeOptions(
        session=str(ctx.root),
        objective=ctx.objective,
        domain=str(ctx.domain_path) if ctx.domain_path else None,
        no_llm=options.no_llm_merge,
        final_md=True,
        backend=options.backend,
        model=options.model,
        strict_json=options.strict_json,
    )
    if run_merge(merge_options) != 0:
        raise RuntimeError("統合に失敗しました")
    return {"status": "ok", "output": str(timeline_path), "final_md": str(final_md_path)}


# --- Step 7: レポート -------------------------------------------------------------


def render_next_steps(ctx: PipelineContext, report: dict[str, Any]) -> str:
    """`session_report` の次アクション候補を「次にやること」として整形する。"""
    next_actions = report["next_actions"]
    failed = next_actions["failed_ranges"]
    zoom_targets = next_actions["zoom_targets"]
    rejected = next_actions["hypothesis_rejected"]
    low_confidence = next_actions["low_confidence_count"]
    flags = next_actions["validated_flags_count"]

    items: list[str] = []
    if ctx.options.dry_run:
        items.append(
            "dry-run なので解析結果はありません。上の実行予定（範囲数・fps・呼び出し予定）を確認したら、"
            "--dry-run を外して同じコマンドを実行してください"
        )
        lines = ["", "## 次にやること", f"1. {items[0]}", "", f"成果物: {ctx.root}"]
        return "\n".join(lines)
    if failed:
        labels = ", ".join(str(item.get("label")) for item in failed[:5])
        items.append(
            f"失敗した範囲が {len(failed)} 件あります（{labels}）。"
            "同じコマンドをもう一度実行してください（完了済みの範囲はスキップされ、失敗分だけ走ります）"
        )
    if flags:
        items.append(
            f"バリデーションのフラグが {flags} 件あります。"
            f"{ctx.merge_dir / REPORT_FILENAME} を開き、negative_match は根拠セルを画像で目視、"
            "boundary は範囲を広げて再解析してください"
        )
    if zoom_targets:
        sample = ", ".join(f"{item['timestamp_sec']}s" for item in zoom_targets[:5])
        items.append(
            f"zoom_targets が {len(zoom_targets)} 件あります（{sample}）。"
            "tile_video_frames.py --timestamps <秒> でズームし、prompts/zoom.txt で確認してください"
        )
    if rejected:
        items.append(
            f"仮説が反証された範囲が {len(rejected)} 件あります。"
            "全体把握の誤認なので final.md の「注意点」に残してください"
        )
    if low_confidence:
        items.append(
            f"confidence=low の出来事が {low_confidence} 件あります。"
            "細部が原因ならズーム（prompts/zoom.txt）、範囲の境界が原因なら精密確認（prompts/refine.txt）を行ってください"
        )
    if not items:
        items.append(
            f"追加の反復は必要なさそうです。{ctx.root / FINAL_MD_FILENAME} を読み、"
            "「注意点」を必要に応じて追記してください"
        )

    lines = ["", "## 次にやること"]
    lines.extend(f"{index}. {item}" for index, item in enumerate(items, start=1))
    lines.append("")
    lines.append(f"成果物: {ctx.root}")
    return "\n".join(lines)


def step_report(ctx: PipelineContext) -> dict[str, Any]:
    """usage 集計と次アクション候補を表示する。"""
    report = build_report(ctx.root, do_estimate=False)
    log("")
    log(render_report_text(report))
    log(render_next_steps(ctx, report))
    return {
        "status": "ok",
        "calls": report["usage"]["calls"],
        "cost_usd_total": report["usage"]["cost_usd_total"],
    }


# --- ドライバ --------------------------------------------------------------------

# (記録名, 表示名, 実行関数)。番号は 1 から順に振る。
STEPS: tuple[tuple[str, str, Callable[[PipelineContext], dict[str, Any]]], ...] = (
    ("overview", "全体把握", step_overview),
    ("plan_ranges", "範囲計画", step_plan_ranges),
    ("tiling", "タイル化", step_tiling),
    ("detail", "詳細解析", step_detail),
    ("validate", "バリデーション", step_validate),
    ("merge", "統合", step_merge),
    ("report", "レポート", step_report),
)


def run_pipeline(options: PipelineOptions) -> int:
    """Step 0〜7 を順に実行する。1 ステップでも失敗したらそこで停止して 1 を返す。"""
    ctx = create_context(options)

    for number, (name, title, func) in enumerate(STEPS, start=1):
        log(f"[Step {number}] {title}: 開始")
        started_at = _now()
        started = time.monotonic()
        try:
            info = func(ctx)
        except Exception as error:  # noqa: BLE001 - ステップ単位で止めて記録を残す
            elapsed = round(time.monotonic() - started, 3)
            ctx.session.record_step(
                name,
                step=number,
                status="error",
                started_at=started_at,
                elapsed_sec=elapsed,
                error=str(error),
            )
            log(f"[Step {number}] {title}: 失敗 ({elapsed:.1f}s) — {error}")
            log(
                "ここまでの成果物と session.json の記録は残っています。"
                "原因を直して同じコマンドを実行すると、完了済みのステップはスキップして再開します"
            )
            return 1

        elapsed = round(time.monotonic() - started, 3)
        status = info.pop("status", "ok")
        ctx.session.record_step(
            name,
            step=number,
            status=status,
            started_at=started_at,
            elapsed_sec=elapsed,
            **info,
        )
        label = STATUS_LABELS.get(status, status)
        detail = f" ({info['output']})" if status == "skipped" and info.get("output") else ""
        log(f"[Step {number}] {title}: {label} ({elapsed:.1f}s){detail}")

    log(f"[完了] セッション: {ctx.root}")
    return 0
