#!/usr/bin/env python
"""タイル manifest の解析。

1つの manifest = 原則1回の API 呼び出し（含まれる全タイルをまとめて渡す）。
タイル数が多い場合は時系列順に1タイル重ねてチャンク分割し、複数回呼び出す。

- `collect_manifests`: `--manifest` / `--summary` から (manifest, note) を列挙
- `resolve_output_base`: `<name>_analysis` までの出力ベースパスを決める
- `chunk_with_overlap`: タイルのチャンク分割
- `analyze_one`: 1 manifest を解析（パートごとに aitool 実行 → JSON 検証 → 統合）
- `run_analysis`: 複数 manifest の並列実行と結果集計
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avs.common import read_json, write_json
from avs.prompts import (
    DEFAULT_OBJECTIVE,
    assemble_prompt,
    build_tile_context,
    resolve_text_arg,
)

DEFAULT_MAX_TILES_PER_CALL = 8
DEFAULT_MODEL = "google/gemini-3.5-flash"

_print_lock = threading.Lock()


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
    expect_json: bool = False
    max_tiles_per_call: int = DEFAULT_MAX_TILES_PER_CALL
    jobs: int = 1
    model: str = DEFAULT_MODEL
    aitool: str | None = None
    api_key: str | None = None
    dry_run: bool = False

    @classmethod
    def from_namespace(cls, namespace: Any) -> "AnalyzeOptions":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})


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


# --- aitool 実行 --------------------------------------------------------------


def resolve_aitool(command: str | None) -> str:
    if command:
        return command
    found = shutil.which("aitool")
    if found:
        return found
    raise RuntimeError(
        "aitool が見つかりません。`uv tool install git+https://github.com/Nu424/aitool-iroiro.git` などで導入してください"
    )


def build_command(
    aitool_path: str,
    model: str,
    prompt_text: str,
    tile_paths: list[Path],
    output_path: Path,
    api_key: str | None,
) -> list[str]:
    command = [
        aitool_path,
        "recognize-image",
        "--model",
        model,
        "--text",
        prompt_text,
        "--output",
        str(output_path),
    ]
    if api_key:
        command.extend(["--api-key", api_key])
    for tile_path in tile_paths:
        command.extend(["--image", str(tile_path)])
    return command


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


# --- 1 manifest の解析 --------------------------------------------------------


def _run_aitool(command: list[str], manifest_path: Path) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"aitool recognize-image に失敗しました (exit {completed.returncode}): {manifest_path}"
        )


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


def analyze_one(
    manifest_path: Path,
    options: AnalyzeOptions,
    prompt_body: str,
    objective: str,
    context_text: str | None,
    note: str | None,
    aitool_path: str,
    base: Path,
) -> dict[str, Any]:
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
        ext = ".raw.txt" if options.expect_json else ".txt"
        raw_text_path = base.with_name(f"{base.name}{part_suffix}{ext}")

        prompt_text = assemble_prompt(prompt_body, objective, tile_context, context_text, note)
        command = build_command(
            aitool_path, options.model, prompt_text, chunk_paths, raw_text_path, options.api_key
        )

        if options.dry_run:
            log(f"  [dry-run] part {part_index + 1}/{part_count}: " + " ".join(command))
            continue

        _run_aitool(command, manifest_path)
        parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)

        if options.expect_json and not json_ok:
            # 1回だけリトライ
            log(f"  [警告] part {part_index + 1}: JSON不正。リトライします")
            retry_prompt = assemble_prompt(
                prompt_body, objective, tile_context, context_text, note, retry_hint=True
            )
            retry_command = build_command(
                aitool_path, options.model, retry_prompt, chunk_paths, raw_text_path, options.api_key
            )
            _run_aitool(retry_command, manifest_path)
            parsed, json_ok = _finalize_output(raw_text_path, base, part_suffix, options.expect_json)
            if not json_ok:
                json_failures += 1
                log(f"  [警告] part {part_index + 1}: リトライも失敗。生テキストのみ保存し継続")

        if options.expect_json and parsed is not None:
            parsed_parts.append(parsed)
        if not options.dry_run:
            raw_texts.append(raw_text_path.read_text(encoding="utf-8"))

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
        raise RuntimeError("--manifest または --summary を指定してください")

    seen: set[Path] = set()
    unique: list[tuple[Path, str | None]] = []
    for path, note in pairs:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((resolved, note))
    return unique


# --- 複数 manifest の実行 ------------------------------------------------------


def run_analysis(options: AnalyzeOptions) -> int:
    if options.jobs < 1:
        raise RuntimeError("--jobs は 1 以上を指定してください")

    prompt_path = Path(options.prompt).expanduser().resolve()
    prompt_body = prompt_path.read_text(encoding="utf-8").strip()
    objective = resolve_text_arg(options.objective) or DEFAULT_OBJECTIVE
    context_text = resolve_text_arg(options.context)

    manifests = collect_manifests(options.summary, options.manifest)
    multiple = len(manifests) > 1
    aitool_path = resolve_aitool(options.aitool) if not options.dry_run else (options.aitool or "aitool")

    # 出力先ベースを事前解決し、主要出力パスの衝突を検査する
    jobs: list[tuple[Path, str | None, Path]] = []
    primary_outputs: dict[Path, Path] = {}
    for manifest_path, note in manifests:
        base = resolve_output_base(manifest_path, options.output_dir, options.output, multiple)
        if base in primary_outputs:
            raise RuntimeError(
                f"出力先が衝突します: {base}.* （{manifest_path} と {primary_outputs[base]}）"
            )
        primary_outputs[base] = manifest_path
        jobs.append((manifest_path, note, base))

    log(f"manifest 件数: {len(jobs)} / jobs={options.jobs} / max-tiles-per-call={options.max_tiles_per_call}")

    failures: list[tuple[Path, str]] = []
    json_failure_total = 0

    def worker(manifest_path: Path, note: str | None, base: Path) -> dict[str, Any]:
        return analyze_one(
            manifest_path, options, prompt_body, objective, context_text, note, aitool_path, base
        )

    if options.jobs == 1 or len(jobs) == 1:
        for manifest_path, note, base in jobs:
            try:
                report = worker(manifest_path, note, base)
                json_failure_total += report["json_failures"]
            except Exception as error:  # noqa: BLE001 - まとめて報告するため継続
                log(f"  失敗: {error}")
                failures.append((manifest_path, str(error)))
    else:
        with ThreadPoolExecutor(max_workers=options.jobs) as executor:
            future_map = {
                executor.submit(worker, manifest_path, note, base): manifest_path
                for manifest_path, note, base in jobs
            }
            for future in as_completed(future_map):
                manifest_path = future_map[future]
                try:
                    report = future.result()
                    json_failure_total += report["json_failures"]
                except Exception as error:  # noqa: BLE001
                    log(f"  失敗: {error}")
                    failures.append((manifest_path, str(error)))

    if json_failure_total:
        log(f"JSON検証に失敗したパート: {json_failure_total} 件（生テキストは保存済み）")
    if failures:
        raise RuntimeError(f"{len(failures)}/{len(jobs)} 件の解析に失敗しました")
    return 0
