# 04-checklane（检道）

Checklane — 数据质检通道：入检批次装载 → 规则命中与严重度裁决 → 隔离/放行签发 → 签发清单与源批对账。

**不是** Feed 多源归并（Feedstitch），**不是** 作业依赖编排（Batchgate），**不是** CDC 变更流按序落库（Changeflow）。
**也不是** 报表看板 / CRUD 后台 / 电商订单 / OA 工单。

## 启动

`ash
docker compose up --build
`

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5400 |
| API | http://localhost:10400 |

本地开发（可选）：

`ash
# backend
cd backend && pip install -r requirements.txt
set DATA_DIR=..\\data
uvicorn app.main:app --reload --port 10400

# frontend
cd frontend && npm install && npm run dev
`

## 主链

登记数据集与规则 → 装载夹具批次入检 → 按规则裁决放行/隔离 → 签发到精选区 → 批次报告与各列表数字同源

## 种子说明

- **Orders Snapshot**：JSONL 商品快照，含必填缺失与类型错误行，用于演示入检与隔离。
- **Inventory Snapshot**：库存快照，独立数据集，用于第二路规则演示。

打开「入检台」选数据集跑一次，再看隔离区 / 精选区 / 批次报告计数是否与本次报告一致。

## 规则限时豁免（rule_waiver_window）

活动期临时压过规则命中，规则本身保持启用，过期自动恢复拦截：

- **登记**：「豁免」页（`/waivers`）或 `POST /api/waivers`；绑定规则，实体键可留空（=该规则下全部实体）或点名单个键；带起止时间，半开区间 `[starts_at, ends_at)`。
- **不删规则**：被豁免的命中不参与严重度裁决（error 也不再触发隔离），但命中仍带 `waived` 标记和 `waiver_id` 留在行清单与批次报告 `waiver.waived_hits` 审计段；报告单列 `waived_rows / waived_hits` 计数。
- **未点名不放过**：豁免单点了实体键时，其他键的同规则命中照常隔离；只有不绑键的豁免才覆盖全部实体。
- **批次开始时刻快照**：入检开始时取一次 `now` 和一份活动豁免单，整批复用，检到一半改钟或改配置不影响本批口径；过期后同夹具重新入检即按原严重度隔离/阻断。
- **互证与持久化**：报告 `checks` 含 `waiver_audit_matches`（行清单回算豁免数 = 报告数，且豁免单 ID 都能追溯到批次快照）、`quarantine_batch_matches`（隔离表本批数 = 报告隔离数）、`release_matches_passed`（签发数 = 通过数）；豁免单落 SQLite，刷新页面后配置仍在。

短时效验证：登记一条 90 秒（页面有「填 90 秒短窗」按钮）的豁免后连跑两次入检，先看到「有效放过」，过期后同夹具「再中被拦」；另登记一条点名为其他键的豁免可对照「未豁免键仍被拦」。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见题单）

当前仓库已实现「单批入检 + 基础规则 + 放行签发」主链与「规则限时豁免」（见上节）。严重度整批阻断、规则包版本、抽样入检、跨字段规则、批次重开、金标对照、批内重复键、放行冻结窗、跨数据集参照检查等均在 ackend/app/modules/* 留空包，按题单逐题换主链路实现。
