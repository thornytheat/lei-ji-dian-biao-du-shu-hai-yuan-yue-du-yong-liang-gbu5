# 园区电表累计读数还原（metercalc）

园区能源组抄表员月底核对用的小工具：电表是**累计读数**，中间换过表、表跑满回绕过，
还要保证峰/谷与总量对得上、缺口不能悄悄插值。系统把读数 CSV 还原成**月度用能表**，
并给出每条数字的依据（区间起止 + 分摊公式）与复核清单。

在还原能力之上，系统还提供**租户账单与换租分摊**：表计换租后按租约生效时段拆分用量，
换租日落在抄表区间内时先分段、再分摊到月份，账单 / 复核清单 / CSV 下载共用同一份分配结果，
并以原子版本切换发布（失败保留旧版、同批重试不新增账单）。

## 能力

- **读数导入**：CSV 严格校验（列契约、ISO 日期真实性、数值、电量类型、重复读数），任一行不合法即整体拒绝并给出行号/列名/原因。
- **换表/回绕识别**：`后读数 < 前读数` 时按换表记录分段求和，或按量程还原回绕量；两者都不成立就隔离为异常。
- **用量还原**：逐区间给出前/后读数、区间天数、用量与算式。
- **月度分摊**：区间跨月按天数比例分摊，保留 2 位小数，用最大余数法保证分摊合计与区间用量**完全相等**。
- **分时校验**：`peak + valley ≈ total`，容差 0.5%，超差记异常但仍输出数值。
- **复核清单**：月度台账（每表每月 total/peak/valley/缺口天数/异常数）+ P1/P2/P3 复核项 + CSV 导出。
- **租户换租账单**：按租约 `[生效日, 到期日)` 拆分用量；换租日落在抄表区间内时凭交接读数先切租户子区间、再按月分摊；
  租约重叠 / 断档 / 缺交接读数 / 交接不自洽**整批拒绝**；每表每月租户账单合计与月度总表对账必须为 0 差额。
- **原子发布与幂等**：账单落库 + 版本切换在单事务内完成，发布失败回滚并保留旧版；同一幂等键重试回放首个批次，不新增账单或重复用量。

## 核心规则

1. **区间用量**：`usage = 后读数 − 前读数`。
   - 后 < 前 且区间被换表记录覆盖（`start < 换表日 ≤ end`）→ 两表分段：
     `旧表尾段 = 拆表读数 − 前读数`，`新表首段 = 后读数 − 装表读数`，合计为区间用量。
   - 否则若 `前读数 ≥ 量程 × 0.8` 且 `后读数 ≤ 量程 × 0.2` → 回绕：`量程 − 前读数 + 后读数`。
   - 两者都不成立（或换表记录与读数不自洽）→ `anomaly`，该区间用量为 null，**不参与汇总**。
   - 量程默认 `99999.9`，可按表台账配置或用请求参数覆盖。
2. **月度分摊**：`[起始读数日, 结束读数日)` 按日历月切桶，份额 = `用量 × 该月天数 / 区间总天数`；
   以「分」为整数单位用最大余数法分配，`sum(分摊) == round(区间用量, 2)`，尾差落在余数最大的月份。
3. **分时一致性**：`|peak + valley − total| ≤ 0.5% × total`，超差进复核清单，数值照常输出。
4. **缺口**：相邻读数间隔 > 31 天 → `gap`，输出缺口区间、缺失天数、建议抄表日（区间中点），**禁止插值**。
5. **汇总**：每表每月 total/peak/valley/缺口天数/异常数（异常数 = 隔离异常 + 分时超差）+ 复核清单。
6. **租户换租分摊**：
   - 租约按半开区间 `[start_date, end_date)` 登记（`2999-12-31` 表示在租）；同一表的租约链必须首尾相接，
     **重叠或断档即整批拒绝**。
   - 换租日落在抄表区间 `[前读数日, 后读数日)` 内时，必须有该日 total/peak/valley **交接读数**；
     先用交接读数把区间切成「旧租户尾段 + 新租户首段」（`sum(子段) == 区间用量`，尾差并入末段守恒），
     每个子段再按日历月天数做最大余数法分摊。
   - 换租日恰好是抄表日时直接用现场读数切分；交接读数与同日现场读数不一致（差 > 0.05）整批拒绝。
   - 回绕 / 换表区间内的换租：回绕段按量程平移后做差，表计轮换不改变租户归属。
   - **双层守恒**：子段合计 = 区间用量；每表每月所有租户账单合计 = 读数侧月度总表，差额必须 0.00 kWh，不平整批拒绝。
7. **发布语义**：`publish` 在单事务内完成 账单落库 → 旧 active 版本置 superseded → 新版本置 active → 读回校验；
   任一步失败整体回滚，旧版继续可用。`validate` 只试算不落版本。请求带 `idempotency_key` 时，同键重试直接回放首个批次
   （包括被拒绝的批次），不新增账单、不产生重复用量。

## 启动

```bash
cd batch-20261114b/projects/gb-628
cp .env.example .env          # 可选，默认端口 18217
docker compose up -d --build
docker compose ps             # 等 app 变 healthy
curl -s http://127.0.0.1:18217/health
```

仪表盘页面：<http://127.0.0.1:18217/>

SQLite 使用命名卷 `metercalc-data:/app/data`，启动时按样例版本幂等 seed（重复启动不产生脏数据）。
重建数据库：`docker compose down -v` 后重新 `up`。

## 演示步骤

1. 打开 `/` 看场景、输入与样例概览（4 块表、4140 行读数、2025-04 ~ 2026-03、换表/回绕/缺口/超差/无法判定各几处）。
2. 进 `/workbench`，月份选 `2026-03`，点「执行还原」，进度依次走 读数校验 → 换表/回绕识别 → 用量还原 → 月度分摊 → 分时校验 → 台账生成。
3. 结果页 `/result/{run_id}` 看：总用量 2225.82 kWh；EM-01 分时超差（峰+谷比总量少 49.2）；EM-03 跨月区间按 26/35 天分摊；EM-04 读数回退被隔离；EM-03 缺口 35 天、建议抄表日 2026-03-09。
4. 换表月份：工作台选 `2025-09`（EM-02 于 09-15 换表，区间用量 28.0 = 13.4 + 14.6）。回绕月份：选 `2025-12`（EM-03 用量 17.4）。
5. `/review` 看 12 个月全量台账与复核清单，导出 `/api/ledger.csv`、`/api/review.csv`。
6. 进 `/billing`，月份选 `2025-10`（EM-01 于 10-01 换租 T-A01 → T-A02），点「生成并发布账单」：
   看租户月度账单、账单合计与月度总表对账差额 0.00、版本 `active/superseded` 切换；再用相同幂等键重试，返回 `replayed=true` 且不新增账单。
   故意把某条租约到期日改早制造断档后上传，发布返回 400 整批拒绝并列出全部问题，旧版本保持 active。

命令行复现：

```bash
curl -s -X POST http://127.0.0.1:18217/api/import/sample
curl -s -X POST http://127.0.0.1:18217/api/restore \
  -H 'Content-Type: application/json' -d '{"month":"2026-03"}' | python3 -m json.tool | head -40
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:18217/api/restore \
  -H 'Content-Type: application/json' -d '{"month":"2026-03","meter_ids":["EM-99"]}'   # 400

# 租户账单：试算 → 原子发布 → 同键重试（回放，不新增账单）→ 三份 CSV
curl -s -X POST http://127.0.0.1:18217/api/billing/runs \
  -H 'Content-Type: application/json' -d '{"month":"2025-10","mode":"validate"}'
curl -s -X POST http://127.0.0.1:18217/api/billing/runs \
  -H 'Content-Type: application/json' -d '{"month":"2025-10","idempotency_key":"batch-001"}'
curl -s -X POST http://127.0.0.1:18217/api/billing/runs \
  -H 'Content-Type: application/json' -d '{"month":"2025-10","idempotency_key":"batch-001"}'  # replayed=true
curl -s http://127.0.0.1:18217/api/billing/active?month=2025-10 | python3 -m json.tool | head -20
```

## API / 页面

| GET|POST 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 + 读数/表数/租约/交接读数计数 |
| GET | `/` | 首页：场景、输入契约、样例概览、演示步骤 |
| GET | `/workbench` | 工作台：选表选月份、导入读数、执行还原（带进度） |
| GET | `/result/{run_id}` | 结果页：月度用能表、分摊依据、缺口、隔离异常、复核清单 |
| GET | `/review` | 月度台账（全期）+ 复核清单 + 导出 |
| GET | `/billing` | 租户账单工作台：租约/交接读数导入、试算与原子发布、版本流水 |
| GET | `/billing/runs/{run_id}` | 账单批次详情：租户账单、对账、换租分段依据、复核清单 |
| POST | `/api/import` | multipart 上传读数 CSV（UTF-8/GBK），严格校验，整体替换 |
| POST | `/api/import/sample` | 重新导入样例（幂等，含租约与交接读数） |
| POST | `/api/restore` | `{month, meter_ids?, full_scale?, max_gap_days?}` → 201 + run_id + 台账 + 依据 |
| GET | `/api/runs`, `/api/runs/{run_id}` | 批次列表 / 批次详情 |
| GET | `/api/runs/{run_id}/ledger.csv`、`/api/runs/{run_id}/review.csv` | 本月台账 / 复核清单 CSV |
| GET | `/api/ledger.csv`、`/api/review.csv` | 全期台账 / 复核清单 CSV |
| POST | `/api/tenants/leases` | 上传租约 JSON（整体替换，字段级严格校验） |
| POST | `/api/tenants/handovers` | 上传交接读数 JSON（整体替换） |
| GET | `/api/tenants/leases`、`/api/tenants/handovers` | 当前租约 / 交接读数 |
| POST | `/api/billing/runs` | `{month?, meter_ids?, mode?, idempotency_key?}` 生成账单；publish 原子发布 / validate 只试算；整批拒绝返回 400 + 全部问题 |
| GET | `/api/billing/runs`、`/api/billing/runs/{run_id}` | 账单批次（含 rejected）列表 / 详情 |
| GET | `/api/billing/active?month=` | 当前生效版本及其账单行 |
| GET | `/api/billing/runs/{run_id}/bills.csv`、`/review.csv`、`/reconcile.csv` | 账单 / 复核 / 对账三份 CSV，与批次同一份分配结果 |

错误输入返回 4xx 且带中文原因：未知表号（400 + 可选表号列表）、月份格式错误（400）、
该月无读数覆盖（400）、量程非正（400）、缺口阈值 < 2（400）、CSV 行级错误（400 + 逐行问题）。

## 目录

```text
app/
  main.py          # 路由（薄）：页面 + JSON API（还原 + 租户账单）
  settings.py      # 路径/常量（量程、缺口阈值、容差、样例版本）
  readings.py      # 读数解析/校验/分组/缺口检测
  restore.py       # 区间用量、换表分段、回绕、异常隔离
  proration.py     # 跨月按天分摊（最大余数法守恒）
  leases.py        # 租约/交接读数解析校验、按租约生效时段拆分区间（重叠/断档/缺交接整批拒绝）
  billing.py       # 租户账单引擎：分段→分摊→账单→对账→复核→CSV（一份分配结果）
  publisher.py     # 原子发布、版本切换、失败回滚、幂等重试
  validation.py    # 峰谷容差、缺口月份重叠、覆盖说明
  summary.py       # 月度台账、复核清单、CSV 导出
  pipeline.py      # 编排：一次还原 → 全月份汇总 + 指定月份聚焦
  store.py         # SQLite（读数/换表/租约/交接/账单/批次/版本/事件）
  seed.py          # 幂等 seed
  templates/       # base/index/workbench/result/review/billing/billing_result
  static/          # app.js, style.css（原生 JS/CSS，无框架无 CDN）
sample_data/
  readings.csv         # 4 块表 × 每日读数 × 12 个月（total/peak/valley）= 4140 行
  meter_changes.json   # 换表记录（旧表号/新表号/换表日/拆表读数/装表读数，按类型分开）
  meters.json          # 表计台账（名称/位置/量程）
  leases.json          # 租约台账（表号/租户/生效日/到期日，9 条，覆盖 4 块表的换租场景）
  handovers.json       # 换租交接读数（换租日 × total/peak/valley，15 条，数值与现场读数对齐）
tests/                 # test_restore / test_proration / test_validation / test_api / test_tenants / test_billing_api
```

样例数据里刻意埋了：1 次换表（EM-02 2025-09-15）、1 次满量程回绕（EM-03 2025-12-17）、
2 段 >31 天缺口（EM-04 通讯中断 46 天、EM-03 连续漏抄 35 天）、1 组峰谷超差（EM-01 2026-03）、
1 处无法判定异常（EM-04 2026-03-17 读数回退且无换表记录）；
租约侧埋了 5 个换租日（EM-01 2025-10-01、EM-02 2025-07-01 / 2026-02-01、EM-03 2025-07-01、
EM-04 2026-02-01），交接读数与对应抄表日现场读数一致，可直接验证「先分段再分摊 + 对账为 0」。

## 已知限制

- 换表记录的拆表/装表读数需由人工签认后登记，系统不做 OCR 或现场采集。
- 回绕判定依赖量程配置（默认 99999.9）；量程配错会把真实回绕判成异常，或反之。
- 缺口期间没有读数，系统不做任何插值；缺口越大，该月日均值参考意义越小。
- 表计倍率（CT 变比）在台账里只登记不参与换算（样例倍率均为 1.0）。
- 单租户单进程演示：无登录、无权限、无并发写保护，SQLite 为本地文件。
