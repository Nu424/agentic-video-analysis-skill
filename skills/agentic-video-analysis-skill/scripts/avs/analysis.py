#!/usr/bin/env python
"""解析本体（タイル manifest / ネイティブ動画クリップ）。

呼び出しは `avs.backends` のバックエンド経由で行う（aitool には依存しない）。

タイル経路: 1 manifest = 原則 1 回の API 呼び出し（含まれる全タイルをまとめて渡す）。
タイル数が多い場合は時系列順に 1 タイル重ねてチャンク分割し、複数回呼び出す。

ネイティブ経路（`--ranges` / `--video --start --end`）: 1 範囲 = 1 クリップ = 1 回の呼び出し。
動画クリップを扱えるバックエンド（gemini）でのみ使える。

- `collect_manifests`: `--manifest` / `--summary` から (manifest, note) を列挙
- `resolve_output_base`: `<name>_analysis` までの出力ベースパスを決める
- `chunk_with_overlap`: タイルのチャンク分割
- `analyze_one`: 1 manifest を解析（パートごとに呼び出し → JSON 検証 → 統合）
- `analyze_clip`: 1 範囲をネイティブ動画クリップとして解析
- `run_analysis`: 入力の種類を判定し、並列実行と結果集計を行う

呼び出しごとに監査記録を残す:
`<base><suffix>.prompt.txt`（送ったプロンプト）、`<base><suffix>.meta.json`
（backend / model / メディア / usage / cost / latency / retries）、
`--session` があれば `<session>/usage.jsonl` に 1 行追記。
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avs.backends import LLMRequest, Media, MediaImage, MediaVideoClip, describe_media, get_backend
from avs.common import (
    format_float_for_path,
    probe_duration_sec,
    read_json,
    read_text_utf8,
    safe_slug,
    write_json,
    write_json_atomic,
)
from avs.prompts import (
    DEFAULT_OBJECTIVE,
    OTHER_STEP,
    SCHEMA_PLACEHOLDER,
    api_schema,
    assemble_prompt,
    build_clip_context,
    build_tile_context,
    load_domain,
    resolve_text_arg,
    schema_for_prompt,
    step_for_prompt,
)
from avs.session import Session, append_usage_line, build_call_meta

DEFAULT_MAX_TILES_PER_CALL = 8
DEFAULT_BACKEND = "openrouter"
DEFAULT_CLIP_FPS = 5.0

_print_lock = threading.Lock()
_summary_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


@dataclass
class AnalyzeOptions:
    """解析の設定。CLI の argparse.Namespace と同じ属性名を持つ。"""

    manifest: list[str] | None = None
    summary: str | None = None
    prompt: str | None = None
    output: str | None = None
    output_dir: str | None = None
    objective: str | None = None
    context: str | None = None
    domain: str | None = None
    expect_json: bool = True  # CLI は --raw で False にする
    max_tiles_per_call: int = DEFAULT_MAX_TILES_PER_CALL
    jobs: int = 1
    backend: str = DEFAULT_BACKEND
    model: str | None = None
    api_key: str | None = None
    strict_json: bool = False
    session: str | None = None
    # usage.jsonl / meta.json に書くステップ名。CLI 引数ではなく
    # `run_analysis` がプロンプト名から決める（`step_for_prompt`）
    step: str = OTHER_STEP
    force: bool = False  # --force: 既存出力があっても再解析する
    strict: bool = False  # --strict: 1件でも失敗したら終了コード1にする（既定は全件失敗のときだけ）
    # ネイティブ動画クリップ入力
    ranges: str | None = None
    video: str | None = None
    start: float | None = None
    end: float | None = None
    fps: float | None = None
    dry_run: bool = False

    @classmethod
    def from_namespace(cls, namespace: Any) -> "AnalyzeOptions":
        values = vars(namespace)
        known = {f.name for f in dataclasses.fields(cls)}
        options = cls(**{key: value for key, value in values.items() if key in known})
        if "raw" in values:  # --raw は expect_json の反転
            options.expect_json = not values["raw"]
        return options


# --- manifest 読み込みとタイル解決 --------------------------------------------


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], Path]:
    manifest = read_json(manifest_path)
    tiles = manifest.get("tiles")
    if not tiles:
        raise RuntimeError(f"manifest に tiles がありません: {manifest_path}")
    return manifest, manifest_path.parent


def resolve_tile_path(manifest_dir: Path, tile: dict[str, Any]) -> Path:
    if tile.get("path"):
        path = Path(tile["path"]).expanduser()
        if path.exists():
            return path.resolve()
    candidate = manifest_dir / tile["filename"]
    if candidate.exists():
        return candidate.resolve()
    raise RuntimeError(f"タイル画像が見つかりません: {tile.get('filename')}")


# --- JSON 抽出 ----------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """コードフェンスを剥がして json.loads する。失敗時は ValueError を送出。"""
    stripped = text.strip()
    match = _FENCE_RE.search(stripped)
    payload = match.group(1) if match else stripped
    return json.loads(payload)


# --- 出力パスの解決 -----------------------------------------------------------


def derive_basename(manifest_path: Path) -> str:
    # multi-tile は <range_dir>/manifest.json、single-tile は <stem>.json
    if manifest_path.stem == "manifest":
        return manifest_path.parent.name
    return manifest_path.stem


def resolve_output_base(
    manifest_path: Path,
    output_dir: str | None,
    output: str | None,
    multiple: bool,
) -> Path:
    """拡張子を含まない出力ベースパス（.../<name>_analysis）を返す。"""
    if output_dir:
        base = derive_basename(manifest_path)
        return (Path(output_dir).expanduser() / f"{base}_analysis").resolve()
    if output:
        if multiple:
            raise RuntimeError(
                "複数 manifest では --output は使えません。--output-dir を使うか省略してください"
            )
        out = Path(output).expanduser()
        return out.with_suffix("").resolve()
    base = derive_basename(manifest_path)
    if manifest_path.stem == "manifest":
        return (manifest_path.parent.parent / f"{base}_analysis").resolve()
    return (manifest_path.parent / f"{base}_analysis").resolve()


# --- タイルのチャンク分割（1タイルオーバーラップ） ---------------------------


def chunk_with_overlap(items: list[Any], max_per: int) -> list[list[Any]]:
    if max_per < 1:
        raise RuntimeError("--max-tiles-per-call は 1 以上を指定してください")
    if len(items) <= max_per:
        return [items]
    step = max(1, max_per - 1)  # 1タイルオーバーラップ
    chunks: list[list[Any]] = []
    index = 0
    while index < len(items):
        chunks.append(items[index : index + max_per])
        if index + max_per >= len(items):
            break
        index += step
    return chunks


# --- 分割JSONの機械的統合 -----------------------------------------------------


def merge_part_results(parsed_parts: list[Any]) -> dict[str, Any]:
    """各パートのJSON(dict)を機械的に統合する。

    - list 値のキーは連結（overview: candidates, detail: events など）
    - スカラ値のキーは `<key>_parts` に配列で保持（summary, hypothesis_verdict など）
    重複しうる旨は SKILL.md で説明。判断はエージェントに委ねる。
    """
    merged: dict[str, Any] = {}
    for part in parsed_parts:
        if not isinstance(part, dict):
            merged.setdefault("_unstructured_parts", []).append(part)
            continue
        for key, value in part.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged.setdefault(f"{key}_parts", []).append(value)
    merged["_merged_from_parts"] = len(parsed_parts)
    return merged


# --- バックエンドの生成 --------------------------------------------------------


def build_backend(options: AnalyzeOptions) -> Any:
    """オプションからバックエンドを作る。

    `--dry-run` では API を呼ばないので、APIキーが無くても組み立てられるように
    ダミーキーを渡す（キー不足で dry-run が落ちると確認の役に立たない）。
    """
    extra: dict[str, Any] = {"strict_json": options.strict_json}
    if options.session:
        extra["cache_dir"] = str(Path(options.session).expanduser() / "uploads")
    api_key = options.api_key or ("dry-run" if options.dry_run else None)
    return get_backend(options.backend, model=options.model, api_key=api_key, **extra)


# --- 監査記録（prompt.txt / meta.json / usage.jsonl） --------------------------


def _write_call_records(
    base: Path,
    part_suffix: str,
    options: AnalyzeOptions,
    responses: list[Any],
    media: list[Media],
    prompt_chars: int,
    json_retries: int,
) -> dict[str, Any]:
    """meta.json を書き、--session があれば usage.jsonl に 1 行追記して meta を返す。

    meta の形と `usage.jsonl` の行は `avs.session.build_call_meta` で統一している
    （merge / audio も同じ形で書く）。`step` はプロンプト名から決まる正確な値。
    """
    meta = build_call_meta(
        responses,
        describe_media(media),
        prompt_chars,
        json_retries=json_retries,
        step=options.step,
    )
    meta_path = base.with_name(f"{base.name}{part_suffix}.meta.json")
    write_json(meta_path, meta)
    append_usage_line(options.session, {"name": f"{base.name}{part_suffix}", **meta})
    return meta


def _finalize_output(
    raw_text_path: Path,
    base: Path,
    part_suffix: str,
    expect_json: bool,
) -> tuple[Any | None, bool]:
    """raw 出力を検証・整形保存する。(parsed, json_ok) を返す。expect_json でなければ (None, True)。"""
    if not expect_json:
        return None, True
    raw_text = raw_text_path.read_text(encoding="utf-8")
    try:
        parsed = extract_json(raw_text)
    except (ValueError, TypeError):
        return None, False
    json_path = base.with_name(f"{base.name}{part_suffix}.json")
    write_json(json_path, parsed)
    return parsed, True


def _log_dry_run(
    backend: Any,
    options: AnalyzeOptions,
    media: list[Media],
    prompt_text: str,
    base: Path,
    part_suffix: str,
    label: str,
) -> None:
    head = prompt_text[:200].replace("\n", " ")
    clips = [item for item in media if isinstance(item, MediaVideoClip)]
    media_desc = (
        " / ".join(describe_media(clips)) if clips else f"画像 {len(media)} 枚"
    )
    log(
        f"  [dry-run] {label}\n"
        f"    backend: {backend.name} / model: {options.model or backend.default_model}\n"
        f"    media: {media_desc}\n"
        f"    prompt(先頭200字): {head}\n"
        f"    出力先: {base.name}{part_suffix}.*"
    )


def _run_call(
    backend: Any,
    options: AnalyzeOptions,
    base: Path,
    part_suffix: str,
    prompt_text: str,
    retry_prompt: str,
    media: list[Media],
    json_schema: dict[str, Any] | None = None,
) -> tuple[Any | None, bool]:
    """1 回分の呼び出し。JSON が不正なら 1 回だけリトライする。(parsed, json_ok) を返す。"""
    ext = ".raw.txt" if options.expect_json else ".txt"
    raw_text_path = base.with_name(f"{base.name}{part_suffix}{ext}")
    base.with_name(f"{base.name}{part_suffix}.prompt.txt").write_text(prompt_text, encoding="utf-8")

    responses = [
        backend.complete(
            LLMRequest(prompt=prompt_text, media=media, json_schema=json_schema, model=options.model)
        )
    ]
    raw_text_path.write_text(responses[-1].text, encoding="utf-8")
    parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)

    json_retries = 0
    if options.expect_json and not json_ok:
        log(f"  [警告] {base.name}{part_suffix}: JSON不正。リトライします")
        json_retries = 1
        base.with_name(f"{base.name}{part_suffix}.retry.prompt.txt").write_text(
            retry_prompt, encoding="utf-8"
        )
        responses.append(
            backend.complete(
                LLMRequest(prompt=retry_prompt, media=media, json_schema=json_schema, model=options.model)
            )
        )
        raw_text_path.write_text(responses[-1].text, encoding="utf-8")
        parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)
        if not json_ok:
            log(f"  [警告] {base.name}{part_suffix}: リトライも失敗。生テキストのみ保存し継続")

    _write_call_records(base, part_suffix, options, responses, media, len(prompt_text), json_retries)
    return parsed, json_ok


# --- 1 manifest の解析 --------------------------------------------------------


def primary_output_path(base: Path, expect_json: bool) -> Path:
    """主要出力（`<base>.json` / raw モードは `<base>.txt`）のパス。"""
    return base.with_suffix(".json" if expect_json else ".txt")


def analyze_one(
    manifest_path: Path,
    options: AnalyzeOptions,
    prompt_body: str,
    objective: str,
    context_text: str | None,
    note: str | None,
    backend: Any,
    base: Path,
    domain: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_path = primary_output_path(base, options.expect_json)
    if output_path.exists() and not options.force and not options.dry_run:
        log(f"[スキップ] 既存: {output_path}")
        return {"manifest": str(manifest_path), "json_failures": 0, "parts": 0, "skipped": True}

    json_schema = api_schema(schema) if options.strict_json else None
    manifest, manifest_dir = load_manifest(manifest_path)
    tiles = manifest["tiles"]
    tile_paths = [resolve_tile_path(manifest_dir, tile) for tile in tiles]

    paired = list(zip(tiles, tile_paths, strict=True))
    chunks = chunk_with_overlap(paired, options.max_tiles_per_call)
    part_count = len(chunks)
    base.parent.mkdir(parents=True, exist_ok=True)

    log(f"[開始] {manifest_path}  タイル{len(tiles)}枚 / {part_count}パート -> {base}.*")

    json_failures = 0
    parsed_parts: list[Any] = []
    raw_texts: list[str] = []

    for part_index, chunk in enumerate(chunks):
        chunk_tiles = [item[0] for item in chunk]
        chunk_paths = [item[1] for item in chunk]
        part_info = (
            (part_index, part_count, chunk_tiles[0]["start_timestamp_sec"], chunk_tiles[-1]["end_timestamp_sec"])
            if part_count > 1
            else None
        )
        tile_context = build_tile_context(manifest, chunk_tiles, chunk_paths, part_info)

        # 出力先: 単一パートは base 直下、複数パートは _partNN
        part_suffix = "" if part_count == 1 else f"_part{part_index:02d}"
        prompt_text = assemble_prompt(
            prompt_body, objective, tile_context, context_text, note, domain=domain, schema=schema
        )
        media: list[Media] = [MediaImage(path=path) for path in chunk_paths]

        if options.dry_run:
            _log_dry_run(
                backend,
                options,
                media,
                prompt_text,
                base,
                part_suffix,
                f"part {part_index + 1}/{part_count}: {manifest_path}",
            )
            continue

        retry_prompt = assemble_prompt(
            prompt_body,
            objective,
            tile_context,
            context_text,
            note,
            retry_hint=True,
            domain=domain,
            schema=schema,
        )
        parsed, json_ok = _run_call(
            backend, options, base, part_suffix, prompt_text, retry_prompt, media, json_schema
        )
        if options.expect_json and not json_ok:
            json_failures += 1
        if options.expect_json and parsed is not None:
            parsed_parts.append(parsed)
        ext = ".raw.txt" if options.expect_json else ".txt"
        raw_texts.append(
            base.with_name(f"{base.name}{part_suffix}{ext}").read_text(encoding="utf-8")
        )

    if options.dry_run:
        return {"manifest": str(manifest_path), "json_failures": 0, "parts": part_count}

    # 複数パートの統合出力
    if part_count > 1:
        if options.expect_json and parsed_parts:
            write_json(base.with_suffix(".json"), merge_part_results(parsed_parts))
        elif not options.expect_json:
            combined = "\n\n---\n\n".join(raw_texts)
            base.with_suffix(".txt").write_text(combined, encoding="utf-8")

    log(f"[完了] {manifest_path}" + (f"  JSON失敗{json_failures}件" if json_failures else ""))
    return {"manifest": str(manifest_path), "json_failures": json_failures, "parts": part_count}


# --- 1 範囲（ネイティブ動画クリップ）の解析 -----------------------------------


@dataclass
class ClipJob:
    """ネイティブ経路の 1 範囲。"""

    label: str
    video: Path
    start_sec: float
    end_sec: float
    fps: float | None
    note: str | None
    base: Path


def analyze_clip(
    job: ClipJob,
    options: AnalyzeOptions,
    prompt_body: str,
    objective: str,
    context_text: str | None,
    backend: Any,
    domain: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_path = primary_output_path(job.base, options.expect_json)
    if output_path.exists() and not options.force and not options.dry_run:
        log(f"[スキップ] 既存: {output_path}")
        return {"label": job.label, "json_failures": 0, "parts": 0, "skipped": True}

    json_schema = api_schema(schema) if options.strict_json else None
    job.base.parent.mkdir(parents=True, exist_ok=True)
    clip_context = build_clip_context(job.start_sec, job.end_sec, job.fps)
    prompt_text = assemble_prompt(
        prompt_body, objective, clip_context, context_text, job.note, domain=domain, schema=schema
    )
    media: list[Media] = [
        MediaVideoClip(
            video=job.video,
            start_sec=job.start_sec,
            end_sec=job.end_sec,
            fps=job.fps,
        )
    ]

    if options.dry_run:
        _log_dry_run(backend, options, media, prompt_text, job.base, "", f"range {job.label}")
        return {"label": job.label, "json_failures": 0, "parts": 1}

    log(f"[開始] range {job.label} ({job.start_sec:.1f}s-{job.end_sec:.1f}s) -> {job.base}.*")
    retry_prompt = assemble_prompt(
        prompt_body,
        objective,
        clip_context,
        context_text,
        job.note,
        retry_hint=True,
        domain=domain,
        schema=schema,
    )
    _, json_ok = _run_call(
        backend, options, job.base, "", prompt_text, retry_prompt, media, json_schema
    )
    json_failures = 0 if json_ok or not options.expect_json else 1
    log(f"[完了] range {job.label}" + ("  JSON失敗1件" if json_failures else ""))
    return {"label": job.label, "json_failures": json_failures, "parts": 1}


def _clip_output_base(options: AnalyzeOptions, config_output_dir: str | None, label: str) -> Path:
    if options.output_dir:
        return (Path(options.output_dir).expanduser() / f"{label}_analysis").resolve()
    if config_output_dir:
        return (Path(config_output_dir).expanduser() / f"{label}_analysis").resolve()
    if options.output:
        return Path(options.output).expanduser().with_suffix("").resolve()
    raise RuntimeError(
        "出力先が決まりません。--output-dir を指定するか、config に output_dir を書いてください"
    )


def collect_clip_jobs(options: AnalyzeOptions) -> list[ClipJob]:
    """`--ranges` / `--video --start --end` から解析対象のクリップを列挙する。"""
    from avs.ranges import load_config, merge_defaults, resolve_video_path  # noqa: PLC0415

    if options.ranges and options.video:
        raise RuntimeError("--ranges と --video は同時に指定できません")

    jobs: list[ClipJob] = []

    if options.ranges:
        config_path = Path(options.ranges).expanduser().resolve()
        config = load_config(config_path)
        video = resolve_video_path(config, config_path.parent)
        duration = _safe_duration(video)
        config_output_dir = config.get("output_dir")
        for index, entry in enumerate(config["ranges"]):
            merged = merge_defaults(config, entry)
            if merged.get("timestamps"):
                raise RuntimeError(
                    "ネイティブ経路（--ranges）は timestamps 指定の zoom 範囲に対応していません"
                )
            pad = float(merged.get("pad", 0.0) or 0.0)
            start = max(0.0, float(merged["start"]) - pad)
            end = float(merged["end"]) + pad
            if duration is not None:
                end = min(end, duration)
            fallback_label = f"range_{index:02d}"
            label = str(merged.get("label") or fallback_label)
            slug = safe_slug(label, fallback_label)
            jobs.append(
                ClipJob(
                    label=label,
                    video=video,
                    start_sec=start,
                    end_sec=end,
                    fps=float(merged.get("fps", DEFAULT_CLIP_FPS)),
                    note=merged.get("note"),
                    base=_clip_output_base(options, config_output_dir, slug),
                )
            )
        if not jobs:
            raise RuntimeError(f"ranges が空です: {config_path}")
        _check_output_collisions(jobs)
        return jobs

    video = Path(options.video).expanduser().resolve()
    if not video.exists():
        raise RuntimeError(f"動画ファイルが見つかりません: {video}")
    duration = _safe_duration(video)
    start = float(options.start) if options.start is not None else 0.0
    end = float(options.end) if options.end is not None else (duration or 0.0)
    if end <= start:
        raise RuntimeError(f"--end は --start より大きい必要があります: {start} - {end}")
    label = f"range_{format_float_for_path(start)}_{format_float_for_path(end)}"
    jobs.append(
        ClipJob(
            label=label,
            video=video,
            start_sec=start,
            end_sec=end,
            fps=float(options.fps) if options.fps is not None else DEFAULT_CLIP_FPS,
            note=None,
            base=_clip_output_base(options, None, label),
        )
    )
    return jobs


def _check_output_collisions(jobs: list[ClipJob]) -> None:
    seen: dict[Path, str] = {}
    for job in jobs:
        if job.base in seen:
            raise RuntimeError(
                f"出力先が衝突します: {job.base}.* （{seen[job.base]} と {job.label}）。"
                "--output ではなく --output-dir を使うか、label を分けてください"
            )
        seen[job.base] = job.label


def _safe_duration(video: Path) -> float | None:
    """ffprobe が使えない環境でも動くように、失敗したら None を返す。"""
    try:
        return probe_duration_sec(video)
    except RuntimeError:
        return None


# --- manifest 列挙と note の対応付け -------------------------------------------


def collect_manifests(summary: str | None, manifests: list[str] | None) -> list[tuple[Path, str | None]]:
    """(manifest_path, note) のリストを返す。summary の results[].note を対応付ける。"""
    pairs: list[tuple[Path, str | None]] = []
    if summary:
        summary_path = Path(summary).expanduser().resolve()
        summary_data = read_json(summary_path)
        for result in summary_data.get("results", []):
            manifest_path = result.get("manifest_path")
            if manifest_path:
                pairs.append((Path(manifest_path).expanduser(), result.get("note")))
    if manifests:
        pairs.extend((Path(manifest).expanduser(), None) for manifest in manifests)
    if not pairs:
        raise RuntimeError("--manifest / --summary / --ranges / --video のいずれかを指定してください")

    seen: set[Path] = set()
    unique: list[tuple[Path, str | None]] = []
    for path, note in pairs:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((resolved, note))
    return unique


# --- 実行 ----------------------------------------------------------------------

# 1 ジョブの実行結果: (job, result, error)。result は worker の戻り値（成功時）、
# error は例外メッセージ（失敗時）。両方 None にはならない。
JobOutcome = tuple[Any, dict[str, Any] | None, str | None]


def _run_jobs(jobs: list[Any], worker: Any, jobs_count: int) -> list[JobOutcome]:
    """worker を直列 or 並列で回す。個々の失敗は例外を握って結果に含め、続行する。"""

    def run_one(job: Any) -> JobOutcome:
        try:
            return job, worker(job), None
        except Exception as error:  # noqa: BLE001 - 個別に隔離してまとめて報告する
            log(f"  失敗: {error}")
            return job, None, str(error)

    if jobs_count == 1 or len(jobs) == 1:
        outcomes = [run_one(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=jobs_count) as executor:
            futures = [executor.submit(run_one, job) for job in jobs]
            outcomes = [future.result() for future in as_completed(futures)]

    json_failure_total = sum(
        result["json_failures"] for _, result, error in outcomes if result and not error
    )
    if json_failure_total:
        log(f"JSON検証に失敗したパート: {json_failure_total} 件（生テキストは保存済み）")
    return outcomes


def _job_status(result: dict[str, Any] | None, error: str | None) -> str:
    if error:
        return "error"
    if result and result.get("skipped"):
        return "skipped"
    return "ok"


def _report_failures_and_decide_exit_code(
    outcomes: list[JobOutcome], total: int, strict: bool
) -> int:
    failures = [(job, error) for job, _, error in outcomes if error]
    if failures:
        log(f"{len(failures)}/{total} 件の解析に失敗しました")
    if total == 0:
        return 0
    if strict and failures:
        return 1
    if len(failures) == total:
        return 1
    return 0


def _write_back_summary(
    summary_path: Path, outcomes_by_manifest: dict[Path, tuple[str, str | None, Path | None]]
) -> None:
    """`--summary` の batch_summary.json の results[] に解析の結果を書き戻す。

    書き戻すキーは `analysis_status` / `analysis_error` / `analysis_output`。
    `analysis_output` は実際に書いた主要出力（`--raw` のときは `.txt`、既定は `.json`）を指す
    （`results[].output` はタイル画像のパスなので上書きしない）。

    書き込みは一時ファイル + `os.replace` で原子的に行う。
    **複数プロセスから同じ summary を更新しないこと**（同一プロセス内はロックで直列化するが、
    プロセス間の排他はしていないので、後から書いた方の内容で上書きされる）。
    """
    with _summary_lock:
        try:
            summary_data = read_json(summary_path)
        except (OSError, ValueError) as error:
            log(f"  [警告] {summary_path} への書き戻しに失敗しました: {error}")
            return
        for result in summary_data.get("results", []):
            manifest_path = result.get("manifest_path")
            if not manifest_path:
                continue
            key = Path(manifest_path).expanduser().resolve()
            outcome = outcomes_by_manifest.get(key)
            if outcome is None:
                continue
            status, error, analysis_output = outcome
            result["analysis_status"] = status
            if analysis_output is not None:
                result["analysis_output"] = str(analysis_output)
            if error:
                result["analysis_error"] = error
            else:
                result.pop("analysis_error", None)
        write_json_atomic(summary_path, summary_data)
    log(f"analysis status を書き戻し: {summary_path}")


def _write_analysis_summary(
    output_dir: Path, video: Path | None, outcomes: list[JobOutcome], expect_json: bool = True
) -> Path:
    """`--ranges` 経路用: config 横に analysis_summary.json を書く（results[]{label,output,status,error?}）。

    `output` は実際に書いた主要出力（`--raw` のときは `.txt`、既定は `.json`）を指す。
    書き込みは一時ファイル + `os.replace` で原子的に行う
    （**複数プロセスから同じ summary を更新しないこと**。排他はしていない）。
    """
    results = []
    for job, result, error in outcomes:
        status = _job_status(result, error)
        entry: dict[str, Any] = {
            "label": job.label,
            "output": str(primary_output_path(job.base, expect_json)),
            "status": status,
        }
        if error:
            entry["error"] = error
        results.append(entry)
    summary_path = output_dir / "analysis_summary.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(summary_path, {"video": str(video) if video else None, "results": results})
    log(f"analysis summary: {summary_path}")
    return summary_path


def _probe_path_for_session(options: AnalyzeOptions) -> Path | None:
    """`--session` 省略時にセッションを探す起点パス。出力先候補から順に選ぶ。"""
    if options.output_dir:
        return Path(options.output_dir).expanduser()
    if options.output:
        return Path(options.output).expanduser().parent
    if options.summary:
        return Path(options.summary).expanduser().parent
    if options.manifest:
        return Path(options.manifest[0]).expanduser().parent
    if options.ranges:
        return Path(options.ranges).expanduser().parent
    if options.video:
        return Path(options.video).expanduser().parent
    return None


def _attach_session_if_found(options: AnalyzeOptions) -> None:
    """`--session` が省略されたとき、出力先から上方向に session.json を探して補う。"""
    if options.session:
        return
    probe_path = _probe_path_for_session(options)
    if probe_path is None:
        return
    session = Session.find(probe_path)
    if session:
        options.session = str(session.root)
        log(f"[セッション] 検出: {session.root}")


def run_analysis(options: AnalyzeOptions) -> int:
    if options.jobs < 1:
        raise RuntimeError("--jobs は 1 以上を指定してください")

    prompt_path = Path(options.prompt).expanduser().resolve()
    prompt_body = read_text_utf8(prompt_path).strip()
    options.step = step_for_prompt(prompt_path)
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE
    context_text = resolve_text_arg(options.context)

    domain = load_domain(options.domain) if options.domain else None
    schema = schema_for_prompt(prompt_path)
    if schema is None and SCHEMA_PLACEHOLDER in prompt_body:
        log(
            f"[警告] {prompt_path.name} は既知のプロンプト名でないため "
            f"{SCHEMA_PLACEHOLDER} を置換できません"
        )

    _attach_session_if_found(options)
    backend = build_backend(options)
    native = bool(options.ranges or options.video)

    if native:
        if options.manifest or options.summary:
            raise RuntimeError(
                "--ranges / --video（ネイティブ動画クリップ）と --manifest / --summary（タイル）は"
                "同時に指定できません"
            )
        if not backend.supports_video_clip:
            raise RuntimeError(
                f"{backend.name} バックエンドは動画クリップ非対応です。"
                "--ranges / --video には --backend gemini を使ってください"
            )
        clip_jobs = collect_clip_jobs(options)
        log(
            f"範囲 件数: {len(clip_jobs)} / backend={backend.name}"
            f" / model={options.model or backend.default_model} / jobs={options.jobs}"
        )

        def clip_worker(job: ClipJob) -> dict[str, Any]:
            return analyze_clip(
                job, options, prompt_body, objective, context_text, backend, domain=domain, schema=schema
            )

        outcomes = _run_jobs(clip_jobs, clip_worker, options.jobs)
        if clip_jobs and not options.dry_run:
            _write_analysis_summary(
                clip_jobs[0].base.parent, clip_jobs[0].video, outcomes, options.expect_json
            )
        return _report_failures_and_decide_exit_code(outcomes, len(clip_jobs), options.strict)

    manifests = collect_manifests(options.summary, options.manifest)
    multiple = len(manifests) > 1

    # 出力先ベースを事前解決し、主要出力パスの衝突を検査する
    tile_jobs: list[tuple[Path, str | None, Path]] = []
    primary_outputs: dict[Path, Path] = {}
    for manifest_path, note in manifests:
        base = resolve_output_base(manifest_path, options.output_dir, options.output, multiple)
        if base in primary_outputs:
            raise RuntimeError(
                f"出力先が衝突します: {base}.* （{manifest_path} と {primary_outputs[base]}）"
            )
        primary_outputs[base] = manifest_path
        tile_jobs.append((manifest_path, note, base))

    log(
        f"manifest 件数: {len(tile_jobs)} / backend={backend.name}"
        f" / model={options.model or backend.default_model}"
        f" / jobs={options.jobs} / max-tiles-per-call={options.max_tiles_per_call}"
    )

    def tile_worker(job: tuple[Path, str | None, Path]) -> dict[str, Any]:
        manifest_path, note, base = job
        return analyze_one(
            manifest_path,
            options,
            prompt_body,
            objective,
            context_text,
            note,
            backend,
            base,
            domain=domain,
            schema=schema,
        )

    outcomes = _run_jobs(tile_jobs, tile_worker, options.jobs)
    if options.summary and not options.dry_run:
        outcomes_by_manifest = {
            job[0]: (
                _job_status(result, error),
                error,
                primary_output_path(job[2], options.expect_json),
            )
            for job, result, error in outcomes
        }
        _write_back_summary(Path(options.summary).expanduser().resolve(), outcomes_by_manifest)
    return _report_failures_and_decide_exit_code(outcomes, len(tile_jobs), options.strict)
