"""SQLite 存储层。

- 单连接 + 进程内可重入锁, 写操作使用 BEGIN IMMEDIATE 串行化;
- meta 表维护资料修订号 (revision), 任何论文/评审人资料变更都会使其 +1;
- published 表保存当前已发布方案、发布序号 (serial, 每次发布/补位发布 +1)
  及其所依据的修订号;
- assignment_decisions 表保存评审人对当前发布版分配关系的确认/回避决定
  (按发布序号隔离);
- submitted_reviews 表保存评审人对"当前发布版中已确认且未回避"槽位的正式评分
  与评语 (按发布序号隔离; 每个有效槽位至多一份, 以收据 receipt 标识;
  补位发布时连续保留的确认槽位连同评语一起复制到新序号);
- reviewer_recusals 表保存评审人声明过的硬回避, 后续普通求解与补位都必须遵守;
- reviewers.active 标记评审资格 (1=启用, 既有评审人初始即启用; 0=停用),
  reviewer_status_changes 表保存会务方停用/启用的变更记录 (仅有效变更写入;
  匹配修订号下重复提交同一状态和原因为幂等请求, 不推进修订号、不写记录);
- feedback_snapshots 表保存会务方按论文发布的、面向作者的匿名反馈快照
  (仅当某发布序号下两名评审人均已确认且各提交一份正式评语时生成;
  以 (paper_id, serial, receipt1, receipt2) 幂等, 来源变化即新发布序号 -> version+1
  与新随机访问码, 旧码立即失效, 旧版行保留仅供会务方追溯);
- institution_merges 表保存会务方提交的机构归并边 (同一机构的不同原名);
  全表边以并查集解释为冲突判定组, 关系传递且不可拆分, 仅有效归并写入并推进修订号,
  普通分配与补位均按归并组判定"作者同机构"与"两名评审人同机构";
- review_corrections 表保存会务方针对已交评语发起的更正请求 (按发布序号 serial 隔离;
  发起时冻结原评语内容/收据供会务方追溯, 并令该稿当前序号的反馈快照访问码立即失效);
  评审人提交的首份有效更正生成新收据并更新该槽位评语 (state: pending -> completed),
  相同内容重试幂等返回更正收据, 内容不同冲突; 发布序号变化后待更正请求自然失效;
- assignment_locks 表保存会务方在普通分配前提交的锁定表 (每篇 0~2 名评审人;
  整表替换, 空表清除全部锁定); 锁定仅约束普通分配, 不改变补位规则;
  被锁评审人失格时锁定行不级联, 由预演/发布诊断冲突并拒绝发布, 不悄悄释放,
  论文删除时其锁定行随论文级联清除;
- feedback_objections 表保存作者凭有效快照访问码对标号 1/2 评语提交的异议
  (同一快照同一标号仅一条, 以 UNIQUE(snapshot_id, label) 保证并发不生成两条;
  相同理由重试幂等返回原记录与原查询凭据, 不同理由冲突; 提交时冻结该标号对应的
  评语收据供受理时定位评审人, 作者视图绝不暴露评审人编号/机构/收据;
  提交返回随机查询凭据 query_token, 快照码失效后仍可凭其查询处理状态;
  会务方驳回不影响快照, 受理须填写非空更正原因并原子核对发布序号与目标收据后
  走既有更正请求流程; 未处理异议随发布序号变化标记 superseded, 历史保留供追溯);
- 发布 = 在同一个写事务内 "校验修订号 -> 重算 -> 写入",
  因此与资料修改并发时不会产生基于旧版资料的方案。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "data", "app.db"))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     TEXT PRIMARY KEY,
    manuscript   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institutions TEXT NOT NULL    -- JSON array
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id  TEXT PRIMARY KEY,
    credential   TEXT NOT NULL,
    topics       TEXT NOT NULL,   -- JSON array
    institution  TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 1),
    avoid_papers TEXT NOT NULL,   -- JSON array
    active       INTEGER NOT NULL DEFAULT 1  -- 评审资格: 1=启用(既有评审人初始即启用), 0=停用
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    serial       INTEGER NOT NULL DEFAULT 0,
    revision     INTEGER NOT NULL,
    plan         TEXT NOT NULL,   -- JSON: {paper_id: [reviewer_id, reviewer_id]}
    explanations TEXT NOT NULL,   -- JSON: 每篇论文的分配与排除原因
    published_at TEXT NOT NULL
);
-- 评审人对当前发布版中分配关系的决定 (按发布序号 serial 隔离; 无行 = pending)
CREATE TABLE IF NOT EXISTS assignment_decisions (
    serial      INTEGER NOT NULL,
    paper_id    TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('confirmed', 'recused')),
    reason      TEXT,            -- 回避原因 (recused 时非空)
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人对"已确认且未回避"槽位提交的正式评分与评语 (按发布序号 serial 隔离;
-- 每个有效任务槽位至多一份; receipt 为首次提交时发放的收据, 相同内容重试原样返回。
-- receipt 不设全局唯一: 补位发布连续保留的确认槽位沿用同一份评语与同一收据,
-- 旧序号行保留供会务方追溯, 因此同一收据可随不同 serial 各存一行)
CREATE TABLE IF NOT EXISTS submitted_reviews (
    serial       INTEGER NOT NULL,
    paper_id     TEXT NOT NULL,
    reviewer_id  TEXT NOT NULL,
    score        INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment      TEXT NOT NULL,
    receipt      TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (serial, paper_id, reviewer_id)
);
-- 评审人一旦对某篇论文声明回避, 即成为后续所有分配 (含补位) 的硬回避
CREATE TABLE IF NOT EXISTS reviewer_recusals (
    reviewer_id      TEXT NOT NULL,
    paper_id         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    created_serial   INTEGER NOT NULL,
    created_revision INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, paper_id)
);
-- 评审资格 (启用/停用) 变更记录, 仅供会务方追溯。仅有效变更写入;
-- 匹配修订号下重复提交同一状态和原因属于幂等请求, 不产生新记录。
CREATE TABLE IF NOT EXISTS reviewer_status_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id   TEXT NOT NULL,
    active        INTEGER NOT NULL,           -- 变更后的目标状态: 1=启用, 0=停用
    reason        TEXT NOT NULL,              -- 会务方填写的非空原因
    revision      INTEGER NOT NULL,           -- 本次有效变更推进后的资料修订号
    changed_at    TEXT NOT NULL
);
-- 面向作者的匿名反馈快照 (会务方按论文发布; 仅当该发布序号下两名评审人均已确认
-- 且各提交一份正式评语时才允许生成)。
-- receipt1/receipt2 按方案槽位固定顺序 (评审人编号字典序) 记录两份评语的收据,
-- 与 (paper_id, serial) 共同构成幂等键: 相同发布序号及两份评语收据的重复请求
-- 返回原快照与原码; 来源 (发布序号) 变化即生成新版本 (version + 1) 与新随机码,
-- 旧版行保留仅供会务方追溯。快照仅冻结论文编号、两份评分与评语, 不保存评审人编号
-- /机构等内部信息; access_code 全局唯一, 仅最新且序号仍为当前发布序号的版本可读。
CREATE TABLE IF NOT EXISTS feedback_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    serial       INTEGER NOT NULL,
    receipt1     TEXT NOT NULL,
    receipt2     TEXT NOT NULL,
    access_code  TEXT NOT NULL UNIQUE,
    review1      TEXT NOT NULL,   -- 冻结 JSON: {"score":..,"comment":..}
    review2      TEXT NOT NULL,   -- 冻结 JSON: {"score":..,"comment":..}
    created_at   TEXT NOT NULL,
    invalidated  INTEGER NOT NULL DEFAULT 0,  -- 1=更正请求发起后访问码立即失效
    UNIQUE (paper_id, serial, receipt1, receipt2)
);
-- 会务方提交的机构归并边 (同一机构的不同原名)。归并关系传递且不可拆分:
-- 全表边以并查集解释为冲突判定组, 任何提交只能增加边 (使组变大),
-- 不存在"拆组/移出"操作; 同组重复提交幂等, 不写边、不推进修订号。
-- 仅有效归并 (使两个原先不同的组合并) 写入一行并推进资料修订号。
CREATE TABLE IF NOT EXISTS institution_merges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name_a       TEXT NOT NULL,   -- 请求中的机构原名 (首尾空白归一化后的原样名称)
    name_b       TEXT NOT NULL,
    revision     INTEGER NOT NULL,  -- 本次有效归并推进后的资料修订号
    merged_at    TEXT NOT NULL
);
-- 普通分配锁定表 (会务方在普通分配前提交, 仅约束普通分配, 不影响补位规则)。
-- 每篇论文至多一行, reviewers_json 为 0~2 个评审人编号的 JSON 数组
-- (空数组行不写入, 缺省 = 不锁定; 整表替换语义下空表 = 清除全部锁定)。
-- 锁定仅为普通求解器的固定槽位输入: 被锁评审人随后被删除/停用/回避或因
-- 资料及机构归并而失格时锁定行不自动级联, 由预演/发布诊断具体冲突
-- (lock_reviewer_not_found 等) 并拒绝发布, 绝不悄悄释放锁定;
-- 论文删除时其锁定行随论文级联清除 (论文本身已不参与分配)。
CREATE TABLE IF NOT EXISTS assignment_locks (
    paper_id       TEXT PRIMARY KEY,
    reviewers_json TEXT NOT NULL,   -- JSON array: 1~2 个评审人编号 (字典序)
    updated_at     TEXT NOT NULL
);
-- 会务方针对已交评语发起的更正请求 (按发布序号 serial 隔离)。
-- 发起时冻结原评语 (评分/评语/收据/提交时间) 供会务方追溯; 评审人凭
-- "请求对应的发布序号 + 原评语收据" 提交更正, 首份有效更正生成新收据并
-- 直接更新该槽位的正式评语 (state 置 completed, 记录更正后内容/新收据/时间),
-- 相同内容重试幂等返回更正收据, 内容不同按冲突拒绝;
-- 发布序号变化后, 旧序号下的待更正请求自然失效 (不再列出也不可提交)。
CREATE TABLE IF NOT EXISTS review_corrections (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    serial               INTEGER NOT NULL,   -- 更正请求针对的发布序号
    paper_id             TEXT NOT NULL,
    reviewer_id          TEXT NOT NULL,
    reason               TEXT NOT NULL,      -- 会务方填写的非空更正原因
    original_score       INTEGER NOT NULL,   -- 被更正评语的冻结内容 (供会务方追溯)
    original_comment     TEXT NOT NULL,
    original_receipt     TEXT NOT NULL,      -- 被更正评语的收据
    original_submitted_at TEXT NOT NULL,
    state                TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    new_score            INTEGER,            -- 更正后内容 (completed 时非空)
    new_comment          TEXT,
    new_receipt          TEXT,               -- 更正评语的新收据
    requested_at         TEXT NOT NULL,
    corrected_at         TEXT
);
-- 作者凭当前有效快照访问码对标号 1/2 评语提交的异议。
-- 同一快照同一标号仅留一条 (UNIQUE(snapshot_id, label), 并发提交也不会生成两条):
-- 相同理由 (首尾空白归一化后一致) 的重试幂等返回原记录与原查询凭据,
-- 不同理由按冲突拒绝; 提交仅在快照当前有效 (序号仍为当前发布序号且未被失效) 时接受,
-- 已失效访问码提交一律按无效码 404 处理。
-- receipt 冻结提交时该标号对应的评语收据, 仅供会务方追溯与受理时按匿名标号
-- 定位评审人 (经当前序号的评语行反查), 绝不出现在作者视图中;
-- query_token 为提交时发放的随机查询凭据, 快照码失效后仍可凭它查询处理状态;
-- state: pending(待处理) / rejected(已驳回, 不影响快照) / accepted(已受理,
-- 已按既有更正请求流程生成待更正请求并令原访问码失效) / superseded
-- (未处理异议随发布序号变化标记过期, 历史保留供会务方追溯)。
CREATE TABLE IF NOT EXISTS feedback_objections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        TEXT NOT NULL,
    serial          INTEGER NOT NULL,   -- 提交时快照所依附的发布序号
    snapshot_id     INTEGER NOT NULL,   -- 对应 feedback_snapshots.id (冻结来源版本)
    label           INTEGER NOT NULL CHECK (label IN (1, 2)),
    reason          TEXT NOT NULL,      -- 非空异议理由 (首尾空白归一化后)
    receipt         TEXT NOT NULL,      -- 该标号对应的评语收据 (仅会务方可见)
    query_token     TEXT NOT NULL UNIQUE,  -- 随机查询凭据 (obj- 前缀)
    state           TEXT NOT NULL CHECK (state IN ('pending', 'rejected', 'accepted', 'superseded')),
    decision_reason TEXT,               -- 会务方处理原因 (受理=非空更正原因, 驳回可选)
    correction_id   INTEGER,            -- 受理时关联的 review_corrections.id
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    UNIQUE (snapshot_id, label)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版数据库补齐新增列 (SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(published)").fetchall()}
    if "serial" not in cols:
        conn.execute("ALTER TABLE published ADD COLUMN serial INTEGER NOT NULL DEFAULT 0")
    reviewer_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviewers)").fetchall()}
    if "active" not in reviewer_cols:
        # 既有评审人初始视为启用
        conn.execute("ALTER TABLE reviewers ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    snapshot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback_snapshots)").fetchall()}
    if "invalidated" not in snapshot_cols:
        # 既有快照默认未被更正请求失效
        conn.execute(
            "ALTER TABLE feedback_snapshots ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0"
        )


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0')")
    return _conn


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"])


def bump_revision(conn: sqlite3.Connection) -> int:
    rev = get_revision(conn) + 1
    conn.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(rev),))
    return rev


# ------------------------------------------------------------ 机构归并组 (并查集)

def institution_merge_edges(conn: sqlite3.Connection):
    """全部机构归并边, 按提交顺序 (id) 返回 [(name_a, name_b), ...]。"""
    return [
        (r["name_a"], r["name_b"])
        for r in conn.execute(
            "SELECT name_a, name_b FROM institution_merges ORDER BY id"
        ).fetchall()
    ]


def institution_groups(conn: sqlite3.Connection, known=None):
    """按并查集解释归并边, 返回 {机构原名: 归并组键}。

    归并关系传递且不可拆分: 全表边构成无向图, 同一连通分量内的所有名称
    映射到同一组键 (组内按名称排序后的首个名称, 随归并扩组可能变化,
    仅用于冲突判定的内部键)。

    known: 可选的当前资料机构原名集合; 给出时结果只包含这些名称
    (已被删除的资料名称仍参与并查集, 不影响其余名称的归组),
    未归并名称映射到自身。
    """
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # 确定性地以较小名称为根, 组键不依赖边的插入方向
            small, large = (ra, rb) if ra < rb else (rb, ra)
            parent[large] = small

    for a, b in institution_merge_edges(conn):
        union(a, b)

    names = set(parent)
    if known is not None:
        names |= set(known)
    return {name: find(name) for name in names}


def current_institution_names(conn: sqlite3.Connection):
    """当前论文作者或评审人资料中出现过的非空机构原名集合 (原样, 不做空白归一化)。"""
    names = set()
    for r in conn.execute("SELECT institution FROM reviewers").fetchall():
        if r["institution"]:
            names.add(r["institution"])
    for r in conn.execute("SELECT institutions FROM papers").fetchall():
        for name in json.loads(r["institutions"]):
            if name:
                names.add(name)
    return names


def load_assignment_locks(conn: sqlite3.Connection):
    """读取普通分配锁定表, 返回 {paper_id: [reviewer_id, ...]} (按论文编号排序)。"""
    return {
        r["paper_id"]: json.loads(r["reviewers_json"])
        for r in conn.execute(
            "SELECT paper_id, reviewers_json FROM assignment_locks ORDER BY paper_id"
        ).fetchall()
    }


@contextmanager
def read_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def write_txn():
    with _lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def reset_for_tests() -> None:
    """关闭并丢弃当前连接 (仅测试使用, 配合重新设置 DB_PATH)。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
