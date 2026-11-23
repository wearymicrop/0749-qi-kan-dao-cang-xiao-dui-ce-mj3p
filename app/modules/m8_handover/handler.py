from fastapi import APIRouter

from app.modules.m8_handover.models import NoticeBatch
from app.modules.m8_handover.service import (
    current_handover,
    handover_state,
    handover_version,
    ingest_batch,
)

router = APIRouter(prefix="/api/handover")


@router.post("/batches")
def deliver_batch(batch: NoticeBatch):
    """送达一批来函（首批须随附 rules），返回本批处理结果与最新版交班清单。"""
    return ingest_batch(batch.model_dump())


@router.get("/current")
def current():
    """当前可交班版本；处理失败后下一班仍取此版，不会被半成品覆盖。"""
    return current_handover()


@router.get("/versions/{version}")
def version_detail(version: int):
    return handover_version(version)


@router.get("/versions")
def versions():
    state = handover_state()
    return {"versions": state["snapshots"], "current": state["current"]}


@router.get("/ledger")
def ledger():
    """来函台账：全部原件（含隔离与重放记录），用于交班审计。"""
    state = handover_state()
    return {
        "inbox": state["inbox"],
        "notices": state["notices"],
        "quarantine": state["quarantine"],
        "replays": state["replays"],
        "rules": state["rules"],
    }
