#!/usr/bin/env python
"""usage の正規化とコスト計算。

正規化キー（`make_usage` が作る dict の形）:

| キー | 意味 |
|------|------|
| `input_tokens` | 入力トークン（画像・動画を含む） |
| `output_tokens` | 出力トークン |
| `reasoning_tokens` | 思考トークン（モデルによっては `output_tokens` に含まれる） |
| `total_tokens` | 合計 |
| `cost_usd` | バックエンドが返した実コスト（無ければ None） |
| `raw` | バックエンドが返した生の usage |

`reasoning_tokens` が `output_tokens` に含まれるかはバックエンドによって違うので、
課金対象の出力トークンは `billable_output_tokens()` が
`input + output + reasoning == total` かどうかで判定する。
"""

from __future__ import annotations

from typing import Any

# USD / 1M トークン。OpenRouter のモデル id とネイティブ id の両方をキーにする。
# 変更するときは as_of を必ず更新する（コスト表示は概算であることを明示する）。
PRICING: dict[str, dict[str, Any]] = {
    "google/gemini-3.7-flash": {"input": 0.75, "output": 3.75, "as_of": "2026-09-03"},
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75, "as_of": "2026-09-03"},
}


def make_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float | None = None,
    raw: Any = None,
) -> dict[str, Any]:
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cost_usd": cost_usd,
        "raw": raw if raw is not None else {},
    }


def empty_usage() -> dict[str, Any]:
    return make_usage()


def billable_output_tokens(usage: dict[str, Any]) -> int:
    """課金対象の出力トークン数。

    `input + output + reasoning == total` が成り立つときは
    思考トークンが出力に含まれていないとみなして加算する（Gemini ネイティブ）。
    成り立たないときは出力に含まれているとみなす（OpenRouter）。
    """
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if reasoning and total and input_tokens + output_tokens + reasoning == total:
        return output_tokens + reasoning
    return output_tokens


def lookup_pricing(model: str | None) -> dict[str, Any] | None:
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    # "provider/model" 形式で登録が無ければモデル名だけでも引く
    if "/" in model:
        tail = model.split("/", 1)[1]
        if tail in PRICING:
            return PRICING[tail]
    return None


def estimate_cost_usd(model: str | None, usage: dict[str, Any]) -> tuple[float | None, bool]:
    """(USD, 概算かどうか) を返す。

    - バックエンドが実コストを返していればそれを使う（概算ではない）
    - 返していなければ単価表から概算する
    - 単価表に無いモデルは (None, False)
    """
    reported = usage.get("cost_usd")
    if reported is not None:
        return float(reported), False

    pricing = lookup_pricing(model)
    if pricing is None:
        return None, False

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = billable_output_tokens(usage)
    usd = input_tokens * float(pricing["input"]) / 1e6 + output_tokens * float(pricing["output"]) / 1e6
    return round(usd, 6), True


def sum_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """複数呼び出しの usage を合算する（JSON リトライ分をまとめるため）。"""
    if not usages:
        return empty_usage()
    if len(usages) == 1:
        return usages[0]

    costs = [item.get("cost_usd") for item in usages if item.get("cost_usd") is not None]
    return {
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in usages),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in usages),
        "reasoning_tokens": sum(int(item.get("reasoning_tokens") or 0) for item in usages),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in usages),
        "cost_usd": sum(float(cost) for cost in costs) if costs else None,
        "raw": [item.get("raw") for item in usages],
    }
