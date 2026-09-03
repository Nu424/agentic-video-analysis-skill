#!/usr/bin/env python
"""セッションディレクトリの管理（session.json / usage.jsonl）とレポート集計。

- `Session`: セッションディレクトリの作成・探索・ステップ記録
- `build_call_meta` / `append_usage_line`: LLM 呼び出し 1 回分の監査記録（`meta.json` と
  `usage.jsonl` の 1 行）を作る。analysis / merge / audio はすべてこれを通す（§4.7）
- `load_usage_records` / `summarize_usage`: `usage.jsonl` の集計（呼び出し回数・トークン・USD・latency）
- `scan_next_actions`: セッション内の `*_analysis.json` / `*_validated.json` /
  `batch_summary.json` / `analysis_summary.json` を走査し、次アクション候補を挙げる
- `estimate_cost`: `ranges/ranges.json` があれば、解析前の概算コストを出す
- `build_report`: 上記をまとめて dict を作る
- `render_report_text`: `build_report` の dict を人が読むテキストにする（ドライバも同じものを出す）

`session_report.py` はこのモジュールを呼ぶだけの薄い CLI。
"""

from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avs.common import probe_duration_sec, read_json, write_json
from avs.cost import estimate_cost_usd, lookup_pricing, sum_usage

SESSION_FILENAME = "session.json"
USAGE_FILENAME = "usage.jsonl"

# --estimate で usage.jsonl に実測が無いときに仮定する 1 タイルあたりトークン数
DEFAULT_TOKENS_PER_TILE_ESTIMATE = 1000
# 単価表に無いモデル用の概算単価（$/1Mトークン。input/outputの単純平均の目安）
FALLBACK_BLENDED_PRICE_PER_M = 2.25

# usage.jsonl の name（例: "cand_a_analysis", "overview_full_analysis"）を
# 大まかなステップに分類するためのキーワード（見つからなければ "other"）。
# `step` を書いていない古い usage.jsonl のためのフォールバック。
STEP_KEYWORDS = ("overview", "chapters", "detail", "zoom", "refine", "audio", "merge")

_usage_lock = threading.Lock()


# --- セッション ------------------------------------------------------------------


@dataclass
class Session:
    """セッションディレクトリ（`session.json` を持つディレクトリ）。"""

    root: Path

    @property
    def session_path(self) -> Path:
        return self.root / SESSION_FILENAME

    @property
    def usage_path(self) -> Path:
        return self.root / USAGE_FILENAME

    @classmethod
    def create(
        cls,
        root: Path | str,
        video: str,
        backend: str,
        model: str | None = None,
        objective: str | None = None,
        domain_path: str | None = None,
    ) -> "Session":
        """セッションディレクトリを作り `session.json` を書く。"""
        root_path = Path(root).expanduser().resolve()
        root_path.mkdir(parents=True, exist_ok=True)

        try:
            duration_sec: float | None = probe_duration_sec(Path(video).expanduser())
        except RuntimeError:
            duration_sec = None

        session = cls(root=root_path)
        write_json(
            session.session_path,
            {
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "video": str(video),
                "duration_sec": duration_sec,
                "backend": backend,
                "model": model,
                "objective": objective,
                "domain": domain_path,
                "steps": [],
            },
        )
        return session

    @classmethod
    def find(cls, start_path: Path | str) -> "Session | None":
        """`start_path` から上方向に `session.json` を探す。見つからなければ None。

        `start_path` はファイル・ディレクトリのどちらでもよく、存在しなくてもよい
        （出力先パスから探すため、まだ作られていないディレクトリを渡すことがある）。
        """
        path = Path(start_path).expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        for candidate in (path, *path.parents):
            if (candidate / SESSION_FILENAME).exists():
                return cls(root=candidate)
        return None

    def load(self) -> dict[str, Any]:
        return read_json(self.session_path)

    def record_step(self, name: str, **info: Any) -> None:
        """`session.json` の `steps[]` に 1 件追記する。"""
        data = self.load()
        step = {
            "name": name,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **info,
        }
        data.setdefault("steps", []).append(step)
        write_json(self.session_path, data)


# --- 監査記録（meta.json / usage.jsonl の 1 行） -------------------------------------


def build_call_meta(
    responses: list[Any],
    media_desc: list[str],
    prompt_chars: int,
    json_retries: int = 0,
    step: str = "other",
) -> dict[str, Any]:
    """LLM 呼び出し 1 回分（JSON リトライ分を含む）の監査メタを作る。

    `responses` は同じ呼び出しの試行（本番 + リトライ）を時系列で渡す。
    usage は `sum_usage` で合算し、latency と retries も全試行の合計にする
    （リトライ分のトークンが集計から抜けるのを防ぐ）。

    キーは analysis / merge / audio の `*.meta.json` で共通。
    `step` は `avs.prompts.step_for_prompt` が決めるステップ名で、
    `usage.jsonl` の集計（`summarize_usage`）が名前の推測をせずに済むようにする。
    """
    usage = sum_usage([response.usage for response in responses])
    model = responses[-1].model
    cost_usd, is_estimate = estimate_cost_usd(model, usage)
    return {
        "backend": responses[-1].backend,
        "model": model,
        "step": step,
        "media": list(media_desc),
        "usage": usage,
        "cost_usd": cost_usd,
        "cost_is_estimate": is_estimate,
        "latency_sec": round(sum(response.latency_sec for response in responses), 3),
        "retries": sum(getattr(response, "retries", 0) or 0 for response in responses) + json_retries,
        "prompt_chars": prompt_chars,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def append_usage_line(session: str | Path | None, record: dict[str, Any]) -> None:
    """`<session>/usage.jsonl` に 1 行追記する（`session` が None なら何もしない）。

    並列実行から呼ばれるのでロックで囲む（同一プロセス内のみ）。
    """
    if not session:
        return
    session_dir = Path(session).expanduser()
    session_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _usage_lock, (session_dir / USAGE_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(line)


# --- usage.jsonl 集計 -------------------------------------------------------------


def load_usage_records(session_dir: Path) -> list[dict[str, Any]]:
    usage_path = session_dir / USAGE_FILENAME
    if not usage_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in usage_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # 壊れた行はスキップ（集計を止めない）
    return records


def classify_step(name: str) -> str:
    """出力名からステップを推測する（`step` を持たない古い usage.jsonl 用のフォールバック）。"""
    lowered = name.lower()
    for keyword in STEP_KEYWORDS:
        if keyword in lowered:
            return keyword
    return "other"


def step_of(record: dict[str, Any]) -> str:
    """usage.jsonl の 1 行のステップ名。`step` があればそれを使う。

    `step` は呼び出し元がプロンプト名から決めた正確な値
    （`cand_00_analysis` のような出力名からのキーワード推測では detail が other に落ちる）。
    """
    step = record.get("step")
    if isinstance(step, str) and step:
        return step
    return classify_step(str(record.get("name") or ""))


def summarize_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """呼び出し回数・トークン内訳・USD（実測/概算を分ける）・latency・ステップ別を集計する。"""
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    actual_cost = 0.0
    estimated_cost = 0.0
    unknown_cost_calls = 0
    latency_sec = 0.0
    by_step: dict[str, dict[str, Any]] = {}

    for record in records:
        usage = record.get("usage") or {}
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
        latency_sec += float(record.get("latency_sec") or 0.0)

        cost = record.get("cost_usd")
        is_estimate = bool(record.get("cost_is_estimate"))
        if cost is None:
            unknown_cost_calls += 1
        elif is_estimate:
            estimated_cost += float(cost)
        else:
            actual_cost += float(cost)

        step = step_of(record)
        bucket = by_step.setdefault(step, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["total_tokens"] += int(usage.get("total_tokens") or 0)
        if cost is not None:
            bucket["cost_usd"] += float(cost)

    for bucket in by_step.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)

    return {
        "calls": len(records),
        "tokens": totals,
        "cost_usd_actual": round(actual_cost, 6),
        "cost_usd_estimated": round(estimated_cost, 6),
        "cost_usd_total": round(actual_cost + estimated_cost, 6),
        "cost_has_estimate": estimated_cost > 0 or unknown_cost_calls > 0,
        "unknown_cost_calls": unknown_cost_calls,
        "latency_sec": round(latency_sec, 3),
        "by_step": by_step,
    }


# --- 次アクション候補 ---------------------------------------------------------------


def _read_json_or_none(path: Path) -> Any | None:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def scan_next_actions(session_dir: Path) -> dict[str, Any]:
    """`*_analysis.json` / `*_validated.json` / `batch_summary.json` / `analysis_summary.json`
    を走査し、次に確認すべき候補（低確信・ズーム対象・仮説反証・失敗範囲・flags）を挙げる。
    """
    low_confidence = 0
    zoom_targets: list[dict[str, Any]] = []
    hypothesis_rejected: list[str] = []

    for path in sorted(session_dir.rglob("*_analysis.json")):
        data = _read_json_or_none(path)
        if not isinstance(data, dict):
            continue
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            if event.get("confidence") == "low":
                low_confidence += 1
            for timestamp in event.get("zoom_targets") or []:
                zoom_targets.append({"file": str(path), "timestamp_sec": timestamp})
        if data.get("hypothesis_verdict") == "rejected":
            hypothesis_rejected.append(str(path))

    validated_flags_count = 0
    for path in sorted(session_dir.rglob("*_validated.json")):
        data = _read_json_or_none(path)
        if not isinstance(data, dict):
            continue
        for event in data.get("events") or []:
            if isinstance(event, dict):
                validated_flags_count += len(event.get("flags") or [])

    failed_ranges: list[dict[str, Any]] = []
    for pattern in ("batch_summary.json", "analysis_summary.json"):
        for path in sorted(session_dir.rglob(pattern)):
            data = _read_json_or_none(path)
            if not isinstance(data, dict):
                continue
            for result in data.get("results") or []:
                if not isinstance(result, dict):
                    continue
                error = result.get("error") or result.get("analysis_error")
                if error:
                    failed_ranges.append(
                        {"file": str(path), "label": result.get("label"), "error": error}
                    )

    return {
        "low_confidence_count": low_confidence,
        "zoom_targets": zoom_targets,
        "hypothesis_rejected": hypothesis_rejected,
        "failed_ranges": failed_ranges,
        "validated_flags_count": validated_flags_count,
    }


# --- 事前見積もり（--estimate） ------------------------------------------------------


def _range_tile_estimate(config: dict[str, Any]) -> tuple[int, float]:
    defaults = config.get("defaults", {})
    n_ranges = 0
    n_tiles = 0.0
    for entry in config.get("ranges", []):
        merged = {**defaults, **entry}
        if merged.get("timestamps"):
            continue  # zoom range はタイル計算の対象外
        n_ranges += 1
        pad = float(merged.get("pad", 0.0) or 0.0)
        span = float(merged["end"]) - float(merged["start"]) + 2 * pad
        fps = float(merged.get("fps", 1.0) or 1.0)
        frames_per_tile = float(merged.get("frames_per_tile", 12) or 12)
        n_tiles += max(1.0, (span * fps) / frames_per_tile)
    return n_ranges, n_tiles


def _average_tokens_per_tile(records: list[dict[str, Any]]) -> tuple[float, bool]:
    samples = []
    for record in records:
        media = record.get("media") or []
        total_tokens = int((record.get("usage") or {}).get("total_tokens") or 0)
        if media and total_tokens:
            samples.append(total_tokens / len(media))
    if samples:
        return statistics.mean(samples), True
    return float(DEFAULT_TOKENS_PER_TILE_ESTIMATE), False


def estimate_cost(session_dir: Path, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """`ranges/ranges.json` があれば、範囲数 × (範囲長 × fps / frames_per_tile) からタイル数を出し、
    1 タイルあたりの平均トークン（usage.jsonl があれば実測平均、無ければ仮定値）×単価表で概算USDを出す。
    `ranges/ranges.json` が無ければ None。
    """
    ranges_path = session_dir / "ranges" / "ranges.json"
    if not ranges_path.exists():
        return None
    config = _read_json_or_none(ranges_path)
    if not isinstance(config, dict):
        return None

    n_ranges, n_tiles = _range_tile_estimate(config)
    avg_tokens_per_tile, tokens_are_measured = _average_tokens_per_tile(records)

    models = [record.get("model") for record in records if record.get("model")]
    model = statistics.mode(models) if models else None
    pricing = lookup_pricing(model)
    blended_price = (
        (float(pricing["input"]) + float(pricing["output"])) / 2
        if pricing
        else FALLBACK_BLENDED_PRICE_PER_M
    )

    estimated_usd = round(n_tiles * avg_tokens_per_tile / 1e6 * blended_price, 4)
    return {
        "n_ranges": n_ranges,
        "n_tiles_estimate": round(n_tiles, 2),
        "avg_tokens_per_tile": round(avg_tokens_per_tile, 1),
        "tokens_are_measured": tokens_are_measured,
        "model_used_for_pricing": model or "(既定単価・概算)",
        "blended_price_per_million_usd": blended_price,
        "estimated_usd": estimated_usd,
    }


# --- レポート組み立て ---------------------------------------------------------------


def render_report_text(report: dict[str, Any]) -> str:
    """`build_report` の dict を人が読むテキストにする（`session_report.py` とドライバが使う）。"""
    usage = report["usage"]
    next_actions = report["next_actions"]
    estimate = report["estimate"]

    lines = [f"# セッションレポート: {report['session']}", ""]

    lines.append("## 呼び出しと使用量")
    lines.append(f"- 呼び出し回数: {usage['calls']}")
    tokens = usage["tokens"]
    lines.append(
        f"- トークン: input={tokens['input_tokens']} output={tokens['output_tokens']}"
        f" reasoning={tokens['reasoning_tokens']} total={tokens['total_tokens']}"
    )
    note = "（概算を含む）" if usage["cost_has_estimate"] else "（実測）"
    lines.append(
        f"- コスト USD: 実測={usage['cost_usd_actual']:.4f}"
        f" / 概算={usage['cost_usd_estimated']:.4f}"
        f" / 合計={usage['cost_usd_total']:.4f} {note}"
    )
    if usage["unknown_cost_calls"]:
        lines.append(f"- コスト不明の呼び出し: {usage['unknown_cost_calls']} 件（単価表に無いモデル）")
    lines.append(f"- 所要時間(latency)合計: {usage['latency_sec']:.1f}s")

    if usage["by_step"]:
        lines.append("")
        lines.append("## ステップ別")
        for step, bucket in sorted(usage["by_step"].items()):
            lines.append(
                f"- {step}: 呼び出し{bucket['calls']}件"
                f" / total_tokens={bucket['total_tokens']}"
                f" / cost_usd={bucket['cost_usd']:.4f}"
            )

    lines.append("")
    lines.append("## 次アクション候補")
    lines.append(f"- confidence=low: {next_actions['low_confidence_count']} 件")
    zoom_targets = next_actions["zoom_targets"]
    lines.append(f"- zoom_targets: {len(zoom_targets)} 件")
    for item in zoom_targets[:20]:
        lines.append(f"  - t={item['timestamp_sec']}s ({item['file']})")
    if len(zoom_targets) > 20:
        lines.append(f"  - ...他 {len(zoom_targets) - 20} 件")
    lines.append(f"- hypothesis_verdict=rejected: {len(next_actions['hypothesis_rejected'])} 件")
    for path in next_actions["hypothesis_rejected"]:
        lines.append(f"  - {path}")
    lines.append(f"- 失敗した範囲: {len(next_actions['failed_ranges'])} 件")
    for item in next_actions["failed_ranges"]:
        lines.append(f"  - {item.get('label')}: {item.get('error')} ({item['file']})")
    if next_actions["validated_flags_count"]:
        lines.append(f"- validated flags 合計: {next_actions['validated_flags_count']} 件")

    if estimate is not None:
        lines.append("")
        lines.append("## 事前見積もり（--estimate）")
        lines.append(f"- 範囲数: {estimate['n_ranges']}")
        lines.append(f"- 推定タイル数: {estimate['n_tiles_estimate']}")
        measured = "実測平均" if estimate["tokens_are_measured"] else "仮定値"
        lines.append(f"- 1タイルあたりトークン({measured}): {estimate['avg_tokens_per_tile']}")
        lines.append(
            f"- 概算コスト USD: {estimate['estimated_usd']:.4f}"
            f"（{estimate['model_used_for_pricing']} の単価表より概算）"
        )

    return "\n".join(lines)


def build_report(session: str | Path, do_estimate: bool = False) -> dict[str, Any]:
    session_dir = Path(session).expanduser().resolve()
    if not session_dir.exists():
        raise RuntimeError(f"セッションディレクトリが見つかりません: {session_dir}")
    records = load_usage_records(session_dir)
    return {
        "session": str(session_dir),
        "usage": summarize_usage(records),
        "next_actions": scan_next_actions(session_dir),
        "estimate": estimate_cost(session_dir, records) if do_estimate else None,
    }
