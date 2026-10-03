"""双盲论文评审分配服务 - HTTP API。

角色:
  - 会务方: 凭本地密钥 (请求头 X-Organizer-Key) 维护论文/评审人资料, 预演并发布分配方案;
  - 评审人: 凭自身凭据 (X-Reviewer-Id + X-Reviewer-Credential) 仅可读取已分配给自己的匿名稿。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    AssignmentDecisionIn,
    BackfillPublishIn,
    FeedbackObjectionDecisionIn,
    FeedbackObjectionIn,
    InstitutionMergeIn,
    LockTableIn,
    PaperIn,
    PaperUpdate,
    PublishIn,
    ReviewCorrectionRequestIn,
    ReviewCorrectionSubmitIn,
    ReviewSubmissionIn,
    ReviewerIn,
    ReviewerStatusIn,
    ReviewerUpdate,
    SnapshotPublishIn,
)
from .solver import (
    Paper,
    Reviewer,
    compute_assignment,
    compute_backfill,
    compute_locked_assignment,
)

ORGANIZER_KEY = os.environ.get("ORGANIZER_KEY", "dev-organizer-key")

app = FastAPI(title="双盲论文评审分配服务", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": "请求参数校验失败 (例如容量不是 >=1 的整数、缺少必填字段)",
            "errors": jsonable_encoder(exc.errors()),
        },
    )


# ---------------------------------------------------------------- 鉴权

def require_organizer(x_organizer_key: str = Header(default=None)) -> bool:
    if not x_organizer_key or x_organizer_key != ORGANIZER_KEY:
        raise HTTPException(status_code=401, detail="会务方密钥无效 (invalid organizer key)")
    return True


def require_reviewer(
    x_reviewer_id: str = Header(default=None),
    x_reviewer_credential: str = Header(default=None),
) -> dict:
    """校验评审人凭据, 返回 {"reviewer_id": .., "active": bool}。

    凭据本身不因停用而失效 (凭据格式与评审接口约定保持兼容): 停用者仍可通过本校验,
    由各具体接口立即拒绝 (403); 凭据缺失/不匹配仍为 401。
    """
    if not x_reviewer_id or not x_reviewer_credential:
        raise HTTPException(status_code=401, detail="缺少评审人编号或凭据 (missing reviewer credentials)")
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT credential, active FROM reviewers WHERE reviewer_id = ?", (x_reviewer_id,)
        ).fetchone()
    if row is None or row["credential"] != x_reviewer_credential:
        raise HTTPException(status_code=401, detail="评审人凭据无效 (invalid reviewer credentials)")
    return {"reviewer_id": x_reviewer_id, "active": bool(row["active"])}


def require_active_reviewer(auth: dict = Depends(require_reviewer)) -> str:
    """评审人接口: 已停用资格立即拒绝 (403); 返回评审人编号。"""
    if not auth["active"]:
        raise HTTPException(
            status_code=403,
            detail="评审资格已停用: 不能取稿、提交决定或评语 (reviewing eligibility is disabled)",
        )
    return auth["reviewer_id"]


# ---------------------------------------------------------------- 工具

def _hard_recusals(conn):
    """返回 {reviewer_id: set(paper_id)}: 评审人通过决定接口声明过的硬回避。"""
    out = {}
    for r in conn.execute("SELECT reviewer_id, paper_id FROM reviewer_recusals").fetchall():
        out.setdefault(r["reviewer_id"], set()).add(r["paper_id"])
    return out


def _recused_pairs(conn):
    """返回 {(reviewer_id, paper_id)}: 供补位求解器单独标注诊断原因。"""
    return {
        (r["reviewer_id"], r["paper_id"])
        for r in conn.execute("SELECT reviewer_id, paper_id FROM reviewer_recusals").fetchall()
    }


def _load_domain(conn):
    """加载当前资料。评审人声明过的硬回避与其录入的 avoid_papers 合并后生效。"""
    hard = _hard_recusals(conn)
    papers = [
        Paper(
            paper_id=r["paper_id"],
            topics=frozenset(json.loads(r["topics"])),
            institutions=frozenset(json.loads(r["institutions"])),
        )
        for r in conn.execute("SELECT * FROM papers").fetchall()
    ]
    reviewers = []
    for r in conn.execute("SELECT * FROM reviewers").fetchall():
        avoids = set(json.loads(r["avoid_papers"])) | hard.get(r["reviewer_id"], set())
        reviewers.append(
            Reviewer(
                reviewer_id=r["reviewer_id"],
                topics=frozenset(json.loads(r["topics"])),
                institution=r["institution"],
                capacity=r["capacity"],
                avoid_papers=frozenset(avoids),
                active=bool(r["active"]),
            )
        )
    return papers, reviewers


def _institution_groups(conn):
    """当前机构归并组 {原名: 组键}, 仅覆盖当前论文作者/评审人资料中出现的名称。

    归并边永久保留 (关系传递且不可拆分); 已删除资料中的名称仍参与并查集,
    不影响其余名称的归组。求解器内一切机构冲突均按该组判定。
    """
    return db.institution_groups(conn, known=db.current_institution_names(conn))


def _paper_row_to_dict(row):
    return {
        "paper_id": row["paper_id"],
        "manuscript": row["manuscript"],
        "topics": json.loads(row["topics"]),
        "institutions": json.loads(row["institutions"]),
    }


def _reviewer_row_to_dict(row):
    return {
        "reviewer_id": row["reviewer_id"],
        "credential": row["credential"],
        "topics": json.loads(row["topics"]),
        "institution": row["institution"],
        "capacity": row["capacity"],
        "avoid_papers": json.loads(row["avoid_papers"]),
        "active": bool(row["active"]),
    }


def _load_published(conn):
    """读取当前发布版; 不存在返回 None。"""
    return conn.execute(
        "SELECT serial, revision, plan, explanations, published_at FROM published WHERE id = 1"
    ).fetchone()


def _load_decisions(conn, serial):
    """读取某发布序号下每个分配槽位的决定。返回 {(paper_id, reviewer_id): row}。"""
    return {
        (d["paper_id"], d["reviewer_id"]): d
        for d in conn.execute(
            "SELECT paper_id, reviewer_id, state, reason, decided_at"
            " FROM assignment_decisions WHERE serial = ?",
            (serial,),
        ).fetchall()
    }


def _load_reviews(conn, serial):
    """读取某发布序号下已提交的正式评语。返回 {(paper_id, reviewer_id): row}。"""
    return {
        (r["paper_id"], r["reviewer_id"]): r
        for r in conn.execute(
            "SELECT paper_id, reviewer_id, score, comment, receipt, submitted_at"
            " FROM submitted_reviews WHERE serial = ?",
            (serial,),
        ).fetchall()
    }


def _correction_view(c):
    """更正记录的会务方追溯视图: 冻结的原评语 + 更正后内容 (pending 时为 None)。"""
    return {
        "correction_id": c["id"],
        "serial": c["serial"],
        "paper_id": c["paper_id"],
        "reviewer_id": c["reviewer_id"],
        "reason": c["reason"],
        "state": c["state"],
        "original": {
            "score": c["original_score"],
            "comment": c["original_comment"],
            "receipt": c["original_receipt"],
            "submitted_at": c["original_submitted_at"],
        },
        "corrected": (
            None
            if c["state"] != "completed"
            else {
                "score": c["new_score"],
                "comment": c["new_comment"],
                "receipt": c["new_receipt"],
                "corrected_at": c["corrected_at"],
            }
        ),
        "requested_at": c["requested_at"],
    }


def _confirmed_map(plan, decisions):
    """已确认且未回避的关系: {paper_id: [reviewer_id, ...]}, 作为补位固定位置。"""
    fixed = {}
    for pid, pair in plan.items():
        kept = [
            rid for rid in pair
            if (d := decisions.get((pid, rid))) is not None and d["state"] == "confirmed"
        ]
        if kept:
            fixed[pid] = kept
    return fixed


def _build_backfill(conn, published_row):
    """基于当前发布版与当前资料执行补位求解, 返回求解结果字典。"""
    plan = json.loads(published_row["plan"])
    serial = published_row["serial"]
    decisions = _load_decisions(conn, serial)
    fixed = _confirmed_map(plan, decisions)
    all_papers, all_reviewers = _load_domain(conn)
    paper_ids = {p.paper_id for p in all_papers}
    scoped_papers = [p for p in all_papers if p.paper_id in plan and p.paper_id in paper_ids]
    result = compute_backfill(
        scoped_papers,
        all_reviewers,
        fixed,
        recused_pairs=_recused_pairs(conn),
        inst_group=_institution_groups(conn),
    )
    result["serial"] = serial
    result["published_revision"] = published_row["revision"]
    return result


def _normal_assignment(conn):
    """普通分配求解: 有锁定表时按锁定槽位严格求解, 无锁定时走原有求解器。

    锁定仅约束普通分配: 锁定槽位必须保留, 失效只报错不释放; 其余位置遵守
    原有全部硬约束与优化次序。返回求解结果字典。
    """
    papers, reviewers = _load_domain(conn)
    recused = _recused_pairs(conn)
    groups = _institution_groups(conn)
    lock_map = db.load_assignment_locks(conn)
    if lock_map:
        return compute_locked_assignment(papers, reviewers, lock_map, recused, groups)
    return compute_assignment(papers, reviewers, recused, groups)


def _normalize_lock_table(raw: dict):
    """规范化提交的锁定表: 编号去首尾空白, 每篇去重并按字典序, 丢弃空列表。

    返回 (normalized, blank_ids): blank_ids 为归一化后为空白的论文/评审人编号。
    """
    normalized, blank_ids = {}, []
    for paper_id, reviewer_ids in raw.items():
        pid = paper_id.strip()
        if not pid:
            blank_ids.append(("paper", paper_id))
            continue
        ids, seen = [], set()
        for rid in reviewer_ids:
            r = rid.strip()
            if not r:
                blank_ids.append(("reviewer", rid))
                continue
            if r not in seen:
                seen.add(r)
                ids.append(r)
        normalized[pid] = sorted(ids)
    return dict(sorted(normalized.items())), blank_ids


# ---------------------------------------------------------------- 基础

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/meta")
def meta(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        published = _load_published(conn)
        recusals = [
            {
                "reviewer_id": r["reviewer_id"],
                "paper_id": r["paper_id"],
                "reason": r["reason"],
                "created_serial": r["created_serial"],
                "created_revision": r["created_revision"],
            }
            for r in conn.execute(
                "SELECT reviewer_id, paper_id, reason, created_serial, created_revision"
                " FROM reviewer_recusals ORDER BY reviewer_id, paper_id"
            ).fetchall()
        ]
    return {
        "revision": rev,
        "published": (
            {"serial": published["serial"], "revision": published["revision"],
             "published_at": published["published_at"]}
            if published
            else None
        ),
        "hard_recusals": recusals,
    }


# ---------------------------------------------------------------- 论文资料 (会务方)

@app.post("/papers", status_code=201)
def create_paper(body: PaperIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (body.paper_id,)).fetchone():
            raise HTTPException(status_code=409, detail=f"论文编号重复: {body.paper_id}")
        conn.execute(
            "INSERT INTO papers(paper_id, manuscript, topics, institutions) VALUES (?,?,?,?)",
            (body.paper_id, body.manuscript, json.dumps(body.topics), json.dumps(body.institutions)),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": body.paper_id, "revision": rev}


@app.put("/papers/{paper_id}")
def update_paper(paper_id: str, body: PaperUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        conn.execute(
            "UPDATE papers SET manuscript = ?, topics = ?, institutions = ? WHERE paper_id = ?",
            (body.manuscript, json.dumps(body.topics), json.dumps(body.institutions), paper_id),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.delete("/papers/{paper_id}")
def delete_paper(paper_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"论文不存在: {paper_id}")
        # 论文删除后不再参与分配, 其普通分配锁定随论文一并清除 (非"悄悄释放":
        # 被锁对象已不存在; 评审人删除不级联, 锁定保留并在预演/发布中暴露)
        conn.execute("DELETE FROM assignment_locks WHERE paper_id = ?", (paper_id,))
        rev = db.bump_revision(conn)
    return {"ok": True, "paper_id": paper_id, "revision": rev}


@app.get("/papers")
def list_papers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM papers ORDER BY paper_id").fetchall()
    return {"papers": [_paper_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------- 评审人资料 (会务方)

@app.post("/reviewers", status_code=201)
def create_reviewer(body: ReviewerIn, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (body.reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=409, detail=f"评审人编号重复: {body.reviewer_id}")
        conn.execute(
            "INSERT INTO reviewers(reviewer_id, credential, topics, institution, capacity, avoid_papers)"
            " VALUES (?,?,?,?,?,?)",
            (
                body.reviewer_id,
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": body.reviewer_id, "revision": rev}


@app.put("/reviewers/{reviewer_id}")
def update_reviewer(reviewer_id: str, body: ReviewerUpdate, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        if not conn.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (reviewer_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        conn.execute(
            "UPDATE reviewers SET credential = ?, topics = ?, institution = ?, capacity = ?,"
            " avoid_papers = ? WHERE reviewer_id = ?",
            (
                body.credential,
                json.dumps(body.topics),
                body.institution,
                body.capacity,
                json.dumps(body.avoid_papers),
                reviewer_id,
            ),
        )
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.delete("/reviewers/{reviewer_id}")
def delete_reviewer(reviewer_id: str, _: bool = Depends(require_organizer)):
    with db.write_txn() as conn:
        cur = conn.execute("DELETE FROM reviewers WHERE reviewer_id = ?", (reviewer_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        rev = db.bump_revision(conn)
    return {"ok": True, "reviewer_id": reviewer_id, "revision": rev}


@app.get("/reviewers")
def list_reviewers(_: bool = Depends(require_organizer)):
    with db.read_txn() as conn:
        rows = conn.execute("SELECT * FROM reviewers ORDER BY reviewer_id").fetchall()
    return {"reviewers": [_reviewer_row_to_dict(r) for r in rows]}


# ------------------------------------------------ 评审资格停用/启用 (会务方)

def _status_change_rows(conn, reviewer_id):
    return conn.execute(
        "SELECT active, reason, revision, changed_at FROM reviewer_status_changes"
        " WHERE reviewer_id = ? ORDER BY id",
        (reviewer_id,),
    ).fetchall()


def _status_change_dict(r):
    return {
        "active": bool(r["active"]),
        "reason": r["reason"],
        "revision": r["revision"],
        "changed_at": r["changed_at"],
    }


@app.post("/reviewers/{reviewer_id}/status")
def set_reviewer_status(
    reviewer_id: str,
    body: ReviewerStatusIn,
    _: bool = Depends(require_organizer),
):
    """会务方调整评审人评审资格 (停用/启用)。

    在同一个写事务内先核对资料修订号, 再处理状态:
      - 修订号与当前不一致 (过期/超前) -> 409, 不改状态;
      - 未知评审人 -> 404; 原因为空白 -> 422;
      - 匹配修订号下重复提交同一状态和原因 -> 幂等 (changed=false), 不推进修订号、不写记录;
      - 同状态异原因 (含对已处于启用状态者再次启用) -> 409, 不改状态;
      - 有效变更推进资料修订号并写入变更记录。
    停用不删除既有发布分配、确认与评语, 已发布的作者反馈快照仍按原规则有效;
    停用者立即不能取稿、提交决定或评语, 且不进入后续普通分配与补位
    (补位释放其已确认槽位); 启用只恢复当前发布版中仍分配给本人的任务访问。
    """
    if body.reviewer_id != reviewer_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的评审人编号 ({reviewer_id}) 与请求体 ({body.reviewer_id}) 不一致",
        )
    reason = body.reason.strip()

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 请求基于修订号 {body.base_revision}, "
                    f"当前修订号为 {rev}, 请重新查看资料后再调整"
                ),
            )
        row = conn.execute(
            "SELECT active FROM reviewers WHERE reviewer_id = ?", (reviewer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        if not reason:
            raise HTTPException(
                status_code=422, detail="状态调整原因必须非空 (non-empty reason is required)"
            )
        currently_active = bool(row["active"])
        last = conn.execute(
            "SELECT active, reason FROM reviewer_status_changes"
            " WHERE reviewer_id = ? ORDER BY id DESC LIMIT 1",
            (reviewer_id,),
        ).fetchone()
        if bool(body.active) == currently_active:
            # 同状态: 仅"同一状态和原因"的重复提交幂等; 异原因拒绝 (不推进修订号)
            same_reason = last is not None and bool(last["active"]) == body.active and last["reason"] == reason
            if not same_reason:
                state_text = "启用" if body.active else "停用"
                raise HTTPException(
                    status_code=409,
                    detail=f"评审人 {reviewer_id} 当前已处于{state_text}状态, 同状态异原因的调整按冲突拒绝",
                )
            return {
                "ok": True,
                "reviewer_id": reviewer_id,
                "active": currently_active,
                "reason": reason,
                "changed": False,
                "revision": rev,
            }
        conn.execute(
            "UPDATE reviewers SET active = ? WHERE reviewer_id = ?",
            (1 if body.active else 0, reviewer_id),
        )
        new_rev = db.bump_revision(conn)
        conn.execute(
            "INSERT INTO reviewer_status_changes"
            "(reviewer_id, active, reason, revision, changed_at) VALUES (?,?,?,?,?)",
            (reviewer_id, 1 if body.active else 0, reason, new_rev, now),
        )
    return {
        "ok": True,
        "reviewer_id": reviewer_id,
        "active": bool(body.active),
        "reason": reason,
        "changed": True,
        "revision": new_rev,
    }


@app.get("/reviewers/{reviewer_id}/status")
def get_reviewer_status(reviewer_id: str, _: bool = Depends(require_organizer)):
    """会务方查看评审人当前资格状态与全部变更记录 (既有评审人初始视为启用, 无记录)。"""
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT active FROM reviewers WHERE reviewer_id = ?", (reviewer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"评审人不存在: {reviewer_id}")
        history = [_status_change_dict(r) for r in _status_change_rows(conn, reviewer_id)]
    return {
        "reviewer_id": reviewer_id,
        "active": bool(row["active"]),
        "history": history,
    }


# ---------------------------------------------------------------- 机构归并组 (会务方)

def _merge_group_view(conn):
    """按归并边构造全部归并组视图: 仅含 >=2 个原名的组, 按组键字典序。

    组的成员取自归并边中出现过的名称 (即会务方曾提交归并的原名);
    这些边永久保留、不可拆分, 故名称即使已不在当前资料中也保留可追溯。
    """
    return [
        {"group_key": root, "names": names, "size": len(names)}
        for root, names in sorted(_merge_component_names(conn).items())
        if len(names) >= 2
    ]


def _merge_component_names(conn):
    """返回 {归并组键: [该连通分量内出现于归并边的全部原名]}。

    与求解器使用的分组不同, 这里不限制为"当前资料中的名称":
    归并边永久保留, 已删除资料中的名称仍是归并组的可追溯成员。
    """
    edges = db.institution_merge_edges(conn)
    groups = db.institution_groups(conn)
    buckets: dict[str, set[str]] = {}
    for a, b in edges:
        buckets.setdefault(groups[a], set()).update((a, b))
    return {root: sorted(names) for root, names in buckets.items()}


@app.post("/institutions/merge-groups")
def merge_institutions(body: InstitutionMergeIn, _: bool = Depends(require_organizer)):
    """会务方把同一机构的两个原名归并为同一冲突判定组。

    - 两个名称都必须是"当前论文作者或评审人资料中出现过"的机构原名;
    - 请求须携带所见资料修订号, 与当前不一致 (过期/超前) 即 409;
    - 名称去除首尾空白后为空白 -> 422; 任一名称未知 (未在当前资料中出现) -> 404;
    - 归并关系传递且不可拆分: 两个名称已在同一组时为同组重复提交,
      幂等返回 (changed=false), 不写边、不推进修订号;
    - 有效归并 (两个原先不同的组首次合并) 写入归并边并推进资料修订号 +1;
    - 被拒请求 (409/422/404) 不写入任何边, 也不留下部分变更。
    返回归并后的名称组 (按名称字典序)、当前修订号与是否变更。
    """
    name_a = body.name_a.strip()
    name_b = body.name_b.strip()

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 归并请求基于修订号 {body.base_revision},"
                    f" 当前修订号为 {rev}, 请重新查看资料后再提交"
                ),
            )
        if not name_a or not name_b:
            raise HTTPException(
                status_code=422,
                detail="机构名称不得为空白 (both institution names must be non-blank)",
            )
        known = db.current_institution_names(conn)
        unknown = sorted({name for name in (name_a, name_b) if name not in known})
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=(
                    "机构原名未出现在当前论文作者或评审人资料中, 无法归并: "
                    + ", ".join(unknown)
                ),
            )

        groups = db.institution_groups(conn, known=known)
        root_a, root_b = groups[name_a], groups[name_b]
        if root_a == root_b:
            # 同组重复提交: 幂等, 不写边、不推进修订号。
            # 名称组按归并边的连通分量返回 (含已离开当前资料的历史原名);
            # 名称与自身归并且尚无任何边时, 组仅含该名称自身。
            members = _merge_component_names(conn).get(root_a, [name_a])
            return {
                "ok": True,
                "changed": False,
                "revision": rev,
                "names": members,
                "group_key": root_a,
                "submitted": [name_a, name_b],
            }

        conn.execute(
            "INSERT INTO institution_merges(name_a, name_b, revision, merged_at)"
            " VALUES (?,?,?,?)",
            (name_a, name_b, rev + 1, now),
        )
        new_rev = db.bump_revision(conn)
        new_groups = db.institution_groups(conn, known=known)
        new_root = new_groups[name_a]
        members = _merge_component_names(conn)[new_root]
    return {
        "ok": True,
        "changed": True,
        "revision": new_rev,
        "names": members,
        "group_key": new_root,
        "submitted": [name_a, name_b],
    }


@app.get("/institutions/merge-groups")
def list_institution_merge_groups(_: bool = Depends(require_organizer)):
    """会务方查看当前全部机构归并组 (归并关系传递且不可拆分; 仅列 >=2 个原名的组)。"""
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        groups = _merge_group_view(conn)
    return {"revision": rev, "groups": groups}


# ---------------------------------------------------------------- 普通分配锁定表 (会务方)

@app.post("/assignment/locks")
def set_assignment_locks(body: LockTableIn, _: bool = Depends(require_organizer)):
    """会务方在普通分配前整表提交评审人锁定。

    - locks 为 {论文编号: [锁定评审人编号 ...]}, 每篇 0~2 名; 整表替换语义:
      未列出的论文清除其锁定, 空表 ({}或缺省) 表示清除全部锁定;
    - 请求须携带所见资料修订号 base_revision, 与当前不一致 (过期/超前) 即 409, 不改表;
    - 同一篇论文的锁定评审人重复 -> 422; 超过两名 -> 422;
      论文或评审人编号去首尾空白后为空白 -> 422;
    - 未知论文或未知评审人 -> 404 (拒绝且不改表);
    - 与当前锁定表 (规范化后) 完全相同的重试幂等返回 (changed=false),
      不推进修订号; 有效变更推进资料修订号 +1;
    - 被锁评审人是否仍合格 (停用/回避/机构冲突/容量) 不在提交时判定,
      由普通预演/发布诊断: 锁定槽位失效只报错、不悄悄释放。
    锁定仅约束普通分配, 不改变补位的确认槽位规则。
    """
    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 锁定表基于修订号 {body.base_revision},"
                    f" 当前修订号为 {rev}, 请重新查看锁定表后再提交"
                ),
            )
        duplicate_papers = []
        oversized = []
        for paper_id, reviewer_ids in body.locks.items():
            if len(reviewer_ids) != len(set(reviewer_ids)):
                duplicate_papers.append(paper_id)
            if len(set(reviewer_ids)) > 2:
                oversized.append(paper_id)
        if duplicate_papers or oversized:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "锁定表参数非法: 每篇论文至多锁定两名评审人且不得重复",
                    "duplicate_reviewer_locks": sorted(set(duplicate_papers)),
                    "more_than_two_locks": sorted(set(oversized)),
                },
            )
        locks, blank_ids = _normalize_lock_table(body.locks)
        if blank_ids:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "锁定表中的论文/评审人编号去首尾空白后不得为空白",
                    "blank": [{"kind": kind, "value": value} for kind, value in blank_ids],
                },
            )
        unknown_papers = sorted(
            pid
            for pid in locks
            if conn.execute("SELECT 1 FROM papers WHERE paper_id = ?", (pid,)).fetchone() is None
        )
        unknown_reviewers = sorted({
            rid
            for ids in locks.values()
            for rid in ids
            if conn.execute(
                "SELECT 1 FROM reviewers WHERE reviewer_id = ?", (rid,)
            ).fetchone() is None
        })
        if unknown_papers or unknown_reviewers:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "锁定表包含未知论文或评审人, 整表拒绝且不改动当前锁定",
                    "unknown_papers": unknown_papers,
                    "unknown_reviewers": unknown_reviewers,
                },
            )

        current = db.load_assignment_locks(conn)
        if current == locks:
            # 相同锁定表重试: 幂等, 不写表、不推进修订号
            return {
                "ok": True,
                "changed": False,
                "revision": rev,
                "locks": locks,
                "locked_papers": len(locks),
                "locked_slots": sum(len(ids) for ids in locks.values()),
            }

        conn.execute("DELETE FROM assignment_locks")
        for pid, ids in locks.items():
            if ids:  # 空列表 = 该篇不锁定, 不落行
                conn.execute(
                    "INSERT INTO assignment_locks(paper_id, reviewers_json, updated_at)"
                    " VALUES (?,?,?)",
                    (pid, json.dumps(ids, ensure_ascii=False), now),
                )
        new_rev = db.bump_revision(conn)
    return {
        "ok": True,
        "changed": True,
        "revision": new_rev,
        "locks": locks,
        "locked_papers": len(locks),
        "locked_slots": sum(len(ids) for ids in locks.values()),
    }


@app.get("/assignment/locks")
def get_assignment_locks(_: bool = Depends(require_organizer)):
    """会务方查看当前普通分配锁定表 (含资料修订号与锁定论文/槽位计数)。"""
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        locks = db.load_assignment_locks(conn)
    return {
        "revision": rev,
        "locks": locks,
        "locked_papers": len(locks),
        "locked_slots": sum(len(ids) for ids in locks.values()),
    }


# ---------------------------------------------------------------- 预演 / 发布 (会务方)

@app.post("/assignment/dry-run")
def dry_run(_: bool = Depends(require_organizer)):
    """基于当前资料试算分配方案; 不改动已发布版本。返回所依据的资料修订号。

    存在普通分配锁定表时, 锁定槽位必须保留 (单人锁定时专长可由搭档满足);
    锁定人失格 (删除/停用/回避/资料或机构归并) 或锁定累计超容量时,
    对应论文在 lock_problems/诊断中说明具体冲突并标记为不可完整分配,
    锁定槽位不被悄悄释放。
    """
    with db.read_txn() as conn:
        rev = db.get_revision(conn)
        result = _normal_assignment(conn)
    result["revision"] = rev
    return result


@app.post("/assignment/publish")
def publish(body: PublishIn, _: bool = Depends(require_organizer)):
    """发布分配方案。必须携带预演所用资料修订号; 资料已变动则拒绝 (409)。

    校验修订号、重算方案、写入发布版在同一个写事务内完成,
    与资料修改并发时不会产生基于旧版资料的方案。
    """
    with db.write_txn() as conn:
        rev = db.get_revision(conn)
        if body.base_revision != rev:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"资料已变更: 预演基于修订号 {body.base_revision}, "
                    f"当前修订号为 {rev}, 请重新预演后再发布"
                ),
            )
        result = _normal_assignment(conn)
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前资料 (含普通分配锁定) 下不存在完整分配方案, 无法发布",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        conn.execute(
            "INSERT INTO published(id, serial, revision, plan, explanations, published_at)"
            " VALUES (1, 1, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET serial = published.serial + 1,"
            " revision = excluded.revision, plan = excluded.plan,"
            " explanations = excluded.explanations, published_at = excluded.published_at",
            (
                rev,
                json.dumps(result["plan"], ensure_ascii=False),
                json.dumps(result["papers"], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        serial = conn.execute("SELECT serial FROM published WHERE id = 1").fetchone()["serial"]
        # 未处理异议随发布序号变化标记过期 (同一写事务, 历史保留供会务方追溯)
        db.expire_pending_objections(conn, serial, datetime.now(timezone.utc).isoformat())
    return {"ok": True, "serial": serial, "revision": rev, "plan": result["plan"]}


@app.get("/assignment")
def get_published(_: bool = Depends(require_organizer)):
    """会务方查询当前已发布方案、每个分配槽位的确认/回避状态及排除原因。"""
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        decisions = _load_decisions(conn, row["serial"])
        plan = json.loads(row["plan"])
        papers = json.loads(row["explanations"])
    for pid, pair in plan.items():
        slots = []
        for rid in pair:
            d = decisions.get((pid, rid))
            slots.append(
                {
                    "reviewer_id": rid,
                    "state": d["state"] if d else "pending",
                    "reason": d["reason"] if d else None,
                    "decided_at": d["decided_at"] if d else None,
                }
            )
        papers.setdefault(pid, {"assigned": pair})["slots"] = slots
    return {
        "serial": row["serial"],
        "revision": row["revision"],
        "published_at": row["published_at"],
        "plan": plan,
        "papers": papers,
    }


@app.get("/papers/{paper_id}/reviews")
def paper_reviews(paper_id: str, _: bool = Depends(require_organizer)):
    """会务方按论文查看当前两名评审人的正式评语提交进度与内容。

    - slots 仅覆盖当前发布版该论文的两个槽位 (含 decision 状态与已交评语);
      进度统计 submitted/confirmed 只计当前槽位;
    - archived 为该论文在往次发布序号上、但已不属于当前方案的评语
      (补位被移出槽位的评语仅供会务方追溯, 不计入新方案进度);
    - corrections 为该论文全部更正请求记录 (冻结的原评语与更正后内容, 供会务方追溯);
    - 被移出后同一评审人后来重新获得该稿时为新序号下的新槽位, 旧评语不会复用。
    """
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案")
        serial = row["serial"]
        plan = json.loads(row["plan"])
        pair = plan.get(paper_id)
        if not pair:
            raise HTTPException(status_code=404, detail=f"当前发布版中不存在论文 {paper_id} 的分配")
        decisions = _load_decisions(conn, serial)
        reviews = _load_reviews(conn, serial)
        slots = []
        for rid in pair:
            d = decisions.get((paper_id, rid))
            r = reviews.get((paper_id, rid))
            slots.append(
                {
                    "reviewer_id": rid,
                    "state": d["state"] if d else "pending",
                    "submitted": r is not None,
                    "review": (
                        None
                        if r is None
                        else {
                            "score": r["score"],
                            "comment": r["comment"],
                            "receipt": r["receipt"],
                            "submitted_at": r["submitted_at"],
                        }
                    ),
                }
            )
        current_receipts = {
            r["receipt"]
            for rid in pair
            if (r := reviews.get((paper_id, rid))) is not None
        }
        archived_rows = conn.execute(
            "SELECT serial, reviewer_id, score, comment, receipt, submitted_at"
            " FROM submitted_reviews WHERE paper_id = ? AND serial != ?"
            " ORDER BY serial, reviewer_id",
            (paper_id, serial),
        ).fetchall()
        archived = [
            {
                "serial": r["serial"],
                "reviewer_id": r["reviewer_id"],
                "score": r["score"],
                "comment": r["comment"],
                "receipt": r["receipt"],
                "submitted_at": r["submitted_at"],
            }
            for r in archived_rows
            # 连续保留槽位沿用的评语与当前槽位同收据 -> 非"移出槽位", 不归档;
            # 被移出槽位 (含同一人后来重新获稿前的旧评语) 均保留供会务方追溯
            if r["receipt"] not in current_receipts
        ]
        corrections = [
            _correction_view(c)
            for c in conn.execute(
                "SELECT * FROM review_corrections WHERE paper_id = ? ORDER BY id",
                (paper_id,),
            ).fetchall()
        ]
    confirmed = sum(1 for s in slots if s["state"] == "confirmed")
    submitted = sum(1 for s in slots if s["submitted"])
    return {
        "paper_id": paper_id,
        "serial": serial,
        "progress": {
            "slots": len(slots),
            "confirmed": confirmed,
            "submitted": submitted,
            "complete": submitted == len(slots),
        },
        "slots": slots,
        "archived_reviews": archived,
        "corrections": corrections,
    }


# ------------------------------------------------ 已交评语更正 (会务方发起)

def _create_review_correction(conn, *, paper_id: str, serial: int, reviewer_id: str, reason: str):
    """在当前写事务内对"当前仍分配、已确认且已交评语"的槽位发起更正请求。

    供会务方更正接口与"异议受理"复用, 保证两条入口走完全相同的既有更正请求流程:
      - 尚未发布 -> 404; serial 与当前发布序号不一致 (过期/超前) -> 409;
        论文不在当前发布版 / 评审人不在该论文当前槽位 -> 404;
        槽位 pending/recused 或尚未交评语 -> 409;
      - 同槽位已有待完成 (pending) 更正请求:
          * 相同原因 -> 幂等: 返回 (既有行, created=False, invalidated=0);
          * 不同原因 -> 409 冲突;
      - 新请求: 冻结原评语, 插入 pending 更正行, 并在同一事务内令该稿当前序号的
        全部作者反馈访问码失效; 返回 (新行, created=True, 本次失效版数)。
    """
    published = _load_published(conn)
    if published is None:
        raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无可更正的评语")
    current_serial = published["serial"]
    if serial != current_serial:
        raise HTTPException(
            status_code=409,
            detail=(
                f"发布序号已过期: 更正请求针对序号 {serial},"
                f" 当前发布序号为 {current_serial}, 请按当前发布版重新发起"
            ),
        )
    plan = json.loads(published["plan"])
    pair = plan.get(paper_id)
    if not pair:
        raise HTTPException(
            status_code=404, detail=f"当前发布版中不存在论文 {paper_id} 的分配"
        )
    if reviewer_id not in pair:
        raise HTTPException(
            status_code=404,
            detail=(
                f"当前发布版中论文 {paper_id} 未分配给 {reviewer_id};"
                " 旧序号或已移出方案的槽位不可发起更正"
            ),
        )
    decision = conn.execute(
        "SELECT state FROM assignment_decisions"
        " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
        (current_serial, paper_id, reviewer_id),
    ).fetchone()
    if decision is None or decision["state"] != "confirmed":
        state = decision["state"] if decision else "pending"
        raise HTTPException(
            status_code=409,
            detail=f"论文 {paper_id} 的槽位当前为 {state} 状态; 仅已确认且未回避的槽位可发起更正",
        )
    review = conn.execute(
        "SELECT score, comment, receipt, submitted_at FROM submitted_reviews"
        " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
        (current_serial, paper_id, reviewer_id),
    ).fetchone()
    if review is None:
        raise HTTPException(
            status_code=409,
            detail=f"评审人 {reviewer_id} 尚未对论文 {paper_id} 提交正式评语, 无可更正内容",
        )
    pending = conn.execute(
        "SELECT * FROM review_corrections"
        " WHERE serial = ? AND paper_id = ? AND reviewer_id = ? AND state = 'pending'",
        (current_serial, paper_id, reviewer_id),
    ).fetchone()
    if pending is not None:
        if pending["reason"] == reason:
            # 相同原因的重复请求: 幂等返回既有待更正请求
            return pending, False, 0
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该槽位已存在待完成的更正请求, 不同原因的重复发起按冲突拒绝",
                "existing": _correction_view(pending),
            },
        )
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO review_corrections"
        "(serial, paper_id, reviewer_id, reason, original_score, original_comment,"
        " original_receipt, original_submitted_at, state, requested_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
        (
            current_serial,
            paper_id,
            reviewer_id,
            reason,
            review["score"],
            review["comment"],
            review["receipt"],
            review["submitted_at"],
            now,
        ),
    )
    # 发起即令该稿既有作者反馈访问码失效
    invalidated = conn.execute(
        "UPDATE feedback_snapshots SET invalidated = 1"
        " WHERE paper_id = ? AND serial = ? AND invalidated = 0",
        (paper_id, current_serial),
    ).rowcount
    row = conn.execute(
        "SELECT * FROM review_corrections WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return row, True, invalidated


@app.post("/papers/{paper_id}/review-corrections")
def request_review_correction(
    paper_id: str,
    body: ReviewCorrectionRequestIn,
    _: bool = Depends(require_organizer),
):
    """会务方针对当前发布版中已交评语发起更正请求。

    - 请求须携带当前发布序号 serial 与非空原因: 序号过期/超前 409, 原因为空白 422;
      尚未发布 404; 论文不在当前发布版 404; 评审人不在该论文当前槽位
      (旧序号或已被移出方案的槽位) 404;
    - 只允许"当前仍分配、已确认且已交评语"的槽位: 槽位 pending/recused 409,
      尚未提交正式评语 409;
    - 同一槽位已有待完成的更正请求时: 相同原因的重复请求幂等返回 (changed=false),
      不同原因 409;
    - 发起即在同一个写事务内令该论文当前序号的全部作者反馈访问码失效
      (invalidated_snapshots 为本次失效的版数), 并冻结原评语供会务方追溯;
    - 更正请求不影响资料修订号与发布序号; 发布序号变化后, 旧序号下的待更正请求失效。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(
            status_code=422, detail="更正原因必须非空 (non-empty reason is required)"
        )

    with db.write_txn() as conn:
        row, created, invalidated = _create_review_correction(
            conn, paper_id=paper_id, serial=body.serial,
            reviewer_id=body.reviewer_id, reason=reason,
        )
    return {
        "ok": True,
        "changed": created,
        **_correction_view(row),
        "invalidated_snapshots": invalidated,
    }


# ------------------------------------------------ 面向作者的匿名反馈快照

# 无效码/已失效码统一 404, 响应体完全相同, 不透露论文是否存在
_SNAPSHOT_INVALID_CODE = HTTPException(
    status_code=404,
    detail="无效或已失效的访问码 (invalid or expired access code)",
)


def _frozen_review(row):
    """从评语行冻结对外内容: 仅评分与评语, 不含收据/评审人/时间等内部字段。"""
    return {"score": row["score"], "comment": row["comment"]}


def _snapshot_public(row):
    """持码者视图: 论文编号 + 按方案槽位固定顺序标号 1、2 的两份评分与评语。"""
    return {
        "paper_id": row["paper_id"],
        "reviews": [
            {"label": 1, **json.loads(row["review1"])},
            {"label": 2, **json.loads(row["review2"])},
        ],
    }


@app.post("/papers/{paper_id}/feedback-snapshot")
def publish_feedback_snapshot(
    paper_id: str,
    body: SnapshotPublishIn,
    _: bool = Depends(require_organizer),
):
    """会务方按论文发布面向作者的匿名反馈快照。

    - 请求须携带当前发布序号 serial: 与当前发布序号不一致 (过期/超前) 即 409;
      尚未发布 404; 该论文不在当前发布版中 404;
    - 仅当该序号下两名评审人均已确认且各提交一份正式评语时才允许发布,
      否则 422 且不写入任何新版本;
    - 相同发布序号及两份评语收据的重复请求幂等返回原快照与原访问码 (changed=false);
      来源变化 (补位/重新发布导致序号前进、评语为新收据) 后生成新版本
      (version+1) 与新随机访问码, 旧码立即失效, 旧版保留仅供会务方追溯;
    - 该论文在当前序号下存在待完成的评语更正请求时拒绝发布 (409):
      更正请求发起时既有访问码已失效, 须待更正完成后按当前评语重新发布。
    """
    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无反馈快照可发布")
        current_serial = published["serial"]
        if body.serial != current_serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"发布序号已过期: 快照请求针对序号 {body.serial},"
                    f" 当前发布序号为 {current_serial}, 请按当前发布版重新请求"
                ),
            )
        plan = json.loads(published["plan"])
        pair = plan.get(paper_id)
        if not pair:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布版中不存在论文 {paper_id} 的分配, 无法发布快照",
            )
        decisions = _load_decisions(conn, current_serial)
        reviews = _load_reviews(conn, current_serial)
        pending_corrections = conn.execute(
            "SELECT reviewer_id FROM review_corrections"
            " WHERE serial = ? AND paper_id = ? AND state = 'pending' ORDER BY id",
            (current_serial, paper_id),
        ).fetchall()
        if pending_corrections:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "该论文存在待完成的评语更正请求, 既有访问码已失效;"
                        " 须待更正完成后按当前评语重新发布快照"
                    ),
                    "pending_corrections": [c["reviewer_id"] for c in pending_corrections],
                },
            )
        slots = [
            (rid, decisions.get((paper_id, rid)), reviews.get((paper_id, rid)))
            for rid in pair  # 方案槽位顺序 (评审人编号字典序) 即快照标号 1、2 的固定顺序
        ]
        waiting_confirmation = [
            rid for rid, d, _ in slots if d is None or d["state"] != "confirmed"
        ]
        waiting_review = [rid for rid, _, r in slots if r is None]
        if waiting_confirmation or waiting_review:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "两名评审人尚未全部确认并各提交一份正式评语, 快照发布拒绝,"
                        " 不产生新版本"
                    ),
                    "slots": len(slots),
                    "confirmed": len(slots) - len(waiting_confirmation),
                    "submitted": len(slots) - len(waiting_review),
                    "waiting_confirmation": waiting_confirmation,
                    "waiting_review": waiting_review,
                },
            )
        receipt1, receipt2 = slots[0][2]["receipt"], slots[1][2]["receipt"]
        existing = conn.execute(
            "SELECT * FROM feedback_snapshots"
            " WHERE paper_id = ? AND serial = ? AND receipt1 = ? AND receipt2 = ?",
            (paper_id, current_serial, receipt1, receipt2),
        ).fetchone()
        if existing is not None:
            return {
                "ok": True,
                "paper_id": paper_id,
                "serial": current_serial,
                "version": existing["version"],
                "access_code": existing["access_code"],
                "changed": False,
                "snapshot": _snapshot_public(existing),
            }
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v"
            " FROM feedback_snapshots WHERE paper_id = ?",
            (paper_id,),
        ).fetchone()["v"]
        while True:
            access_code = f"fbk-{secrets.token_hex(16)}"
            if conn.execute(
                "SELECT 1 FROM feedback_snapshots WHERE access_code = ?", (access_code,)
            ).fetchone() is None:
                break
        conn.execute(
            "INSERT INTO feedback_snapshots"
            "(paper_id, version, serial, receipt1, receipt2, access_code,"
            " review1, review2, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                paper_id,
                version,
                current_serial,
                receipt1,
                receipt2,
                access_code,
                json.dumps(_frozen_review(slots[0][2]), ensure_ascii=False),
                json.dumps(_frozen_review(slots[1][2]), ensure_ascii=False),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM feedback_snapshots WHERE access_code = ?", (access_code,)
        ).fetchone()
    return {
        "ok": True,
        "paper_id": paper_id,
        "serial": current_serial,
        "version": version,
        "access_code": access_code,
        "changed": True,
        "snapshot": _snapshot_public(row),
    }


@app.get("/feedback-snapshots/{access_code}")
def read_feedback_snapshot(access_code: str):
    """持码者读取论文当前有效反馈快照 (无需会务方/评审人凭据)。

    仅返回论文编号与按固定顺序标号 1、2 的两份评分和评语,
    不暴露评审人编号、机构、收据及任何内部诊断字段。
    持码者只能读取当前有效快照: 补位或重新发布导致发布序号变化时,
    旧码立即失效; 无效码、已失效码统一 404, 不透露论文是否存在。
    """
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT * FROM feedback_snapshots WHERE access_code = ?", (access_code,)
        ).fetchone()
        if row is None:
            raise _SNAPSHOT_INVALID_CODE
        published = _load_published(conn)
        # 快照仅在其发布序号仍为当前发布序号且未被更正请求失效时有效;
        # 发布序号单调递增, 序号相同即意味着该论文没有更新的快照版本
        if published is None or published["serial"] != row["serial"] or row["invalidated"]:
            raise _SNAPSHOT_INVALID_CODE
        return _snapshot_public(row)


@app.get("/papers/{paper_id}/feedback-snapshots")
def list_feedback_snapshots(paper_id: str, _: bool = Depends(require_organizer)):
    """会务方追溯某篇论文的全部快照版本 (含已失效旧版及其访问码与评语收据)。"""
    with db.read_txn() as conn:
        published = _load_published(conn)
        current_serial = published["serial"] if published else None
        rows = conn.execute(
            "SELECT version, serial, receipt1, receipt2, access_code,"
            " review1, review2, created_at, invalidated FROM feedback_snapshots"
            " WHERE paper_id = ? ORDER BY version",
            (paper_id,),
        ).fetchall()
        versions = [
            {
                "version": r["version"],
                "serial": r["serial"],
                "access_code": r["access_code"],
                "active": current_serial == r["serial"] and not r["invalidated"],
                "receipts": [r["receipt1"], r["receipt2"]],
                "reviews": [
                    {"label": 1, **json.loads(r["review1"])},
                    {"label": 2, **json.loads(r["review2"])},
                ],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    return {"paper_id": paper_id, "current_serial": current_serial, "snapshots": versions}


# ------------------------------------------------ 作者异议 (持快照访问码)

# 无效/已失效访问码与无效查询凭据统一 404, 响应体相同, 不透露论文是否存在
_OBJECTION_INVALID_TOKEN = HTTPException(
    status_code=404,
    detail="无效或已失效的凭据 (invalid or expired token)",
)


def _objection_author_view(row):
    """作者视图: 仅论文编号、匿名标号、本人理由、处理状态与状态时间。

    绝不包含评审人编号、机构、评语收据、快照访问码/版本/序号等内部字段;
    受理时的更正原因通过 resolution.reason 告知处理结果, 驳回说明通过
    resolution.note 返回, 过期无附言。
    """
    state = row["state"]
    if state == "accepted":
        resolution = {
            "decided_at": row["decided_at"],
            "reason": row["resolution_note"],
        }
    elif state == "rejected":
        resolution = {
            "decided_at": row["decided_at"],
            "note": row["resolution_note"],
        }
    else:
        resolution = None
    return {
        "paper_id": row["paper_id"],
        "label": row["label"],
        "reason": row["reason"],
        "state": state,
        "submitted_at": row["created_at"],
        "resolved_at": (
            row["decided_at"] if state in ("rejected", "accepted", "expired") else None
        ),
        "resolution": resolution,
    }


@app.post("/feedback-objections")
def submit_feedback_objection(body: FeedbackObjectionIn):
    """作者 (持码者) 凭当前有效的快照访问码, 对标号 1 或 2 的评语提交异议。

    - 无需会务方/评审人凭据, 仅凭请求体中的快照访问码; 理由去除首尾空白后必须非空;
    - 访问码无效或已失效 (旧发布序号、已被更正请求或异议受理失效) 统一 404,
      不透露论文是否存在; 已失效访问码不得提交新异议;
    - 同一快照同一标号仅留一条: 相同理由的重试幂等返回原记录与原查询凭据
      (changed=false); 理由不同返回 409 冲突, 已存异议不变;
      UNIQUE(snapshot_id, label) + BEGIN IMMEDIATE 写事务保证并发提交也只生成一条;
    - 提交成功返回随机查询凭据 (obj- 前缀): 持凭据可随时查看处理状态,
      即使快照访问码后来失效; 响应不暴露评审人编号、机构或评语收据。
    """
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="异议理由必须非空 (non-empty reason is required)")

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        snapshot = conn.execute(
            "SELECT * FROM feedback_snapshots WHERE access_code = ?", (body.access_code,)
        ).fetchone()
        if snapshot is None:
            raise _OBJECTION_INVALID_TOKEN
        published = _load_published(conn)
        # 与持码读取同一有效口径: 仅当前发布序号且未被更正请求失效的快照可提交异议
        if published is None or published["serial"] != snapshot["serial"] or snapshot["invalidated"]:
            raise _OBJECTION_INVALID_TOKEN

        existing = conn.execute(
            "SELECT * FROM feedback_objections WHERE snapshot_id = ? AND label = ?",
            (snapshot["id"], body.label),
        ).fetchone()
        if existing is not None:
            if existing["reason"] == reason:
                # 相同理由重试: 幂等返回原记录 (不论该异议当前处于何种状态)
                return {
                    "ok": True,
                    "changed": False,
                    "query_token": existing["query_token"],
                    "objection": _objection_author_view(existing),
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该快照此标号已存在一条异议, 理由不同的提交按冲突拒绝",
                    "state": existing["state"],
                },
            )

        frozen = json.loads(snapshot["review1"] if body.label == 1 else snapshot["review2"])
        target_receipt = snapshot["receipt1"] if body.label == 1 else snapshot["receipt2"]
        while True:
            token = f"obj-{secrets.token_hex(16)}"
            if conn.execute(
                "SELECT 1 FROM feedback_objections WHERE query_token = ?", (token,)
            ).fetchone() is None:
                break
        try:
            conn.execute(
                "INSERT INTO feedback_objections"
                "(snapshot_id, paper_id, serial, label, reason, frozen_review_json,"
                " target_receipt, state, query_token, created_at)"
                " VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)",
                (
                    snapshot["id"],
                    snapshot["paper_id"],
                    snapshot["serial"],
                    body.label,
                    reason,
                    json.dumps(frozen, ensure_ascii=False),
                    target_receipt,
                    token,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # 并发提交同一快照同一标号: 退化为幂等/冲突语义, 绝不生成两条
            row = conn.execute(
                "SELECT * FROM feedback_objections WHERE snapshot_id = ? AND label = ?",
                (snapshot["id"], body.label),
            ).fetchone()
            if row is not None and row["reason"] == reason:
                return {
                    "ok": True,
                    "changed": False,
                    "query_token": row["query_token"],
                    "objection": _objection_author_view(row),
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该快照此标号已存在一条异议 (并发提交), 理由不同的提交按冲突拒绝",
                    "state": row["state"] if row else "pending",
                },
            )
        row = conn.execute(
            "SELECT * FROM feedback_objections WHERE query_token = ?", (token,)
        ).fetchone()
    return {
        "ok": True,
        "changed": True,
        "query_token": token,
        "objection": _objection_author_view(row),
    }


@app.get("/feedback-objections/{query_token}")
def read_feedback_objection(query_token: str):
    """持查询凭据查看异议处理状态 (无需任何其他凭据)。

    查询凭据独立于快照访问码: 即使快照码后来因发布序号变化或更正请求失效,
    持凭据仍可查看状态。无效凭据统一 404, 不透露论文是否存在;
    响应不暴露评审人编号、机构或评语收据。
    """
    with db.read_txn() as conn:
        row = conn.execute(
            "SELECT * FROM feedback_objections WHERE query_token = ?", (query_token,)
        ).fetchone()
        if row is None:
            raise _OBJECTION_INVALID_TOKEN
        return _objection_author_view(row)


# ------------------------------------------------ 异议会务方处理

def _resolve_objection_reviewer(conn, obj):
    """按异议的匿名标号定位其对应的评审人编号 (仅供会务方视图与受理流程)。

    标号按方案槽位固定顺序 (评审人编号字典序): label 1/2 即该发布序号槽位顺序。
    异议提交时冻结了目标评语收据: 若异议序号仍为当前发布序号, 直接取当前槽位并
    以收据核对; 旧序号 (异议已随发布过期/受理) 的方案行已不在 published 表,
    改按"该论文 + 冻结序号 + 冻结收据"在评语历史行中追溯定位
    (连续保留槽位沿用同收据, 旧序号原行也始终保留)。
    受理后若评审人已完成更正, submitted_reviews 的收据会原地更新为新收据,
    此时从关联更正记录冻结的 original_receipt 定位评审人。
    返回 (reviewer_id 或 None, 评语行或 None)。
    """
    published = _load_published(conn)
    if published is not None and published["serial"] == obj["serial"]:
        pair = json.loads(published["plan"]).get(obj["paper_id"])
        if pair and obj["label"] - 1 < len(pair):
            rid = pair[obj["label"] - 1]
            review = conn.execute(
                "SELECT receipt FROM submitted_reviews"
                " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
                (obj["serial"], obj["paper_id"], rid),
            ).fetchone()
            if review is not None and review["receipt"] == obj["target_receipt"]:
                return rid, review
    row = conn.execute(
        "SELECT reviewer_id FROM submitted_reviews"
        " WHERE serial = ? AND paper_id = ? AND receipt = ?",
        (obj["serial"], obj["paper_id"], obj["target_receipt"]),
    ).fetchone()
    if row is not None:
        return row["reviewer_id"], row
    if obj["correction_id"] is not None:
        corr = conn.execute(
            "SELECT reviewer_id, original_receipt FROM review_corrections WHERE id = ?",
            (obj["correction_id"],),
        ).fetchone()
        if corr is not None and corr["original_receipt"] == obj["target_receipt"]:
            return corr["reviewer_id"], None
    return None, None


def _objection_organizer_view(conn, obj):
    """会务方视图: 异议详情 + 冻结的原反馈 + 按匿名标号定位的评审人 + 关联更正。"""
    reviewer_id, _ = _resolve_objection_reviewer(conn, obj)
    snapshot = conn.execute(
        "SELECT version, access_code, invalidated FROM feedback_snapshots WHERE id = ?",
        (obj["snapshot_id"],),
    ).fetchone()
    correction = None
    if obj["correction_id"] is not None:
        c = conn.execute(
            "SELECT * FROM review_corrections WHERE id = ?", (obj["correction_id"],)
        ).fetchone()
        if c is not None:
            correction = _correction_view(c)
    return {
        "objection_id": obj["id"],
        "paper_id": obj["paper_id"],
        "serial": obj["serial"],
        "snapshot_version": snapshot["version"] if snapshot else None,
        "label": obj["label"],
        "reviewer_id": reviewer_id,
        "reason": obj["reason"],
        "state": obj["state"],
        "frozen_feedback": {
            "label": obj["label"],
            **json.loads(obj["frozen_review_json"]),
        },
        "target_receipt": obj["target_receipt"],
        "snapshot_access_code": snapshot["access_code"] if snapshot else None,
        "snapshot_invalidated": bool(snapshot["invalidated"]) if snapshot else None,
        "reject_note": obj["resolution_note"] if obj["state"] == "rejected" else None,
        "accepted_reason": obj["resolution_note"] if obj["state"] == "accepted" else None,
        "correction": correction,
        "query_token": obj["query_token"],
        "submitted_at": obj["created_at"],
        "decided_at": obj["decided_at"],
    }


def _list_objections(conn, *, paper_id: str | None = None, state: str | None = None):
    sql = (
        "SELECT * FROM feedback_objections"
        + (" WHERE paper_id = ?" if paper_id is not None else "")
        + (" AND state = ?" if paper_id is not None and state is not None
           else " WHERE state = ?" if state is not None else "")
        + " ORDER BY id"
    )
    params = []
    if paper_id is not None:
        params.append(paper_id)
    if state is not None:
        params.append(state)
    return [_objection_organizer_view(conn, r) for r in conn.execute(sql, params).fetchall()]


@app.get("/papers/{paper_id}/objections")
def list_paper_objections(
    paper_id: str,
    state: str | None = Query(default=None, pattern="^(pending|rejected|accepted|expired)$"),
    _: bool = Depends(require_organizer),
):
    """会务方按论文查看全部作者异议 (含驳回/受理/已过期历史) 与冻结的原反馈。

    可选 ?state=pending|rejected|accepted|expired 过滤; 视图含按匿名标号定位到的
    评审人编号、目标评语收据、快照访问码及受理后关联的更正请求, 仅供会务方追溯。
    """
    with db.read_txn() as conn:
        published = _load_published(conn)
        current_serial = published["serial"] if published else None
        objections = _list_objections(conn, paper_id=paper_id, state=state)
    return {
        "paper_id": paper_id,
        "current_serial": current_serial,
        "objections": objections,
    }


@app.get("/objections")
def list_all_objections(
    state: str | None = Query(default=None, pattern="^(pending|rejected|accepted|expired)$"),
    _: bool = Depends(require_organizer),
):
    """会务方查看全部论文的异议 (含已过期历史), 可选 ?state= 过滤; 按提交顺序返回。"""
    with db.read_txn() as conn:
        objections = _list_objections(conn, state=state)
    return {"objections": objections}


@app.post("/objections/{objection_id}/decision")
def decide_feedback_objection(
    objection_id: int,
    body: FeedbackObjectionDecisionIn,
    _: bool = Depends(require_organizer),
):
    """会务方对一条异议选择驳回或受理。

    - 异议不存在 -> 404; 仅 pending 异议可处理, 已驳回/已受理/已过期 -> 409;
    - 驳回 (reject): 不影响快照 (快照访问码仍可读取), 记录可选驳回说明;
    - 受理 (accept): 必须填写非空更正原因, 且在同一个写事务内原子核对:
        1) 快照发布序号仍为当前发布序号 (发布版未变化);
        2) 目标评语收据未变 (该标号评语未被改过/替换);
        3) 按匿名标号能定位到当前槽位评审人, 且该槽位尚无待完成更正请求;
      核对通过则走既有更正请求流程 (_create_review_correction): 插入待更正请求,
      该稿原访问码立即失效, 异议置 accepted 并关联更正请求;
      发布版/目标评语已变化或该槽位已有待完成更正请求时拒绝受理 (409),
      异议状态保持不变。
    """
    note = body.note.strip() if body.note is not None else None
    reason = body.reason.strip() if body.reason is not None else None
    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        obj = conn.execute(
            "SELECT * FROM feedback_objections WHERE id = ?", (objection_id,)
        ).fetchone()
        if obj is None:
            raise HTTPException(status_code=404, detail=f"异议不存在: {objection_id}")
        if obj["state"] != "pending":
            # 已随发布序号变化过期的异议: 仍给出发布版变化的原子核对明细,
            # 但拒绝处理且不改变 (过期) 状态; 已驳回/已受理为终态不可重复处理。
            if obj["state"] == "expired":
                published = _load_published(conn)
                current_serial = published["serial"] if published else None
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "该异议已随发布序号变化过期; 快照发布序号已不是当前序号,"
                            " 拒绝受理且不改变异议状态"
                        ),
                        "state": "expired",
                        "snapshot_serial": obj["serial"],
                        "current_serial": current_serial,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该异议已处理, 不可重复处理",
                    "state": obj["state"],
                },
            )

        if body.decision == "reject":
            conn.execute(
                "UPDATE feedback_objections SET state = 'rejected',"
                " resolution_note = ?, decided_at = ? WHERE id = ?",
                (note, now, objection_id),
            )
            row = conn.execute(
                "SELECT * FROM feedback_objections WHERE id = ?", (objection_id,)
            ).fetchone()
            return {"ok": True, "changed": True, "objection": _objection_organizer_view(conn, row)}

        # decision == "accept"
        if not reason:
            raise HTTPException(
                status_code=422,
                detail="受理异议必须填写非空更正原因 (non-empty correction reason is required)",
            )
        published = _load_published(conn)
        if published is None or published["serial"] != obj["serial"]:
            current_serial = published["serial"] if published else None
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "快照发布序号已不是当前序号 (发布版或目标评语已变化),"
                        " 拒绝受理且不改变异议状态"
                    ),
                    "snapshot_serial": obj["serial"],
                    "current_serial": current_serial,
                },
            )
        plan = json.loads(published["plan"])
        pair = plan.get(obj["paper_id"])
        if not pair or obj["label"] - 1 >= len(pair):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "论文已不在当前发布版, 拒绝受理且不改变异议状态",
                    "paper_id": obj["paper_id"],
                },
            )
        reviewer_id = pair[obj["label"] - 1]
        review = conn.execute(
            "SELECT receipt FROM submitted_reviews"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (obj["serial"], obj["paper_id"], reviewer_id),
        ).fetchone()
        # 原子核对目标评语收据未变 (含"评语经更正后收据已变"的情形)
        if review is None or review["receipt"] != obj["target_receipt"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "目标评语收据已变化 (评语已被更正或替换), 拒绝受理且不改变异议状态"
                    ),
                    "target_receipt": obj["target_receipt"],
                    "current_receipt": review["receipt"] if review else None,
                },
            )
        # 该目标槽位已有待完成更正请求时一律拒绝受理 (无论原因是否相同), 异议状态不变
        existing_pending = conn.execute(
            "SELECT id, reason FROM review_corrections"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ? AND state = 'pending'",
            (obj["serial"], obj["paper_id"], reviewer_id),
        ).fetchone()
        if existing_pending is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该槽位已存在待完成的更正请求, 拒绝受理且不改变异议状态",
                    "existing_correction_id": existing_pending["id"],
                },
            )
        # 走既有更正请求流程: 槽位资格核对、原访问码立即失效均复用同一事务逻辑。
        correction, _created, invalidated = _create_review_correction(
            conn, paper_id=obj["paper_id"], serial=obj["serial"],
            reviewer_id=reviewer_id, reason=reason,
        )
        conn.execute(
            "UPDATE feedback_objections SET state = 'accepted',"
            " resolution_note = ?, correction_id = ?, decided_at = ? WHERE id = ?",
            (reason, correction["id"], now, objection_id),
        )
        row = conn.execute(
            "SELECT * FROM feedback_objections WHERE id = ?", (objection_id,)
        ).fetchone()
        view = _objection_organizer_view(conn, row)
    return {
        "ok": True,
        "changed": True,
        "invalidated_snapshots": invalidated,
        "objection": view,
    }


# ---------------------------------------------------------------- 评审人视图

# ---------------------------------------------------------------- 评审人决定与视图

@app.post("/reviewer/assignments/{paper_id}/decision")
def submit_decision(
    paper_id: str,
    body: AssignmentDecisionIn,
    reviewer_id: str = Depends(require_active_reviewer),
):
    """评审人对当前发布版中分配给自己的论文确认接受或声明回避。

    - 同一决定重复提交幂等 (不推进修订号);
    - 已回避的任务不能再确认 (409);
    - 未分配给本人 / 论文不存在 / 尚未发布 -> 404 (视为未分配任务, 拒绝操作);
    - 回避原因必须非空 (422);
    - 有效决定 (新确认、首次回避、确认后改回避) 推进资料修订号,
      回避立即撤销该评审人对本匿名稿的读取, 并成为后续分配的硬回避。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    if body.decision == "recuse":
        reason = (body.reason or "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="回避原因必须非空 (recuse reason is required)")
    else:
        reason = None

    now = datetime.now(timezone.utc).isoformat()
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 不存在可操作的任务")
        plan = json.loads(published["plan"])
        pair = plan.get(paper_id)
        if not pair or reviewer_id not in pair:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布版中论文 {paper_id} 未分配给 {reviewer_id}, 拒绝操作",
            )
        serial = published["serial"]
        existing = conn.execute(
            "SELECT state, reason FROM assignment_decisions"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (serial, paper_id, reviewer_id),
        ).fetchone()

        if body.decision == "confirm":
            if existing is not None and existing["state"] == "recused":
                raise HTTPException(
                    status_code=409,
                    detail="该任务已声明回避, 不能再确认; 如需参与请联系会务方",
                )
            if existing is not None and existing["state"] == "confirmed":
                changed = False
                rev = db.get_revision(conn)
            else:
                conn.execute(
                    "INSERT INTO assignment_decisions"
                    "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(serial, paper_id, reviewer_id)"
                    " DO UPDATE SET state = excluded.state, reason = excluded.reason,"
                    " decided_at = excluded.decided_at",
                    (serial, paper_id, reviewer_id, "confirmed", None, now),
                )
                rev = db.bump_revision(conn)
                changed = True
            return {
                "ok": True,
                "paper_id": paper_id,
                "reviewer_id": reviewer_id,
                "state": "confirmed",
                "changed": changed,
                "revision": rev,
                "serial": serial,
            }

        # decision == "recuse"
        if existing is not None and existing["state"] == "recused":
            changed = False
            rev = db.get_revision(conn)
        else:
            conn.execute(
                "INSERT INTO assignment_decisions"
                "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(serial, paper_id, reviewer_id)"
                " DO UPDATE SET state = excluded.state, reason = excluded.reason,"
                " decided_at = excluded.decided_at",
                (serial, paper_id, reviewer_id, "recused", reason, now),
            )
            conn.execute(
                "INSERT INTO reviewer_recusals"
                "(reviewer_id, paper_id, reason, created_serial, created_revision, created_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(reviewer_id, paper_id) DO UPDATE SET"
                " reason = excluded.reason, created_serial = excluded.created_serial,"
                " created_revision = excluded.created_revision, created_at = excluded.created_at",
                (reviewer_id, paper_id, reason, serial, db.get_revision(conn) + 1, now),
            )
            rev = db.bump_revision(conn)
            changed = True
        return {
            "ok": True,
            "paper_id": paper_id,
            "reviewer_id": reviewer_id,
            "state": "recused",
            "reason": reason if changed else existing["reason"],
            "changed": changed,
            "revision": rev,
            "serial": serial,
        }


@app.get("/reviewer/assignments")
def reviewer_assignments(reviewer_id: str = Depends(require_active_reviewer)):
    """评审人凭自身凭据读取已分配给自己的匿名稿 (仅当前发布版, 不含作者机构)。

    - 已回避的论文立即从取稿列表中移除 (仅在 recused 中保留编号与本人填写的原因);
    - 补位重新发布后, 不在新方案中的关系不再授权取稿。
    """
    with db.read_txn() as conn:
        row = _load_published(conn)
        if row is None:
            return {"reviewer_id": reviewer_id, "serial": None, "assignments": [], "recused": []}
        serial = row["serial"]
        plan = json.loads(row["plan"])
        decisions = _load_decisions(conn, serial)
        mine = sorted(pid for pid, rs in plan.items() if reviewer_id in rs)
        out, recused, states = [], [], {}
        for pid in mine:
            d = decisions.get((pid, reviewer_id))
            state = d["state"] if d else "pending"
            states[pid] = state
            if state == "recused":
                recused.append({"paper_id": pid, "reason": d["reason"], "decided_at": d["decided_at"]})
                continue
            paper = conn.execute(
                "SELECT paper_id, topics, manuscript FROM papers WHERE paper_id = ?", (pid,)
            ).fetchone()
            if paper is not None:
                out.append(
                    {
                        "paper_id": paper["paper_id"],
                        "topics": json.loads(paper["topics"]),
                        "manuscript": paper["manuscript"],
                    }
                )
    return {
        "reviewer_id": reviewer_id,
        "serial": serial,
        "assignments": out,
        "recused": recused,
        "states": states,
    }


@app.post("/reviewer/assignments/{paper_id}/review")
def submit_review(
    paper_id: str,
    body: ReviewSubmissionIn,
    reviewer_id: str = Depends(require_active_reviewer),
):
    """评审人对当前发布版中"已确认且未回避"的有效任务提交正式评分与评语。

    - 请求体须携带所针对的发布序号 serial, 与当前发布序号不一致 (过期/超前) 即 409;
    - 同一事务内核对: 发布序号、(论文, 评审人) 分配关系、决定状态;
      未发布/论文不存在/未分配给本人/已被移出当前方案 -> 404,
      槽位未确认 (pending) 或已回避 (recused) -> 409;
    - 评分必须是 1~5 的整数, 评语去除首尾空白后必须非空, 否则 422;
    - 同一有效任务只收一份:
        * 评分与评语完全相同的重试 -> 幂等返回原收据 (changed=false, 200);
        * 内容不同 -> 409 冲突, 已存评语不变;
    - 被拒请求不落库, 也不推进资料修订号。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=422, detail="正式评语必须非空 (non-empty comment is required)")

    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 不存在可评审的任务")
        current_serial = published["serial"]
        if body.serial != current_serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"发布序号已过期: 提交针对序号 {body.serial}, 当前发布序号为 {current_serial},"
                    " 请按当前发布版重新提交"
                ),
            )
        plan = json.loads(published["plan"])
        pair = plan.get(paper_id)
        if not pair or reviewer_id not in pair:
            raise HTTPException(
                status_code=404,
                detail=f"当前发布版中论文 {paper_id} 未分配给 {reviewer_id}, 拒绝评语提交",
            )
        decision = conn.execute(
            "SELECT state FROM assignment_decisions"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (current_serial, paper_id, reviewer_id),
        ).fetchone()
        if decision is None or decision["state"] != "confirmed":
            state = decision["state"] if decision else "pending"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"论文 {paper_id} 的槽位当前为 {state} 状态; 仅已确认且未回避的任务可提交正式评语"
                ),
            )
        existing = conn.execute(
            "SELECT score, comment, receipt, submitted_at FROM submitted_reviews"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (current_serial, paper_id, reviewer_id),
        ).fetchone()
        if existing is not None:
            if existing["score"] == body.score and existing["comment"] == comment:
                # 完全相同的重试: 原样返回原收据
                return {
                    "ok": True,
                    "paper_id": paper_id,
                    "reviewer_id": reviewer_id,
                    "serial": current_serial,
                    "score": existing["score"],
                    "comment": existing["comment"],
                    "receipt": existing["receipt"],
                    "submitted_at": existing["submitted_at"],
                    "changed": False,
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该有效任务已有一份正式评语, 内容不一致的提交按冲突拒绝",
                    "existing": {
                        "score": existing["score"],
                        "receipt": existing["receipt"],
                        "submitted_at": existing["submitted_at"],
                    },
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        receipt = f"rvw-{secrets.token_hex(8)}"
        conn.execute(
            "INSERT INTO submitted_reviews"
            "(serial, paper_id, reviewer_id, score, comment, receipt, submitted_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (current_serial, paper_id, reviewer_id, body.score, comment, receipt, now),
        )
    return {
        "ok": True,
        "paper_id": paper_id,
        "reviewer_id": reviewer_id,
        "serial": current_serial,
        "score": body.score,
        "comment": comment,
        "receipt": receipt,
        "submitted_at": now,
        "changed": True,
    }


@app.get("/reviewer/reviews")
def reviewer_reviews(reviewer_id: str = Depends(require_active_reviewer)):
    """评审人查看本人在当前发布版有效任务上已提交的正式评语。

    仅返回当前发布版中分配给本人、且已确认未回避槽位的评语;
    不包含另一评审人的任何信息, 也不返回作者机构。
    """
    with db.read_txn() as conn:
        published = _load_published(conn)
        if published is None:
            return {"reviewer_id": reviewer_id, "serial": None, "reviews": []}
        serial = published["serial"]
        plan = json.loads(published["plan"])
        decisions = _load_decisions(conn, serial)
        reviews = _load_reviews(conn, serial)
        out = []
        for pid in sorted(pid for pid, rs in plan.items() if reviewer_id in rs):
            d = decisions.get((pid, reviewer_id))
            if d is None or d["state"] != "confirmed":
                continue  # pending/已回避/被移出的槽位均非本人当前有效任务
            r = reviews.get((pid, reviewer_id))
            if r is None:
                continue
            out.append(
                {
                    "paper_id": pid,
                    "score": r["score"],
                    "comment": r["comment"],
                    "receipt": r["receipt"],
                    "submitted_at": r["submitted_at"],
                }
            )
    return {"reviewer_id": reviewer_id, "serial": serial, "reviews": out}


# ------------------------------------------------ 评审人评语更正

@app.get("/reviewer/review-corrections")
def reviewer_correction_tasks(reviewer_id: str = Depends(require_active_reviewer)):
    """评审人查看本人在当前发布版上的待更正任务 (会务方发起的评语更正请求)。

    仅列出当前发布序号下状态为待更正 (pending) 的任务, 含会务方填写的更正原因
    与被更正评语的冻结内容 (本人评语); 发布序号变化后, 旧序号下的待更正请求失效,
    不再列出。不包含其他评审人的任何信息。
    """
    with db.read_txn() as conn:
        published = _load_published(conn)
        if published is None:
            return {"reviewer_id": reviewer_id, "serial": None, "corrections": []}
        serial = published["serial"]
        rows = conn.execute(
            "SELECT * FROM review_corrections"
            " WHERE serial = ? AND reviewer_id = ? AND state = 'pending' ORDER BY id",
            (serial, reviewer_id),
        ).fetchall()
    return {
        "reviewer_id": reviewer_id,
        "serial": serial,
        "corrections": [_correction_view(r) for r in rows],
    }


@app.post("/reviewer/review-corrections/{paper_id}")
def submit_review_correction(
    paper_id: str,
    body: ReviewCorrectionSubmitIn,
    reviewer_id: str = Depends(require_active_reviewer),
):
    """评审人按待更正任务提交更正后的正式评分与评语。

    - 请求体须携带更正请求对应的发布序号 serial 与原评语收据 original_receipt:
      序号过期/超前 409; 不存在对应的 (待) 更正任务 404;
    - 评分必须是 1~5 的整数, 评语去除首尾空白后必须非空, 否则 422;
    - 首份有效更正生成新收据, 并直接更新该槽位的正式评语 (原评语在更正记录中
      冻结保留, 供会务方追溯);
    - 同一更正任务只收一份: 评分与评语 (首尾空白归一化后) 完全相同的重试
      幂等返回更正收据 (changed=false); 内容不同 409 冲突, 已存更正不变;
    - 发布序号变化后, 旧序号下的待更正请求失效, 不可再提交;
    - 被拒请求不落库, 也不推进资料修订号。
    """
    if body.paper_id != paper_id:
        raise HTTPException(
            status_code=422,
            detail=f"路径中的论文编号 ({paper_id}) 与请求体 ({body.paper_id}) 不一致",
        )
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=422, detail="更正评语必须非空 (non-empty comment is required)")

    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 不存在待更正任务")
        current_serial = published["serial"]
        if body.serial != current_serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"发布序号已过期: 更正提交针对序号 {body.serial},"
                    f" 当前发布序号为 {current_serial}, 待更正请求已失效"
                ),
            )
        corr = conn.execute(
            "SELECT * FROM review_corrections"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ? AND original_receipt = ?",
            (current_serial, paper_id, reviewer_id, body.original_receipt),
        ).fetchone()
        if corr is None:
            raise HTTPException(
                status_code=404,
                detail="不存在与该收据对应的更正任务 (或该任务已随发布序号变化而失效)",
            )
        if corr["state"] == "completed":
            if corr["new_score"] == body.score and corr["new_comment"] == comment:
                # 完全相同的重试: 原样返回更正收据
                return {
                    "ok": True,
                    "paper_id": paper_id,
                    "reviewer_id": reviewer_id,
                    "serial": current_serial,
                    "correction_id": corr["id"],
                    "score": corr["new_score"],
                    "comment": corr["new_comment"],
                    "receipt": corr["new_receipt"],
                    "original_receipt": corr["original_receipt"],
                    "corrected_at": corr["corrected_at"],
                    "changed": False,
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该更正任务已有一份更正评语, 内容不一致的提交按冲突拒绝",
                    "existing": {
                        "score": corr["new_score"],
                        "receipt": corr["new_receipt"],
                        "corrected_at": corr["corrected_at"],
                    },
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        receipt = f"rvw-{secrets.token_hex(8)}"
        conn.execute(
            "UPDATE submitted_reviews SET score = ?, comment = ?, receipt = ?, submitted_at = ?"
            " WHERE serial = ? AND paper_id = ? AND reviewer_id = ?",
            (body.score, comment, receipt, now, current_serial, paper_id, reviewer_id),
        )
        conn.execute(
            "UPDATE review_corrections SET state = 'completed', new_score = ?,"
            " new_comment = ?, new_receipt = ?, corrected_at = ? WHERE id = ?",
            (body.score, comment, receipt, now, corr["id"]),
        )
    return {
        "ok": True,
        "paper_id": paper_id,
        "reviewer_id": reviewer_id,
        "serial": current_serial,
        "correction_id": corr["id"],
        "score": body.score,
        "comment": comment,
        "receipt": receipt,
        "original_receipt": corr["original_receipt"],
        "corrected_at": now,
        "changed": True,
    }


# ---------------------------------------------------------------- 补位 (会务方)

@app.post("/assignment/backfill/dry-run")
def backfill_dry_run(_: bool = Depends(require_organizer)):
    """以当前发布版及资料修订号预演补位, 不改变发布版。

    已确认且未回避的关系固定, 其余位置在原有硬约束与优化次序下重算;
    无完整补位方案时返回未补齐论文及限制原因 (feasible=false)。
    响应中的 revision 与 serial 供补位发布复核。
    """
    with db.read_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无可补位版本")
        rev = db.get_revision(conn)
        result = _build_backfill(conn, published)
    result["revision"] = rev
    return result


@app.post("/assignment/backfill/publish")
def backfill_publish(body: BackfillPublishIn, _: bool = Depends(require_organizer)):
    """补位发布。必须同时复核资料修订号与发布序号, 任一过期则 409。

    成功后:
      - 当前发布版被新发布版替换 (发布序号 +1), 仅按新发布版授权取稿;
      - 仍在新方案中的 (论文, 评审人) 槽位保留其确认状态;
      - 无完整补位方案时 422, 当前发布版保持不变。
    """
    with db.write_txn() as conn:
        published = _load_published(conn)
        if published is None:
            raise HTTPException(status_code=404, detail="尚未发布任何分配方案, 无可补位版本")
        rev = db.get_revision(conn)
        serial = published["serial"]
        if body.base_revision != rev or body.base_serial != serial:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"补位预演已过期: 预演基于 修订号 {body.base_revision}/发布序号 {body.base_serial},"
                    f" 当前为 修订号 {rev}/发布序号 {serial},"
                    " 期间存在资料变更或新的发布, 请重新预演"
                ),
            )
        result = _build_backfill(conn, published)
        if not result["feasible"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "当前发布版无法补齐为完整方案, 补位发布拒绝",
                    "unassigned": result["unassigned"],
                    "diagnostics": result["diagnostics"],
                },
            )
        new_plan = result["plan"]
        old_decisions = _load_decisions(conn, serial)
        old_reviews = _load_reviews(conn, serial)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE published SET serial = serial + 1, revision = ?, plan = ?,"
            " explanations = ?, published_at = ? WHERE id = 1",
            (
                rev,
                json.dumps(new_plan, ensure_ascii=False),
                json.dumps(result["papers"], ensure_ascii=False),
                now,
            ),
        )
        new_serial = serial + 1
        # 未处理异议随发布序号变化在同一写事务内标记过期 (驳回不影响快照; 历史保留)
        db.expire_pending_objections(conn, new_serial, now)
        # 仍在新方案中的槽位保留确认状态 (已回避槽位不可能出现在新方案中);
        # 连续保留的确认槽位连同其正式评语一并沿用 (原收据/时间不变);
        # 被移出槽位的评语保留在旧序号下, 仅供会务方追溯, 不计入新方案进度
        carried = 0
        carried_reviews = 0
        for pid, pair in new_plan.items():
            for rid in pair:
                d = old_decisions.get((pid, rid))
                if d is not None and d["state"] == "confirmed":
                    conn.execute(
                        "INSERT INTO assignment_decisions"
                        "(serial, paper_id, reviewer_id, state, reason, decided_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (new_serial, pid, rid, "confirmed", None, d["decided_at"]),
                    )
                    carried += 1
                    r = old_reviews.get((pid, rid))
                    if r is not None:
                        conn.execute(
                            "INSERT INTO submitted_reviews"
                            "(serial, paper_id, reviewer_id, score, comment, receipt, submitted_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (
                                new_serial,
                                pid,
                                rid,
                                r["score"],
                                r["comment"],
                                r["receipt"],
                                r["submitted_at"],
                            ),
                        )
                        carried_reviews += 1
    return {
        "ok": True,
        "serial": new_serial,
        "revision": rev,
        "carried_confirmations": carried,
        "carried_reviews": carried_reviews,
        "plan": new_plan,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
