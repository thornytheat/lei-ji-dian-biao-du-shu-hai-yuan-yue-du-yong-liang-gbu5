"""租户账单引擎：读数还原 → 按租约生效时段拆分 → 月度分摊 → 租户月度账单与复核清单。

关键不变量：
1. 先分段、再分摊：换租日落在抄表区间内时，先按交接读数切成租户子区间，子区间再按月分摊。
2. 一份分配结果：账单、复核清单、CSV 下载全部来自同一份 allocations，不二次计算。
3. 双层守恒：子区间用量合计 == 区间用量；租户月度账单合计（按表×月）== 月度总表。
4. 整批拒绝：租约重叠/断档、缺交接读数、交接不自洽、守恒校验不过 → 不产出任何账单。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from . import leases as leases_mod
from . import proration, readings, restore, validation
from .leases import BillingError
from .proration import parse_month
from .readings import KIND_LABELS, KINDS

LEVEL_LABELS = {"P1": "P1 立即复核", "P2": "P2 本月内复核", "P3": "P3 确认留存"}


# --------------------------------------------------------------------------- 输入指纹（幂等）


def input_signature(
    rows: list[dict],
    leases: list[dict],
    handovers: list[dict],
    *,
    month: str | None,
    meter_ids: list[str],
    full_scale: float,
    max_gap_days: int,
    tou_tolerance: float,
) -> str:
    payload = {
        "readings": [(r["meter_id"], r["read_at"].isoformat(), r["kind"], round(float(r["reading_kwh"]), 1))
                     for r in sorted(rows, key=lambda r: (r["meter_id"], r["read_at"].isoformat(), r["kind"]))],
        "leases": [(l["meter_id"], l["tenant_id"], l["start_date"].isoformat(), l["end_date"].isoformat())
                   for l in sorted(leases, key=lambda l: (l["meter_id"], l["start_date"].isoformat()))],
        "handovers": [(h["meter_id"], h["handover_at"].isoformat(), h["kind"], h["reading_kwh"])
                      for h in sorted(handovers, key=lambda h: (h["meter_id"], h["handover_at"].isoformat(), h["kind"]))],
        "params": {
            "month": month or "",
            "meter_ids": meter_ids,
            "full_scale": round(float(full_scale), 2),
            "max_gap_days": max_gap_days,
            "tou_tolerance": tou_tolerance,
        },
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- 核心分配


def build_allocation(
    rows: list[dict],
    meters: list[dict],
    changes: list[dict],
    leases: list[dict],
    handovers: list[dict],
    *,
    meter_ids: list[str] | None = None,
    full_scale: float,
    max_gap_days: int,
    tou_tolerance: float,
) -> dict:
    """执行完整分配，返回一份供账单/复核/下载共用的结果；不合法整批抛 BillingError。"""
    if not rows:
        raise BillingError("读数库为空，请先导入读数 CSV 或样例数据")
    if not leases:
        raise BillingError("租约库为空，请先导入租约 JSON")

    series_all = readings.group_series(rows)
    available = sorted({meter for (meter, _kind) in series_all})
    requested = [m for m in (meter_ids or []) if m]
    unknown = [m for m in requested if m not in available]
    if unknown:
        raise BillingError(
            f"表号不存在：{', '.join(unknown)}",
            [{"field": "meter_ids", "message": f"可选表号：{', '.join(available)}"}],
        )
    selected = requested or sorted({lease["meter_id"] for lease in leases} | set(available))
    selected = [m for m in selected if m in available]
    series = {key: value for key, value in series_all.items() if key[0] in selected}

    # 1) 租约 / 交接整批校验（重叠、断档、缺交接、不自洽）
    leases_mod.validate(leases, handovers, series_all, selected)
    leases_by_meter: dict[str, list[dict]] = {}
    for lease in leases:
        if lease["meter_id"] in selected:
            leases_by_meter.setdefault(lease["meter_id"], []).append(lease)
    for items in leases_by_meter.values():
        items.sort(key=lambda item: item["start_date"])
    hand_index = {(h["meter_id"], h["handover_at"], h["kind"]): float(h["reading_kwh"]) for h in handovers}

    # 2) 读数还原（沿用既有规则：换表分段 / 回绕 / 异常隔离 / 缺口标记）
    gaps_by_meter = readings.meter_gaps(series, max_gap_days)
    restored = restore.restore_all(
        series, meters, changes,
        full_scale=full_scale, max_gap_days=max_gap_days, meter_gaps=gaps_by_meter,
    )
    meter_index = {str(item.get("meter_id")): item for item in meters or []}

    # 3) 逐区间：先按租约切租户子区间，再逐子区间按月分摊
    # allocations[(meter, kind)][tenant][month] -> kWh；evidence 保留全部依据
    allocations: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    evidence: list[dict] = []
    split_count = 0
    conservation_problems: list[dict] = []

    for meter_id in selected:
        ordered = leases_by_meter[meter_id]
        for kind in KINDS:
            kind_result = restored.get(meter_id, {}).get("kinds", {}).get(kind)
            if not kind_result:
                continue
            tenant_month: dict[str, dict[str, float]] = {}
            points = series[(meter_id, kind)]
            for interval in kind_result["intervals"]:
                if interval.get("usage") is None:
                    evidence.append(_evidence_row(meter_id, meter_index, kind, interval, []))
                    continue
                subs = leases_mod.split_interval(interval, ordered, hand_index, points)
                if len(subs) > 1:
                    split_count += 1
                # 守恒检查一：子段合计 == 区间用量
                sub_sum = round(sum(s["usage"] for s in subs), 2)
                if abs(sub_sum - round(interval["usage"], 2)) > 0.011:
                    conservation_problems.append(
                        {"meter_id": meter_id, "kind": kind,
                         "message": (
                             f"表 {meter_id} {kind} 区间 {interval['start']} → {interval['end']} "
                             f"租户分段合计 {sub_sum:g} ≠ 区间用量 {interval['usage']:g}"
                         )}
                    )
                sub_evidence = []
                for sub in subs:
                    split_proration = proration.prorate(sub["start"], sub["end"], sub["usage"])
                    bucket = tenant_month.setdefault(sub["tenant_id"], {})
                    for part in split_proration["parts"]:
                        bucket[part["month"]] = round(bucket.get(part["month"], 0.0) + part["share"], 2)
                    sub_evidence.append(
                        {
                            "tenant_id": sub["tenant_id"],
                            "tenant_name": sub["tenant_name"],
                            "start": sub["start"].isoformat(),
                            "end": sub["end"].isoformat(),
                            "days": sub["days"],
                            "usage": sub["usage"],
                            "cut_reading": sub["cut_reading"],
                            "cut_source": sub["cut_source"],
                            "proration": split_proration,
                        }
                    )
                evidence.append(_evidence_row(meter_id, meter_index, kind, interval, sub_evidence))
            allocations[(meter_id, kind)] = tenant_month

    if conservation_problems:
        raise BillingError(
            f"分段守恒校验发现 {len(conservation_problems)} 处问题，已整批拒绝", conservation_problems
        )

    # 4) 租户月度账单（一份分配结果汇总）
    bills, bill_index = _build_bills(selected, allocations, leases_by_meter, meter_index, tou_tolerance)

    # 5) 守恒检查二：每表每月 租户账单合计 == 月度总表（读数侧分摊）
    master = _master_ledger(selected, restored)
    reconcile_rows = _reconcile(bills, master, selected, leases_by_meter)
    for row in reconcile_rows:
        if not row["balanced"]:
            conservation_problems.append(
                {"meter_id": row["meter_id"], "month": row["month"],
                 "message": (
                     f"表 {row['meter_id']} {row['month']} 租户账单合计 {row['billed_total']:g} "
                     f"≠ 月度总表 {row['master_total']:g}（差 {row['diff']:+g}）"
                 )}
            )
    if conservation_problems:
        raise BillingError(
            f"账单与月度总表对账发现 {len(conservation_problems)} 处不平，已整批拒绝", conservation_problems
        )

    months = sorted({month for bucket in allocations.values() for tenant in bucket.values() for month in tenant})
    if not months:
        raise BillingError("读数区间不足两个抄表点，无法生成任何月份账单")

    review = _review_items(
        bills, bill_index, restored, gaps_by_meter, leases_by_meter, meter_index, reconcile_rows, tou_tolerance
    )
    tenants = _tenant_dir(leases_by_meter)
    stats = _stats(bills, review, evidence, split_count, restored, reconcile_rows)
    return {
        "selected": selected,
        "available": available,
        "months": months,
        "allocations": allocations,
        "bills": bills,
        "bill_index": bill_index,
        "evidence": evidence,
        "review": review,
        "reconcile": reconcile_rows,
        "tenants": tenants,
        "master": master,
        "gaps_by_meter": gaps_by_meter,
        "restored": restored,
        "stats": stats,
        "full_scale": round(float(full_scale), 2),
        "max_gap_days": max_gap_days,
        "tou_tolerance": tou_tolerance,
    }


# --------------------------------------------------------------------------- 账单 / 对账 / 复核


def _tenant_dir(leases_by_meter: dict[str, list[dict]]) -> dict[str, dict]:
    tenants: dict[str, dict] = {}
    for items in leases_by_meter.values():
        for lease in items:
            tenants.setdefault(
                lease["tenant_id"],
                {"tenant_id": lease["tenant_id"], "tenant_name": lease["tenant_name"], "meters": set()},
            )["meters"].add(lease["meter_id"])
    return {tid: {**v, "meters": sorted(v["meters"])} for tid, v in sorted(tenants.items())}


def _build_bills(
    selected: list[str],
    allocations: dict,
    leases_by_meter: dict[str, list[dict]],
    meter_index: dict,
    tou_tolerance: float,
) -> tuple[list[dict], dict]:
    bills: list[dict] = []
    bill_index: dict[tuple[str, str, str], dict] = {}
    for meter_id in selected:
        meta = meter_index.get(meter_id, {})
        # 该表出现过的全部 (租户, 月份)
        pairs = sorted(
            {
                (tenant_id, month)
                for kind in KINDS
                for tenant_id, bucket in allocations.get((meter_id, kind), {}).items()
                for month in bucket
            }
        )
        for tenant_id, month in pairs:
            values = {
                kind: round(allocations.get((meter_id, kind), {}).get(tenant_id, {}).get(month, 0.0), 2)
                for kind in KINDS
            }
            # 三类都为 0 且该租户当月未真正覆盖时跳过
            if all(v == 0.0 for v in values.values()):
                continue
            tou = validation.time_of_use_check(values["total"], values["peak"], values["valley"], tou_tolerance)
            lease = next((item for item in leases_by_meter.get(meter_id, []) if item["tenant_id"] == tenant_id), None)
            row = {
                "meter_id": meter_id,
                "meter_name": meta.get("name") or f"表 {meter_id}",
                "tenant_id": tenant_id,
                "tenant_name": lease["tenant_name"] if lease else tenant_id,
                "month": month,
                "total": values["total"],
                "peak": values["peak"],
                "valley": values["valley"],
                "tou": tou,
            }
            bills.append(row)
            bill_index[(meter_id, tenant_id, month)] = row
    bills.sort(key=lambda r: (r["month"], r["meter_id"], r["tenant_id"]))
    return bills, bill_index


def _master_ledger(selected: list[str], restored: dict) -> dict[tuple[str, str], float]:
    """读数侧月度总表：区间用量按月分摊后的 (meter, month) -> total kWh。"""
    master: dict[tuple[str, str], float] = {}
    for meter_id in selected:
        for kind in KINDS:
            kind_result = restored.get(meter_id, {}).get("kinds", {}).get(kind)
            if not kind_result:
                continue
            allocated = proration.allocate(kind_result["intervals"])
            for month, value in allocated["by_month"].items():
                if kind == "total":
                    master[(meter_id, month)] = round(master.get((meter_id, month), 0.0) + value, 2)
    return master


def _reconcile(
    bills: list[dict],
    master: dict[tuple[str, str], float],
    selected: list[str],
    leases_by_meter: dict[str, list[dict]],
) -> list[dict]:
    rows: list[dict] = []
    months = sorted({month for (_m, month) in master})
    for meter_id in selected:
        for month in months:
            if (meter_id, month) not in master:
                continue
            billed = round(
                sum(b["total"] for b in bills if b["meter_id"] == meter_id and b["month"] == month), 2
            )
            master_total = master[(meter_id, month)]
            diff = round(billed - master_total, 2)
            tenant_ids = sorted(
                {b["tenant_id"] for b in bills if b["meter_id"] == meter_id and b["month"] == month}
            )
            rows.append(
                {
                    "meter_id": meter_id,
                    "month": month,
                    "master_total": master_total,
                    "billed_total": billed,
                    "diff": diff,
                    "balanced": abs(diff) <= 0.011,
                    "tenants": tenant_ids,
                    "tenant_count": len(tenant_ids),
                }
            )
    return rows


def _review_items(
    bills: list[dict],
    bill_index: dict,
    restored: dict,
    gaps_by_meter: dict,
    leases_by_meter: dict,
    meter_index: dict,
    reconcile_rows: list[dict],
    tou_tolerance: float,
) -> list[dict]:
    items: list[dict] = []

    # P1：分时超差（账单口径）
    for bill in bills:
        if not bill["tou"]["ok"] and bill["tou"]["status"] == "mismatch":
            tou = bill["tou"]
            items.append(
                {
                    "level": "P1", "level_label": LEVEL_LABELS["P1"], "kind": "租户账单分时超差",
                    "month": bill["month"], "meter_id": bill["meter_id"], "meter_name": bill["meter_name"],
                    "tenant_id": bill["tenant_id"], "tenant_name": bill["tenant_name"],
                    "summary": (
                        f"{bill['tenant_name']}（{bill['tenant_id']}）账单峰 {tou['peak']:g} + 谷 {tou['valley']:g} "
                        f"= {tou['sum']:g}，总量 {tou['total']:g}，偏差 {tou['diff']:+g} kWh"
                    ),
                    "action": "核对峰段寄存器与时钟电池，按现场表码复核后做退补；账单数值照常出具",
                    "basis": "租户月度分配后 peak+valley 与 total 比对",
                }
            )

    # P1：隔离异常（区间用量未计入任何租户账单）
    for meter_id, entry in restored.items():
        meter_name = meter_index.get(meter_id, {}).get("name") or f"表 {meter_id}"
        for anomaly in entry.get("anomalies", []):
            tenants = _tenants_of_range(leases_by_meter.get(meter_id, []),
                                        date.fromisoformat(anomaly["start"]), date.fromisoformat(anomaly["end"]))
            items.append(
                {
                    "level": "P1", "level_label": LEVEL_LABELS["P1"], "kind": "读数回退无法判定",
                    "month": anomaly["start"][:7], "meter_id": meter_id, "meter_name": meter_name,
                    "tenant_id": "/".join(t[0] for t in tenants) or "—",
                    "tenant_name": "/".join(t[1] for t in tenants) or "无责任租户",
                    "summary": f"{anomaly['start']} → {anomaly['end']} 读数 {anomaly['prev_value']:g} → "
                               f"{anomaly['next_value']:g}，涉及 {'/'.join(anomaly['kinds'])}",
                    "action": "现场核表确认换表/回绕是否漏登记，补齐后整批重算；该区间用量未计入任何租户账单",
                    "basis": anomaly["reason"],
                }
            )

    # P2：抄表缺口（落在哪个租户租期就提示哪个租户）
    for meter_id, gaps in gaps_by_meter.items():
        meter_name = meter_index.get(meter_id, {}).get("name") or f"表 {meter_id}"
        for gap in gaps:
            tenants = _tenants_of_range(leases_by_meter.get(meter_id, []), gap["start"], gap["end"])
            items.append(
                {
                    "level": "P2", "level_label": LEVEL_LABELS["P2"], "kind": "抄表缺口",
                    "month": gap["start"].isoformat()[:7], "meter_id": meter_id, "meter_name": meter_name,
                    "tenant_id": "/".join(t[0] for t in tenants) or "—",
                    "tenant_name": "/".join(t[1] for t in tenants) or "—",
                    "summary": f"{gap['start'].isoformat()} → {gap['end'].isoformat()} 间隔 {gap['span_days']} 天，"
                               f"缺失 {gap['missing_days']} 天，建议抄表日 {gap['suggested_read_at']}",
                    "action": "补抄一次并把抄表周期压到 ≤31 天；缺口不插值，分摊按区间天数比例",
                    "basis": gap["note"],
                }
            )

    # P2：一个月内多租户分摊（换租跨月的重点复核）
    for row in reconcile_rows:
        if row["tenant_count"] > 1:
            meter_name = meter_index.get(row["meter_id"], {}).get("name") or f"表 {row['meter_id']}"
            items.append(
                {
                    "level": "P2", "level_label": LEVEL_LABELS["P2"], "kind": "换租月多租户分摊",
                    "month": row["month"], "meter_id": row["meter_id"], "meter_name": meter_name,
                    "tenant_id": "/".join(row["tenants"]), "tenant_name": "",
                    "summary": f"{row['month']} 该表由 {row['tenant_count']} 个租户分摊，"
                               f"账单合计 {row['billed_total']:g} = 月度总表 {row['master_total']:g}",
                    "action": "核对交接读数签认单与分段公式，确认各租户份额后归档",
                    "basis": "先按交接读数分段、再按月分摊；对账差额 0.00 kWh",
                }
            )

    # P3：换表 / 回绕（仅归档确认，用量已还原并计入对应租户）
    for meter_id, entry in restored.items():
        meter_name = meter_index.get(meter_id, {}).get("name") or f"表 {meter_id}"
        for change in entry.get("changes", []):
            info = change.get("change") or {}
            items.append(
                {
                    "level": "P3", "level_label": LEVEL_LABELS["P3"], "kind": "换表分段确认",
                    "month": change["start"].isoformat()[:7], "meter_id": meter_id, "meter_name": meter_name,
                    "tenant_id": "", "tenant_name": "",
                    "summary": f"{info.get('change_date', change['start'])} 换表 "
                               f"{info.get('old_meter_no', '旧表')} → {info.get('new_meter_no', '新表')}，"
                               f"区间用量 {change.get('usage'):g} kWh",
                    "action": "确认拆表/装表读数已签认；换表不换租户，用量仍按租期归属",
                    "basis": change.get("formula") or "",
                }
            )
        for rollover in entry.get("rollovers", []):
            items.append(
                {
                    "level": "P3", "level_label": LEVEL_LABELS["P3"], "kind": "回绕确认",
                    "month": rollover["start"].isoformat()[:7], "meter_id": meter_id, "meter_name": meter_name,
                    "tenant_id": "", "tenant_name": "",
                    "summary": f"{rollover['start'].isoformat()} → {rollover['end'].isoformat()} 满量程回绕，"
                               f"用量 {rollover.get('usage'):g} kWh",
                    "action": "确认满位回绕属实，回绕量已按租期计入对应租户账单",
                    "basis": rollover.get("formula") or "",
                }
            )
    items.sort(key=lambda item: (item["level"], item["month"], item["meter_id"], item["kind"]))
    return items


def _tenants_of_range(ordered: list[dict], start: date, end: date) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for lease in ordered:
        if lease["start_date"] < end and lease["end_date"] > start:
            found.append((lease["tenant_id"], lease["tenant_name"]))
    return found


def _stats(bills, review, evidence, split_count, restored, reconcile_rows) -> dict:
    return {
        "bills": len(bills),
        "tenants": len({b["tenant_id"] for b in bills}),
        "meters": len({b["meter_id"] for b in bills}),
        "months": len({b["month"] for b in bills}),
        "billed_total": round(sum(b["total"] for b in bills), 2),
        "split_intervals": split_count,
        "review_items": len(review),
        "review_p1": sum(1 for i in review if i["level"] == "P1"),
        "review_p2": sum(1 for i in review if i["level"] == "P2"),
        "review_p3": sum(1 for i in review if i["level"] == "P3"),
        "isolated_anomalies": sum(len(e.get("anomalies", [])) for e in restored.values()),
        "reconcile_rows": len(reconcile_rows),
        "reconcile_unbalanced": sum(1 for r in reconcile_rows if not r["balanced"]),
        "tou_flagged": sum(1 for b in bills if not b["tou"]["ok"] and b["tou"]["status"] == "mismatch"),
    }


def _evidence_row(meter_id: str, meter_index: dict, kind: str, interval: dict, sub_evidence: list[dict]) -> dict:
    meta = meter_index.get(meter_id, {})
    return {
        "meter_id": meter_id,
        "meter_name": meta.get("name") or f"表 {meter_id}",
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "start": interval["start"].isoformat(),
        "end": interval["end"].isoformat(),
        "days": interval["days"],
        "status": interval["status"],
        "usage": interval.get("usage"),
        "formula": interval.get("formula", ""),
        "reason": interval.get("reason", ""),
        "gap": interval.get("gap", False),
        "segments": interval.get("segments") or [],
        "subs": sub_evidence,
    }


# --------------------------------------------------------------------------- 聚焦月份 + CSV


def focus_month(allocation: dict, month: str) -> dict:
    bills = [b for b in allocation["bills"] if b["month"] == month]
    review = [i for i in allocation["review"] if i["month"] == month]
    reconcile = [r for r in allocation["reconcile"] if r["month"] == month]
    month_start, month_end = parse_month(month)
    evidence = [
        e for e in allocation["evidence"]
        if date.fromisoformat(e["start"]) < month_end and date.fromisoformat(e["end"]) > month_start
    ]
    totals = {
        "billed_total": round(sum(b["total"] for b in bills), 2),
        "peak_total": round(sum(b["peak"] for b in bills), 2),
        "valley_total": round(sum(b["valley"] for b in bills), 2),
        "bills": len(bills),
        "tenants": len({b["tenant_id"] for b in bills}),
        "meters": len({b["meter_id"] for b in bills}),
        "review_items": len(review),
        "reconcile_unbalanced": sum(1 for r in reconcile if not r["balanced"]),
    }
    return {
        "month": month,
        "has_data": bool(bills),
        "bills": bills,
        "review": review,
        "reconcile": reconcile,
        "evidence": evidence,
        "totals": totals,
    }


def _run_scope(run: dict) -> str | None:
    """批次聚焦月份；全期批次返回 None（不筛选）。"""
    return run.get("month") or None


def bills_csv(bills: list[dict]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["月份", "表号", "表名", "租户编号", "租户名称", "总电量kWh", "峰段kWh", "谷段kWh",
                     "峰谷合计", "偏差kWh", "分时校验"])
    for row in bills:
        tou = row["tou"]
        writer.writerow(
            [
                row["month"], row["meter_id"], row["meter_name"], row["tenant_id"], row["tenant_name"],
                f"{row['total']:.2f}", f"{row['peak']:.2f}", f"{row['valley']:.2f}",
                "" if tou["sum"] is None else f"{tou['sum']:.2f}",
                "" if tou["diff"] is None else f"{tou['diff']:.2f}",
                "通过" if tou["ok"] else ("超差" if tou["status"] == "mismatch" else "缺数据"),
            ]
        )
    return buffer.getvalue()


def reconcile_csv(rows: list[dict]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["月份", "表号", "租户数", "租户账单合计kWh", "月度总表kWh", "差额kWh", "对账结论", "租户编号"])
    for row in rows:
        writer.writerow(
            [
                row["month"], row["meter_id"], row["tenant_count"], f"{row['billed_total']:.2f}",
                f"{row['master_total']:.2f}", f"{row['diff']:.2f}",
                "平衡" if row["balanced"] else "不平", "/".join(row["tenants"]),
            ]
        )
    return buffer.getvalue()


def review_csv(items: list[dict]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["月份", "级别", "表号", "表名", "租户", "类型", "问题说明", "建议动作", "依据"])
    for item in items:
        tenant = item.get("tenant_name") or item.get("tenant_id") or ""
        writer.writerow(
            [item["month"], item["level_label"], item["meter_id"], item["meter_name"], tenant,
             item["kind"], item["summary"], item["action"], item["basis"]]
        )
    return buffer.getvalue()
