"""本轮功能测试: 作者对反馈快照评语的异议 (提交 -> 查询 -> 会务方驳回/受理)。

覆盖需求:
- 作者凭当前有效快照访问码对标号 1/2 的评语提交非空异议理由; 无效码/已失效码
  (更正请求失效、发布序号变化) 统一 404; 标号非法/理由空白 422;
- 同一快照同一标号仅留一条: 相同理由重试幂等返回原记录与原查询凭据 (changed=false),
  不同理由 409, 并发提交也不生成两条;
- 提交返回随机查询凭据, 快照码失效后仍可凭它查看处理状态;
  作者响应不暴露评审人编号、机构或评语收据;
- 会务方凭密钥查看异议及冻结的原反馈, 选择驳回 (不影响快照) 或受理
  (非空更正原因 + 原子核对发布序号与目标收据, 走既有更正请求流程, 原访问码立即失效);
  发布版或目标评语已变化、已有待更正请求时拒绝受理且不改异议状态;
- 未处理异议随发布序号变化标记过期 (superseded), 历史供会务方追溯。
"""
import os
import tempfile
import threading

_TMPDIR = tempfile.mkdtemp(prefix="review-objections-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}
BAD_ORG = {"X-Organizer-Key": "wrong-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots", "review_corrections", "assignment_locks",
            "feedback_objections",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution, capacity=3, topics=None, avoid=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution,
            "capacity": capacity,
            "avoid_papers": avoid or [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score, comment):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def setup_snapshot(client, pid="P1"):
    """建评审人/论文 -> 发布 -> 两人确认 -> 交齐评语 -> 发布快照。返回 (pub, snap, (rid_a, rid_b))。"""
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid, f"Inst-{rid}")
    add_paper(client, pid)
    pub = publish(client)
    serial = pub["serial"]
    rid_a, rid_b = pub["plan"][pid]
    decide(client, rid_a, pid, "confirm")
    decide(client, rid_b, pid, "confirm")
    submit_review(client, rid_a, pid, serial, 4, "甲的评语")
    submit_review(client, rid_b, pid, serial, 2, "乙的评语")
    r = client.post(f"/papers/{pid}/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r.status_code == 200, r.text
    return pub, r.json(), (rid_a, rid_b)


def submit_objection(client, code, label, reason):
    return client.post(
        f"/feedback-snapshots/{code}/objections",
        json={"label": label, "reason": reason},
    )


def resolve(client, pid, objection_id, decision, reason=None):
    body = {"decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/papers/{pid}/objections/{objection_id}/resolve", headers=ORG, json=body
    )


# ------------------------------------------------------------ 提交异议 (作者, 凭访问码)

def test_submit_objection_success_returns_query_token(client):
    _, snap, _ = setup_snapshot(client)
    r = submit_objection(client, snap["access_code"], 1, "评分与评语内容不符")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] is True
    assert j["query_token"].startswith("obj-")
    ob = j["objection"]
    assert ob["paper_id"] == "P1" and ob["label"] == 1
    assert ob["reason"] == "评分与评语内容不符"
    assert ob["state"] == "pending"
    assert ob["decision_reason"] is None and ob["resolved_at"] is None


def test_submit_objection_author_response_hides_internal_fields(client):
    pub, snap, (rid_a, rid_b) = setup_snapshot(client)
    r = submit_objection(client, snap["access_code"], 2, "  措辞过于严厉  ")
    assert r.status_code == 200
    assert r.json()["objection"]["reason"] == "措辞过于严厉"  # 首尾空白归一化
    raw = r.text
    for secret in (rid_a, rid_b, "Inst-", "rvw-", "receipt", "reviewer",
                   "institution", "snapshot_id", "correction"):
        assert secret not in raw, f"作者响应泄露内部字段: {secret}"
    # 查询凭据视图同样不泄露
    token = r.json()["query_token"]
    raw2 = client.get(f"/objections/{token}").text
    for secret in (rid_a, rid_b, "Inst-", "rvw-", "receipt", "reviewer"):
        assert secret not in raw2, f"查询视图泄露内部字段: {secret}"


def test_submit_objection_invalid_or_expired_code_uniform_404(client):
    pub, snap, (rid_a, _) = setup_snapshot(client)
    code = snap["access_code"]
    serial = pub["serial"]
    # 无效码 -> 404 (与快照读取响应体一致, 不透露论文是否存在)
    r = submit_objection(client, "fbk-nope", 1, "x")
    assert r.status_code == 404
    assert r.json() == client.get("/feedback-snapshots/fbk-nope").json()
    # 快照被更正请求失效后 -> 已失效访问码不得提交新异议
    client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": rid_a,
              "reason": "请更正"},
    )
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    r = submit_objection(client, code, 1, "异议")
    assert r.status_code == 404
    assert r.json() == client.get("/feedback-snapshots/fbk-nope").json()


def test_submit_objection_after_serial_change_is_404(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    backfill_publish(client)  # 发布序号前进 -> 旧码失效
    assert submit_objection(client, code, 1, "异议").status_code == 404


def test_submit_objection_validation_errors(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    # 标号非法 -> 422
    assert submit_objection(client, code, 0, "x").status_code == 422
    assert submit_objection(client, code, 3, "x").status_code == 422
    assert submit_objection(client, code, "1", "x").status_code == 422
    # 理由缺失/空白 -> 422
    assert client.post(
        f"/feedback-snapshots/{code}/objections", json={"label": 1}
    ).status_code == 422
    assert submit_objection(client, code, 1, "   ").status_code == 422
    # 被拒请求不落库
    assert client.get("/papers/P1/objections", headers=ORG).json()["objections"] == []


def test_same_label_same_reason_idempotent_different_conflicts(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    first = submit_objection(client, code, 1, "评分偏低").json()
    assert first["changed"] is True
    # 相同理由重试 (含首尾空白归一化) -> 原记录原凭据
    again = submit_objection(client, code, 1, " 评分偏低 ")
    assert again.status_code == 200
    j = again.json()
    assert j["changed"] is False
    assert j["query_token"] == first["query_token"]
    assert j["objection"]["created_at"] == first["objection"]["created_at"]
    # 不同理由 -> 409
    assert submit_objection(client, code, 1, "另一个理由").status_code == 409
    # 仍只有一条记录
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert len(obs) == 1
    # 另一标号不受影响
    other = submit_objection(client, code, 2, "对第二份评语有异议")
    assert other.status_code == 200 and other.json()["changed"] is True
    assert other.json()["query_token"] != first["query_token"]


def test_concurrent_submissions_never_create_two_records(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    results = []

    def worker(reason):
        results.append(submit_objection(client, code, 1, reason).status_code)

    threads = [threading.Thread(target=worker, args=(f"理由-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 并发提交不同理由: 恰好一条成功, 其余全部冲突
    assert sorted(results) == [200] + [409] * 7
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert len(obs) == 1
    # 并发提交相同理由: 全部幂等返回同一凭据
    tokens, results2 = set(), []

    def worker2():
        r = submit_objection(client, code, 2, "共同理由")
        results2.append(r.status_code)
        tokens.add(r.json()["query_token"])

    threads = [threading.Thread(target=worker2) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results2 == [200] * 8 and len(tokens) == 1
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert len(obs) == 2


# ------------------------------------------------------------ 查询凭据 (作者)

def test_query_token_survives_snapshot_invalidation(client):
    pub, snap, (rid_a, _) = setup_snapshot(client)
    token = submit_objection(client, snap["access_code"], 1, "异议").json()["query_token"]
    # 快照码因更正请求失效后, 凭据仍可查询处理状态
    client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": pub["serial"], "reviewer_id": rid_a,
              "reason": "请更正"},
    )
    assert client.get(f"/feedback-snapshots/{snap['access_code']}").status_code == 404
    r = client.get(f"/objections/{token}")
    assert r.status_code == 200
    assert r.json()["state"] == "pending"
    # 无效凭据 -> 404
    assert client.get("/objections/obj-nope").status_code == 404
    assert client.get("/objections/whatever").status_code == 404


# ------------------------------------------------------------ 会务方查看异议

def test_organizer_lists_objections_with_frozen_feedback(client):
    pub, snap, (rid_a, rid_b) = setup_snapshot(client)
    token = submit_objection(client, snap["access_code"], 1, "评分与内容不符").json()[
        "query_token"
    ]
    r = client.get("/papers/P1/objections", headers=ORG)
    assert r.status_code == 200
    obs = r.json()["objections"]
    assert len(obs) == 1
    ob = obs[0]
    assert ob["state"] == "pending" and ob["label"] == 1
    assert ob["serial"] == pub["serial"] and ob["snapshot_version"] == 1
    assert ob["query_token"] == token
    # 冻结的原反馈: 标号 1 对应甲的评语 (槽位字典序第一位)
    assert ob["frozen_review"] == {"score": 4, "comment": "甲的评语"}
    assert ob["receipt"].startswith("rvw-")
    # 会务方可凭收据对照当前槽位评语
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert ob["receipt"] in {s["review"]["receipt"] for s in view["slots"]}


def test_organizer_endpoints_require_key(client):
    _, snap, _ = setup_snapshot(client)
    submit_objection(client, snap["access_code"], 1, "异议")
    assert client.get("/papers/P1/objections").status_code == 401
    assert client.get("/papers/P1/objections", headers=BAD_ORG).status_code == 401
    body = {"decision": "reject"}
    assert client.post("/papers/P1/objections/1/resolve", json=body).status_code == 401
    assert client.post(
        "/papers/P1/objections/1/resolve", headers=BAD_ORG, json=body
    ).status_code == 401


def test_list_objections_unknown_paper_is_empty(client):
    r = client.get("/papers/NOPE/objections", headers=ORG)
    assert r.status_code == 200 and r.json()["objections"] == []


# ------------------------------------------------------------ 驳回

def test_reject_does_not_affect_snapshot(client):
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    token = submit_objection(client, code, 1, "异议理由").json()["query_token"]
    ob_id = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0][
        "objection_id"
    ]
    r = resolve(client, "P1", ob_id, "reject")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["decision"] == "rejected"
    assert j["objection"]["state"] == "rejected"
    assert j["objection"]["resolved_at"] is not None
    # 驳回不影响快照: 访问码仍可读
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200
    # 作者凭据可见处理结果
    assert client.get(f"/objections/{token}").json()["state"] == "rejected"
    # 已处理异议再次处理 -> 409
    assert resolve(client, "P1", ob_id, "reject").status_code == 409
    assert resolve(client, "P1", ob_id, "accept", "更正原因").status_code == 409


def test_reject_with_optional_reason_visible_to_author(client):
    _, snap, _ = setup_snapshot(client)
    token = submit_objection(client, snap["access_code"], 2, "异议").json()["query_token"]
    ob_id = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0][
        "objection_id"
    ]
    r = resolve(client, "P1", ob_id, "reject", "评语符合评审规范")
    assert r.status_code == 200
    assert r.json()["objection"]["decision_reason"] == "评语符合评审规范"
    author_view = client.get(f"/objections/{token}").json()
    assert author_view["state"] == "rejected"
    assert author_view["decision_reason"] == "评语符合评审规范"


# ------------------------------------------------------------ 受理

def test_accept_creates_correction_and_invalidates_code(client):
    pub, snap, (rid_a, rid_b) = setup_snapshot(client)
    code = snap["access_code"]
    serial = pub["serial"]
    token = submit_objection(client, code, 1, "评分与评语明显不符").json()["query_token"]
    ob = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    rev_before = client.get("/meta", headers=ORG).json()["revision"]

    r = resolve(client, "P1", ob["objection_id"], "accept", "请更正评分与评语的一致性")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["decision"] == "accepted"
    assert j["objection"]["state"] == "accepted"
    assert j["objection"]["correction_id"] == j["correction"]["correction_id"]
    assert j["invalidated_snapshots"] == 1
    # 按匿名标号定位评审人: 标号 1 -> 槽位字典序第一位 (rid_a)
    corr = j["correction"]
    assert corr["reviewer_id"] == rid_a
    assert corr["state"] == "pending" and corr["serial"] == serial
    assert corr["reason"] == "请更正评分与评语的一致性"
    assert corr["original"]["comment"] == "甲的评语"
    assert corr["original"]["receipt"] == ob["receipt"]
    # 原访问码立即失效
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    # 受理不推进资料修订号
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before
    # 评审人可见自己的待更正任务, 并走完既有更正流程
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_a)).json()
    assert len(tasks["corrections"]) == 1
    assert tasks["corrections"][0]["reason"] == "请更正评分与评语的一致性"
    r2 = client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": serial,
              "original_receipt": ob["receipt"], "score": 5, "comment": "更正后的甲评语"},
    )
    assert r2.status_code == 200, r2.text
    # 更正完成后会务方按现有规则重新发布快照
    r3 = client.post("/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial})
    assert r3.status_code == 200 and r3.json()["version"] == 2
    # 作者凭据可见受理结果
    author_view = client.get(f"/objections/{token}").json()
    assert author_view["state"] == "accepted"
    assert author_view["decision_reason"] == "请更正评分与评语的一致性"
    # 已受理异议再次处理 -> 409
    assert resolve(client, "P1", ob["objection_id"], "reject").status_code == 409


def test_accept_requires_non_empty_reason(client):
    _, snap, _ = setup_snapshot(client)
    submit_objection(client, snap["access_code"], 1, "异议")
    ob_id = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0][
        "objection_id"
    ]
    assert resolve(client, "P1", ob_id, "accept").status_code == 422
    assert resolve(client, "P1", ob_id, "accept", "   ").status_code == 422
    # 被拒后异议仍为待处理
    ob = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert ob["state"] == "pending"


def test_resolve_unknown_objection_is_404(client):
    _, snap, _ = setup_snapshot(client)
    submit_objection(client, snap["access_code"], 1, "异议")
    assert resolve(client, "P1", 999, "reject").status_code == 404
    assert resolve(client, "P2", 1, "reject").status_code == 404


def test_accept_rejected_when_serial_changed_and_objection_superseded(client):
    _, snap, _ = setup_snapshot(client)
    token = submit_objection(client, snap["access_code"], 1, "异议").json()["query_token"]
    ob_id = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0][
        "objection_id"
    ]
    # 发布序号变化 -> 未处理异议标记过期, 历史保留
    out = backfill_publish(client)
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert len(obs) == 1 and obs[0]["state"] == "superseded"
    assert obs[0]["resolved_at"] is not None
    # 作者凭据可见过期状态
    assert client.get(f"/objections/{token}").json()["state"] == "superseded"
    # 已过期异议不可再受理/驳回
    assert resolve(client, "P1", ob_id, "accept", "原因").status_code == 409
    assert resolve(client, "P1", ob_id, "reject").status_code == 409
    assert out["serial"] == snap["serial"] + 1


def test_accept_rejected_when_target_review_changed(client):
    pub, snap, (rid_a, _) = setup_snapshot(client)
    serial = pub["serial"]
    submit_objection(client, snap["access_code"], 1, "对甲的评语有异议")
    ob = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    # 目标评语先经既有更正流程更正 -> 收据变化
    client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": rid_a, "reason": "更正"},
    )
    client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers(rid_a),
        json={"paper_id": "P1", "serial": serial,
              "original_receipt": ob["receipt"], "score": 5, "comment": "更正后的评语"},
    )
    # 受理 -> 409 (目标评语已变化), 异议状态不变
    r = resolve(client, "P1", ob["objection_id"], "accept", "受理原因")
    assert r.status_code == 409
    ob_after = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert ob_after["state"] == "pending"
    assert ob_after["correction_id"] is None


def test_accept_rejected_when_pending_correction_exists(client):
    pub, snap, (rid_a, _) = setup_snapshot(client)
    serial = pub["serial"]
    submit_objection(client, snap["access_code"], 1, "异议")
    ob = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    # 该槽位已有待完成更正请求
    client.post(
        "/papers/P1/review-corrections",
        headers=ORG,
        json={"paper_id": "P1", "serial": serial, "reviewer_id": rid_a, "reason": "请更正"},
    )
    r = resolve(client, "P1", ob["objection_id"], "accept", "受理原因")
    assert r.status_code == 409
    ob_after = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert ob_after["state"] == "pending"
    # 会务方视图中仍只有会务方直接发起的那一条更正请求
    view = client.get("/papers/P1/reviews", headers=ORG).json()
    assert len(view["corrections"]) == 1


def test_accept_rejected_when_slot_no_longer_confirmed(client):
    pub, snap, (rid_a, _) = setup_snapshot(client)
    submit_objection(client, snap["access_code"], 1, "异议")
    ob = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    # 评审人随后声明回避 (槽位不再是已确认状态)
    decide(client, rid_a, "P1", "recuse", reason="利益冲突")
    r = resolve(client, "P1", ob["objection_id"], "accept", "受理原因")
    assert r.status_code == 409
    ob_after = client.get("/papers/P1/objections", headers=ORG).json()["objections"][0]
    assert ob_after["state"] == "pending"


def test_two_labels_objections_independent_resolution(client):
    """同一快照两个标号的异议互不影响: 一个驳回, 另一个仍可受理。"""
    pub, snap, (rid_a, rid_b) = setup_snapshot(client)
    code = snap["access_code"]
    submit_objection(client, code, 1, "对评语一有异议")
    submit_objection(client, code, 2, "对评语二有异议")
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    ob1, ob2 = obs
    assert ob1["label"] == 1 and ob2["label"] == 2
    # 驳回标号 1
    assert resolve(client, "P1", ob1["objection_id"], "reject").status_code == 200
    # 快照不受驳回影响, 仍有效
    assert client.get(f"/feedback-snapshots/{code}").status_code == 200
    # 受理标号 2 -> 定位到第二位评审人, 访问码失效
    r = resolve(client, "P1", ob2["objection_id"], "accept", "请更正结论措辞")
    assert r.status_code == 200
    assert r.json()["correction"]["reviewer_id"] == rid_b
    assert r.json()["invalidated_snapshots"] == 1
    assert client.get(f"/feedback-snapshots/{code}").status_code == 404
    tasks = client.get("/reviewer/review-corrections", headers=reviewer_headers(rid_b)).json()
    assert len(tasks["corrections"]) == 1
    tasks_a = client.get(
        "/reviewer/review-corrections", headers=reviewer_headers(rid_a)
    ).json()
    assert tasks_a["corrections"] == []


def test_objection_states_tracked_in_organizer_history(client):
    """会务方可追溯全部异议 (含 superseded/rejected/accepted 历史)。"""
    _, snap, _ = setup_snapshot(client)
    code = snap["access_code"]
    submit_objection(client, code, 1, "异议一")
    submit_objection(client, code, 2, "异议二")
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    resolve(client, "P1", obs[0]["objection_id"], "reject")
    resolve(client, "P1", obs[1]["objection_id"], "accept", "请更正")
    # 新快照版本发布后, 作者对新码提交新异议; 随后序号变化使其过期
    serial = client.get("/meta", headers=ORG).json()["published"]["serial"]
    # 受理已令旧码失效; 更正完成后重发快照
    rid_b = client.get("/papers/P1/reviews", headers=ORG).json()["corrections"][0][
        "reviewer_id"
    ]
    receipt = client.get("/papers/P1/reviews", headers=ORG).json()["corrections"][0][
        "original"
    ]["receipt"]
    client.post(
        "/reviewer/review-corrections/P1",
        headers=reviewer_headers(rid_b),
        json={"paper_id": "P1", "serial": serial,
              "original_receipt": receipt, "score": 4, "comment": "更正后的乙评语"},
    )
    snap2 = client.post(
        "/papers/P1/feedback-snapshot", headers=ORG, json={"serial": serial}
    ).json()
    submit_objection(client, snap2["access_code"], 1, "新一轮异议")
    backfill_publish(client)
    obs = client.get("/papers/P1/objections", headers=ORG).json()["objections"]
    assert [o["state"] for o in obs] == ["rejected", "accepted", "superseded"]
    assert [o["snapshot_version"] for o in obs] == [1, 1, 2]
