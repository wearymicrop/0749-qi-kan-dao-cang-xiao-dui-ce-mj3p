import copy
import sqlite3

import pytest

from app.config import database_path
from app.modules.m8_handover import service
from app.source import load_sample

RULES = load_sample("dirty")["rules"]


def pause(letter_no, chain, year, volume, first, last=None, **extra):
    row = {"letter_no": letter_no, "kind": "suspension", "effect": "pause",
           "chain": chain, "year": year, "volume": volume,
           "issue_first": str(first), "issue_last": None if last is None else str(last)}
    row.update(extra)
    return row


def terminate(letter_no, chain, year, volume, issue):
    return {"letter_no": letter_no, "kind": "suspension", "effect": "terminate",
            "chain": chain, "year": year, "volume": volume,
            "issue_first": str(issue), "issue_last": None}


def resume(letter_no, chain, year, volume, first, last=None):
    return {"letter_no": letter_no, "kind": "resumption", "chain": chain, "year": year,
            "volume": volume, "issue_first": str(first),
            "issue_last": None if last is None else str(last)}


@pytest.fixture
def first_batch():
    return copy.deepcopy({"rules": RULES, "notices": [
        pause("HT-2026-01", "CST", 2026, "18", 3, 4, title="海岸观测季刊", issued_on="2026-09-01"),
        terminate("HT-2026-02", "GEO", 2026, "4", 12),
    ]})


def test_first_batch_marks_pending_and_terminated(client, first_batch):
    result = client.post("/api/handover/batches", json=first_batch).json()
    snapshot = result["snapshot"]
    assert result["version"] == 1 and result["reused"] is False
    assert snapshot["totals"] == {"affected": 3, "suspended": 1, "resumed": 0, "pending": 2}
    by_key = {(r["chain"], r["issue"]): r for r in snapshot["slots"]}
    assert by_key["CST", 3]["status"] == "pending"
    assert by_key["CST", 4]["status"] == "pending"
    assert by_key["GEO", 12]["status"] == "suspended"
    slot = by_key["CST", 3]
    assert slot["basis"][0]["letter_no"] == "HT-2026-01"
    assert slot["basis"][0]["cover"] == "3–4期"


def test_later_batch_resumes_slots_and_keeps_basis_trail(client, first_batch):
    client.post("/api/handover/batches", json=first_batch)
    second = client.post("/api/handover/batches", json={"notices": [
        resume("HF-2026-07", "CST", 2026, "18", 3, 3)]}).json()
    assert second["version"] == 2
    snapshot = second["snapshot"]
    by_key = {(r["chain"], r["issue"]): r for r in snapshot["slots"]}
    resumed = by_key["CST", 3]
    assert resumed["status"] == "resumed" and resumed["latest_letter"] == "HF-2026-07"
    assert [b["letter_no"] for b in resumed["basis"]] == ["HT-2026-01", "HF-2026-07"]
    assert [b["kind"] for b in resumed["basis"]] == ["suspension", "resumption"]
    # 尚未补到的期位继续挂"仍待确认"
    assert by_key["CST", 4]["status"] == "pending"
    assert by_key["GEO", 12]["status"] == "suspended"


def test_same_letter_rerun_does_not_change_slots_or_version(client, first_batch):
    first = client.post("/api/handover/batches", json=first_batch).json()
    again = client.post("/api/handover/batches", json={"notices": list(first_batch["notices"])}).json()
    # 重放不改变任何期位状态与计数，但台账多了两条重放记录，故形成新版本
    assert again["version"] == first["version"] + 1 and again["reused"] is False
    assert again["replayed"] == 2 and again["accepted"] == 0
    ledger = client.get("/api/handover/ledger").json()
    assert len(ledger["notices"]) == 2
    assert len(ledger["replays"]) == 2
    assert {r["letter_no"] for r in ledger["replays"]} == {"HT-2026-01", "HT-2026-02"}
    assert all(r["original_seq"] <= 2 for r in ledger["replays"])
    # 状态与计数没有被重复增减
    assert again["snapshot"]["totals"] == first["snapshot"]["totals"]
    assert again["snapshot"]["slots"] == first["snapshot"]["slots"]


def test_quarantined_letter_never_moves_slots(client, first_batch):
    client.post("/api/handover/batches", json=first_batch)
    bad = client.post("/api/handover/batches", json={"notices": [
        resume("HF-BAD-1", "CST", 2026, "18", 2, 2),            # 该期无停刊依据
        pause("HT-BAD-2", "CST", 2026, "18", 9),                # 越出本卷范围
        pause("HT-BAD-3", "NOPE", 2026, "18", 1),               # 订阅链/年度不在订购段
        {"letter_no": "HT-BAD-4"},                              # 残缺行
        None,                                                   # 空行
        "原件残页",                                              # 非对象行
    ]}).json()
    snapshot = bad["snapshot"]
    assert bad["quarantined"] == 6 and bad["accepted"] == 0
    # 先前采信的两件不受影响
    assert snapshot["ledger"]["accepted"] == 2
    codes = {row["code"] for row in snapshot["quarantine"]}
    assert codes == {"RESUMPTION_WITHOUT_SUSPENSION", "OUTSIDE_RANGE", "OUT_OF_ORDER",
                     "NOTICE_SCHEMA", "EMPTY_ROW"}
    # 隔离不改变期位状态，但仍生成新版本（台账变了）
    assert snapshot["totals"] == {"affected": 3, "suspended": 1, "resumed": 0, "pending": 2}


def test_conflicting_same_number_letter_is_quarantined_without_overwrite(client, first_batch):
    client.post("/api/handover/batches", json=first_batch)
    conflict = client.post("/api/handover/batches", json={"notices": [
        pause("HT-2026-01", "CST", 2026, "18", 4, 4)]}).json()  # 同编号不同范围
    assert conflict["quarantined"] == 1
    row = conflict["snapshot"]["quarantine"][-1]
    assert row["code"] == "LETTER_CONFLICT" and row["raw"]["issue_first"] == "4"
    ledger = client.get("/api/handover/ledger").json()
    assert [n["letter_no"] for n in ledger["notices"] if n["letter_no"] == "HT-2026-01"]
    original = next(n for n in ledger["notices"] if n["letter_no"] == "HT-2026-01")
    assert original["cover"]["first"] == 3  # 先前来函没有被覆盖


def test_failed_batch_keeps_previous_snapshot_for_handover(client, first_batch, monkeypatch):
    first = client.post("/api/handover/batches", json=first_batch).json()

    def boom(*_args, **_kwargs):
        raise RuntimeError("补件处理失败")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "save_snapshot", boom)
        with pytest.raises(RuntimeError):
            service.ingest_batch({"notices": [resume("HF-2026-07", "CST", 2026, "18", 3)]})
    # 旧版清单仍可交班
    current = client.get("/api/handover/current").json()
    assert current["digest"] == first["snapshot"]["digest"]
    with sqlite3.connect(database_path()) as db:
        versions = db.execute("SELECT COUNT(*) FROM handover_snapshots").fetchone()[0]
    assert versions == 1


def test_originals_are_never_lost_after_publication_failure(client, first_batch, monkeypatch):
    client.post("/api/handover/batches", json=first_batch)
    ledger_before = client.get("/api/handover/ledger").json()
    before = len(ledger_before["inbox"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("快照发布中断")

    # 用独立上下文注入故障，退出时只还原 save_snapshot，不触碰夹具的环境变量
    with monkeypatch.context() as patch:
        patch.setattr(service.store, "save_snapshot", boom)
        with pytest.raises(RuntimeError):
            service.ingest_batch({"notices": [
                resume("HF-LOST", "CST", 2026, "18", 3),
                pause("HT-LOST", "CST", 2026, "18", 4),
            ]})
    ledger = client.get("/api/handover/ledger").json()
    # 事务②失败不影响事务①：两件原件都在台账里，且已采信来函照常登记
    assert len(ledger["inbox"]) == before + 2
    assert {row["letter_no"] for row in ledger["notices"]} == {
        "HT-2026-01", "HT-2026-02", "HF-LOST", "HT-LOST"}
    # 修复后重发同一批：幂等命中，不产生重复期位变动
    retry = client.post("/api/handover/batches", json={"notices": [
        resume("HF-LOST", "CST", 2026, "18", 3),
        pause("HT-LOST", "CST", 2026, "18", 4),
    ]}).json()
    assert retry["replayed"] == 2 and retry["accepted"] == 0
    by_key = {(r["chain"], r["issue"]): r for r in retry["snapshot"]["slots"]}
    assert by_key["CST", 3]["status"] == "resumed"


def test_pinned_rules_are_reused_by_later_batches(client, first_batch):
    client.post("/api/handover/batches", json=first_batch)
    second = client.post("/api/handover/batches", json={"notices": [
        resume("HF-2026-07", "CST", 2026, "18", 3)]}).json()
    assert second["version"] == 2
    ledger = client.get("/api/handover/ledger").json()
    assert ledger["rules"]["rules"] == RULES


def test_first_batch_without_rules_is_rejected(client):
    response = client.post("/api/handover/batches", json={"notices": [pause("X", "CST", 2026, 18, 3)]})
    assert response.status_code == 422 and response.json()["code"] == "NO_RULES"


def test_version_endpoints_serve_history(client, first_batch):
    first = client.post("/api/handover/batches", json=first_batch).json()
    client.post("/api/handover/batches", json={"notices": [resume("HF-1", "CST", 2026, "18", 3)]})
    old = client.get(f"/api/handover/versions/{first['version']}").json()
    assert old["digest"] == first["snapshot"]["digest"]
    listing = client.get("/api/handover/versions").json()
    assert [v["version"] for v in listing["versions"]] == [1, 2]
    assert client.get("/api/handover/versions/99").status_code == 404
    assert client.get("/api/handover/current").json()["digest"] == listing["current"]["digest"]


def test_current_before_any_batch_is_404(client):
    assert client.get("/api/handover/current").status_code == 404


def test_padded_and_fullwidth_volume_issue_normalize(client, first_batch):
    client.post("/api/handover/batches", json=first_batch)
    result = client.post("/api/handover/batches", json={"notices": [
        {"letter_no": "HF-WIDTH", "kind": "resumption", "chain": "CST", "year": 2026,
         "volume": "１８", "issue_first": "03", "issue_last": "04"}]})
    assert result.status_code == 200 and result.json()["quarantined"] == 0
    snapshot = result.json()["snapshot"]
    assert all(r["status"] == "resumed" for r in snapshot["slots"] if r["chain"] == "CST")


def test_empty_batch_keeps_snapshot_digest(client, first_batch):
    first = client.post("/api/handover/batches", json=first_batch).json()
    again = client.post("/api/handover/batches", json={"notices": []}).json()
    assert again["reused"] is True and again["snapshot"]["digest"] == first["snapshot"]["digest"]


def test_partial_resumption_leaves_remainder_pending(client):
    client.post("/api/handover/batches", json={"rules": RULES, "notices": [
        pause("HT-RANGE", "CST", 2026, "18", 1, 4)]})
    result = client.post("/api/handover/batches", json={"notices": [
        resume("HF-PART", "CST", 2026, "18", 1, 2)]}).json()
    statuses = {row["issue"]: row["status"] for row in result["snapshot"]["slots"]}
    assert statuses == {1: "resumed", 2: "resumed", 3: "pending", 4: "pending"}


def test_terminated_slot_can_be_revived_by_later_resumption(client):
    client.post("/api/handover/batches", json={"rules": RULES, "notices": [
        terminate("HT-END", "GEO", 2026, "4", 12)]})
    result = client.post("/api/handover/batches", json={"notices": [
        resume("HF-REVIVE", "GEO", 2026, "4", 12)]}).json()
    slot = next(row for row in result["snapshot"]["slots"] if (row["chain"], row["issue"]) == ("GEO", 12))
    assert slot["status"] == "resumed"
    assert [b["letter_no"] for b in slot["basis"]] == ["HT-END", "HF-REVIVE"]


def test_resumption_overlapping_suspended_range_is_accepted(client):
    # 复刊范围只需与某个在案停刊期位有交集即可，交集外的期位不在复刊函内不受影响
    client.post("/api/handover/batches", json={"rules": RULES, "notices": [
        pause("HT-RANGE", "CST", 2026, "18", 1, 4)]})
    result = client.post("/api/handover/batches", json={"notices": [
        resume("HF-OVERLAP", "CST", 2026, "18", 3, 4)]}).json()
    assert result["quarantined"] == 0 and result["accepted"] == 1
    statuses = {row["issue"]: row["status"] for row in result["snapshot"]["slots"]}
    assert statuses == {1: "pending", 2: "pending", 3: "resumed", 4: "resumed"}


def test_full_shift_scenario_end_to_end(client):
    """验收组交班主流程：先按已到函件出清单，后续补件增量恢复，重跑不重复，坏件不污染。"""
    # 1) 出版方突然停刊，首批函件到达，形成可交班清单
    first = client.post("/api/handover/batches", json={"rules": RULES, "notices": [
        pause("HT-1", "CST", 2026, "18", 1, 4),
    ]}).json()
    assert first["version"] == 1
    assert {row["issue"]: row["status"] for row in first["snapshot"]["slots"]} == {
        1: "pending", 2: "pending", 3: "pending", 4: "pending"}

    # 2) 提前复刊依据分批补送：先恢复第 1、2 期
    second = client.post("/api/handover/batches", json={"notices": [
        resume("HF-1", "CST", 2026, "18", 1, 2)]}).json()
    statuses = {row["issue"]: row["status"] for row in second["snapshot"]["slots"]}
    assert statuses == {1: "resumed", 2: "resumed", 3: "pending", 4: "pending"}

    # 3) 同一份来函被交班系统重跑：期位不重复增减
    rerun = client.post("/api/handover/batches", json={"notices": [
        resume("HF-1", "CST", 2026, "18", 1, 2)]}).json()
    assert rerun["replayed"] == 1
    assert {row["issue"]: row["status"] for row in rerun["snapshot"]["slots"]} == statuses

    # 4) 后续补件里混入坏件：隔离处理，旧期位不动，清单仍可交班
    third = client.post("/api/handover/batches", json={"notices": [
        resume("HF-2", "CST", 2026, "18", 3, 3),
        pause("HT-BAD", "CST", 2026, "18", 99),
    ]}).json()
    assert third["quarantined"] == 1 and third["accepted"] == 1
    final = {row["issue"]: row["status"] for row in third["snapshot"]["slots"]}
    assert final == {1: "resumed", 2: "resumed", 3: "resumed", 4: "pending"}

    # 5) 每一期位都能追溯到完整依据链
    slot3 = next(row for row in third["snapshot"]["slots"] if row["issue"] == 3)
    assert [b["letter_no"] for b in slot3["basis"]] == ["HT-1", "HF-2"]

    # 6) 原件与先前来函全部留痕，可经台账审计（5 件来函逐件入账：1+1+1+2）
    ledger = client.get("/api/handover/ledger").json()
    assert len(ledger["inbox"]) == 5
    assert {n["letter_no"] for n in ledger["notices"]} == {"HT-1", "HF-1", "HF-2"}
    assert [q["code"] for q in ledger["quarantine"]] == ["OUTSIDE_RANGE"]
    assert len(ledger["replays"]) == 1

    # 7) 历史版本都还在，旧版可随时调出交班
    assert [v["version"] for v in client.get("/api/handover/versions").json()["versions"]] == [1, 2, 3, 4]
    old = client.get("/api/handover/versions/1").json()
    assert old["digest"] == first["snapshot"]["digest"]
