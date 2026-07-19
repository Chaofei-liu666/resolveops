"""ResolveOps MVP: an auditable fulfilment-exception agent.

The ERP adapter below is deliberately local and deterministic so the demo can run
without credentials.  Its boundary is intentionally small: replace `ERPAdapter`
with an ERPNext REST client when connecting a real tenant.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
DB = ROOT / "resolveops.db"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, args).fetchall()]


def event(db: sqlite3.Connection, case_id: str, kind: str, message: str, data: dict | None = None) -> None:
    db.execute(
        "INSERT INTO case_events(case_id, created_at, kind, message, data) VALUES (?, ?, ?, ?, ?)",
        (case_id, now(), kind, message, json.dumps(data or {}, ensure_ascii=False)),
    )


def init_db() -> None:
    with closing(connection()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
              id TEXT PRIMARY KEY, order_id TEXT NOT NULL, customer TEXT NOT NULL,
              status TEXT NOT NULL, plan_version INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS case_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
              created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
              action_hash TEXT NOT NULL, action_json TEXT NOT NULL, status TEXT NOT NULL,
              requested_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT
            );
            CREATE TABLE IF NOT EXISTS transfers (
              id TEXT PRIMARY KEY, case_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
              from_warehouse TEXT NOT NULL, to_warehouse TEXT NOT NULL, sku TEXT NOT NULL,
              quantity INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resource_locks (
              resource_key TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL
            );
            """
        )
        if not db.execute("SELECT 1 FROM cases LIMIT 1").fetchone():
            created = now()
            db.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("CASE-1042", "SO-2026-1042", "恒远科技", "new", 0, created, created),
            )
            db.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("CASE-1043", "SO-2026-1043", "北辰医疗", "new", 0, created, created),
            )
            event(db, "CASE-1042", "case_created", "订单履约异常进入队列：成都仓库存不足。")
            event(db, "CASE-1043", "case_created", "订单履约异常进入队列：成都仓库存不足。")
        db.commit()


class ERPAdapter:
    """A tiny in-process stand-in for a read/write ERP boundary."""

    data = {
        "CASE-1042": {
            "order": {"id": "SO-2026-1042", "sku": "SKU-A12", "required": 30, "due": "2026-07-16"},
            "local": {"warehouse": "成都仓", "available": 0, "version": 17},
            "alternative": {"warehouse": "重庆仓", "available": 40, "version": 11},
            "customer": {"split_shipment": True, "tier": "A", "credit_ok": True},
            "in_transit": 0,
        },
        "CASE-1043": {
            "order": {"id": "SO-2026-1043", "sku": "SKU-B08", "required": 20, "due": "2026-07-17"},
            "local": {"warehouse": "成都仓", "available": 4, "version": 8},
            "alternative": {"warehouse": "重庆仓", "available": 28, "version": 4},
            "customer": {"split_shipment": False, "tier": "B", "credit_ok": True},
            "in_transit": 0,
        },
    }

    def investigate(self, case_id: str) -> dict[str, Any]:
        try:
            return self.data[case_id]
        except KeyError as exc:
            raise HTTPException(404, "ERP 中未找到该订单") from exc


erp = ERPAdapter()


def planned_action(case_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    shortage = evidence["order"]["required"] - evidence["local"]["available"]
    return {
        "tool": "create_transfer_draft",
        "case_id": case_id,
        "from_warehouse": evidence["alternative"]["warehouse"],
        "to_warehouse": evidence["local"]["warehouse"],
        "sku": evidence["order"]["sku"],
        "quantity": shortage,
        "expected": "订单库存缺口归零；ERP 中存在一张 Draft 调拨单。",
        "verification": "重新读取调拨单字段与订单缺口，不信任写接口返回。",
        "risk": "medium",
    }


def action_digest(action: dict[str, Any], version: int) -> str:
    raw = json.dumps({"plan_version": version, "action": action}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def acquire_resource_lock(db: sqlite3.Connection, resource_key: str, holder: str) -> bool:
    """Atomically reserve one shared business resource for a write action.

    SQLite's UNIQUE constraint is the coordinator in this local MVP.  A deployed
    multi-node version should retain this boundary but use a shared transactional
    store (for example a PostgreSQL row/advisory lock) with a lease/heartbeat.
    """
    try:
        db.execute(
            "INSERT INTO resource_locks(resource_key, holder, acquired_at) VALUES (?, ?, ?)",
            (resource_key, holder, now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def release_resource_lock(db: sqlite3.Connection, resource_key: str, holder: str) -> None:
    # The holder condition prevents one case from releasing another case's lock.
    db.execute("DELETE FROM resource_locks WHERE resource_key=? AND holder=?", (resource_key, holder))


class Decision(BaseModel):
    approver: str = "warehouse_manager"


app = FastAPI(title="ResolveOps MVP", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/cases")
def list_cases() -> list[dict[str, Any]]:
    with closing(connection()) as db:
        return rows(db, "SELECT * FROM cases ORDER BY created_at")


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    with closing(connection()) as db:
        case = db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case 不存在")
        result = dict(case)
        result["events"] = rows(db, "SELECT * FROM case_events WHERE case_id = ? ORDER BY id", (case_id,))
        result["approvals"] = rows(db, "SELECT * FROM approvals WHERE case_id = ? ORDER BY requested_at DESC", (case_id,))
        result["transfers"] = rows(db, "SELECT * FROM transfers WHERE case_id = ?", (case_id,))
        return result


@app.post("/api/cases/{case_id}/investigate")
def investigate(case_id: str) -> dict[str, Any]:
    evidence = erp.investigate(case_id)
    action = planned_action(case_id, evidence)
    with closing(connection()) as db:
        case = db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "Case 不存在")
        if case["status"] not in {"new", "replan_required"}:
            raise HTTPException(409, "当前状态不能重复调查")
        version = case["plan_version"] + 1
        digest = action_digest(action, version)
        approval_id = f"APR-{case_id.split('-')[-1]}-V{version}"
        db.execute("UPDATE cases SET status=?, plan_version=?, updated_at=? WHERE id=?", ("waiting_approval", version, now(), case_id))
        event(db, case_id, "evidence_collected", "完成并发只读调查：订单、两地库存、客户约束与在途采购。", evidence)
        event(db, case_id, "plan_created", "推荐跨仓调拨；行动附带预期结果、验证查询与风险等级。", action)
        db.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (approval_id, case_id, version, digest, json.dumps(action, ensure_ascii=False), now()),
        )
        event(db, case_id, "approval_requested", "已创建调用级审批：仅允许此计划版本、此工具和此参数执行一次。", {"approval_id": approval_id, "action_hash": digest})
        db.commit()
    return {"case_id": case_id, "plan_version": version, "action": action, "approval_id": approval_id}


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, decision: Decision) -> dict[str, str]:
    with closing(connection()) as db:
        approval = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not approval or approval["status"] != "pending":
            raise HTTPException(409, "审批不存在或已处理")
        db.execute("UPDATE approvals SET status='approved', decided_at=?, decided_by=? WHERE id=?", (now(), decision.approver, approval_id))
        db.execute("UPDATE cases SET status='approved', updated_at=? WHERE id=?", (now(), approval["case_id"]))
        event(db, approval["case_id"], "approval_granted", f"{decision.approver} 批准了绑定行动。", {"approval_id": approval_id})
        db.commit()
    return {"status": "approved"}


@app.post("/api/cases/{case_id}/execute")
def execute(case_id: str) -> dict[str, Any]:
    with closing(connection()) as db:
        case = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        approval = db.execute("SELECT * FROM approvals WHERE case_id=? AND status='approved' ORDER BY requested_at DESC", (case_id,)).fetchone()
        if not case or not approval or case["status"] != "approved":
            raise HTTPException(409, "需要有效审批后才能执行")
        action = json.loads(approval["action_json"])
        if approval["plan_version"] != case["plan_version"] or approval["action_hash"] != action_digest(action, case["plan_version"]):
            raise HTTPException(409, "审批与当前计划不匹配，拒绝执行")
        resource_key = f"inventory:{action['from_warehouse']}:{action['sku']}"
        holder = f"{case_id}:v{case['plan_version']}"
        if not acquire_resource_lock(db, resource_key, holder):
            event(db, case_id, "resource_busy", "共享库存正被其他 Case 处理；保持当前状态，等待稍后重试。", {"resource_key": resource_key})
            db.commit()
            raise HTTPException(409, "共享库存正被其他 Case 锁定")
        # Commit the claim before crossing the ERP boundary.  Otherwise a second
        # service process could not observe the lock until this entire request
        # completed, which would defeat the coordination purpose.
        event(db, case_id, "resource_locked", "已获得来源仓库库存写锁。", {"resource_key": resource_key})
        db.commit()
        try:
            source = erp.investigate(case_id)["alternative"]
            if source["available"] < action["quantity"]:
                db.execute("UPDATE cases SET status='replan_required', updated_at=? WHERE id=?", (now(), case_id))
                event(db, case_id, "business_state_changed", "写入前复核失败：替代仓库存已变化，停止执行并要求重新规划。")
                raise HTTPException(409, "库存已变化，需重新规划")
            key = f"{case_id}:transfer:v{case['plan_version']}"
            transfer = db.execute("SELECT * FROM transfers WHERE idempotency_key=?", (key,)).fetchone()
            if not transfer:
                transfer_id = f"TR-{case_id.split('-')[-1]}"
                db.execute(
                    "INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (transfer_id, case_id, key, action["from_warehouse"], action["to_warehouse"], action["sku"], action["quantity"], "Draft", now()),
                )
                db.execute("UPDATE approvals SET status='consumed' WHERE id=?", (approval["id"],))
                event(db, case_id, "tool_executed", "已创建调拨草稿；审批凭证已一次性消费。", {"transfer_id": transfer_id, "idempotency_key": key})
            db.execute("UPDATE cases SET status='verifying', updated_at=? WHERE id=?", (now(), case_id))
        finally:
            release_resource_lock(db, resource_key, holder)
            event(db, case_id, "resource_released", "已释放来源仓库库存写锁。", {"resource_key": resource_key})
            db.commit()
    return verify(case_id)


def verify(case_id: str) -> dict[str, Any]:
    with closing(connection()) as db:
        transfer = db.execute("SELECT * FROM transfers WHERE case_id=? ORDER BY created_at DESC", (case_id,)).fetchone()
        if not transfer:
            raise HTTPException(409, "没有可验证的调拨单")
        evidence = erp.investigate(case_id)
        expected = evidence["order"]["required"] - evidence["local"]["available"]
        passed = transfer["status"] == "Draft" and transfer["quantity"] == expected
        if passed:
            db.execute("UPDATE cases SET status='resolved', updated_at=? WHERE id=?", (now(), case_id))
            event(db, case_id, "verification_passed", "独立验证通过：调拨单存在、数量正确，订单缺口已被覆盖。", {"transfer_id": transfer["id"], "expected_quantity": expected})
        else:
            db.execute("UPDATE cases SET status='replan_required', updated_at=? WHERE id=?", (now(), case_id))
            event(db, case_id, "verification_failed", "独立验证失败；没有宣布成功，已转入重新规划。")
        db.commit()
    return {"status": "resolved" if passed else "replan_required", "verification_passed": passed}
