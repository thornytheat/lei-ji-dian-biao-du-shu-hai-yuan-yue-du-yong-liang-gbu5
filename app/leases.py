"""租约与换租交接读数：解析、严格校验、按租约生效时段拆分读数区间。

口径（半开区间，与抄表区间 [read_at_i, read_at_{i+1}) 一致）：
- 租约 lease 在 [start_date, end_date) 内生效；end_date=2999-12-31 表示在租。
- 同一表的租约链必须首尾相接：既不允许重叠，也不允许断档。
- 换租日（租约链内部边界）落在抄表区间内时，必须有该日 total/peak/valley 交接读数，
  先按交接读数把区间切成两段（旧租户尾段 + 新租户首段），再各自按月分摊。
- 租约重叠、断档、缺少交接读数、交接读数与现场读数不自洽 → 整批拒绝（BillingError）。
"""

from __future__ import annotations

import json
from datetime import date

from . import readings
from .readings import KINDS

FAR_FUTURE = date(2999, 12, 31)


class BillingError(Exception):
    """租约/交接/分配阶段的整批拒绝：一次性带出全部问题。"""

    def __init__(self, message: str, problems: list[dict] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []
        self.status = status


# --------------------------------------------------------------------------- 解析


def _as_date(value, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return readings.parse_date(str(value))
    except ValueError as exc:
        raise ValueError(str(exc).replace("日期", f"{field} 日期")) from exc


def parse_leases(raw: list[dict]) -> list[dict]:
    """解析租约列表，做字段级校验；任一非法即抛 BillingError（带全部问题）。"""
    problems: list[dict] = []
    leases: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw or [], start=1):
        prefix = f"第 {index} 条租约"
        meter_id = str(item.get("meter_id") or "").strip()
        tenant_id = str(item.get("tenant_id") or "").strip()
        if not meter_id:
            problems.append({"index": index, "field": "meter_id", "message": f"{prefix}表号为空"})
        if not tenant_id:
            problems.append({"index": index, "field": "tenant_id", "message": f"{prefix}租户编号为空"})
        try:
            start = _as_date(item.get("start_date"), "start_date")
        except ValueError as exc:
            problems.append({"index": index, "field": "start_date", "message": str(exc)})
            start = None
        end_text = item.get("end_date")
        if end_text in (None, "", "2999-12-31"):
            end = FAR_FUTURE
        else:
            try:
                end = _as_date(end_text, "end_date")
            except ValueError as exc:
                problems.append({"index": index, "field": "end_date", "message": str(exc)})
                end = None
        if start and end and end <= start:
            problems.append(
                {"index": index, "field": "end_date",
                 "message": f"{prefix}结束日 {end.isoformat()} 必须晚于生效日 {start.isoformat()}"}
            )
        key = (meter_id, tenant_id, start.isoformat() if start else "")
        if key in seen and meter_id and start:
            problems.append(
                {"index": index, "field": "tenant_id",
                 "message": f"{prefix}重复登记：{meter_id} / {tenant_id} / {start.isoformat()}"}
            )
        seen.add(key)
        if meter_id and tenant_id and start and end and end > start:
            leases.append(
                {
                    "meter_id": meter_id,
                    "tenant_id": tenant_id,
                    "tenant_name": str(item.get("tenant_name") or tenant_id),
                    "start_date": start,
                    "end_date": end,
                    "note": str(item.get("note") or ""),
                }
            )
    if problems:
        raise BillingError(f"租约有 {len(problems)} 处错误，已整批拒绝", problems)
    return leases


def parse_handovers(raw: list[dict]) -> list[dict]:
    """解析交接读数：每表每日每类一条，数值非负；重复/非法整体拒绝。"""
    problems: list[dict] = []
    handovers: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw or [], start=1):
        prefix = f"第 {index} 条交接读数"
        meter_id = str(item.get("meter_id") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        if not meter_id:
            problems.append({"index": index, "field": "meter_id", "message": f"{prefix}表号为空"})
        if kind not in KINDS:
            problems.append(
                {"index": index, "field": "kind",
                 "message": f"{prefix}电量类型 {kind!r} 非法，只支持 total/peak/valley"}
            )
        try:
            when = _as_date(item.get("handover_at"), "handover_at")
        except ValueError as exc:
            problems.append({"index": index, "field": "handover_at", "message": str(exc)})
            when = None
        try:
            value = readings.parse_reading(str(item.get("reading_kwh")))
        except ValueError as exc:
            problems.append({"index": index, "field": "reading_kwh", "message": f"{prefix}{exc}"})
            value = None
        key = (meter_id, when.isoformat() if when else "", kind)
        if key in seen and meter_id and when:
            problems.append(
                {"index": index, "field": "handover_at",
                 "message": f"{prefix}重复：{meter_id} {when.isoformat()} {kind} 已登记"}
            )
        seen.add(key)
        if meter_id and kind in KINDS and when and value is not None:
            handovers.append(
                {"meter_id": meter_id, "handover_at": when, "kind": kind, "reading_kwh": value}
            )
    if problems:
        raise BillingError(f"交接读数有 {len(problems)} 处错误，已整批拒绝", problems)
    return handovers


def load_lease_files(text: str) -> dict:
    """解析租约 JSON 上传内容。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BillingError("租约文件不是合法 JSON", [{"field": "file", "message": str(exc)}]) from exc
    return {"leases": parse_leases(data.get("leases") or [])}


def load_handover_files(text: str) -> dict:
    """解析交接读数 JSON 上传内容。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BillingError("交接读数文件不是合法 JSON", [{"field": "file", "message": str(exc)}]) from exc
    return {"handovers": parse_handovers(data.get("handovers") or [])}


# --------------------------------------------------------------------------- 校验


def _boundary_dates(meter_leases: list[dict]) -> list[date]:
    ordered = sorted(meter_leases, key=lambda item: item["start_date"])
    return [item["start_date"] for item in ordered[1:]]


def validate(
    leases: list[dict],
    handovers: list[dict],
    series: dict[tuple[str, str], list[dict]],
    meter_ids: list[str],
) -> list[dict]:
    """整批校验。通过返回按表排序的租约；任一问题抛 BillingError（含全部问题）。"""
    problems: list[dict] = []
    by_meter: dict[str, list[dict]] = {}
    for lease in leases:
        by_meter.setdefault(lease["meter_id"], []).append(lease)

    hand_index: dict[tuple[str, date, str], float] = {
        (row["meter_id"], row["handover_at"], row["kind"]): float(row["reading_kwh"])
        for row in handovers
    }
    # 交接读数必须落在某条租约边界上，否则属于无主数据
    lease_boundaries: set[tuple[str, date]] = set()
    for meter_id, items in by_meter.items():
        for day in _boundary_dates(items):
            lease_boundaries.add((meter_id, day))
    for row in handovers:
        if (row["meter_id"], row["handover_at"]) not in lease_boundaries:
            problems.append(
                {
                    "field": "handovers",
                    "meter_id": row["meter_id"],
                    "message": (
                        f"交接读数 {row['meter_id']} {row['handover_at'].isoformat()} {row['kind']} "
                        "没有对应的换租日（租约边界），无法归属"
                    ),
                }
            )

    ordered_all: list[dict] = []
    for meter_id in meter_ids:
        items = by_meter.get(meter_id)
        if not items:
            problems.append(
                {"field": "leases", "meter_id": meter_id,
                 "message": f"表 {meter_id} 没有任何租约，无法按租户拆分用量"}
            )
            continue
        ordered = sorted(items, key=lambda item: item["start_date"])
        ordered_all.extend(ordered)

        # 1) 重叠 / 断档：租约链必须首尾相接
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev["end_date"] > nxt["start_date"]:
                problems.append(
                    {"field": "leases", "meter_id": meter_id,
                     "message": (
                         f"表 {meter_id} 租约重叠：{prev['tenant_id']} 至 {prev['end_date'].isoformat()}"
                         f" 与 {nxt['tenant_id']} 自 {nxt['start_date'].isoformat()} 生效"
                     )}
                )
            elif prev["end_date"] < nxt["start_date"]:
                problems.append(
                    {"field": "leases", "meter_id": meter_id,
                     "message": (
                         f"表 {meter_id} 租约断档：{prev['tenant_id']} 于 {prev['end_date'].isoformat()} 到期，"
                         f"{nxt['tenant_id']} 于 {nxt['start_date'].isoformat()} 才生效，中间无责任租户"
                     )}
                )

        # 2) 覆盖读数全期
        first_read = min(points[0]["read_at"] for kind in KINDS if (meter_id, kind) in series
                         for points in [series[(meter_id, kind)]])
        last_read = max(points[-1]["read_at"] for kind in KINDS if (meter_id, kind) in series
                        for points in [series[(meter_id, kind)]])
        if ordered[0]["start_date"] > first_read:
            problems.append(
                {"field": "leases", "meter_id": meter_id,
                 "message": (
                     f"表 {meter_id} 首条租约 {ordered[0]['tenant_id']} 自 {ordered[0]['start_date'].isoformat()} 生效，"
                     f"晚于首个抄表日 {first_read.isoformat()}，前期用量无责任租户"
                 )}
            )
        if ordered[-1]["end_date"] <= last_read:
            problems.append(
                {"field": "leases", "meter_id": meter_id,
                 "message": (
                     f"表 {meter_id} 末条租约 {ordered[-1]['tenant_id']} 于 {ordered[-1]['end_date'].isoformat()} 到期，"
                     f"早于或等于末个抄表日 {last_read.isoformat()}，后期用量无责任租户"
                 )}
            )

        # 3) 每个换租日 × 每类电量：现场读数或交接读数至少有其一，且两者同时存在时必须一致
        for day in _boundary_dates(ordered):
            for kind in KINDS:
                points = series.get((meter_id, kind), [])
                actual = next((p["value"] for p in points if p["read_at"] == day), None)
                hand_value = hand_index.get((meter_id, day, kind))
                if actual is None and hand_value is None:
                    problems.append(
                        {"field": "handovers", "meter_id": meter_id,
                         "message": (
                             f"表 {meter_id} 换租日 {day.isoformat()} 落在抄表区间内且缺少 {kind} 交接读数，"
                             "无法在两租户间切分该区间"
                         )}
                    )
                elif actual is not None and hand_value is not None and abs(actual - hand_value) > 0.05:
                    problems.append(
                        {"field": "handovers", "meter_id": meter_id,
                         "message": (
                             f"表 {meter_id} {day.isoformat()} {kind} 交接读数 {hand_value:g} 与现场读数 "
                             f"{actual:g} 不一致（差 {hand_value - actual:+g}），交接数据不自洽"
                         )}
                    )

    # 交接读数引用了未选/未知表
    for meter_id in {row["meter_id"] for row in handovers}:
        if meter_id not in meter_ids:
            problems.append(
                {"field": "handovers", "meter_id": meter_id,
                 "message": f"交接读数引用了未知表号 {meter_id}"}
            )
    if problems:
        raise BillingError(
            f"租约/交接校验发现 {len(problems)} 处问题，已整批拒绝，未生成任何账单", problems
        )
    return ordered_all


# --------------------------------------------------------------------------- 拆分


def lease_at(ordered: list[dict], day: date) -> dict | None:
    """返回 day 当天生效的租约（day ∈ [start_date, end_date)）。"""
    for lease in ordered:
        if lease["start_date"] <= day < lease["end_date"]:
            return lease
    return None


def split_interval(
    interval: dict,
    ordered: list[dict],
    hand_index: dict[tuple[str, date, str], float],
    points: list[dict],
) -> list[dict]:
    """把一个已还原区间按租约边界 + 交接读数拆成租户子区间。

    前置：validate 已保证切分点读数可得（现场读数或交接读数）。
    用量守恒：sum(子区间 usage) == 区间 usage（round 到 2 位，尾差并入最后一段）。
    """
    start, end = interval["start"], interval["end"]
    usage = interval.get("usage")
    if usage is None:
        return []
    cuts = sorted(
        lease["start_date"]
        for lease in ordered
        if start < lease["start_date"] < end
    )
    if not cuts:
        lease = lease_at(ordered, start)
        return [
            {
                "start": start,
                "end": end,
                "days": interval["days"],
                "usage": round(float(usage), 2),
                "tenant_id": lease["tenant_id"] if lease else None,
                "tenant_name": lease["tenant_name"] if lease else "无责任租户",
                "cut_reading": None,
                "cut_source": None,
            }
        ]

    boundaries = [start, *cuts, end]
    # 切分点读数：优先现场读数（同一序列），否则取交接读数
    series_values = {p["read_at"]: float(p["value"]) for p in points}
    cut_values: dict[date, float] = {}
    for day in cuts:
        if day in series_values:
            cut_values[day] = series_values[day]
        else:
            cut_values[day] = float(hand_index[(interval["meter_id"], day, interval["kind"])])

    sub: list[dict] = []
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        lease = lease_at(ordered, seg_start)
        sub.append(
            {
                "start": seg_start,
                "end": seg_end,
                "days": (seg_end - seg_start).days,
                "tenant_id": lease["tenant_id"] if lease else None,
                "tenant_name": lease["tenant_name"] if lease else "无责任租户",
                "_lo": None,
                "_hi": None,
                "cut_reading": None,
                "cut_source": None,
            }
        )
    # 各段读数边界
    for index, item in enumerate(sub):
        item["_lo"] = float(interval["prev_value"]) if index == 0 else cut_values[item["start"]]
        item["_hi"] = float(interval["next_value"]) if index == len(sub) - 1 else cut_values[item["end"]]
    for day in cuts:
        owner = next(item for item in sub if item["end"] == day)
        owner["cut_reading"] = round(cut_values[day], 2)
        owner["cut_source"] = "现场读数" if day in series_values else "交接读数"

    # 分段做差；回绕类区间按量程平移，换表类区间直接用各自表码做差。
    # 尾差并入最后一段，保证 sum(子段) 与已还原区间用量完全相等。
    full_scale = float(interval.get("full_scale") or 0.0)
    raw_cents = []
    for item in sub:
        delta = item["_hi"] - item["_lo"]
        if interval.get("status") == "rollover" and item["_hi"] < item["_lo"]:
            delta += full_scale
        # 正常/回绕区间的交接读数必须落在区间读数方向内，否则交接数据不自洽
        if interval.get("status") != "meter_change" and delta < -0.005:
            raise BillingError(
                f"表 {interval['meter_id']} {interval['kind']} 区间 {start.isoformat()} → {end.isoformat()} "
                f"在 {item['start'].isoformat()} 的交接读数 {item['_lo']:g} → {item['_hi']:g} 出现负用量，"
                "交接读数与抄表区间不自洽，已整批拒绝",
                [{"field": "handovers", "meter_id": interval["meter_id"],
                  "message": "交接读数落在区间读数方向之外，无法分段"}],
            )
        raw_cents.append(int(round(delta * 100)))
    total_cents = int(round(float(usage) * 100))
    raw_cents[-1] += total_cents - sum(raw_cents)
    for item, cents in zip(sub, raw_cents):
        item["usage"] = round(cents / 100.0, 2)
        item.pop("_lo", None)
        item.pop("_hi", None)
    return sub
