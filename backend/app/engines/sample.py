from __future__ import annotations

import hashlib
import json
import math
from typing import Any

# 采样算法版本：一旦发布，抽样判定语义不得静默改变。
# sha256-bucket-v1 = 对 (seed, business_key) 求 sha256，取前 12 位十六进制折算为 [0,1) 桶值；
# 桶值 < rate 者入样，结果按 business_key 排序。空结果时按桶值升序补足 ceil(rate*n) 条。
SAMPLING_ALGORITHM = "sha256-bucket-v1"


def _bucket(seed: str, business_key: str) -> float:
    """Deterministic 0..1 bucket from seed + key.

    抽样判定只允许吃显式入参 seed / business_key，禁止混入墙钟、进程熵等未入参随机源。
    """
    digest = hashlib.sha256(f"{seed}|{business_key}".encode("utf-8")).hexdigest()
    # use first 12 hex chars -> int -> [0, 1)
    n = int(digest[:12], 16)
    return n / float(16**12)


def source_fingerprint(items: list[dict[str, Any]]) -> str:
    """Stable fingerprint over the full source payload.

    Used to prove that a batch was sampled from the same source snapshot when it is
    re-verified later. Sorting by business_key keeps the digest independent of the
    physical row order of the source.
    """
    ordered = sorted(items, key=lambda x: str(x.get("business_key", "")))
    payload = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sample_keys_fingerprint(keys: list[str]) -> str:
    """Fingerprint over the selected business-key set (sorted, one per line)."""
    joined = "\n".join(sorted(str(k) for k in keys))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def default_seed_for(subject_code: str) -> str:
    """Deterministic default seed for the allow-default policy.

    Deliberately derived only from the subject code and the sampling algorithm
    version — no wall clock, no process entropy. The exact value is written back to
    the batch, so a later run can reproduce it even if the derivation changes.
    """
    digest = hashlib.sha256(f"{subject_code}|{SAMPLING_ALGORITHM}".encode("utf-8")).hexdigest()
    return f"auto:{subject_code}:{digest[:16]}"


def sample_items(
    items: list[dict[str, Any]],
    rate: float,
    seed: str,
) -> list[dict[str, Any]]:
    """
    Deterministic sampling by seed.
    Items whose bucket < rate are included; sorted by business_key for stability.
    Ensures at least one item when source is non-empty and rate > 0.
    """
    if not items:
        return []
    rate = max(0.0, min(1.0, float(rate)))
    if rate <= 0:
        return []
    if rate >= 1.0:
        return sorted(items, key=lambda x: str(x.get("business_key", "")))

    selected = [
        item
        for item in items
        if _bucket(seed, str(item.get("business_key", ""))) < rate
    ]
    if not selected and items:
        # fallback: take ceil(rate * n) by bucket order for reproducibility
        ranked = sorted(
            items,
            key=lambda x: (_bucket(seed, str(x.get("business_key", ""))), str(x.get("business_key", ""))),
        )
        n = max(1, min(len(items), math.ceil(rate * len(items))))
        selected = ranked[:n]
    return sorted(selected, key=lambda x: str(x.get("business_key", "")))
