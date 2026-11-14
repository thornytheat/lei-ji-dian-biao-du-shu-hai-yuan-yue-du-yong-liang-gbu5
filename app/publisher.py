"""账单发布服务：校验 → 完整分配 → 一次性原子切换；失败保留旧版；同批重试幂等。

发布语义：
- 新结果在单个 SQLite 事务内完成全部守恒校验、账单落库、版本切换；任一步失败整体回滚，旧版保持 active。
- 同一 idempotency_key 的重试直接返回首个批次，不新增账单、不重复用量、不产生新版本。
- 版本切换 = 旧 active 置 superseded + 新版本置 active，同一事务内完成，外部永远只看到完整的一版。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from . import billing, store
from .leases import BillingError


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _new_run_id() -> str:
    return f"bill-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _new_version_id() -> str:
    return f"ver-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def publish_billing(
    conn: sqlite3.Connection,
    *,
    rows: list[dict],
    meters: list[dict],
    changes: list[dict],
    leases: list[dict],
    handovers: list[dict],
    month: str | None,
    meter_ids: list[str] | None,
    full_scale: float,
    max_gap_days: int,
    tou_tolerance: float,
    idempotency_key: str | None,
    mode: str = "publish",
) -> dict:
    """执行一次账单批次。mode='validate' 只校验不落库；'publish' 原子发布。"""
    # 1) 幂等：同 key 直接回放首个批次（成功/试算/拒绝同等待遇），绝不重复写
    if idempotency_key:
        existing = store.get_billing_run_by_idem(conn, idempotency_key)
        if existing is not None:
            if existing.get("status") == "rejected":
                replay_error = BillingError(
                    existing.get("error") or "该批次此前已被整批拒绝",
                    existing.get("problems", []),
                )
                replay_error.rejected_run = {**existing, "replayed": True}
                raise replay_error
            existing["replayed"] = True
            return existing

    run_id = _new_run_id()
    created_at = now_iso()
    sig = billing.input_signature(
        rows, leases, handovers,
        month=month, meter_ids=meter_ids or [],
        full_scale=full_scale, max_gap_days=max_gap_days, tou_tolerance=tou_tolerance,
    )

    # 2) 完整分配（不合法会抛 BillingError，此时未写任何账单/版本）
    try:
        allocation = billing.build_allocation(
            rows, meters, changes, leases, handovers,
            meter_ids=meter_ids, full_scale=full_scale,
            max_gap_days=max_gap_days, tou_tolerance=tou_tolerance,
        )
    except BillingError as exc:
        # 整批拒绝：记一条 rejected 批次（无账单、无版本），同幂等键重试时在入口直接回放
        rejected = {
            "run_id": _new_run_id(),
            "created_at": created_at,
            "month": month or "",
            "status": "rejected",
            "selected": meter_ids or [],
            "available": [],
            "idempotency_key": idempotency_key,
            "input_sig": sig,
            "mode": mode,
            "error": exc.message,
            "problems": exc.problems,
            "replayed": False,
        }
        store.save_billing_run(conn, rejected)
        store.add_event(
            conn, "billing_rejected", f"账单批次整批拒绝：{exc.message}",
            {"problems": exc.problems[:20], "month": month, "input_sig": sig}, created_at,
        )
        exc.rejected_run = rejected
        raise

    focus = billing.focus_month(allocation, month) if month else None
    if month and not focus["has_data"]:
        raise BillingError(
            f"{month} 没有任何租约/读数覆盖的账单区间",
            [{"field": "month", "message": f"账单覆盖：{allocation['months'][0]} ~ {allocation['months'][-1]}"}],
        )

    bill_rows = [
        {
            "meter_id": b["meter_id"], "tenant_id": b["tenant_id"], "month": b["month"],
            "kind": kind, "amount_kwh": b[kind],
        }
        for b in allocation["bills"] for kind in ("total", "peak", "valley")
    ]
    run_payload = {
        "run_id": run_id,
        "created_at": created_at,
        "month": month or "",
        "status": "published" if mode == "publish" else "validated",
        "selected": allocation["selected"],
        "available": allocation["available"],
        "idempotency_key": idempotency_key,
        "input_sig": sig,
        "mode": mode,
        "full_scale": allocation["full_scale"],
        "max_gap_days": allocation["max_gap_days"],
        "tou_tolerance": allocation["tou_tolerance"],
        "stats": allocation["stats"],
        "bills": allocation["bills"],
        "review": allocation["review"],
        "reconcile": allocation["reconcile"],
        "tenants": allocation["tenants"],
        "months": allocation["months"],
        "focus": focus,
        "evidence": focus["evidence"] if focus else allocation["evidence"],
        "replayed": False,
        "version_id": None,
        "previous_version": None,
    }

    if mode == "validate":
        store.save_billing_run(conn, run_payload)
        store.add_event(
            conn, "billing_validate",
            f"账单校验通过（未发布）：{run_id}，账单 {len(bill_rows)} 行",
            {"run_id": run_id, "month": month, "input_sig": sig}, created_at,
        )
        return run_payload

    # 3) 原子发布：单事务内 落账单 → 切版本 → 记事件；失败整体回滚，旧版保持 active
    version_id = _new_version_id()
    previous = store.active_billing_version(conn, month)
    version_payload = {
        "version_id": version_id,
        "created_at": created_at,
        "run_id": run_id,
        "month": month or "",
        "status": "active",
        "input_sig": sig,
        "stats": allocation["stats"],
        "previous_version": previous["version_id"] if previous else None,
    }
    try:
        store.save_billing_run(conn, run_payload)
        inserted = store.insert_bills(conn, version_id, run_id, bill_rows)
        if inserted != len(bill_rows):
            raise RuntimeError("账单落库行数与分配结果不一致，触发回滚")
        store.deactivate_billing_versions(conn, month)
        store.save_billing_version(conn, version_payload)
        # 发布后再读回做落地校验：账单行数必须与分配结果完全一致
        persisted = store.bills_of_version(conn, version_id)
        if len(persisted) != len(bill_rows):
            raise RuntimeError("发布后读回账单行数不一致，触发回滚")
        store.add_event(
            conn, "billing_publish",
            f"账单发布 {version_id}（批次 {run_id}）：{allocation['stats']['bills']} 张租户月度账单，"
            f"旧版 {version_payload['previous_version'] or '无'} 已保留为 superseded",
            {"run_id": run_id, "version_id": version_id, "month": month,
             "input_sig": sig, "previous_version": version_payload["previous_version"]},
            created_at,
        )
        run_payload["version_id"] = version_id
        run_payload["previous_version"] = version_payload["previous_version"]
        # 再次持久化带 version_id 的批次记录（INSERT OR REPLACE），随后显式提交
        store.save_billing_run(conn, run_payload)
        conn.commit()
    except Exception as exc:
        # 显式回滚发布事务；外层 store.session 退出时无脏数据可提交
        conn.rollback()
        survived = store.active_billing_version(conn, month)
        # 失败事件用独立短事务记录，绝不把失败版本/账单带进去
        try:
            store.add_event(
                conn, "billing_publish_failed",
                f"账单发布失败已回滚，旧版 {survived['version_id'] if survived else '无'} 保持有效：{exc}",
                {"run_id": run_id, "month": month, "input_sig": sig, "error": str(exc)}, now_iso(),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        return {
            "publish_failed": True,
            "error": (
                f"账单发布失败，已回滚并保留旧版"
                f"（{survived['version_id'] if survived else '此前无发布版本'}）"
            ),
            "detail": str(exc),
            "run_id": run_id,
        }

    return run_payload
