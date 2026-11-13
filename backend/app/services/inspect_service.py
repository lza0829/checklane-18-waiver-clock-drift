from __future__ import annotations

import json
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.parse_fixture import parse_jsonl
from app.engines.rule_engine import decide_disposition, evaluate_row
from app.modules.rule_waiver_window import active_waivers, apply_waivers
from app.repositories import batches as batch_repo
from app.repositories import curated as curated_repo
from app.repositories import datasets as dataset_repo
from app.repositories import quarantine as quarantine_repo
from app.repositories import rules as rules_repo
from app.seed import _now


def run_inspect(dataset_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        ds = dataset_repo.get_dataset(conn, dataset_id)
        if not ds:
            raise ValueError(f"dataset {dataset_id} not found")
        if not ds["enabled"]:
            raise ValueError(f"dataset {dataset_id} disabled")

        path = FIXTURES_DIR / ds["fixture_path"]
        rows = parse_jsonl(path)
        rules = [r for r in rules_repo.list_rules(conn, dataset_id) if r.get("enabled")]

        # 批次开始时刻快照：整批只认开跑那一刻有效的豁免，
        # 开跑后改窗/改钟不改写本批已开始的判定。
        batch_start = _now()
        waivers = active_waivers(conn, dataset_id, batch_start)

        scanned = len(rows)
        passed = failed = quarantined = released = 0
        waived_rows = 0
        waived_hit_count = 0
        waived_audit: list[dict[str, Any]] = []
        row_results: list[dict[str, Any]] = []
        waiver_ids: set[int] = set()

        # pre-insert batch shell with zeros; update via rewrite after we know counts
        # simpler: compute in memory then insert once
        for idx, payload in enumerate(rows, start=1):
            waiver_ids |= {w["id"] for w in waivers}
            # 先按全部启用规则求值（规则不因豁免而停用），再压过有效期内的命中
            raw_hits = evaluate_row(payload, rules)
            entity_key = str(payload.get("entity_key") or "") or None
            effective_hits, waived_hits = apply_waivers(raw_hits, entity_key, waivers)
            # 裁决只看仍生效的命中；被豁免的 error 不再触发隔离
            disposition = decide_disposition(effective_hits)
            if disposition == "quarantine":
                quarantined += 1
                failed += 1
            else:
                passed += 1
            if waived_hits:
                waived_rows += 1
                waived_hit_count += len(waived_hits)
                for h in waived_hits:
                    waived_audit.append(
                        {
                            "row_no": idx,
                            "entity_key": entity_key,
                            "rule_id": h["rule_id"],
                            "rule_name": h["name"],
                            "severity": h["severity"],
                            "detail": h["detail"],
                            "waiver_id": h["waiver_id"],
                        }
                    )
            # 行清单保留全部命中（含 waived 标记），被豁免的命中仍可审计
            all_hits = [*waived_hits, *effective_hits]
            row_results.append(
                {
                    "row_no": idx,
                    "entity_key": entity_key,
                    "payload": payload,
                    "disposition": disposition,
                    "rule_hits": all_hits,
                    "waived_hits": waived_hits,
                }
            )

        checks = [
            {
                "name": "scan_breakdown",
                "ok": scanned == passed + quarantined,
                "detail": f"scanned({scanned}) = passed({passed}) + quarantined({quarantined})",
            }
        ]

        # insert batch first to get id, then rows / quarantine / curated
        report = {
            "dataset_id": dataset_id,
            "dataset_name": ds["name"],
            "fixture_path": ds["fixture_path"],
            "rule_count": len(rules),
            "counts": {
                "scanned": scanned,
                "passed": passed,
                "failed": failed,
                "quarantined": quarantined,
                "released": 0,
                "waived_rows": waived_rows,
                "waived_hits": waived_hit_count,
            },
            "waiver": {
                "effective_at": batch_start,
                "active_waivers": [
                    {
                        "id": w["id"],
                        "rule_id": w["rule_id"],
                        "entity_key": w["entity_key"],
                        "starts_at": w["starts_at"],
                        "ends_at": w["ends_at"],
                        "note": w["note"],
                    }
                    for w in waivers
                ],
                "waived_rows": waived_rows,
                "waived_hit_count": waived_hit_count,
                "waived_hits": waived_audit,
            },
            "checks": checks,
        }
        batch_id = batch_repo.insert_batch(
            conn,
            dataset_id=dataset_id,
            status="inspected",
            scanned=scanned,
            passed=passed,
            failed=failed,
            quarantined=quarantined,
            released=0,
            report=report,
            created_at=batch_start,
        )

        for item in row_results:
            batch_repo.insert_row(
                conn,
                batch_id=batch_id,
                row_no=item["row_no"],
                entity_key=item["entity_key"],
                payload=item["payload"],
                disposition=item["disposition"],
                rule_hits=item["rule_hits"],
            )
            if item["disposition"] == "quarantine":
                reasons = "; ".join(
                    f"{h['name']}:{h['detail']}"
                    for h in item["rule_hits"]
                    if h.get("severity") == "error" and not h.get("waived")
                )
                quarantine_repo.insert(
                    conn,
                    batch_id=batch_id,
                    dataset_id=dataset_id,
                    row_no=item["row_no"],
                    entity_key=item["entity_key"],
                    reason=reasons or "rule hit",
                    payload=item["payload"],
                    created_at=batch_start,
                )
            else:
                # pass rows auto-release to curated (base chain)
                key = item["entity_key"] or f"row-{item['row_no']}"
                curated_repo.upsert(
                    conn,
                    dataset_id=dataset_id,
                    batch_id=batch_id,
                    entity_key=key,
                    payload=item["payload"],
                    released_at=batch_start,
                )
                released += 1

        report["counts"]["released"] = released
        report["checks"].append(
            {
                "name": "release_matches_passed",
                "ok": released == passed,
                "detail": f"released({released}) = passed({passed})",
            }
        )
        report["checks"].append(
            {
                "name": "quarantine_table_matches",
                "ok": quarantine_repo.count_all(conn) >= quarantined,
                "detail": "quarantine rows persisted for this and prior batches",
            }
        )

        # 互证：从已落库的行清单/隔离表回算，与报告豁免数、隔离数对账
        persisted_rows = batch_repo.list_batch_rows(conn, batch_id)
        persisted_waived_hits = sum(
            1 for r in persisted_rows for h in r["rule_hits"] if h.get("waived")
        )
        persisted_waived_rows = sum(
            1 for r in persisted_rows if any(h.get("waived") for h in r["rule_hits"])
        )
        referenced_waiver_ids = {
            h["waiver_id"]
            for r in persisted_rows
            for h in r["rule_hits"]
            if h.get("waived")
        }
        batch_quarantine = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine WHERE batch_id=?", (batch_id,)
        ).fetchone()["c"]
        report["checks"].append(
            {
                "name": "quarantine_batch_matches",
                "ok": batch_quarantine == quarantined,
                "detail": f"quarantine table for batch({batch_quarantine}) = report quarantined({quarantined})",
            }
        )
        report["checks"].append(
            {
                "name": "waiver_audit_matches",
                "ok": (
                    persisted_waived_hits == waived_hit_count
                    and persisted_waived_rows == waived_rows
                    and referenced_waiver_ids <= waiver_ids
                ),
                "detail": (
                    f"row-list waived hits({persisted_waived_hits}) = report({waived_hit_count}); "
                    f"waived rows({persisted_waived_rows}) = report({waived_rows}); "
                    f"all waiver ids trace back to batch-start snapshot"
                ),
            }
        )
        conn.execute(
            """
            UPDATE inspect_batches
            SET released_count=?, report_json=?, status=?
            WHERE id=?
            """,
            (released, json.dumps(report, ensure_ascii=False), "released" if released else "inspected", batch_id),
        )
        conn.commit()

        detail = batch_repo.get_batch(conn, batch_id)
        assert detail is not None
        detail["rows"] = persisted_rows
        detail["curated_total"] = curated_repo.count_all(conn, dataset_id)
        detail["quarantine_total"] = quarantine_repo.count_all(conn)
        return detail
    finally:
        conn.close()


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        datasets = conn.execute("SELECT COUNT(*) AS c FROM datasets").fetchone()["c"]
        rules = conn.execute("SELECT COUNT(*) AS c FROM rules WHERE enabled=1").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM inspect_batches").fetchone()["c"]
        return {
            "dataset_count": datasets,
            "enabled_rule_count": rules,
            "batch_count": batches,
            "curated_count": curated_repo.count_all(conn),
            "quarantine_count": quarantine_repo.count_all(conn),
        }
    finally:
        conn.close()
