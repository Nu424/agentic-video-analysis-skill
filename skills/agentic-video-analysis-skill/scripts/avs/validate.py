#!/usr/bin/env python
"""detail 解析結果の後段バリデーション。

**削除はしない。フラグを付けるだけ。** 採用・不採用の判断はエージェント（人間）に残す。

入力は detail の `<name>_analysis.json`、対応する manifest（あれば）、`domain.json`（あれば）。
出力は `<name>_validated.json`（元 JSON のコピーに各 event の `flags` と `confidence_adjusted`
を足したもの）と、`validation_report.json`（フラグ種別ごとの件数と該当項目の一覧）。

| フラグ | 条件 |
|-------|------|
| `negative_match`       | `title + summary + visual` が `negatives[].pattern` にマッチ（`window` があれば時間も重なる） |
| `no_cell_evidence`     | タイル経路で `visual[]` にセルラベル（`F<番号>` / `t=<秒>s`）の引用が1つも無い |
| `evidence_out_of_range`| `visual[]` が引用した時刻が、manifest の範囲（pad 込み）の外 |
| `boundary`             | `start_sec` / `end_sec` が範囲端から1フレーム間隔（1/fps）以内 |
| `duration_outlier`     | `end_sec < start_sec`、または長さが `max_event_sec` 超 |
| `low_confidence`       | 元の `confidence` が `low` |
| `hypothesis_rejected`  | `hypothesis_verdict == "rejected"`（トップレベルの `flags` に付く） |

`confidence_adjusted` は元の `confidence` から
`negative_match` / `evidence_out_of_range` があれば `low`、
`no_cell_evidence` があれば1段下げる。
"""

from __future__ import annotations

import copy
import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avs.common import read_json, write_json
from avs.prompts import load_domain
from avs.session import Session

DEFAULT_MAX_EVENT_SEC = 30.0
REPORT_FILENAME = "validation_report.json"

FLAG_NEGATIVE_MATCH = "negative_match"
FLAG_NO_CELL_EVIDENCE = "no_cell_evidence"
FLAG_EVIDENCE_OUT_OF_RANGE = "evidence_out_of_range"
FLAG_BOUNDARY = "boundary"
FLAG_DURATION_OUTLIER = "duration_outlier"
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_HYPOTHESIS_REJECTED = "hypothesis_rejected"

ALL_FLAGS = (
    FLAG_NEGATIVE_MATCH,
    FLAG_NO_CELL_EVIDENCE,
    FLAG_EVIDENCE_OUT_OF_RANGE,
    FLAG_BOUNDARY,
    FLAG_DURATION_OUTLIER,
    FLAG_LOW_CONFIDENCE,
    FLAG_HYPOTHESIS_REJECTED,
)

CONFIDENCE_ORDER = ("high", "medium", "low")

# 秒数の比較に使う許容誤差（浮動小数の丸め対策）
EPSILON = 1e-6

# `t=39.4s` / `t=1:02.5` / `t=1:02.5s` を拾う
_EVIDENCE_TIME_RE = re.compile(r"t\s*=\s*(?:(\d+)\s*:\s*)?(\d+(?:\.\d+)?)\s*s?", re.IGNORECASE)
# セルラベルの引用（`F12` など）
_CELL_LABEL_RE = re.compile(r"\bF\d+", re.IGNORECASE)


# --- 小さなユーティリティ -------------------------------------------------------


def parse_evidence_times(texts: list[str] | None) -> list[float]:
    """根拠テキストから `t=<秒>s` / `t=<分>:<秒>` を抽出して秒のリストにする。"""
    times: list[float] = []
    for text in texts or []:
        if not isinstance(text, str):
            continue
        for minutes, seconds in _EVIDENCE_TIME_RE.findall(text):
            value = float(seconds)
            if minutes:
                value += float(minutes) * 60.0
            times.append(value)
    return times


def has_cell_reference(texts: list[str] | None) -> bool:
    """`F<番号>` または `t=` の引用があるか。"""
    for text in texts or []:
        if not isinstance(text, str):
            continue
        if _CELL_LABEL_RE.search(text) or "t=" in text.replace(" ", ""):
            return True
    return False


def is_tile_manifest(manifest: dict[str, Any] | None) -> bool:
    """タイル経路の manifest か（zoom manifest はセルラベル系ルールの対象外）。"""
    if not manifest:
        return False
    approach = str(manifest.get("approach") or "")
    return "tile" in approach and "zoom" not in approach


def manifest_range(manifest: dict[str, Any] | None) -> tuple[float, float] | None:
    """manifest の `extraction` から、根拠時刻を照合する範囲 `[start - pad, end + pad]` を返す。

    `start_sec` / `end_sec` は既に pad 済み（かつ動画長でクリップ済み）なので、
    要求範囲 ± pad との和集合をとって、広い方を許容範囲にする。
    """
    if not manifest:
        return None
    extraction = manifest.get("extraction") or {}
    start = extraction.get("start_sec")
    end = extraction.get("end_sec")
    if start is None or end is None:
        return None
    low = float(start)
    high = float(end)
    pad = float(extraction.get("pad_sec") or 0.0)
    requested_start = extraction.get("requested_start_sec")
    requested_end = extraction.get("requested_end_sec")
    if requested_start is not None:
        low = min(low, float(requested_start) - pad)
    if requested_end is not None:
        high = max(high, float(requested_end) + pad)
    if high < low:
        return None
    return low, high


def manifest_fps(manifest: dict[str, Any] | None) -> float | None:
    if not manifest:
        return None
    fps = (manifest.get("extraction") or {}).get("fps")
    if fps is None:
        return None
    try:
        value = float(fps)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def downgrade_confidence(confidence: str, steps: int = 1) -> str:
    """`high` -> `medium` -> `low` と下げる（`low` より下は無い）。"""
    if confidence not in CONFIDENCE_ORDER:
        return confidence
    index = CONFIDENCE_ORDER.index(confidence)
    return CONFIDENCE_ORDER[min(index + steps, len(CONFIDENCE_ORDER) - 1)]


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start <= b_end + EPSILON and b_start <= a_end + EPSILON


def _event_text(event: dict[str, Any]) -> str:
    parts = [str(event.get("title") or ""), str(event.get("summary") or "")]
    parts.extend(str(item) for item in (event.get("visual") or []) if isinstance(item, str))
    return "\n".join(part for part in parts if part)


def _event_span(event: dict[str, Any]) -> tuple[float | None, float | None]:
    def as_float(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    return as_float(event.get("start_sec")), as_float(event.get("end_sec"))


# --- ルール本体 ------------------------------------------------------------------


def matched_negatives(event: dict[str, Any], domain: dict[str, Any] | None) -> list[str]:
    """`negatives[].pattern` にマッチしたネガティブ事象名を返す。"""
    if not domain:
        return []
    text = _event_text(event)
    if not text:
        return []
    start_sec, end_sec = _event_span(event)
    matched: list[str] = []
    for entry in domain.get("negatives") or []:
        if not isinstance(entry, dict):
            continue
        pattern = entry.get("pattern")
        if not pattern:
            continue
        try:
            if not re.search(pattern, text, re.IGNORECASE):
                continue
        except re.error:
            continue  # 形式検証は load_domain が行う。ここでは集計を止めない
        window = entry.get("window")
        if (
            window
            and start_sec is not None
            and end_sec is not None
            and not _overlaps(start_sec, end_sec, float(window[0]), float(window[1]))
        ):
            continue
        matched.append(str(entry.get("name") or pattern))
    return matched


def validate_event(
    event: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    domain: dict[str, Any] | None = None,
    max_event_sec: float = DEFAULT_MAX_EVENT_SEC,
) -> dict[str, Any]:
    """1 event を検証し、`flags` と `confidence_adjusted` を足したコピーを返す。"""
    checked = copy.deepcopy(event)
    flags: list[str] = []

    visual = [item for item in (checked.get("visual") or []) if isinstance(item, str)]
    start_sec, end_sec = _event_span(checked)

    negatives = matched_negatives(checked, domain)
    if negatives:
        flags.append(FLAG_NEGATIVE_MATCH)
        checked["negative_matches"] = negatives

    tile_manifest = is_tile_manifest(manifest)
    if tile_manifest and not has_cell_reference(visual):
        flags.append(FLAG_NO_CELL_EVIDENCE)

    allowed_range = manifest_range(manifest)
    if allowed_range is not None:
        low, high = allowed_range
        outside = [
            value
            for value in parse_evidence_times(visual)
            if value < low - EPSILON or value > high + EPSILON
        ]
        if outside:
            flags.append(FLAG_EVIDENCE_OUT_OF_RANGE)
            checked["evidence_times_out_of_range"] = outside

    fps = manifest_fps(manifest)
    if allowed_range is not None and fps:
        low, high = allowed_range
        tolerance = 1.0 / fps
        touches = [
            value
            for value in (start_sec, end_sec)
            if value is not None
            and (abs(value - low) <= tolerance + EPSILON or abs(value - high) <= tolerance + EPSILON)
        ]
        if touches:
            flags.append(FLAG_BOUNDARY)

    if start_sec is not None and end_sec is not None:
        duration = end_sec - start_sec
        if duration < -EPSILON or duration > max_event_sec + EPSILON:
            flags.append(FLAG_DURATION_OUTLIER)

    confidence = checked.get("confidence")
    if confidence == "low":
        flags.append(FLAG_LOW_CONFIDENCE)

    adjusted = confidence if isinstance(confidence, str) and confidence in CONFIDENCE_ORDER else None
    if adjusted is not None:
        if FLAG_NO_CELL_EVIDENCE in flags:
            adjusted = downgrade_confidence(adjusted)
        if FLAG_NEGATIVE_MATCH in flags or FLAG_EVIDENCE_OUT_OF_RANGE in flags:
            adjusted = "low"

    checked["flags"] = flags
    checked["confidence_adjusted"] = adjusted
    return checked


def validate_analysis(
    analysis: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    domain: dict[str, Any] | None = None,
    max_event_sec: float = DEFAULT_MAX_EVENT_SEC,
) -> dict[str, Any]:
    """解析 JSON 全体を検証したコピーを返す（元データは削らない）。"""
    validated = copy.deepcopy(analysis)
    events = validated.get("events")
    validated["events"] = [
        validate_event(event, manifest, domain, max_event_sec) if isinstance(event, dict) else event
        for event in (events or [])
    ]

    top_flags = [flag for flag in (validated.get("flags") or []) if isinstance(flag, str)]
    if validated.get("hypothesis_verdict") == "rejected" and FLAG_HYPOTHESIS_REJECTED not in top_flags:
        top_flags.append(FLAG_HYPOTHESIS_REJECTED)
    validated["flags"] = top_flags
    validated["validated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return validated


# --- ファイル単位の処理 -----------------------------------------------------------


def validated_path_for(analysis_path: Path, output_dir: Path | None = None) -> Path:
    """`<name>_analysis.json` -> `<name>_validated.json`。"""
    name = analysis_path.stem.removesuffix("_analysis")
    directory = output_dir if output_dir is not None else analysis_path.parent
    return directory / f"{name}_validated.json"


def analysis_path_for_manifest(manifest_path: Path) -> Path:
    """manifest のパスから、対応する `<name>_analysis.json` のパスを組み立てる。

    タイル化の出力規則（複数タイルは `<range_dir>/manifest.json`、
    単一タイルは `<name>.json`）に対応する。
    """
    if manifest_path.stem == "manifest":
        return manifest_path.parent.parent / f"{manifest_path.parent.name}_analysis.json"
    return manifest_path.parent / f"{manifest_path.stem}_analysis.json"


def guess_manifest_path(analysis_path: Path) -> Path | None:
    """`<name>_analysis.json` から対応する manifest を推測する。見つからなければ None。"""
    name = analysis_path.stem.removesuffix("_analysis")
    candidates = [
        analysis_path.parent / name / "manifest.json",
        analysis_path.parent / f"{name}.json",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate != analysis_path:
            return candidate
    return None


def _load_manifest(manifest_path: Path | None) -> dict[str, Any] | None:
    if manifest_path is None or not manifest_path.exists():
        return None
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def validate_file(
    analysis_path: Path,
    manifest_path: Path | None = None,
    domain: dict[str, Any] | None = None,
    max_event_sec: float = DEFAULT_MAX_EVENT_SEC,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """1 ファイルを検証して `<name>_validated.json` を書き、結果の要約を返す。"""
    analysis = read_json(analysis_path)
    if not isinstance(analysis, dict):
        raise RuntimeError(f"解析結果がオブジェクトではありません: {analysis_path}")

    if manifest_path is None:
        manifest_path = guess_manifest_path(analysis_path)
    manifest = _load_manifest(manifest_path)

    validated = validate_analysis(analysis, manifest, domain, max_event_sec)
    output_path = validated_path_for(analysis_path, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, validated)

    return {
        "analysis": str(analysis_path),
        "manifest": str(manifest_path) if manifest_path else None,
        "manifest_used": manifest is not None,
        "output": str(output_path),
        "n_events": len(validated.get("events") or []),
        "validated": validated,
    }


# --- レポート --------------------------------------------------------------------


def build_report(file_results: list[dict[str, Any]]) -> dict[str, Any]:
    """フラグ種別ごとの件数と、該当項目 (file, event index, title, start_sec) の一覧を作る。"""
    counts: dict[str, int] = {flag: 0 for flag in ALL_FLAGS}
    entries: dict[str, list[dict[str, Any]]] = {flag: [] for flag in ALL_FLAGS}
    n_events = 0

    for result in file_results:
        validated = result.get("validated") or {}
        file_name = result.get("analysis")
        for index, event in enumerate(validated.get("events") or []):
            if not isinstance(event, dict):
                continue
            n_events += 1
            for flag in event.get("flags") or []:
                counts[flag] = counts.get(flag, 0) + 1
                entries.setdefault(flag, []).append(
                    {
                        "file": file_name,
                        "event_index": index,
                        "title": event.get("title"),
                        "start_sec": event.get("start_sec"),
                    }
                )
        for flag in validated.get("flags") or []:
            counts[flag] = counts.get(flag, 0) + 1
            entries.setdefault(flag, []).append(
                {
                    "file": file_name,
                    "event_index": None,
                    "title": None,
                    "start_sec": None,
                }
            )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_files": len(file_results),
        "n_events": n_events,
        "flag_counts": counts,
        "flags": entries,
        "files": [
            {
                "analysis": result.get("analysis"),
                "output": result.get("output"),
                "manifest": result.get("manifest"),
                "manifest_used": result.get("manifest_used"),
                "n_events": result.get("n_events"),
            }
            for result in file_results
        ],
    }


def write_report(report: dict[str, Any], report_path: Path) -> Path:
    """`validation_report.json` を書く。既存ファイルがあれば `merge_diff` などの他キーは残す。"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if report_path.exists():
        try:
            loaded = read_json(report_path)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    existing.update(report)
    write_json(report_path, existing)
    return report_path


# --- CLI 本体 --------------------------------------------------------------------


@dataclass
class ValidateOptions:
    """バリデーションの設定。CLI の argparse.Namespace と同じ属性名を持つ。"""

    analysis: list[str] | None = None
    summary: str | None = None
    domain: str | None = None
    max_event_sec: float = DEFAULT_MAX_EVENT_SEC
    report: bool = False
    report_output: str | None = None
    output_dir: str | None = None
    session: str | None = None
    targets: list[tuple[Path, Path | None]] = field(default_factory=list, repr=False)

    @classmethod
    def from_namespace(cls, namespace: Any) -> "ValidateOptions":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in vars(namespace).items() if key in known})


def collect_targets(options: ValidateOptions) -> list[tuple[Path, Path | None]]:
    """(解析JSON, manifest or None) の組を列挙する。"""
    targets: list[tuple[Path, Path | None]] = []
    seen: set[Path] = set()

    if options.summary:
        summary_path = Path(options.summary).expanduser().resolve()
        summary = read_json(summary_path)
        for result in (summary or {}).get("results") or []:
            if not isinstance(result, dict):
                continue
            manifest_value = result.get("manifest_path")
            if not manifest_value:
                continue
            manifest_path = Path(manifest_value).expanduser()
            analysis_path = analysis_path_for_manifest(manifest_path)
            if not analysis_path.exists():
                print(f"[スキップ] 解析結果がありません: {analysis_path}")
                continue
            key = analysis_path.resolve()
            if key in seen:
                continue
            seen.add(key)
            targets.append((analysis_path, manifest_path))

    for value in options.analysis or []:
        analysis_path = Path(value).expanduser()
        if not analysis_path.exists():
            raise RuntimeError(f"解析結果が見つかりません: {analysis_path}")
        key = analysis_path.resolve()
        if key in seen:
            continue
        seen.add(key)
        targets.append((analysis_path, None))

    if not targets:
        raise RuntimeError("対象がありません。--summary または --analysis を指定してください")
    return targets


def resolve_report_path(options: ValidateOptions, first_analysis: Path) -> Path:
    """レポートの出力先。`--report-output` > `<session>/merge/` > 解析結果の隣。"""
    if options.report_output:
        return Path(options.report_output).expanduser().resolve()
    session = (
        Session(Path(options.session).expanduser().resolve())
        if options.session
        else Session.find(first_analysis)
    )
    if session is not None:
        return session.root / "merge" / REPORT_FILENAME
    return first_analysis.parent / REPORT_FILENAME


def run_validation(options: ValidateOptions) -> int:
    targets = options.targets or collect_targets(options)
    domain = load_domain(options.domain) if options.domain else None
    output_dir = Path(options.output_dir).expanduser().resolve() if options.output_dir else None

    print(f"[開始] バリデーション: {len(targets)}件" + ("（domain あり）" if domain else ""))
    file_results: list[dict[str, Any]] = []
    for analysis_path, manifest_path in targets:
        result = validate_file(
            analysis_path,
            manifest_path,
            domain,
            options.max_event_sec,
            output_dir,
        )
        file_results.append(result)
        flagged = sum(1 for event in result["validated"].get("events") or [] if event.get("flags"))
        print(
            f"  {analysis_path.name}: {result['n_events']}件"
            f" / フラグ付き {flagged}件"
            + ("" if result["manifest_used"] else " (manifest 無し: セルラベル系ルールはスキップ)")
            + f" -> {result['output']}"
        )

    report = build_report(file_results)
    if options.report or options.report_output:
        report_path = resolve_report_path(options, targets[0][0])
        write_report(report, report_path)
        print(f"レポート: {report_path}")

    summary_line = " / ".join(
        f"{flag}={count}" for flag, count in report["flag_counts"].items() if count
    )
    print(f"[完了] {report['n_events']}件を検証" + (f"  {summary_line}" if summary_line else ""))
    return 0
