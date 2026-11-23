import hashlib
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone

from pydantic import ValidationError

from app.config import RULE_VERSION
from app.errors import LedgerError
from app.manifest import canonical
from app.modules.m1_orders.service import build_orders
from app.modules.m8_handover import store
from app.modules.m8_handover.models import Notice
from app.modules.m8_handover.parser import parse_cover

STATUS_LABELS = {"suspended": "停刊", "resumed": "已恢复", "pending": "仍待确认"}
EFFECT_LABELS = {"pause": "暂停出刊", "terminate": "终止出版"}


def notice_digest(raw):
    """同一份来函重跑的幂等键：编号、类型、效力与覆盖范围完全一致才视为同一件。"""
    payload = {key: raw.get(key) for key in
               ("letter_no", "kind", "effect", "chain", "year", "volume", "issue_first", "issue_last")}
    return hashlib.sha256(canonical(payload).encode()).hexdigest()

def _slot_index(orders):
    index = {(slot["chain"], slot["year"], slot["issue"]): slot for slot in orders["slots"]}
    years = {(row["chain"], row["year"]): row for row in orders["years"]}
    return index, years


def validate_notice(raw, slot_index, year_index):
    try:
        notice = Notice.model_validate(raw).model_dump()
    except ValidationError as error:
        fields = [str(item["loc"][0]) for item in error.errors() if item["loc"]]
        return None, ("NOTICE_SCHEMA", fields)
    try:
        cover = parse_cover(notice["volume"], notice["issue_first"], notice["issue_last"])
    except LedgerError as error:
        return None, (error.code, ["volume", "issue_first", "issue_last"])
    year_row = year_index.get((notice["chain"], notice["year"]))
    if year_row is None:
        return None, ("OUT_OF_ORDER", ["chain", "year"])
    if cover["volume"] != year_row["volume"]:
        return None, ("WRONG_VOLUME", ["volume"])
    if cover["first"] < year_row["first"] or cover["last"] > year_row["last"]:
        return None, ("OUTSIDE_RANGE", ["issue_first", "issue_last"])
    keys = [(notice["chain"], notice["year"], issue) for issue in range(cover["first"], cover["last"] + 1)]
    if any(key not in slot_index for key in keys):
        return None, ("OUTSIDE_RANGE", ["issue_first", "issue_last"])
    return {**notice, "cover": cover, "keys": keys}, None


def derive_handover(orders, notices):
    """从全部已采信来函推导每个受影响期位的停/复/待确认状态与依据链。"""
    effects = defaultdict(list)
    for notice in notices:
        for key in notice["keys"]:
            effects[tuple(key)].append(notice)
    rows = []
    for slot in orders["slots"]:
        key = (slot["chain"], slot["year"], slot["issue"])
        trail = effects.get(key)
        if not trail:
            continue
        latest = trail[-1]
        if latest["kind"] == "resumption":
            status = "resumed"
        elif latest["effect"] == "terminate":
            status = "suspended"
        else:
            status = "pending"
        rows.append({
            **slot, "status": status,
            "basis": [{
                "letter_no": item["letter_no"], "kind": item["kind"],
                "effect": item["effect"] if item["kind"] == "suspension" else None,
                "title": item.get("title"), "issued_on": item.get("issued_on"),
                "cover": f'{item["cover"]["first"]}–{item["cover"]["last"]}期',
            } for item in trail],
            "latest_letter": latest["letter_no"],
        })
    counts = Counter(row["status"] for row in rows)
    return rows, {key: counts[key] for key in ("suspended", "resumed", "pending")}


def _document(orders, notices, quarantine, replays, inbox):
    rows, counts = derive_handover(orders, notices)
    # 摘要覆盖整份台账：采信、隔离、重放或原件任一处变化都会产生新版，避免错误复用旧清单
    ledger_digest = hashlib.sha256(canonical({
        "notices": [{"letter_no": n["letter_no"], "digest": n["digest"]} for n in notices],
        "quarantine": [{"seq": q["inbox_seq"], "code": q["code"]} for q in quarantine],
        "replays": [{"seq": r["inbox_seq"], "digest": r["digest"]} for r in replays],
        "inbox": [{"seq": item["seq"], "raw": item["raw"]} for item in inbox],
    }).encode()).hexdigest()
    digest = hashlib.sha256(f"{RULE_VERSION}:{ledger_digest}".encode()).hexdigest()
    return {
        "digest": digest,
        "version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {"affected": len(rows), **counts},
        "slots": rows,
        "ledger": {
            "inbox_count": len(inbox),
            "accepted": len(notices),
            "quarantine": len(quarantine),
            "replays": len(replays),
        },
        "quarantine": quarantine,
        "replays": replays,
        "inbox": inbox,
    }


def ingest_batch(batch, received_at=None):
    """两段式处理：
    事务①逐件把原件写入来函台账（坏件隔离、重件记重放），逐件提交、不回滚原件；
    事务②依据已采信来函重新发布交班清单，发布失败时旧版清单仍可交班。
    """
    received_at = received_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    batch_id = uuid.uuid4().hex
    db = store.connect()
    try:
        with db:
            pinned = store.pinned_rules(db)
            rules = pinned["rules"] if pinned is not None else batch.get("rules")
            if not rules:
                raise LedgerError("NO_RULES", "首批来函须随附订购规则，后续批次沿用首批规则")
            orders = build_orders(rules)
            store.register_batch(db, batch_id, received_at, batch.get("note"))
        slot_index, year_index = _slot_index(orders)

        accepted = quarantined = replayed = 0
        for line, raw in enumerate(batch["notices"], 1):
            # 每件来函独立事务：任何一件处理失败都不影响其他原件入账
            with db:
                inbox_seq = store.stamp_inbox(db, batch_id, received_at, raw)
                if raw is None or raw == {} or raw == "":
                    store.insert_quarantine(db, inbox_seq, batch_id, line, "EMPTY_ROW", [], raw)
                    quarantined += 1
                    continue
                if not isinstance(raw, dict):
                    store.insert_quarantine(db, inbox_seq, batch_id, line, "NOTICE_SCHEMA", ["notices"], raw)
                    quarantined += 1
                    continue
                digest = notice_digest(raw)
                existing = store.find_notice(db, digest)
                if existing is not None:
                    # 同一份来函重跑：只登记重放，绝不重复增减期位
                    store.insert_replay(db, inbox_seq, batch_id, line,
                                        existing["seq"], existing["letter_no"], digest)
                    replayed += 1
                    continue
                notice, error = validate_notice(raw, slot_index, year_index)
                if error is not None:
                    code, fields = error
                    store.insert_quarantine(db, inbox_seq, batch_id, line, code, fields, raw)
                    quarantined += 1
                    continue
                same_no = db.execute(
                    "SELECT kind FROM handover_notices WHERE letter_no = ?", (notice["letter_no"],)).fetchall()
                if same_no and notice["kind"] in {row[0] for row in same_no}:
                    # 同编号又不是同一件来函：效力/范围冲突，原件隔离，不覆盖先前来函
                    store.insert_quarantine(db, inbox_seq, batch_id, line, "LETTER_CONFLICT", ["letter_no"], raw)
                    quarantined += 1
                    continue
                if notice["kind"] == "resumption":
                    suspended_keys = set()
                    for prior in store.load_notices(db):
                        if prior["kind"] == "suspension":
                            suspended_keys.update(tuple(key) for key in prior["keys"])
                    if not set(notice["keys"]) & suspended_keys:
                        store.insert_quarantine(db, inbox_seq, batch_id, line, "RESUMPTION_WITHOUT_SUSPENSION",
                                                ["kind", "issue_first", "issue_last"], raw)
                        quarantined += 1
                        continue
                letter_id = hashlib.sha256(
                    f'{notice["letter_no"]}:{notice["kind"]}:{inbox_seq}'.encode()).hexdigest()
                store.insert_notice(db, letter_id, inbox_seq, batch_id, {**notice, "digest": digest}, digest)
                accepted += 1

        # 事务②：依据全部已采信来函发布新版清单；此步失败不触碰台账，旧版继续交班
        with db:
            store.pin_rules(db, rules, hashlib.sha256(canonical(rules).encode()).hexdigest())
            notices = store.load_notices(db)
            full_quarantine = store.load_quarantine(db)
            full_replays = store.load_replays(db)
            full_inbox = store.load_inbox(db)
            document = _document(orders, notices, full_quarantine, full_replays, full_inbox)
            version, reused, stored = store.save_snapshot(db, received_at, document)
        return {"batch_id": batch_id, "version": version, "reused": reused, "snapshot": stored,
                "accepted": accepted, "quarantined": quarantined, "replayed": replayed}
    finally:
        db.close()


def current_handover():
    with closing(store.connect()) as db:
        snapshot = store.current_snapshot(db)
        if snapshot is None:
            raise LedgerError("NO_HANDOVER", "尚未形成交班清单，请先送达首批来函", 404)
        return snapshot


def handover_version(version):
    with closing(store.connect()) as db:
        snapshot = store.get_snapshot(db, version)
        if snapshot is None:
            raise LedgerError("VERSION_NOT_FOUND", "未找到该版交班清单", 404)
        return snapshot


def handover_state():
    return store.read_state()
