"""规则限时豁免（rule_waiver_window）。

活动期内临时压过某条规则的命中，但规则本身保持启用：
- 豁免单绑定规则，可再绑定实体键（不绑 = 该规则下全部实体）；
- 带 [starts_at, ends_at) 半开有效期，仅在有效期内压过命中；
- 点名实体键的豁免不得顺带放过其他实体键；
- 是否有效以「批次开始时刻」为准，整批复用同一快照，避免检到一半口径漂移。

本模块只放纯逻辑与接收外部 conn 的 CRUD，不直接持有数据库连接，
与 repositories 层的写法保持一致。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


class WaiverError(ValueError):
    """豁免单登记参数非法（规则不存在 / 时间窗非法）。"""


def now_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时间；无时区后缀按 UTC 处理，统一返回带 UTC 时区的 datetime。"""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_active(waiver: dict[str, Any], effective_at: str | datetime) -> bool:
    """半开区间 [starts_at, ends_at)：开始时刻生效，结束时刻即失效。"""
    moment = effective_at if isinstance(effective_at, datetime) else parse_ts(effective_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    start = parse_ts(waiver["starts_at"])
    end = parse_ts(waiver["ends_at"])
    return start <= moment < end


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    # 显式 None，避免空串被当成「点名了空实体」
    d["entity_key"] = d["entity_key"] or None
    return d


def create_waiver(
    conn: sqlite3.Connection,
    *,
    rule_id: int,
    entity_key: str | None,
    starts_at: str,
    ends_at: str,
    note: str | None = None,
    created_at: str | None = None,
) -> int:
    rule = conn.execute("SELECT id FROM rules WHERE id=?", (rule_id,)).fetchone()
    if not rule:
        raise WaiverError(f"rule {rule_id} not found")
    try:
        start = parse_ts(starts_at)
        end = parse_ts(ends_at)
    except (TypeError, ValueError) as exc:
        raise WaiverError(f"bad timestamp: {exc}") from exc
    if not start < end:
        raise WaiverError("ends_at must be later than starts_at")
    key = entity_key.strip() if isinstance(entity_key, str) and entity_key.strip() else None
    cur = conn.execute(
        """
        INSERT INTO rule_waivers(rule_id, entity_key, starts_at, ends_at, note, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (rule_id, key, starts_at.strip(), ends_at.strip(), note, created_at or now_ts()),
    )
    return int(cur.lastrowid)


def get_waiver(conn: sqlite3.Connection, waiver_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, rule_id, entity_key, starts_at, ends_at, note, created_at "
        "FROM rule_waivers WHERE id=?",
        (waiver_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_waivers(
    conn: sqlite3.Connection,
    dataset_id: int | None = None,
    *,
    effective_at: str | None = None,
) -> list[dict[str, Any]]:
    """列出豁免单；可按数据集过滤，并可附 effective_at 计算此刻是否活动。"""
    if dataset_id is None:
        rows = conn.execute(
            """
            SELECT w.id, w.rule_id, w.entity_key, w.starts_at, w.ends_at, w.note, w.created_at,
                   r.dataset_id, r.name AS rule_name, r.severity, r.enabled
            FROM rule_waivers w JOIN rules r ON r.id = w.rule_id
            ORDER BY w.id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT w.id, w.rule_id, w.entity_key, w.starts_at, w.ends_at, w.note, w.created_at,
                   r.dataset_id, r.name AS rule_name, r.severity, r.enabled
            FROM rule_waivers w JOIN rules r ON r.id = w.rule_id
            WHERE r.dataset_id=?
            ORDER BY w.id DESC
            """,
            (dataset_id,),
        ).fetchall()
    moment = effective_at or now_ts()
    out = []
    for row in rows:
        d = _row_to_dict(row)
        d["active"] = is_active(d, moment)
        out.append(d)
    return out


def delete_waiver(conn: sqlite3.Connection, waiver_id: int) -> bool:
    cur = conn.execute("DELETE FROM rule_waivers WHERE id=?", (waiver_id,))
    return cur.rowcount > 0


def active_waivers(
    conn: sqlite3.Connection, dataset_id: int, effective_at: str | datetime
) -> list[dict[str, Any]]:
    """批次开始时刻快照：只返回该数据集下在 effective_at 处于有效期内的豁免单。

    入检流程在批次开始时调用一次，整批复用返回值，中途改钟/改配置不影响本批。
    时间比较走 Python（parse_ts 归一化 UTC），不依赖字符串字典序。
    """
    rows = conn.execute(
        """
        SELECT w.id, w.rule_id, w.entity_key, w.starts_at, w.ends_at, w.note, w.created_at
        FROM rule_waivers w JOIN rules r ON r.id = w.rule_id
        WHERE r.dataset_id=?
        """,
        (dataset_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows if is_active(_row_to_dict(r), effective_at)]


def match_waiver(
    hit: dict[str, Any], entity_key: str | None, waivers: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """命中能否被豁免：规则必须相同；豁免单点名实体键时键必须完全相等。

    未点名实体键（entity_key IS NULL）= 该规则下全部实体；
    点名了别的键 → 不放行，防止顺带放过。
    """
    for waiver in waivers:
        if waiver["rule_id"] != hit["rule_id"]:
            continue
        # 点名了实体键的豁免只放行完全相等的键；未点名（None）= 该规则下全部实体
        if waiver["entity_key"] is not None and waiver["entity_key"] != entity_key:
            continue
        return waiver
    return None


def apply_waivers(
    hits: list[dict[str, Any]],
    entity_key: str | None,
    waivers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把一行的原始命中拆成（仍生效命中, 被豁免命中）。

    被豁免的命中不参与严重度裁决，但保留规则名/严重度/明细并打上
    waived 标记与 waiver_id，作为「本批哪些命中被豁免」的审计凭据。
    """
    effective_hits: list[dict[str, Any]] = []
    waived_hits: list[dict[str, Any]] = []
    for hit in hits:
        waiver = match_waiver(hit, entity_key, waivers)
        annotated = dict(hit)
        if waiver is None:
            annotated["waived"] = False
            effective_hits.append(annotated)
        else:
            annotated["waived"] = True
            annotated["waiver_id"] = waiver["id"]
            waived_hits.append(annotated)
    return effective_hits, waived_hits
