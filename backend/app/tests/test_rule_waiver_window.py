"""rule_waiver_window 端到端测试。

覆盖题面四条要求：
1. 登记豁免（绑规则 + 可选实体键 + 起止时间），未点名实体键不被顺带放过；
2. 豁免不删规则：报告单列豁免条数/命中，过期后同夹具重新命中并按原严重度隔离；
3. 以批次开始时刻判定有效期；豁免数/隔离数/签发数与行清单互证；刷新后配置仍在；
4. 页面所依赖的 CRUD API 可用（前端另见 Waivers.vue）。
"""

from __future__ import annotations

import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.main
    import app.seed
    import app.services.inspect_service as svc
    from app.modules import rule_waiver_window as waiver

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.seed)
    importlib.reload(svc)
    importlib.reload(app.main)
    app.seed.init_db()
    return svc, waiver, app.main


def _make_waiver(waiver, conn, rule_id, *, entity_key=None, start=None, end=None, note=None):
    now = datetime.now(timezone.utc)
    start = start or now - timedelta(minutes=5)
    end = end or now + timedelta(minutes=5)
    return waiver.create_waiver(
        conn,
        rule_id=rule_id,
        entity_key=entity_key,
        starts_at=_iso(start),
        ends_at=_iso(end),
        note=note,
    )


def test_baseline_without_waiver(env):
    svc, _w, _m = env
    detail = svc.run_inspect(1)
    # ORD-3 缺 sku、ORD-4 价格非数字 → 2 条 error 隔离；ORD-5 仅 warn 放行
    assert detail["quarantined_count"] == 2
    assert detail["passed_count"] == 4
    report = detail["report"]
    assert report["counts"]["waived_hits"] == 0
    assert report["waiver"]["active_waivers"] == []


def test_active_key_scoped_waiver_suppresses_only_named_hit(env):
    svc, waiver, _m = env
    conn = svc.connect()
    try:
        # 只豁免 ORD-3 的 sku 必填规则（rule 1, error）
        wid = _make_waiver(waiver, conn, rule_id=1, entity_key="ORD-3", note="补货宽限")
        # 另一张豁免单点了别的键：不得顺带放过 ORD-4
        _make_waiver(waiver, conn, rule_id=2, entity_key="SOME-OTHER-KEY")
        conn.commit()
    finally:
        conn.close()

    detail = svc.run_inspect(1)
    # ORD-3 被放过；ORD-4 未被点名，仍按原 error 隔离
    assert detail["quarantined_count"] == 1
    assert detail["passed_count"] == 5
    assert detail["released_count"] == 5

    report = detail["report"]
    # 规则仍全部启用，豁免不是停用/删除
    assert report["rule_count"] == 3
    assert report["counts"]["waived_rows"] == 1
    assert report["counts"]["waived_hits"] == 1

    wsec = report["waiver"]
    assert wsec["effective_at"]
    assert len(wsec["active_waivers"]) == 2
    assert {w["id"] for w in wsec["active_waivers"]} == {wid, wid + 1}
    audit = wsec["waived_hits"]
    assert len(audit) == 1
    assert audit[0] == {
        "row_no": 3,
        "entity_key": "ORD-3",
        "rule_id": 1,
        "rule_name": "sku 必填",
        "severity": "error",
        "detail": "字段为空",
        "waiver_id": wid,
    }

    # 行清单保留被豁免命中的审计痕迹，带 waived 标记与 waiver_id
    row3 = next(r for r in detail["rows"] if r["entity_key"] == "ORD-3")
    assert row3["disposition"] == "pass"
    assert len(row3["rule_hits"]) == 1
    assert row3["rule_hits"][0]["waived"] is True
    assert row3["rule_hits"][0]["waiver_id"] == wid

    row4 = next(r for r in detail["rows"] if r["entity_key"] == "ORD-4")
    assert row4["disposition"] == "quarantine"
    assert all(not h.get("waived") for h in row4["rule_hits"])

    # 互证对账全部通过
    assert all(c["ok"] for c in report["checks"])
    names = {c["name"] for c in report["checks"]}
    assert {"waiver_audit_matches", "quarantine_batch_matches", "release_matches_passed"} <= names


def test_expired_waiver_same_fixture_rehits_at_original_severity(env):
    svc, waiver, _m = env
    now = datetime.now(timezone.utc)
    conn = svc.connect()
    try:
        # 完全落在过去的豁免窗 → 对当前批次无效
        _make_waiver(
            waiver,
            conn,
            rule_id=1,
            entity_key="ORD-3",
            start=now - timedelta(hours=2),
            end=now - timedelta(hours=1),
        )
        conn.commit()
    finally:
        conn.close()

    detail = svc.run_inspect(1)
    # 同夹具重新命中，按原 error 严重度隔离，恢复到无豁免基线
    assert detail["quarantined_count"] == 2
    assert detail["passed_count"] == 4
    assert detail["report"]["counts"]["waived_hits"] == 0
    assert detail["report"]["waiver"]["active_waivers"] == []
    row3 = next(r for r in detail["rows"] if r["entity_key"] == "ORD-3")
    assert row3["disposition"] == "quarantine"
    assert row3["rule_hits"][0]["severity"] == "error"
    assert not row3["rule_hits"][0].get("waived")


def test_short_lived_window_active_then_expired_rehit(env):
    """短时效实跑：有效放过 → 睡过失效点 → 同夹具再中。"""
    svc, waiver, _m = env
    now = datetime.now(timezone.utc)
    conn = svc.connect()
    try:
        _make_waiver(
            waiver,
            conn,
            rule_id=1,
            entity_key="ORD-3",
            start=now - timedelta(seconds=2),
            end=now + timedelta(seconds=1),
        )
        conn.commit()
    finally:
        conn.close()

    active = svc.run_inspect(1)
    assert active["quarantined_count"] == 1
    assert active["report"]["counts"]["waived_hits"] == 1

    time.sleep(1.2)  # 越过 ends_at
    expired = svc.run_inspect(1)
    assert expired["quarantined_count"] == 2
    assert expired["report"]["counts"]["waived_hits"] == 0


def test_global_waiver_covers_all_keys_but_scoped_does_not(env):
    svc, waiver, _m = env
    conn = svc.connect()
    try:
        # 不绑实体键：规则 1 下全部实体都压过
        _make_waiver(waiver, conn, rule_id=1, entity_key=None)
        conn.commit()
    finally:
        conn.close()

    detail = svc.run_inspect(1)
    # ORD-3 的 error 被全局豁免；ORD-4 命中的是另一条规则，仍隔离
    assert detail["quarantined_count"] == 1
    audit_keys = {h["entity_key"] for h in detail["report"]["waiver"]["waived_hits"]}
    assert audit_keys == {"ORD-3"}


def test_batch_start_snapshot_half_open_boundary(env):
    """有效期按传入的批次开始时刻判定，半开区间 [starts_at, ends_at)。"""
    _svc, waiver, _m = env
    conn = _svc.connect()
    try:
        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=10)
        end = now + timedelta(minutes=20)
        wid = _make_waiver(waiver, conn, rule_id=1, start=start, end=end)
        conn.commit()
        row = waiver.get_waiver(conn, wid)

        # 批次开始于窗前 → 不活动
        assert waiver.active_waivers(conn, 1, _iso(now)) == []
        # 开始时刻恰好落在 starts_at → 活动（闭端）
        assert [w["id"] for w in waiver.active_waivers(conn, 1, _iso(start))] == [wid]
        # 批次开始时刻恰好落在 ends_at → 已失效（开端）
        assert waiver.active_waivers(conn, 1, _iso(end)) == []
        # 窗内 → 活动
        assert [w["id"] for w in waiver.active_waivers(conn, 1, _iso(now + timedelta(minutes=15)))] == [wid]
        assert row["entity_key"] is None
    finally:
        conn.close()


def test_waiver_persists_across_connections(env):
    """刷新（新连接）后豁免配置仍在。"""
    svc, waiver, _m = env
    conn = svc.connect()
    try:
        wid = _make_waiver(waiver, conn, rule_id=1, entity_key="ORD-3", note="持久化")
        conn.commit()
    finally:
        conn.close()

    conn2 = svc.connect()
    try:
        rows = waiver.list_waivers(conn2, 1)
        assert len(rows) == 1
        assert rows[0]["id"] == wid
        assert rows[0]["entity_key"] == "ORD-3"
        assert rows[0]["note"] == "持久化"
        assert rows[0]["rule_name"] == "sku 必填"
        assert rows[0]["active"] is True
    finally:
        conn2.close()


def test_apply_waivers_mixed_waived_and_effective_hits(env):
    """一行同时命中两条 error：豁免一条，另一条仍按原严重度隔离。"""
    _svc, waiver, _m = env
    hits = [
        {"rule_id": 1, "name": "sku 必填", "field": "sku", "rule_type": "required",
         "severity": "error", "detail": "字段为空"},
        {"rule_id": 2, "name": "price 须为数字", "field": "price", "rule_type": "type_number",
         "severity": "error", "detail": "期望数字"},
    ]
    waivers = [{"id": 7, "rule_id": 1, "entity_key": "ORD-3", "starts_at": "x", "ends_at": "y"}]
    effective, waived = waiver.apply_waivers(hits, "ORD-3", waivers)
    assert len(effective) == 1 and effective[0]["rule_id"] == 2 and effective[0]["waived"] is False
    assert len(waived) == 1
    assert waived[0]["rule_id"] == 1 and waived[0]["waived"] is True and waived[0]["waiver_id"] == 7
    # 点名 ORD-3 的豁免对其他键 / 无键行无效
    assert waiver.match_waiver(hits[0], "ORD-4", waivers) is None
    assert waiver.match_waiver(hits[0], None, waivers) is None


def test_global_waiver_matches_keyless_row(env):
    """不绑实体键的豁免对无 entity_key 的行同样生效。"""
    _svc, waiver, _m = env
    hit = {"rule_id": 1, "name": "sku 必填", "field": "sku", "rule_type": "required",
           "severity": "error", "detail": "字段为空"}
    waivers = [{"id": 9, "rule_id": 1, "entity_key": None, "starts_at": "x", "ends_at": "y"}]
    assert waiver.match_waiver(hit, None, waivers)["id"] == 9
    assert waiver.match_waiver(hit, "ANY-KEY", waivers)["id"] == 9
    # 规则不同不豁免
    assert waiver.match_waiver({**hit, "rule_id": 2}, "ANY-KEY", waivers) is None


def test_create_validation_errors(env):
    _svc, waiver, _m = env
    conn = _svc.connect()
    now = datetime.now(timezone.utc)
    try:
        with pytest.raises(waiver.WaiverError):
            waiver.create_waiver(
                conn, rule_id=999, entity_key=None,
                starts_at=_iso(now), ends_at=_iso(now + timedelta(minutes=1)),
            )
        with pytest.raises(waiver.WaiverError):
            waiver.create_waiver(
                conn, rule_id=1, entity_key=None,
                starts_at=_iso(now), ends_at=_iso(now - timedelta(minutes=1)),
            )
        with pytest.raises(waiver.WaiverError):
            waiver.create_waiver(
                conn, rule_id=1, entity_key=None, starts_at="not-a-time",
                ends_at=_iso(now + timedelta(minutes=1)),
            )
    finally:
        conn.close()


def test_waiver_crud_api(env):
    _svc, _w, main = env
    now = datetime.now(timezone.utc)
    with TestClient(main.app) as client:
        # 登记
        r = client.post(
            "/api/waivers",
            json={
                "rule_id": 1,
                "entity_key": "ORD-3",
                "starts_at": _iso(now - timedelta(minutes=1)),
                "ends_at": _iso(now + timedelta(minutes=5)),
                "note": "api 登记",
            },
        )
        assert r.status_code == 201
        wid = r.json()["id"]
        assert r.json()["entity_key"] == "ORD-3"

        # 列表（含活动标记）
        r = client.get("/api/waivers", params={"dataset_id": 1})
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["active"] is True

        # 非法时间窗 → 400
        r = client.post(
            "/api/waivers",
            json={"rule_id": 1, "starts_at": _iso(now), "ends_at": _iso(now)},
        )
        assert r.status_code == 400

        # 规则不存在 → 400
        r = client.post(
            "/api/waivers",
            json={"rule_id": 404, "starts_at": _iso(now), "ends_at": _iso(now + timedelta(minutes=1))},
        )
        assert r.status_code == 400

        # 删除
        r = client.delete(f"/api/waivers/{wid}")
        assert r.status_code == 200
        r = client.delete(f"/api/waivers/{wid}")
        assert r.status_code == 404
        assert client.get("/api/waivers").json() == []


def test_inspect_api_with_waiver(env):
    _svc, _w, main = env
    now = datetime.now(timezone.utc)
    with TestClient(main.app) as client:
        client.post(
            "/api/waivers",
            json={
                "rule_id": 1,
                "entity_key": "ORD-3",
                "starts_at": _iso(now - timedelta(minutes=1)),
                "ends_at": _iso(now + timedelta(minutes=5)),
            },
        )
        r = client.post("/api/inspect", json={"dataset_id": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["quarantined_count"] == 1
        assert body["report"]["counts"]["waived_hits"] == 1
        assert body["report"]["waiver"]["waived_hits"][0]["entity_key"] == "ORD-3"
