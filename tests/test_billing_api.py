"""HTTP 层租户账单：发布、幂等重试、版本切换、整批拒绝、失败回滚、三份 CSV 同源。"""

from __future__ import annotations

import csv
import io
import json

import pytest

from app import publisher, store


def test_health_reports_leases_and_handovers(client):
    body = client.get("/health").json()
    assert body["leases"] == 9
    assert body["handovers"] == 15


def test_billing_page_and_result_page_render(client):
    page = client.get("/billing")
    assert page.status_code == 200
    assert "租户账单与换租分摊" in page.text
    assert "先分段再分摊" in page.text


def test_validate_mode_does_not_publish_version(client):
    resp = client.post("/api/billing/runs", json={"month": "2025-10", "mode": "validate"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "validated"
    assert body["version_id"] is None
    versions = client.get("/api/billing/runs").json()["versions"]
    assert versions == []  # 试算不落版本


def test_publish_creates_balanced_bills_and_active_version(client):
    resp = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "t-pub"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "published" and body["version_id"]
    assert body["stats"]["reconcile_unbalanced"] == 0
    assert all(row["balanced"] and row["diff"] == 0.0 for row in body["reconcile"])
    active = client.get("/api/billing/active?month=2025-10").json()
    assert active["active"]["version_id"] == body["version_id"]
    # 账单含租户字段
    assert all({"tenant_id", "tenant_name", "total", "peak", "valley"} <= set(b) for b in body["bills"])


def test_sample_handover_splits_october_between_two_tenants(client):
    # EM-01 于 2025-10-01 换租（T-A01 → T-A02），交接读数与现场读数同日
    body = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "t-em01"}).json()
    em01 = [b for b in body["bills"] if b["meter_id"] == "EM-01"]
    tenants = {b["tenant_id"] for b in em01}
    assert tenants == {"T-A02"}  # 换租日即 10/1，10 月用量全部归新租户
    # 9 月账单归旧租户
    sep = client.post("/api/billing/runs", json={"month": "2025-09", "idempotency_key": "t-em01-sep"}).json()
    sep_old = [b for b in sep["bills"] if b["meter_id"] == "EM-01"]
    assert {b["tenant_id"] for b in sep_old} == {"T-A01"}


def test_idempotent_retry_replays_without_new_bills(client):
    first = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "idem-1"}).json()
    second = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "idem-1"}).json()
    assert second["replayed"] is True
    assert second["run_id"] == first["run_id"]
    assert second["version_id"] == first["version_id"]
    runs = client.get("/api/billing/runs").json()["runs"]
    assert len([r for r in runs if r["run_id"] == first["run_id"]]) == 1


def test_republish_supersedes_old_version_atomically(client):
    v1 = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "switch-1"}).json()
    v2 = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "switch-2"}).json()
    assert v2["previous_version"] == v1["version_id"]
    assert v2["version_id"] != v1["version_id"]
    versions = client.get("/api/billing/runs").json()["versions"]
    statuses = {v["version_id"]: v["status"] for v in versions}
    assert statuses[v1["version_id"]] == "superseded"
    assert statuses[v2["version_id"]] == "active"
    # 旧版本账单仍可查询（保留旧版）
    old_detail = client.get(f"/api/billing/runs/{v1['run_id']}").json()["run"]
    assert old_detail["status"] == "published"


def test_batch_rejection_creates_no_bills_and_keeps_old_version(client):
    good = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "keep-1"}).json()
    broken = {"leases": []}
    for lease in client.get("/api/tenants/leases").json()["leases"]:
        item = dict(lease)
        if item["meter_id"] == "EM-02" and item["tenant_id"] == "T-B01":
            item["end_date"] = "2025-06-25"  # 制造断档 06-25 → 07-01
        broken["leases"].append(item)
    client.post("/api/tenants/leases",
                files={"file": ("broken.json", json.dumps(broken).encode(), "application/json")})

    rejected = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "keep-bad"})
    assert rejected.status_code == 400
    body = rejected.json()
    assert body["rejected"] is True
    assert any("断档" in p["message"] for p in body["problems"])

    # 旧版本仍 active
    active = client.get("/api/billing/active?month=2025-10").json()
    assert active["active"]["version_id"] == good["version_id"]

    # 同键重试：仍拒绝且不新增 rejected 批次
    again = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "keep-bad"})
    assert again.status_code == 400
    runs = client.get("/api/billing/runs").json()["runs"]
    assert len([r for r in runs if r["status"] == "rejected"]) == 1

    # 拒绝批次不能下载账单
    rejected_run = next(r for r in runs if r["status"] == "rejected")
    assert client.get(f"/api/billing/runs/{rejected_run['run_id']}/bills.csv").status_code == 409
    page = client.get(f"/billing/runs/{rejected_run['run_id']}")
    assert page.status_code == 200 and "整批拒绝" in page.text

    client.post("/api/import/sample")  # 恢复样例，避免污染后续用例


def test_publish_failure_rolls_back_and_keeps_old_version(client, monkeypatch):
    first = client.post("/api/billing/runs", json={"month": "2025-09", "idempotency_key": "rb-1"}).json()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure inside publish transaction")

    monkeypatch.setattr(store, "insert_bills", boom)
    monkeypatch.setattr(publisher.store, "insert_bills", boom)
    failed = client.post("/api/billing/runs", json={"month": "2025-09", "idempotency_key": "rb-2"})
    assert failed.status_code == 500
    assert "保留旧版" in failed.json()["error"]

    active = client.get("/api/billing/active?month=2025-09").json()
    assert active["active"]["version_id"] == first["version_id"]
    # 失败事务回滚：失败批次 rb-2 没有产生任何版本（版本通过 run_id 关联可查）
    versions = client.get("/api/billing/runs").json()["versions"]
    assert all(v["run_id"] != failed.json()["run_id"] for v in versions)
    monkeypatch.undo()
    recovered = client.post("/api/billing/runs", json={"month": "2025-09", "idempotency_key": "rb-3"})
    assert recovered.status_code == 201
    assert recovered.json()["previous_version"] == first["version_id"]


def test_three_csv_downloads_share_one_allocation(client):
    body = client.post("/api/billing/runs", json={"month": "2025-10", "idempotency_key": "csv-1"}).json()
    run_id = body["run_id"]
    bills_csv = client.get(f"/api/billing/runs/{run_id}/bills.csv")
    review_csv = client.get(f"/api/billing/runs/{run_id}/review.csv")
    reconcile_csv = client.get(f"/api/billing/runs/{run_id}/reconcile.csv")
    assert bills_csv.status_code == review_csv.status_code == reconcile_csv.status_code == 200

    bill_rows = list(csv.reader(io.StringIO(bills_csv.text)))
    rec_rows = list(csv.reader(io.StringIO(reconcile_csv.text)))
    assert bill_rows[0][:5] == ["月份", "表号", "表名", "租户编号", "租户名称"]
    assert rec_rows[0][:4] == ["月份", "表号", "租户数", "租户账单合计kWh"]
    # CSV 行数与 API 返回同源同量
    assert len(bill_rows) - 1 == len(body["bills"])
    assert len(rec_rows) - 1 == len(body["reconcile"])
    # 对账 CSV 全部平衡
    assert all(row[6] == "平衡" for row in rec_rows[1:])
    # 账单 CSV 合计 == 对账 CSV 的账单合计
    csv_bill_total = round(sum(float(r[5]) for r in bill_rows[1:]), 2)
    csv_rec_total = round(sum(float(r[3]) for r in rec_rows[1:]), 2)
    assert csv_bill_total == csv_rec_total


def test_full_period_run_covers_twelve_months_and_conserves(client):
    body = client.post("/api/billing/runs", json={"idempotency_key": "full-1"}).json()
    assert body["stats"]["months"] == 12
    months = {b["month"] for b in body["bills"]}
    assert months == {f"2025-{m:02d}" for m in range(4, 13)} | {f"2026-{m:02d}" for m in range(1, 4)}
    # 全期账单 grand total 与月度总表 grand total 相等
    billed = round(sum(r["billed_total"] for r in body["reconcile"]), 2)
    master = round(sum(r["master_total"] for r in body["reconcile"]), 2)
    assert billed == master == body["stats"]["billed_total"]


def test_billing_rejects_bad_month_and_unknown_meter(client):
    bad_month = client.post("/api/billing/runs", json={"month": "2025-13"})
    assert bad_month.status_code == 400 and "月份" in bad_month.json()["detail"]
    unknown = client.post("/api/billing/runs", json={"meter_ids": ["EM-99"]})
    assert unknown.status_code == 400 and "表号不存在" in unknown.json()["error"]


def test_lease_upload_validation_rejects_broken_json(client):
    resp = client.post("/api/tenants/leases",
                       files={"file": ("x.json", b"{not json", "application/json")})
    assert resp.status_code == 400 and resp.json()["ok"] is False

    bad = {"leases": [{"meter_id": "M1", "tenant_id": "", "start_date": "2026-01-01",
                       "end_date": "2026-02-01"}]}
    resp2 = client.post("/api/tenants/leases",
                        files={"file": ("x.json", json.dumps(bad).encode(), "application/json")})
    assert resp2.status_code == 400
    assert any(p["field"] == "tenant_id" for p in resp2.json()["problems"])


def test_result_page_lists_segment_evidence(client):
    body = client.post("/api/billing/runs", json={"month": "2025-09", "idempotency_key": "ev-1"}).json()
    page = client.get(f"/billing/runs/{body['run_id']}")
    assert page.status_code == 200
    assert "换租分段与分摊依据" in page.text
    assert "复核清单" in page.text
