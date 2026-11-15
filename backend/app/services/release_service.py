from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.sample import SAMPLING_ALGORITHM
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import releases as release_repo
from app.repositories import samples as sample_repo
from app.repositories import settings as settings_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _same_sampling_params(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return str(a["seed"]) == str(b["seed"]) and float(a["rate"]) == float(b["rate"])


def list_releases(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = release_repo.list_decisions(conn, limit=limit)
        packs = {p["id"]: p for p in pack_repo.list_packs(conn, limit=500)}
        for r in rows:
            p = packs.get(r["pack_id"])
            r["pack_no"] = p["pack_no"] if p else None
            r["pack_status"] = p["status"] if p else None
        return rows
    finally:
        conn.close()


def decide(pack_id: int, decision: str, reason: str | None = None) -> dict[str, Any]:
    decision = (decision or "").strip().lower()
    if decision not in ("pass", "reject"):
        raise HTTPException(status_code=400, detail="决策须为 pass 或 reject")

    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        # 只允许从“就绪”批次打包出的、未被换代策略降级的包进入放行流程。
        # 旧种子批次生成的包（reference / voided）不得与新生批次并存可放行。
        if pack["status"] in ("reference", "voided"):
            label = {
                "reference": "只读对照包",
                "voided": "已作废包",
            }[pack["status"]]
            raise HTTPException(
                status_code=409,
                detail=f"该证据包为{label}，不得放行；请按当前批次重新生成证据包。",
            )

        batch = sample_repo.get_batch(conn, pack["batch_id"])
        if not batch or batch["status"] != "ready":
            raise HTTPException(
                status_code=409,
                detail="证据包关联的采样批次已退出可放行路径，不得放行。",
            )
        if batch["algorithm"] != SAMPLING_ALGORITHM:
            raise HTTPException(
                status_code=409,
                detail="证据包关联的采样批次算法版本与当前不一致，不得放行。",
            )
        # 兜底：同对象只认最新就绪批次的采样参数（种子 + 采样率）。
        # 同参数重跑等价；参数不同则旧样本的包不得放行，避免新旧样本同时可放行。
        latest = sample_repo.latest_ready_for_subject(conn, pack["subject_id"])
        if latest and not _same_sampling_params(batch, latest):
            raise HTTPException(
                status_code=409,
                detail="该证据包对应的种子或采样率已非该对象当前参数，不得放行；请按最新批次重新打包。",
            )

        results = check_repo.list_results(conn, pack_id=pack_id)
        if not results:
            raise HTTPException(status_code=400, detail="请先跑校验清单")

        all_ok = check_repo.all_passed(conn, pack_id)
        require = settings_repo.get_setting(conn, "require_checklist_pass", "1") == "1"

        if decision == "pass":
            if require and not all_ok:
                raise HTTPException(
                    status_code=400,
                    detail="清单未全部通过，不能放行",
                )
            if not reason:
                reason = "清单全部通过，予以放行"
            status = "released"
        else:
            if not reason or not str(reason).strip():
                raise HTTPException(status_code=400, detail="驳回须填写原因")
            status = "rejected"

        now = _now()
        rid = release_repo.create_decision(
            conn,
            pack_id=pack_id,
            decision=decision,
            reason=reason,
            created_at=now,
        )
        pack_repo.update_status(conn, pack_id, status)
        conn.commit()

        return {
            "id": rid,
            "pack_id": pack_id,
            "pack_no": pack["pack_no"],
            "decision": decision,
            "reason": reason,
            "created_at": now,
            "checklist_all_passed": all_ok,
        }
    finally:
        conn.close()
