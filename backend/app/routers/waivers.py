from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules.rule_waiver_window import WaiverError, create_waiver, delete_waiver, get_waiver, list_waivers
from app.schemas.entities import WaiverCreate

router = APIRouter(tags=["waivers"])


@router.get("/waivers")
def get_waivers(dataset_id: int | None = None):
    conn = connect()
    try:
        return list_waivers(conn, dataset_id)
    finally:
        conn.close()


@router.post("/waivers", status_code=201)
def post_waiver(body: WaiverCreate):
    conn = connect()
    try:
        try:
            waiver_id = create_waiver(
                conn,
                rule_id=body.rule_id,
                entity_key=body.entity_key,
                starts_at=body.starts_at,
                ends_at=body.ends_at,
                note=body.note,
            )
        except WaiverError as exc:
            raise HTTPException(400, str(exc)) from exc
        conn.commit()
        return get_waiver(conn, waiver_id)
    finally:
        conn.close()


@router.delete("/waivers/{waiver_id}")
def remove_waiver(waiver_id: int):
    conn = connect()
    try:
        if not delete_waiver(conn, waiver_id):
            raise HTTPException(404, "waiver not found")
        conn.commit()
        return {"id": waiver_id, "deleted": True}
    finally:
        conn.close()
