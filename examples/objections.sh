#!/usr/bin/env bash
# 本轮用法端到端示例: 作者对反馈快照评语的异议
#   发布 -> 两人确认并各交一份正式评语 -> 会务方发布反馈快照 ->
#   作者凭访问码对标号 1 的评语提交异议 (返回随机查询凭据) ->
#   相同理由重试幂等 / 不同理由冲突 -> 会务方查看异议及冻结的原反馈 ->
#   会务方受理 (非空更正原因, 原子核对后走既有更正请求流程, 原访问码立即失效) ->
#   评审人完成更正 -> 快照重发 -> 作者凭查询凭据查看处理状态 ->
#   另一标号异议被驳回 (不影响快照) -> 发布序号变化使未处理异议过期
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/objections.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文, 发布并交齐评语 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"
for H in "$RA $CA" "$RB $CB"; do
  set -- $H
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
done
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"

echo "== 2. 会务方发布反馈快照, 作者取得访问码 =="
SNAP=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE=$(echo "$SNAP" | j 'd["access_code"]')
echo "access_code=$CODE"
curl -s "$BASE/feedback-snapshots/$CODE"; echo

echo "== 3. 作者凭访问码对标号 1 的评语提交异议 (非空理由), 返回随机查询凭据 =="
OBJ=$(curl -s -X POST "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' \
  -d '{"label": 1, "reason": "评分与评语内容明显不符, 请求复核"}')
echo "$OBJ"
TOKEN=$(echo "$OBJ" | j 'd["query_token"]')
echo "-> 查询凭据: $TOKEN (快照码失效后仍可凭它查看处理状态)"
echo "-> 作者响应不含评审人编号/机构/评语收据, 仅论文编号、标号、理由与状态"

echo "== 3b. 相同理由重试 -> 幂等返回原记录原凭据 (changed=false); 不同理由 -> 409 =="
curl -s -X POST "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' \
  -d '{"label": 1, "reason": "评分与评语内容明显不符, 请求复核"}' \
  | j '(d["changed"], d["query_token"] == "'"$TOKEN"'")'
curl -s -o /dev/null -w '不同理由重试      -> HTTP %{http_code}\n' -X POST \
  "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' -d '{"label": 1, "reason": "换一个理由"}'
curl -s -o /dev/null -w '空白理由          -> HTTP %{http_code}\n' -X POST \
  "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' -d '{"label": 1, "reason": "   "}'
curl -s -o /dev/null -w '非法标号          -> HTTP %{http_code}\n' -X POST \
  "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' -d '{"label": 3, "reason": "x"}'
curl -s -o /dev/null -w '无效访问码        -> HTTP %{http_code}\n' -X POST \
  "$BASE/feedback-snapshots/fbk-nope/objections" \
  -H 'Content-Type: application/json' -d '{"label": 1, "reason": "x"}'

echo "== 4. 作者对另一标号也提交异议 (互不影响) =="
curl -s -X POST "$BASE/feedback-snapshots/$CODE/objections" \
  -H 'Content-Type: application/json' \
  -d '{"label": 2, "reason": "结论偏强的判断缺乏依据"}' | j '(d["changed"], d["objection"]["state"])'

echo "== 5. 会务方查看该论文全部异议及冻结的原反馈 =="
curl -s "$BASE/papers/P1/objections" -H "$ORG" | j '[(o["objection_id"], o["label"], o["state"], o["frozen_review"]) for o in d["objections"]]'
OB1=$(curl -s "$BASE/papers/P1/objections" -H "$ORG" | j '[o["objection_id"] for o in d["objections"] if o["label"] == 1][0]')
OB2=$(curl -s "$BASE/papers/P1/objections" -H "$ORG" | j '[o["objection_id"] for o in d["objections"] if o["label"] == 2][0]')

echo "== 6. 会务方受理标号 1 的异议: 非空更正原因, 原子核对后走既有更正请求流程 =="
ACC=$(curl -s -X POST "$BASE/papers/P1/objections/$OB1/resolve" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"decision": "accept", "reason": "评分与评语内容明显不符, 请更正"}')
echo "$ACC"
echo "-> 受理即令原访问码失效 (invalidated_snapshots=$(echo "$ACC" | j 'd["invalidated_snapshots"]')):"
curl -s -o /dev/null -w "   原访问码读取 -> HTTP %{http_code} (404)\n" "$BASE/feedback-snapshots/$CODE"
echo "-> 已按匿名标号定位评审人并生成待更正请求:"
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  | j '[(c["paper_id"], c["reason"], c["state"]) for c in d["corrections"]]'

echo "== 6b. 受理校验: 空更正原因 422; 已受理/已驳回异议再处理 409 =="
curl -s -o /dev/null -w '空原因受理        -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/objections/$OB2/resolve" -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"decision": "accept", "reason": "  "}'
curl -s -o /dev/null -w '已受理再处理      -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/objections/$OB1/resolve" -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"decision": "reject"}'

echo "== 7. 作者凭查询凭据查看处理状态 (accepted) =="
curl -s "$BASE/objections/$TOKEN"; echo

echo "== 8. 评审人完成更正, 会务方按现有规则重发快照 (新版本 + 新访问码) =="
RCT=$(curl -s "$BASE/papers/P1/reviews" -H "$ORG" | j 'd["corrections"][0]["original"]["receipt"]')
curl -s -o /dev/null -X POST "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"original_receipt\": \"$RCT\", \"score\": 5, \"comment\": \"更正: 选题重要, 方法扎实, 建议接收。\"}"
SNAP2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "新访问码: $CODE2 (version=$(echo "$SNAP2" | j 'd["version"]'))"

echo "== 9. 会务方驳回标号 2 的异议: 不影响快照 (新访问码仍可读) =="
curl -s -X POST "$BASE/papers/P1/objections/$OB2/resolve" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"decision": "reject", "reason": "评语符合评审规范"}' \
  | j '(d["decision"], d["objection"]["state"])'
curl -s -o /dev/null -w "驳回后新访问码读取 -> HTTP %{http_code} (200, 不受影响)\n" \
  "$BASE/feedback-snapshots/$CODE2"

echo "== 10. 作者对新快照的标号 2 提交异议, 随后发布序号变化使其过期 =="
OBJ3=$(curl -s -X POST "$BASE/feedback-snapshots/$CODE2/objections" \
  -H 'Content-Type: application/json' \
  -d '{"label": 2, "reason": "仍有疑问"}')
TOKEN3=$(echo "$OBJ3" | j 'd["query_token"]')
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
curl -s -o /dev/null -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}"
echo "发布序号已前进 -> 未处理异议标记过期 (superseded):"
curl -s "$BASE/objections/$TOKEN3" | j 'd["state"]'
echo "-> 已失效访问码不得提交新异议:"
curl -s -o /dev/null -w '   旧码提交异议 -> HTTP %{http_code} (404)\n' -X POST \
  "$BASE/feedback-snapshots/$CODE2/objections" \
  -H 'Content-Type: application/json' -d '{"label": 1, "reason": "x"}'

echo "== 11. 会务方追溯全部异议历史 (含已受理/已驳回/已过期) =="
curl -s "$BASE/papers/P1/objections" -H "$ORG" \
  | j '[(o["objection_id"], o["label"], o["state"], o["snapshot_version"]) for o in d["objections"]]'
echo "完成。"
