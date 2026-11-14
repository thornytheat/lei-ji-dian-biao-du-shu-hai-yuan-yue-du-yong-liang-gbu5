"""租约/交接读数解析与按租约拆分：先分段再分摊、守恒、整批拒绝口径。"""

from __future__ import annotations

from datetime import date

import pytest

from app import billing, leases
from app.leases import BillingError


def _rows_m1():
    rows = []
    for day, (t, p, v) in [
        (date(2026, 1, 1), (100.0, 60.0, 40.0)),
        (date(2026, 1, 31), (200.0, 120.0, 80.0)),
        (date(2026, 2, 28), (300.0, 180.0, 120.0)),
    ]:
        for kind, value in (("total", t), ("peak", p), ("valley", v)):
            rows.append({"meter_id": "M1", "read_at": day, "reading_kwh": value, "kind": kind})
    return rows


def _leases(switch: str = "2026-01-15"):
    return leases.parse_leases(
        [
            {"meter_id": "M1", "tenant_id": "X", "tenant_name": "旧租户",
             "start_date": "2026-01-01", "end_date": switch},
            {"meter_id": "M1", "tenant_id": "Y", "tenant_name": "新租户",
             "start_date": switch, "end_date": "2999-12-31"},
        ]
    )


def _handovers(day: str = "2026-01-15"):
    return leases.parse_handovers(
        [
            {"meter_id": "M1", "handover_at": day, "kind": "total", "reading_kwh": 150.0},
            {"meter_id": "M1", "handover_at": day, "kind": "peak", "reading_kwh": 90.0},
            {"meter_id": "M1", "handover_at": day, "kind": "valley", "reading_kwh": 60.0},
        ]
    )


def _meters():
    return [{"meter_id": "M1", "full_scale": 99999.9}]


def test_parse_leases_rejects_bad_rows_with_all_problems():
    with pytest.raises(BillingError) as exc:
        leases.parse_leases(
            [
                {"meter_id": "", "tenant_id": "T1", "start_date": "2026-01-01", "end_date": "2026-02-01"},
                {"meter_id": "M1", "tenant_id": "", "start_date": "2026-01-01", "end_date": "2026-02-01"},
                {"meter_id": "M1", "tenant_id": "T2", "start_date": "2026-02-30", "end_date": "2026-03-01"},
                {"meter_id": "M1", "tenant_id": "T3", "start_date": "2026-03-01", "end_date": "2026-02-01"},
            ]
        )
    fields = {p["field"] for p in exc.value.problems}
    assert {"meter_id", "tenant_id", "start_date", "end_date"} <= fields


def test_parse_handovers_rejects_unknown_kind_and_duplicate():
    with pytest.raises(BillingError) as exc:
        leases.parse_handovers(
            [
                {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "kwh", "reading_kwh": 1.0},
                {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "total", "reading_kwh": -3.0},
                {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "total", "reading_kwh": 4.0},
            ]
        )
    messages = " ".join(p["message"] for p in exc.value.problems)
    assert "total/peak/valley" in messages
    assert "为负" in messages or "重复" in messages


def test_handover_inside_interval_splits_before_monthly_proration():
    alloc = billing.build_allocation(
        _rows_m1(), _meters(), [], _leases(), _handovers(),
        full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
    )
    # 第一个区间 100→200（100 kWh / 30 天）被交接读数 150 切成 50 + 50
    first = next(e for e in alloc["evidence"] if e["kind"] == "total" and e["start"] == "2026-01-01")
    assert len(first["subs"]) == 2
    old, new = first["subs"]
    assert (old["tenant_id"], old["usage"], old["days"]) == ("X", 50.0, 14)
    assert (new["tenant_id"], new["usage"], new["days"]) == ("Y", 50.0, 16)
    assert old["cut_reading"] == 150.0 and old["cut_source"] == "交接读数"
    # 分段守恒
    assert round(sum(s["usage"] for s in first["subs"]), 2) == first["usage"] == 100.0


def test_tenant_bill_totals_follow_lease_periods_and_conserve():
    alloc = billing.build_allocation(
        _rows_m1(), _meters(), [], _leases(), _handovers(),
        full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
    )
    by_tenant = {}
    for bill in alloc["bills"]:
        by_tenant.setdefault(bill["tenant_id"], 0.0)
        by_tenant[bill["tenant_id"]] = round(by_tenant[bill["tenant_id"]] + bill["total"], 2)
    # 旧租户只承担交接前 50 kWh；新租户承担剩余 150 kWh（跨 1/2 月分摊但总量守恒）
    assert by_tenant == {"X": 50.0, "Y": 150.0}
    assert round(sum(by_tenant.values()), 2) == 200.0
    # 每表每月账单合计 == 月度总表
    assert all(row["balanced"] for row in alloc["reconcile"])
    assert all(row["diff"] == 0.0 for row in alloc["reconcile"])


def test_handover_on_reading_date_uses_actual_reading():
    # 换租日 2026-01-31 恰好是抄表日：区间边界天然切分，切分来源标为现场读数
    hand = _handovers("2026-01-31")
    hand[0]["reading_kwh"] = 200.0
    hand[1]["reading_kwh"] = 120.0
    hand[2]["reading_kwh"] = 80.0
    alloc = billing.build_allocation(
        _rows_m1(), _meters(), [], _leases("2026-01-31"), hand,
        full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
    )
    assert all(row["balanced"] for row in alloc["reconcile"])
    tenants = {b["tenant_id"] for b in alloc["bills"]}
    assert tenants == {"X", "Y"}
    x_total = round(sum(b["total"] for b in alloc["bills"] if b["tenant_id"] == "X"), 2)
    assert x_total == 100.0  # 只承担 1 月整月区间


def test_missing_handover_rejects_whole_batch():
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], _leases(), [],
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert any("交接读数" in p["message"] for p in exc.value.problems)


def test_overlapping_leases_reject_whole_batch():
    overlap = leases.parse_leases(
        [
            {"meter_id": "M1", "tenant_id": "X", "start_date": "2026-01-01", "end_date": "2026-01-20"},
            {"meter_id": "M1", "tenant_id": "Y", "start_date": "2026-01-15", "end_date": "2999-12-31"},
        ]
    )
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], overlap, _handovers(),
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert any("重叠" in p["message"] for p in exc.value.problems)


def test_lease_gap_rejects_whole_batch():
    gap = leases.parse_leases(
        [
            {"meter_id": "M1", "tenant_id": "X", "start_date": "2026-01-01", "end_date": "2026-01-10"},
            {"meter_id": "M1", "tenant_id": "Y", "start_date": "2026-01-15", "end_date": "2999-12-31"},
        ]
    )
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], gap, _handovers(),
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert any("断档" in p["message"] for p in exc.value.problems)


def test_inconsistent_handover_reading_rejects():
    hand = _handovers("2026-01-31")  # 当天现场 total=200，故意登记 999
    hand[0]["reading_kwh"] = 999.0
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], _leases("2026-01-31"), hand,
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert any("不一致" in p["message"] for p in exc.value.problems)


def test_orphan_handover_without_lease_boundary_rejects():
    orphan = leases.parse_handovers(
        [
            {"meter_id": "M1", "handover_at": "2026-02-10", "kind": "total", "reading_kwh": 250.0},
            {"meter_id": "M1", "handover_at": "2026-02-10", "kind": "peak", "reading_kwh": 150.0},
            {"meter_id": "M1", "handover_at": "2026-02-10", "kind": "valley", "reading_kwh": 100.0},
        ]
    )
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], _leases(), orphan,
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert any("没有对应的换租日" in p["message"] for p in exc.value.problems)


def test_handover_outside_interval_direction_rejects_negative_segment():
    # 交接读数 250 大于区间后读数 200 → 新租户子段为负，必须整批拒绝
    hand = leases.parse_handovers(
        [
            {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "total", "reading_kwh": 250.0},
            {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "peak", "reading_kwh": 150.0},
            {"meter_id": "M1", "handover_at": "2026-01-15", "kind": "valley", "reading_kwh": 100.0},
        ]
    )
    with pytest.raises(BillingError) as exc:
        billing.build_allocation(
            _rows_m1(), _meters(), [], _leases(), hand,
            full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
        )
    assert "负用量" in exc.value.message or "不自洽" in exc.value.message


def test_rollover_interval_split_conserves_usage():
    # 回绕区间（99997.6 → 15.1，量程 99999.9，用量 17.4）中间换租
    rows = []
    for day, value in [(date(2025, 12, 16), 99997.6), (date(2025, 12, 18), 15.1)]:
        rows.append({"meter_id": "R1", "read_at": day, "reading_kwh": value, "kind": "total"})
        rows.append({"meter_id": "R1", "read_at": day, "reading_kwh": value * 0.62, "kind": "peak"})
        rows.append({"meter_id": "R1", "read_at": day, "reading_kwh": value * 0.38, "kind": "valley"})
    lease = leases.parse_leases(
        [
            {"meter_id": "R1", "tenant_id": "X", "start_date": "2025-12-01", "end_date": "2025-12-17"},
            {"meter_id": "R1", "tenant_id": "Y", "start_date": "2025-12-17", "end_date": "2999-12-31"},
        ]
    )
    hand = leases.parse_handovers(
        [
            {"meter_id": "R1", "handover_at": "2025-12-17", "kind": "total", "reading_kwh": 99999.0},
            {"meter_id": "R1", "handover_at": "2025-12-17", "kind": "peak", "reading_kwh": 61999.4},
            {"meter_id": "R1", "handover_at": "2025-12-17", "kind": "valley", "reading_kwh": 37999.6},
        ]
    )
    alloc = billing.build_allocation(
        rows, [{"meter_id": "R1", "full_scale": 99999.9}], [], lease, hand,
        full_scale=99999.9, max_gap_days=31, tou_tolerance=0.005,
    )
    evidence = next(e for e in alloc["evidence"] if e["meter_id"] == "R1" and e["kind"] == "total")
    assert evidence["status"] == "rollover"
    assert round(sum(s["usage"] for s in evidence["subs"]), 2) == evidence["usage"] == 17.4
    assert all(r["balanced"] for r in alloc["reconcile"])
