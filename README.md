# 双盲论文评审分配服务

基于 **Python + FastAPI + SQLite** 的双盲评审分配服务。会务方凭本地密钥维护论文与评审人资料，
求解器在硬约束下自动计算全局最优分配方案，支持预演、版本化发布与评审人匿名稿分发。

## 功能与规则

**分配硬约束**（每篇论文恰好 2 名评审人）：

1. 两名评审人必须来自**不同机构**；
2. 两人中**至少一人擅长该论文主题**（擅长主题与论文主题交集非空；论文未标注主题时视为均满足）；
3. 评审人**不得与任一作者同机构**；
4. 评审人**不得评审自己声明回避的论文**；
5. 评审人已分配论文数**不得超过其容量**。

> 约束 1、3 中的"机构"一律按会务方维护的**机构归并组**判定：同一机构的不同原名归并后
> 即视为同一机构（原名逐字相同与由归并造成的同组在预演诊断中分别标注，详见下文
> "机构归并组"）。

**优化目标**（多解时按优先级）：

1. **最小化最高已用容量比例**（全体评审人中 `已分配数 / 容量` 的最大值）；
2. 在此前提下，按 **(论文编号, 评审人编号) 展开的全局序列取字典序最小**的方案。

**无完整方案时**：返回最大部分分配、未分配论文清单及每篇的限制原因（合格评审人不足、
容量占满、无擅长者、机构冲突等），**已发布版本保持不变**。

**普通分配评审人锁定表（会务方）**：会务方可在普通分配前对
`POST /assignment/locks` 整表提交锁定，请求体为
`{"base_revision": N, "locks": {"P1": ["R1", "R2"], "P2": ["R3"]}}`：

- **整表替换**：每篇论文可锁定 **0～2 名评审人**；未列出的论文视为不锁定；
  **空表（`{}`）表示清除全部锁定**；每篇内部按评审人编号字典序归一化（提交顺序无关）；
- 请求在**同一个写事务内先核对修订号**：过期/超前 `409`，拒绝且不改表；
  **未知论文或未知评审人 `404`**（整表拒绝、不留部分变更）；同篇重复锁定或超过两名 `422`，
  论文/评审人编号去首尾空白后为空白 `422`；
- **相同锁定表重试幂等**（规范化后一致即 `changed=false`，不推进修订号、不写表）；
  **有效变更推进资料修订号 +1**；响应返回**当前修订号与是否变更**、锁定论文/槽位计数；
  `GET /assignment/locks` 可随时查看当前锁定表；
- **普通预演与普通发布必须保留锁定槽位**，其余位置在机构冲突、主题专长、回避、资格、
  容量等**全部既有硬约束**与**既有优化次序**下重算；**单人锁定时专长要求可由另一人满足**
  （搭档必须擅长且与锁定者不同机构）；
- **锁定人随后被删除、停用、回避，或因资料变更/机构归并而失格，或锁定累计超过其容量时，
  绝不悄悄释放锁定**：普通预演对该论文给出具体冲突（`lock_*` 原因代码）并标记为
  **不可完整分配**（`feasible=false`）；普通发布 **422 拒绝且不改变当前发布版**；
  会务方须显式调整/清除锁定后才能再次发布；
- **锁定仅约束普通分配**：补位预演/发布不读取锁定表，仍只固定当前发布版中已确认且未回避的
  槽位，补位的确认槽位规则完全不变；
- 论文删除时其锁定行随论文级联清除（论文已不参与分配）；评审人删除不级联——锁定保留并在
  下次预演/发布时以 `lock_reviewer_not_found` 暴露，由会务方显式处理。

**版本化发布**：每次资料变更（论文/评审人增删改）使资料修订号 +1；预演返回所依据的修订号，
发布必须携带该修订号，不一致则 `409` 拒绝。校验修订号、重算方案、写入发布版在**同一个
SQLite 写事务**（`BEGIN IMMEDIATE`）内完成，与资料修改并发不会产生基于旧版资料的方案。

每次发布（含补位发布）还会使**发布序号 `serial`** +1，用于标识槽位决定所依附的发布版。

**评审人确认/回避（发布分配后）**：评审人凭现有本人凭据（`X-Reviewer-Id` +
`X-Reviewer-Credential`）对**当前发布版中分配给自己**的论文提交决定：

- `confirm` 确认接受，或 `recuse` 并附**非空回避原因**；
- 同一决定重复提交**幂等**（`changed=false`，不推进修订号）；**已回避任务不能再确认**（`409`）；
  未分配给本人的任务、不存在的论文、尚未发布时一律拒绝（`404`）；空回避原因 `422`；
- **有效决定**（新确认、首次回避、确认后改回避）使资料修订号 +1；
- 回避**立即**停止该评审人读取该匿名稿（取稿列表不再包含该论文），并写入**硬回避表**——
  后续普通求解与补位都不得再把该论文分给该评审人（诊断原因
  `hard_recusal_after_decline`）；
- 回避后**当前发布方案不变**，仍可供会务方核对（每个槽位带 `confirmed/recused/pending`
  状态），其他评审人取稿不受影响。

**正式评语收集（已确认任务）**：评审人凭现有本人凭据（`X-Reviewer-Id` +
`X-Reviewer-Credential`）对**当前发布版中已确认且未回避**的槽位提交
**1～5 的整数评分**与**非空评语**，请求体须携带所针对的**发布序号 `serial`**：

- 提交在**同一个写事务**内核对发布序号、（论文, 评审人）分配关系与决定状态：
  序号过期/超前 `409`；尚未发布、论文不存在、未分配给本人或已被移出当前方案 `404`；
  槽位为 `pending` 或 `recused` `409`；评分非 1～5 整数或评语为空白 `422`；
  被拒请求**不落库**，也不推进资料修订号；
- **同一有效任务只收一份**：评分与评语（首尾空白归一化后）**完全相同的重试**幂等返回
  **原收据 `receipt`**（`changed=false`）；**内容不同**返回 `409` 冲突，已存评语不变；
- 评语收集不影响资料修订号、发布序号与补位求解；
- **会务方可按论文**查看当前两名评审人的提交进度（已确认数/已交数/是否齐全）与评语全文；
  补位发布时**连续保留的确认槽位沿用原评语（同收据）**，**被移出槽位的评语仅供会务方追溯**
  （响应中的 `archived_reviews`），**不计入新方案进度**；同一评审人**后来重新获得该稿**时
  属于新序号下的新槽位，**不得复用旧评语**，须重新确认并重新提交；
- 评审人仅可查看**本人当前有效任务**上的评语（`GET /reviewer/reviews`），
  看不到另一评审人的评语，响应也不含作者机构。

**补位预演与补位发布**：会务方以**当前发布版 + 当前资料修订号**预演补位
（`POST /assignment/backfill/dry-run`）：

- 当前发布版中**已确认且未回避**的关系固定不动；其余位置（pending、已回避释放、
  或因资料变更而失效的固定位置）遵守原有全部硬约束与优化次序（先最小化最高容量比例、
  再全局字典序最小）重算；
- 无完整补位方案时返回未补齐论文及限制原因（`feasible=false`），**不改变发布版**；
- 补位发布（`POST /assignment/backfill/publish`）必须**同时复核两种版本**：资料修订号
  `base_revision` 与发布序号 `base_serial`，任一过期（期间有资料变更或任何新发布）即 `409`；
- 补位发布成功后发布序号 +1，**仅按新发布版授权取稿**；仍在新方案中的
  （论文, 评审人）槽位**保留其确认状态**，被移出方案的关系不再授权取稿。

**面向作者的匿名反馈快照**：会务方按论文发布（`POST /papers/{paper_id}/feedback-snapshot`，
请求体携带**当前发布序号 `serial`**）：

- 仅当该版**两名评审人均已确认且各提交一份正式评语**时才能发布；
  尚未发布/论文不在当前发布版 `404`，发布序号过期或超前 `409`，
  确认或评语资料不齐 `422`，**被拒请求不留下新版本**；
- 快照按方案槽位的**固定顺序**（评审人编号字典序）将两份评语标为 **1、2**；
  持码者视图**只含论文编号、两份评分与评语**，不暴露评审人编号、机构、评语收据及任何内部诊断；
- 发布返回该论文的**随机访问码**（`fbk-` 前缀），持码者凭码读取
  （`GET /feedback-snapshots/{code}`，**无需任何凭据**），**只能读取当前有效快照**：
  补位或重新发布导致发布序号变化时**旧码立即失效**；会务方对该稿已交评语
  **发起更正请求时既有访问码同样立即失效**，待更正完成后由会务方按现有规则重新发布；
- **相同发布序号及两份评语收据**的重复请求幂等返回**原快照与原码**（`changed=false`）；
  来源变化（新发布序号下的新槽位/新收据）后**生成新版本**（`version+1`、新访问码），
  旧版保留但仅供会务方追溯（`GET /papers/{paper_id}/feedback-snapshots`，
  含每版序号、访问码、评语收据及是否当前有效）；
- **无效码/已失效码统一 `404`**，响应体相同，**不透露论文是否存在**。

**评审资格停用/启用（会务方）**：既有评审人初始视为**启用**。会务方凭密钥对
`POST /reviewers/{reviewer_id}/status` 提交**评审人编号、所见资料修订号 `base_revision`、
目标状态 `active`（`false` 停用 / `true` 启用）与非空原因**：

- 状态变更在**同一个写事务内先核对修订号**：修订号过期/超前 `409`、未知评审人 `404`、
  空原因 `422`，被拒即不改状态、不推进修订号；
- **匹配修订号下重复提交同一状态和原因**幂等返回（`changed=false`），不推进修订号、不写记录；
  **同状态异原因**（含对初始即为启用者再次启用）`409`；**有效变更**推进资料修订号，
  基于旧修订号的后续普通发布/补位发布按原规则拒绝；
- 停用后该评审人**现有凭据仍然有效但立即 403**：不能取稿、提交决定或评语；
  **既有发布分配、确认和评语不删除**，当前发布版仍可供会务方核对，
  **已发布的作者反馈快照仍按原有规则有效**；
- 停用者**不进入后续普通分配和补位**：普通预演的排除原因标注 `reviewer_disabled`；
  补位时其**已确认槽位被释放**（诊断原因 `fixed_reviewer_disabled`），
  其他仍合格的已确认槽位保持固定；
- **启用后只恢复当前发布版中仍分配给本人的任务访问**；不找回已被补位替换的槽位，
  被归档的评语也不恢复（同一人后来重新获稿仍属新槽位，须重新确认、重新提交评语）；
- `GET /reviewers/{reviewer_id}/status` 可查看当前状态与**全部变更记录**
  （每次有效变更的目标状态、原因、推进后的修订号与时间）；评审人列表与详情同步包含 `active`。

**机构归并组（同一机构的不同原名，会务方）**：会务方凭密钥对
`POST /institutions/merge-groups` 提交**两个当前论文作者或评审人资料中出现过的
机构原名**与**所见资料修订号 `base_revision`**，把它们归为同一个冲突判定组：

- 归并关系**传递且不可拆分**：A 与 B 同组、B 与 C 同组，则 A 与 C 同组；
  系统只提供归并，不存在拆组/移出操作（归并边永久保留）；
- **同组重复提交幂等**（含交换两个名称的顺序、名称与自身归并）：`changed=false`，
  不写归并边、不推进修订号；**有效归并**（两个原先不同的组首次合并）推进资料修订号 +1；
- 请求在**同一个写事务内先核对修订号**：修订号过期/超前 `409`、名称首尾空白归一化后
  为空白 `422`、任一名称未出现在当前资料中 `404`，被拒请求不写入任何边、不留下部分变更；
- 成功响应返回**归并后的名称组**（`names`，按名称字典序）、**当前修订号** `revision`
  与**是否变更** `changed`；会务方可 `GET /institutions/merge-groups`
  查看全部归并组（含已删除资料名称的历史归并，仅供追溯）；
- **普通分配与补位都按归并组判定机构冲突**——"评审人与作者同机构"与
  "两名评审人是否同机构"一律比较归并组而非原名；预演解释须**标明由归并造成的冲突**
  （原因代码带 `_via_merge` 后缀），原名逐字相同的冲突仍用原有原因代码；
  **原始机构名称及既有响应字段保持原样**（资料、方案、评语、快照中均不改名）；
- **已确认槽位因归并而失效时，补位不得固定该槽位**（两名已确认评审人被归并为同机构时
  诊断 `fixed_pair_violates_institution_rule_via_merge`，释放后一槽位重算）；
  旧发布版与评语仍可在会务方视图中追溯，补位发布继续遵守**双版本复核**
  （`base_revision` + `base_serial`）及**评语继承**（保留槽位沿用同收据、
  移出槽位评语进入 `archived_reviews`）规则。

**已交评语更正（会务方发起 → 评审人更正 → 快照重发）**：会务方发现已交评语需要更正时，
凭密钥对 `POST /papers/{paper_id}/review-corrections` 指定**当前发布序号 `serial`、论文、
评审人及非空原因**发起更正请求：

- 只允许**当前仍分配、已确认且已交评语**的槽位：尚未发布/论文不在当前发布版 `404`，
  评审人不在该论文当前槽位（旧序号或已移出方案的槽位）`404`，发布序号过期/超前 `409`，
  槽位 `pending`/`recused` 或尚未交评语 `409`，原因为空白 `422`；被拒请求不落库；
- 同一槽位已有待完成更正请求时，**相同原因的重复发起幂等**（`changed=false`），
  **不同原因 `409`**；更正请求不推进资料修订号与发布序号；
- **发起即在同一个写事务内令该稿既有作者反馈访问码失效**（响应含失效版数
  `invalidated_snapshots`），旧码立即 `404`；待更正期间按原序号发布快照 `409`；
- 评审人凭现有本人凭据 `GET /reviewer/review-corrections` 查看**自己的待更正任务**
  （含更正原因与被更正评语的冻结内容，不含他人任务），并对
  `POST /reviewer/review-corrections/{paper_id}` 以**请求对应的发布序号和原评语收据**
  提交**新 1～5 整数评分与非空评语**；
- **首份有效更正生成新收据**并直接更新该槽位的正式评语，**原评语在更正记录中冻结保留**
  供会务方追溯（`GET /papers/{paper_id}/reviews` 响应新增 `corrections`）；
  **同内容重试幂等返回更正收据**（`changed=false`），**异内容 `409` 冲突**，已存更正不变；
- **发布序号变化（补位/重新发布）后，旧序号下的待更正请求失效**：不再列出，
  按旧序号提交 `409`、按新序号配旧收据提交 `404`；
- **补位保留槽位时沿用更正后的评语**（同新收据），**移出槽位的评语照常归档**
  （`archived_reviews`）；更正完成后须由会务方**按现有完整性规则重新发布快照**
  （当前序号 + 两人确认 + 两份评语），生成新版本与新访问码，旧码保持失效；
- 既有正式评语首提交接口的一次提交及冲突语义不变（更正后该接口对更正后内容的
  相同重试幂等返回更正收据，对原评语内容 `409`）；评审人不得查看他人评语。

**作者对快照评语的异议（持码提交 → 会务方驳回/受理 → 既有更正流程）**：作者凭
**当前有效**的反馈快照访问码，对标号 1 或 2 的评语提交**非空异议理由**
（`POST /feedback-snapshots/{access_code}/objections`，无需任何凭据）：

- 仅当前有效快照可提交：访问码无效、已被更正请求失效或所依附发布序号已非当前
  序号时，与读取快照**统一 `404`**（不透露论文是否存在）；标号非 1/2、
  理由去首尾空白后为空白 `422`；
- **同一快照同一标号仅留一条**：相同理由（首尾空白归一化后一致）的重试**幂等返回
  原记录与原查询凭据**（`changed=false`）；**不同理由 `409` 冲突**；
  `(快照, 标号)` 唯一约束 + 写事务串行化保证**并发提交也不会生成两条**；
- 提交返回**随机查询凭据**（`obj-` 前缀）：即使快照码后来失效，仍可凭它查询
  处理状态（`GET /objections/{query_token}`，无需凭据；无效凭据统一 `404`）；
- **作者响应不暴露评审人编号、机构或评语收据**，仅含论文编号、标号、理由与处理状态。

会务方凭现有密钥查看异议及**冻结的原反馈**（`GET /papers/{paper_id}/objections`，
含快照版本、该标号冻结的评分与评语、定位收据与全部历史），并对
`POST /papers/{paper_id}/objections/{objection_id}/resolve` 选择**驳回或受理**：

- 仅待处理（`pending`）异议可处理，已驳回/已受理/已过期的再次处理 `409`，
  异议不存在 `404`；
- **驳回不影响快照**（既有访问码保持原状），原因可选；
- **受理须填写非空更正原因**（`422`），并在**同一个写事务内原子核对**：
  快照所依附的**发布序号仍为当前序号**、**目标评语收据未变**（冻结收据仍对应
  当前序号的评语行）、目标槽位仍为已确认且**无待完成更正请求**；任一不满足
  （发布版或目标评语已变化、已有待更正请求）即 `409` **拒绝受理且不改异议状态**；
- 受理通过后**按匿名标号定位评审人**（冻结收据 → 当前序号评语行）并**走既有
  更正请求流程**：冻结原评语生成待更正请求，**该稿既有作者反馈访问码立即失效**
  （响应含 `invalidated_snapshots`），后续更正提交、快照重发与补位沿用规则
  与会务方直接发起的更正完全一致；受理/驳回不推进资料修订号与发布序号；
- **未处理异议随发布序号变化标记过期**（`superseded`）：普通发布与补位发布推进
  序号时，所有待处理异议自动过期，**历史保留供会务方追溯**（异议列表含全部状态）。

**权限模型**：

| 角色 | 凭据 | 权限 |
|---|---|---|
| 会务方 | 本地密钥（请求头 `X-Organizer-Key`） | 维护资料、预演、发布、查询分配与排除原因、按论文查看评审评语与提交进度、发布与追溯面向作者的匿名反馈快照、停用/启用评审资格并查看变更记录、发起评语更正请求并追溯更正记录、提交并查看普通分配评审人锁定表、查看作者异议（含冻结原反馈）并驳回/受理 |
| 评审人 | 自身凭据（`X-Reviewer-Id` + `X-Reviewer-Credential`） | 资格启用时：对已发布方案中分配给自己的任务确认/回避、读取匿名稿（不含作者机构）、提交并查看本人正式评语、查看本人待更正任务并提交更正评语；资格停用后凭据仍有效但上述操作一律 `403` |
| 持码者 | 快照随机访问码（路径参数，无需请求头） | 仅可读取对应论文**当前有效**的匿名反馈快照（论文编号 + 标号 1、2 的评分与评语），并对当前有效快照的标号 1/2 评语提交异议 |
| 持异议凭据者 | 异议随机查询凭据（路径参数，无需请求头） | 仅可查看对应异议的处理状态（论文编号、标号、理由与状态；快照码失效后仍可查） |

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

服务地址：**http://localhost:8000**（交互式 API 文档：http://localhost:8000/docs）

SQLite 数据库文件在首次启动时自动初始化，持久化于名为 `review_data` 的卷（容器内 `/data/app.db`）。

### 仅 Docker

```bash
docker build -t review-assignment .
docker run -d -p 8000:8000 \
  -e ORGANIZER_KEY=please-change-me \
  -v review_data:/data \
  review-assignment
```

### 本地开发（无 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ORGANIZER_KEY=dev-organizer-key python -m app.main
```

## 配置

均通过环境变量配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ORGANIZER_KEY` | `dev-organizer-key` | 会务方本地密钥，**生产环境必须修改** |
| `DB_PATH` | `./data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

Compose 下可用 `ORGANIZER_KEY=xxx PORT=9000 docker compose up -d` 覆盖。

## API 一览

会务方接口均需请求头 `X-Organizer-Key`；评审人接口需 `X-Reviewer-Id` 与 `X-Reviewer-Credential`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（无需凭据） |
| GET | `/meta` | 当前资料修订号与发布状态 |
| POST | `/papers` | 录入论文（编号/匿名稿/主题/作者机构），重复编号 `409` |
| PUT / DELETE | `/papers/{paper_id}` | 更新 / 删除论文，不存在 `404` |
| GET | `/papers` | 论文列表 |
| POST | `/reviewers` | 录入评审人（编号/凭据/擅长主题/机构/容量/回避论文），重复编号 `409`，非法容量 `422`；既有评审人初始为启用状态（`active=true`） |
| PUT / DELETE | `/reviewers/{reviewer_id}` | 更新 / 删除评审人 |
| GET | `/reviewers` | 评审人列表（含每人 `active` 资格状态） |
| POST | `/reviewers/{reviewer_id}/status` | 停用/启用评审资格：`{"reviewer_id","base_revision","active","reason"}`；事务内先核对修订号，过期/超前 `409`，未知评审人 `404`，空原因 `422`，同状态异原因 `409`，同状态同原因重复提交幂等（`changed=false`），有效变更推进修订号 |
| GET | `/reviewers/{reviewer_id}/status` | 查看评审人当前资格状态与全部变更记录（含每次原因、修订号、时间），未知评审人 `404` |
| POST | `/institutions/merge-groups` | 机构归并：`{"name_a","name_b","base_revision"}`；两名称须为当前资料中出现的机构原名；过期/超前修订号 `409`，空白名称 `422`，未知名称 `404`，同组重复提交幂等（`changed=false`，不推进修订号），有效归并推进修订号；返回归并后的名称组 `names`、当前修订号与 `changed` |
| GET | `/institutions/merge-groups` | 查看全部机构归并组（传递闭包，仅列 ≥2 个原名的组；归并不可拆分） |
| POST | `/assignment/locks` | 普通分配锁定表（整表替换）：`{"base_revision":N,"locks":{"P1":["R1","R2"]}}`，每篇 0~2 名，空表清除全部锁定；未知论文/评审人 `404`，同篇重复或超两人/空白编号 `422`，过期修订号 `409`，均不改表；相同表重试幂等（`changed=false`），有效变更推进修订号；返回修订号、`changed` 与锁定计数 |
| GET | `/assignment/locks` | 会务方查看当前锁定表（`locks`、资料修订号、锁定论文/槽位计数） |
| POST | `/assignment/dry-run` | 预演：返回方案、未分配论文与限制原因、资料修订号；有锁定时保留锁定槽位，锁定失效以 `lock_*` 诊断并标为不可完整分配；不影响已发布版 |
| POST | `/assignment/publish` | 发布：携带 `{"base_revision": N}`，修订号过期 `409`，无可行方案 `422` |
| POST | `/assignment/backfill/dry-run` | 补位预演：固定已确认关系，其余位置按原约束/优化次序重算；返回 `revision` 与 `serial` |
| POST | `/assignment/backfill/publish` | 补位发布：`{"base_revision": N, "base_serial": M}` 双版本复核，过期 `409`，补不齐 `422` |
| GET | `/assignment` | 查询已发布方案（含 `serial`）及每篇论文的分配/排除原因、每槽位确认/回避状态 |
| GET | `/papers/{paper_id}/reviews` | 会务方按论文查看当前两名评审人的评语提交进度、评语全文，及被移出槽位的追溯评语（`archived_reviews`）与全部更正记录（`corrections`） |
| POST | `/papers/{paper_id}/review-corrections` | 会务方发起评语更正请求：`{"paper_id","serial","reviewer_id","reason"}`；仅当前仍分配、已确认且已交评语的槽位；旧序号 `409`、已移出槽位 `404`、未确认/未交评语 `409`、空原因 `422`；同槽位同原因重复发起幂等（`changed=false`），异原因 `409`；发起即令该稿既有反馈访问码失效 |
| POST | `/papers/{paper_id}/feedback-snapshot` | 会务方发布面向作者的匿名反馈快照：`{"serial": N}`；资料不齐 `422` 不留新版，序号过期/超前 `409`，未发布/论文不在方案 `404`；成功返回随机访问码与快照，同序号同两份评语收据重复请求返回原码（`changed=false`） |
| GET | `/feedback-snapshots/{access_code}` | 持码者读取当前有效快照（**无需凭据**）：仅论文编号与标号 1、2 的两份评分和评语；无效码/已失效旧码统一 `404` |
| GET | `/papers/{paper_id}/feedback-snapshots` | 会务方追溯某论文的全部快照版本（含已失效旧版、访问码、评语收据与当前有效标记） |
| POST | `/feedback-snapshots/{access_code}/objections` | 作者凭当前有效访问码对快照标号 1/2 的评语提交异议：`{"label":1|2,"reason":"..."}`；无效/已失效码统一 `404`，标号非法/理由空白 `422`；同一快照同一标号仅一条，相同理由重试幂等返回原记录与原查询凭据（`changed=false`），不同理由 `409`，并发不生成两条；返回随机查询凭据 `obj-...`，响应不含评审人编号/机构/收据 |
| GET | `/objections/{query_token}` | 持异议查询凭据查看处理状态（**无需凭据**，快照码失效后仍可查）：论文编号、标号、理由与 `pending/rejected/accepted/superseded` 状态；无效凭据统一 `404` |
| GET | `/papers/{paper_id}/objections` | 会务方查看该论文全部异议（含已过期/已处理历史）及各自冻结的原反馈（快照版本、该标号评分与评语、定位收据） |
| POST | `/papers/{paper_id}/objections/{objection_id}/resolve` | 会务方处理异议：`{"decision":"reject"\|"accept","reason"?}`；驳回不影响快照（原因可选）；受理须非空更正原因（`422`）并原子核对发布序号与目标收据，通过后走既有更正请求流程并令原访问码立即失效；发布版/目标评语已变化或已有待更正请求 `409` 且不改异议状态；非待处理异议 `409`，未知异议 `404` |
| GET | `/reviewer/assignments` | 评审人读取分配给自己的匿名稿（凭据失效 `401`；**资格停用 `403`**；已回避论文不返稿，仅列于 `recused`） |
| POST | `/reviewer/assignments/{paper_id}/decision` | 评审人提交 `{"paper_id","decision":"confirm"|"recuse","reason"?}`；幂等；未分配 `404`，已回避再确认 `409`，空回避原因 `422`，**资格停用 `403`** |
| POST | `/reviewer/assignments/{paper_id}/review` | 评审人提交 `{"paper_id","serial","score":1~5整数,"comment"}`；同内容重试返原收据，内容不同 `409`，序号过期/超前 `409`，未分配/已移出 `404`，未确认/已回避 `409`，非法评分/空评语 `422`，**资格停用 `403`** |
| GET | `/reviewer/reviews` | 评审人查看本人当前有效任务上已提交的评语（不含他人评语与作者机构；**资格停用 `403`**） |
| GET | `/reviewer/review-corrections` | 评审人查看本人在当前发布序号下的待更正任务（含更正原因与原评语冻结内容；不含他人任务；**资格停用 `403`**） |
| POST | `/reviewer/review-corrections/{paper_id}` | 评审人提交更正评语：`{"paper_id","serial","original_receipt","score","comment"}`；首份有效更正生成新收据并更新槽位评语（原评语冻结保留）；同内容重试返更正收据（`changed=false`），异内容 `409`，序号过期/超前 `409`，无对应任务 `404`，非法评分/空评语 `422`，**资格停用 `403`** |

## 调用示例

完整可运行脚本见 [`examples/demo.sh`](examples/demo.sh)（服务启动后执行 `./examples/demo.sh`）；
确认/回避 + 补位流程见 [`examples/backfill.sh`](examples/backfill.sh)
（`./examples/backfill.sh`，建议对新库运行）；
正式评语收集流程见 [`examples/reviews.sh`](examples/reviews.sh)
（`./examples/reviews.sh`，建议对新库运行）；
匿名反馈快照流程见 [`examples/snapshots.sh`](examples/snapshots.sh)
（`./examples/snapshots.sh`，建议对新库运行）；
本轮评审资格停用/启用流程见 [`examples/eligibility.sh`](examples/eligibility.sh)
（`./examples/eligibility.sh`，建议对新库运行）；
本轮机构归并流程见 [`examples/institutions.sh`](examples/institutions.sh)
（`./examples/institutions.sh`，建议对新库运行）；
本轮评语更正流程见 [`examples/corrections.sh`](examples/corrections.sh)
（`./examples/corrections.sh`，建议对新库运行）；
本轮普通分配锁定表流程见 [`examples/locks.sh`](examples/locks.sh)
（`./examples/locks.sh`，建议对新库运行）；
本轮作者异议流程见 [`examples/objections.sh`](examples/objections.sh)
（`./examples/objections.sh`，建议对新库运行）。

```bash
BASE=http://localhost:8000
ORG='X-Organizer-Key: dev-organizer-key'

# 1. 录入评审人
curl -X POST $BASE/reviewers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []
}'

# 2. 录入论文
curl -X POST $BASE/papers -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]
}'

# 3. 预演（记下返回的 revision）
curl -X POST $BASE/assignment/dry-run -H "$ORG"

# 4. 发布（携带预演所用修订号；资料已变动则返回 409，需重新预演）
curl -X POST $BASE/assignment/publish -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": 2}'

# 5. 会务方查询分配与排除原因
curl $BASE/assignment -H "$ORG"

# 6. 评审人凭自身凭据读取已分配匿名稿
curl $BASE/reviewer/assignments -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
```

### 本轮用法：评审人确认/回避与会务方补位

```bash
# 7. 评审人确认当前分配给自己的论文（有效决定推进资料修订号；重复提交幂等）
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "confirm"}'
# -> {"state":"confirmed","changed":true,"revision":N,"serial":S,...}

# 8. 另一评审人声明回避（原因非空；提交后立即不能再读取该稿，且成为硬回避）
curl -X POST $BASE/reviewer/assignments/P1/decision \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者有合作"}'

# 9. 会务方补位预演：已确认关系固定，其余位置重算；记下 revision 与 serial
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> {"feasible":true,"revision":N,"serial":S,"plan":{...},"fixed":{"P1":["R1"]},...}

# 10. 补位发布：双版本复核；期间资料变更或有过任何发布都会 409，需重新预演
curl -X POST $BASE/assignment/backfill/publish -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": N, "base_serial": S}'
# -> {"ok":true,"serial":S+1,"revision":N,"carried_confirmations":1,"plan":{...}}
```

评审人取稿响应新增 `states`（本人在当前发布版各槽位的 `confirmed/recused/pending`）
与 `recused`（已回避论文编号、本人填写的原因、时间）；`assignments` 中不再出现已回避论文。

### 本轮用法：正式评分与评语收集

```bash
# 11. 评审人先确认任务 (仅 confirmed 槽位可交正式评语), 再提交 1~5 整数评分与非空评语
curl -X POST $BASE/reviewer/assignments/P1/review \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "score": 4, "comment": "选题重要, 建议补充消融实验。"}'
# -> {"ok":true,"score":4,"comment":"...","receipt":"rvw-xxxx","changed":true,"serial":S,...}

# 12. 完全相同的重试 (评分+评语一致, 首尾空白归一化) -> 原收据, changed=false
#     内容不同 -> 409 冲突, 已存评语不变; 过期/超前 serial -> 409
# 13. 评审人查看本人当前有效任务的评语 (看不到另一评审人, 也无作者机构)
curl $BASE/reviewer/reviews -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
# -> {"reviewer_id":"R1","serial":S,"reviews":[{"paper_id":"P1","score":4,"comment":"...","receipt":"rvw-xxxx",...}]}

# 14. 会务方按论文查看两名评审人的提交进度与评语全文
curl $BASE/papers/P1/reviews -H "$ORG"
# -> {"paper_id":"P1","serial":S,
#     "progress":{"slots":2,"confirmed":2,"submitted":1,"complete":false},
#     "slots":[{"reviewer_id":"R1","state":"confirmed","submitted":true,"review":{...}},
#              {"reviewer_id":"R2","state":"confirmed","submitted":false,"review":null}],
#     "archived_reviews":[]}
```

补位发布成功后，响应新增 `carried_reviews`（连续保留确认槽位所沿用的评语份数）：
沿用的评语在新序号下**收据与内容不变**；被移出槽位的评语不再出现在任何人的当前视图，
仅由会务方在该论文的 `archived_reviews`（带原 `serial`）中追溯；同一人后来重新获稿时
须重新确认并提交新评语（新收据）。

### 本轮用法：面向作者的匿名反馈快照

```bash
# 15. 两名评审人均已确认并各交一份正式评语后, 会务方携带当前发布序号发布快照
curl -X POST $BASE/papers/P1/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"serial": S}'
# -> {"ok":true,"paper_id":"P1","serial":S,"version":1,
#     "access_code":"fbk-xxxx","changed":true,
#     "snapshot":{"paper_id":"P1","reviews":[
#       {"label":1,"score":4,"comment":"..."},{"label":2,"score":2,"comment":"..."}]}}

# 16. 相同发布序号及两份评语收据重复请求 -> 原快照原码, changed=false
#     序号过期/超前 -> 409; 确认/评语未齐 -> 422 (不产生新版本); 论文不在方案 -> 404

# 17. 作者(持码者)读取: 无需任何凭据, 只见论文编号与标号 1、2 的评分/评语
curl $BASE/feedback-snapshots/fbk-xxxx
# -> {"paper_id":"P1","reviews":[{"label":1,"score":4,"comment":"..."},
#                                {"label":2,"score":2,"comment":"..."}]}

# 18. 无效码或补位/重新发布导致序号前进后的旧码 -> 统一 404 (不透露论文是否存在)
#     在新序号上重新发布成功则生成 version+1 与新访问码

# 19. 会务方追溯该论文的全部快照版本 (含已失效旧版)
curl $BASE/papers/P1/feedback-snapshots -H "$ORG"
# -> {"paper_id":"P1","current_serial":S2,"snapshots":[
#     {"version":1,"serial":S1,"active":false,"access_code":"fbk-....","receipts":[...],"reviews":[...]},
#     {"version":2,"serial":S2,"active":true, "access_code":"fbk-....","receipts":[...],"reviews":[...]}]}
```

快照响应不含评审人编号、评审人机构、作者机构、评语收据、发布序号/版本及任何内部诊断字段；
标号 1、2 的固定顺序为方案槽位顺序（评审人编号字典序）。

### 本轮用法：评审资格停用/启用与变更记录

```bash
# 20. 先取当前资料修订号, 再停用评审人 R1 (原因必须非空)
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/reviewers/R1/status -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R1\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假暂停本轮评审\"}"
# -> {"ok":true,"reviewer_id":"R1","active":false,"changed":true,"revision":N+1,"reason":"..."}

# 21. 匹配修订号重复提交同一状态和原因 -> changed=false (不推进修订号、不写记录)
#     同状态异原因 -> 409; 过期/超前修订号 -> 409; 未知评审人 -> 404; 空原因 -> 422
# 22. R1 凭据仍有效, 但取稿/提交决定/提交评语/查看评语立即全部 403 (错误凭据仍为 401)
curl -s -o /dev/null -w '%{http_code}\n' $BASE/reviewer/assignments \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'   # -> 403
# 23. 既有发布分配/确认/评语不删除; 已发布的作者反馈快照仍有效;
#     后续普通预演 R1 被标注 reviewer_disabled; 补位预演释放其已确认槽位
#     (fixed_reviewer_disabled), 其他合格的已确认槽位保持固定
# 24. 重新启用: 只恢复当前发布版中仍分配给 R1 的任务; 已被补位替换的槽位不找回
curl -X POST $BASE/reviewers/R1/status -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R1\", \"base_revision\": $REV2, \"active\": true, \"reason\": \"假期结束恢复评审\"}"
# -> {"ok":true,"active":true,"changed":true,"revision":N2,...}

# 25. 会务方查看当前状态与全部变更记录 (每次有效变更的目标状态/原因/修订号/时间)
curl $BASE/reviewers/R1/status -H "$ORG"
# -> {"reviewer_id":"R1","active":true,"history":[
#     {"active":false,"reason":"长期休假暂停本轮评审","revision":8,"changed_at":"..."},
#     {"active":true,"reason":"假期结束恢复评审","revision":12,"changed_at":"..."}]}
```

### 本轮用法：机构归并组（同一机构的不同原名）

```bash
# 26. 先取当前资料修订号, 再提交两个"当前资料中出现过"的机构原名
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/institutions/merge-groups -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"name_a\": \"Acme-U\", \"name_b\": \"Acme University\", \"base_revision\": $REV}"
# -> {"ok":true,"changed":true,"revision":N+1,
#     "names":["Acme University","Acme-U"],"group_key":"Acme University",
#     "submitted":["Acme-U","Acme University"]}

# 27. 同组重复提交 (含交换顺序) 幂等 changed=false, 不推进修订号、不写归并边;
#     名称与自身归并同样幂等; 归并关系传递: A~B、B~C => A 与 C 同组, 再提交 A、C 幂等
#     过期/超前修订号 -> 409; 空白名称 -> 422; 名称未出现在当前论文/评审人资料 -> 404
#     (被拒请求不写入任何边, 不留下部分变更)
curl $BASE/institutions/merge-groups -H "$ORG"
# -> {"revision":N+1,"groups":[
#     {"group_key":"Acme University","names":["Acme University","Acme-U"],"size":2}]}

# 28. 普通预演按归并组判定冲突, 解释标明"由归并造成":
curl -X POST $BASE/assignment/dry-run -H "$ORG"
# 评审人机构 "Acme-U" 与作者机构 "Acme University" 已归并 ->
#   papers.P1.excluded: [{"reviewer_id":"R1","reason":"same_institution_as_author_via_merge"}]
# 两名评审人原名不同但同归并组 -> 不能同篇配对; 全部合格者同组且无可行方案时,
#   诊断原因 eligible_reviewers_share_single_institution_via_merge
# (原名逐字相同的冲突仍使用 same_institution_as_author 等既有原因代码;
#  资料与响应中的原始机构名称保持原样, 不做替换)

# 29. 已确认槽位因归并失效 (两名已确认评审人被归并为同机构) 时,
#     补位预演不固定该槽位:
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"
# -> fixed_problems.P1: [{"reviewer_id":"R4",
#     "reason":"fixed_pair_violates_institution_rule_via_merge"}],
#    fixed.P1: ["R3"], plan.P1: ["R3","R5"]
# 30. 补位发布仍需双版本复核; 保留槽位的确认与评语沿用 (同收据),
#     被移出槽位的评语仅供会务方追溯 (archived_reviews)
curl -X POST $BASE/assignment/backfill/publish -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": N, "base_serial": S}'
```

### 本轮用法：已交评语更正（会务方发起 → 评审人更正 → 快照重发）

```bash
# 31. 会务方发现 R1 的已交评语需要更正: 指定当前发布序号 + 论文 + 评审人 + 非空原因
curl -X POST $BASE/papers/P1/review-corrections -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "reviewer_id": "R1", "reason": "评分与评语内容明显不符, 请更正"}'
# -> {"ok":true,"changed":true,"correction_id":1,"state":"pending","serial":S,
#     "original":{"score":4,"comment":"...","receipt":"rvw-....",...},
#     "invalidated_snapshots":1,...}   # 该稿既有作者反馈访问码立即失效 (旧码 404)
#    仅当前仍分配、已确认且已交评语的槽位可发起: 旧序号 409, 已移出槽位 404,
#    pending/recused 或未交评语 409, 空原因 422; 同槽位同原因重复发起幂等, 异原因 409

# 32. 待更正期间按原序号发布快照 -> 409 (须待更正完成后重新发布)

# 33. 评审人凭本人凭据查看自己的待更正任务 (含更正原因与原评语收据, 不含他人任务)
curl $BASE/reviewer/review-corrections -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'
# -> {"reviewer_id":"R1","serial":S,"corrections":[{"correction_id":1,"reason":"...",
#     "original":{"score":4,"comment":"...","receipt":"rvw-...."},"state":"pending",...}]}

# 34. 评审人以请求对应的发布序号 + 原评语收据提交新 1~5 整数评分与非空评语
curl -X POST $BASE/reviewer/review-corrections/P1 \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "serial": S, "original_receipt": "rvw-....",
       "score": 5, "comment": "更正: 选题重要, 方法扎实, 建议接收。"}'
# -> {"ok":true,"receipt":"rvw-<新收据>","original_receipt":"rvw-....","changed":true,...}
#    首份有效更正生成新收据并更新槽位评语, 原评语冻结保留供会务方追溯;
#    同内容重试 -> 返更正收据 changed=false; 异内容 -> 409; 收据无对应任务 -> 404

# 35. 会务方按论文追溯更正记录 (原评语与更正后内容对照)
curl $BASE/papers/P1/reviews -H "$ORG"
# -> {"paper_id":"P1","serial":S,...,"slots":[...],
#     "corrections":[{"state":"completed","original":{...},"corrected":{...},...}]}

# 36. 更正完成后, 会务方按现有完整性规则重新发布快照 -> version+1 与新访问码, 旧码保持失效
curl -X POST $BASE/papers/P1/feedback-snapshot -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"serial": S}'

# 37. 补位发布: 保留槽位沿用更正后的评语 (同新收据);
#     发布序号变化后, 旧序号下的待更正请求失效 (不再列出, 按旧序号提交 409)
```

### 本轮用法：普通分配评审人锁定表

```bash
# 38. 会务方先取资料修订号, 再整表提交锁定: P1 锁 R4 一人, P2 锁 R1+R2 两人;
#     未列出的论文不锁定, 空表 {} 清除全部锁定
REV=$(curl -s $BASE/meta -H "$ORG" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
curl -X POST $BASE/assignment/locks -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R4\"], \"P2\": [\"R1\", \"R2\"]}}"
# -> {"ok":true,"changed":true,"revision":N+1,
#     "locks":{"P1":["R4"],"P2":["R1","R2"]},"locked_papers":2,"locked_slots":3}

# 39. 相同锁定表重试 -> changed=false (不推进修订号); 每篇按评审人编号字典序归一化
#     过期/超前修订号 -> 409; 未知论文/评审人 -> 404 (整表拒绝、不改表);
#     同篇重复锁定或超过两名 -> 422; 空白编号 -> 422
# 40. 会务方查看当前锁定表
curl $BASE/assignment/locks -H "$ORG"
# -> {"revision":N+1,"locks":{"P1":["R4"],...},"locked_papers":2,"locked_slots":3}

# 41. 普通预演: 锁定槽位保留; 单人锁定时搭档必须与锁定者不同机构且可补足专长
curl -X POST $BASE/assignment/dry-run -H "$ORG"
# -> {"feasible":true,"plan":{"P1":["R1","R4"],...},
#     "locks":{"P1":["R4"],"P2":["R1","R2"]},"lock_problems":{},...}

# 42. 锁定人随后被停用/删除/回避, 或资料及机构归并使其失格, 或锁定累计超容量:
#     预演不释放槽位, 而是说明具体冲突并标为不可完整分配; 发布 422 且当前发布版不变
# -> {"feasible":false,"unassigned":["P1"],"diagnostics":{"P1":{
#       "locks":["R4"],"lock_problems":[{"reviewer_id":"R4","reason":"lock_reviewer_disabled"}],
#       "reasons":["lock_reviewer_disabled"]}}}

# 43. 补位预演/发布不读取锁定表 (响应无 locks 字段), 仍只固定已确认且未回避的槽位:
curl -X POST $BASE/assignment/backfill/dry-run -H "$ORG"

# 44. 会务方调整或清除锁定后普通分配恢复:
curl -X POST $BASE/assignment/locks -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV2, \"locks\": {}}"   # 空表清除全部锁定
```

### 本轮用法：作者对快照评语的异议（持码提交 → 会务方驳回/受理）

```bash
# 45. 快照发布后, 作者凭当前有效访问码对标号 1 的评语提交非空异议理由
curl -X POST $BASE/feedback-snapshots/fbk-xxxx/objections \
  -H 'Content-Type: application/json' \
  -d '{"label": 1, "reason": "评分与评语内容明显不符, 请求复核"}'
# -> {"ok":true,"changed":true,"query_token":"obj-....",
#     "objection":{"paper_id":"P1","label":1,"reason":"...","state":"pending",...}}
#    响应不含评审人编号、机构或评语收据; 无效/已失效码统一 404;
#    标号非 1/2 或理由空白 -> 422

# 46. 相同理由重试 -> 幂等返回原记录与原查询凭据 (changed=false);
#     不同理由 -> 409; 同一快照同一标号仅留一条 (并发提交也不会生成两条)

# 47. 作者凭查询凭据查看处理状态 (无需任何凭据; 快照码失效后仍可查)
curl $BASE/objections/obj-....
# -> {"paper_id":"P1","label":1,"state":"pending",...}
#    无效凭据统一 404

# 48. 会务方查看该论文全部异议及冻结的原反馈
curl $BASE/papers/P1/objections -H "$ORG"
# -> {"paper_id":"P1","objections":[
#     {"objection_id":1,"label":1,"state":"pending","serial":S,"snapshot_version":1,
#      "frozen_review":{"score":4,"comment":"..."},"receipt":"rvw-....",...}]}

# 49. 会务方受理: 须填写非空更正原因; 同一写事务内原子核对发布序号仍为当前序号、
#     目标评语收据未变、槽位已确认且无待完成更正请求, 通过后走既有更正请求流程
curl -X POST $BASE/papers/P1/objections/1/resolve -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d '{"decision": "accept", "reason": "评分与评语内容明显不符, 请更正"}'
# -> {"ok":true,"decision":"accepted","objection":{"state":"accepted",...},
#     "correction":{"reviewer_id":"R1","state":"pending",...},
#     "invalidated_snapshots":1}   # 原访问码立即失效 (旧码 404)
#    发布版/目标评语已变化或已有待更正请求 -> 409 且异议状态不变; 空原因 -> 422

# 50. 或会务方驳回: 不影响快照 (既有访问码保持原状), 原因可选
curl -X POST $BASE/papers/P1/objections/2/resolve -H "$ORG" \
  -H 'Content-Type: application/json' -d '{"decision": "reject"}'
# -> {"ok":true,"decision":"rejected","objection":{"state":"rejected",...}}

# 51. 评审人在既有待更正任务中完成更正后, 会务方按现有规则重发快照;
#     普通/补位发布推进发布序号时, 未处理异议自动标记 superseded,
#     历史保留于 GET /papers/P1/objections 供会务方追溯
```

预演响应示例（可行）：

```json
{
  "feasible": true,
  "revision": 7,
  "plan": {"P1": ["R1", "R2"]},
  "unassigned": [],
  "max_used_capacity_ratio": "3/4",
  "papers": {"P1": {"assigned": ["R1", "R2"],
    "eligible_reviewers": ["R1", "R2", "R3"],
    "excluded": [{"reviewer_id": "R4", "reason": "same_institution_as_author"}]}},
  "diagnostics": {}
}
```

预演响应示例（无完整方案，`papers` 为最大部分分配，`diagnostics` 给出每篇未分配论文的限制原因）：

```json
{
  "feasible": false,
  "unassigned": ["P2"],
  "diagnostics": {"P2": {
    "eligible_reviewers": ["R3"],
    "eligible_at_capacity": ["R3"],
    "excluded": [{"reviewer_id": "R1", "reason": "same_institution_as_author"}],
    "reasons": ["fewer_than_two_eligible_reviewers", "eligible_reviewers_at_capacity"]
  }}
}
```

## 错误码

| 状态码 | 场景 |
|---|---|
| 401 | 会务方密钥缺失/错误；评审人凭据缺失/失效 |
| 403 | 评审资格已停用：凭据本身仍有效，但取稿、提交决定、提交评语、查看本人评语一律立即拒绝 |
| 404 | 更新/删除不存在的论文或评审人；调整资格或查看变更记录时评审人未知；尚未发布方案；评审人对**未分配给自己/已被移出当前方案**的论文提交决定或评语；会务方查询当前发布版中不存在的论文评语；尚无发布版时补位预演/发布；反馈快照的访问码无效或已失效（旧码或已被更正请求失效的码，不区分论文是否存在）；未发布或论文不在当前发布版时发布快照；机构归并时任一名称未出现在当前论文作者或评审人资料中；更正请求针对未分配给该评审人的槽位（含已移出方案的槽位）；评审人提交更正时收据无对应（待）更正任务；锁定表包含未知论文或未知评审人（整表拒绝、不改动当前锁定）；凭无效/已失效访问码提交异议（与读取快照同一响应体）；异议查询凭据无效；处理的异议不存在或不属于该论文 |
| 409 | 论文/评审人编号重复；发布携带的修订号已过期（资料已变更）；补位发布携带的资料修订号或发布序号过期；已回避任务再次确认；评语提交携带的发布序号过期/超前；对 `pending`/`recused` 槽位提交评语；同一有效任务已有评语而提交内容不同（冲突）；反馈快照请求携带的发布序号过期/超前；资格调整携带的修订号过期/超前；同状态异原因的资格调整（含对初始启用者再次启用）；机构归并携带的修订号过期/超前；更正请求携带的发布序号过期/超前；对未确认/未交评语的槽位发起更正；同槽位异原因重复发起更正；论文存在待完成更正请求时发布快照；更正提交携带的发布序号过期/超前；同一更正任务已有更正评语而提交内容不同；锁定表携带的资料修订号过期/超前；同一快照同一标号已有异议而提交理由不同；对已驳回/已受理/已过期异议再次处理；受理时发布版已变化、目标评语收据已变、槽位已非确认或已有待完成更正请求（异议状态不变） |
| 422 | 非法容量（非整数、小于 1）、缺字段等参数校验失败；无可行方案时强行发布；回避原因为空；补位预演无法补齐为完整方案时强行补位发布；评分不是 1～5 的整数或评语为空白；路径论文编号与请求体不一致；路径评审人编号与请求体不一致；资格调整原因为空；发布反馈快照时两名评审人未全部确认或两份正式评语未交齐（不留下新版本）；机构归并的名称去首尾空白后为空白；更正请求原因为空；更正评分不是 1～5 的整数或更正评语为空白；锁定表中同一篇论文重复锁定同一评审人或每篇锁定超过两名；锁定表中的论文/评审人编号去首尾空白后为空白；存在锁定槽位失效（删除/停用/回避、资料或机构归并失格、锁定超容量）导致不可完整分配时强行普通发布（不改变当前发布版）；异议标号非 1/2 或异议理由为空白；受理异议时更正原因为空白 |

排除/限制原因代码：`same_institution_as_author`（与作者同机构，原名逐字相同）、
`same_institution_as_author_via_merge`（评审人与作者机构原名不同但已归并为同一冲突判定组）、
`on_reviewer_avoid_list`
（在回避名单）、`hard_recusal_after_decline`（评审人发布后声明回避形成的硬回避）、
`reviewer_disabled`（评审资格已停用，不进入普通分配）、
`fewer_than_two_eligible_reviewers`（合格者不足两人）、
`eligible_reviewers_at_capacity`（合格者容量占满）、`no_expert_among_eligible_reviewers`
（合格者无人擅长主题）、`eligible_reviewers_share_single_institution`（合格者同属一个机构）、
`eligible_reviewers_share_single_institution_via_merge`（合格者机构原名互异但同属一个归并组）、
`no_valid_reviewer_pair`（不存在合法评审人对）、`all_valid_pairs_blocked_by_capacity`
（所有合法评审人对均被容量阻塞）。补位专用：`fixed_reviewer_not_found`（固定关系中的评审人
已被删除）、`fixed_reviewer_disabled`（固定关系中的评审人资格已停用，槽位释放）、
`fixed_pair_violates_institution_rule` / `fixed_pair_violates_expert_rule`
（数据变更后固定两人不再满足不同机构/至少一名擅长）、
`fixed_pair_violates_institution_rule_via_merge`（固定两人原名不同，但机构归并后同组，
已确认槽位因此失效、不予固定）、`fixed_assignments_exceed_capacity`
（固定关系累计超过评审人容量）、`no_valid_pair_containing_fixed_reviewer`
（含固定评审人的合法对不存在）、`all_fixed_compatible_pairs_blocked_by_capacity`
（与固定评审人相容的评审人对均被容量阻塞）。普通分配锁定专用（预演 `lock_problems`/诊断中；
锁定槽位失效只报错不释放，普通发布据此 422 拒绝）：`lock_reviewer_not_found`
（被锁评审人已被删除）、`lock_reviewer_disabled`（被锁评审人资格已停用）、
`lock_same_institution_as_author` / `lock_same_institution_as_author_via_merge`
（被锁评审人与作者同机构，后者由机构归并造成）、`lock_on_reviewer_avoid_list`
（被锁评审人录入了对该论文的回避）、`lock_hard_recusal_after_decline`
（被锁评审人发布后声明过回避，形成硬回避）、
`lock_pair_violates_institution_rule` / `lock_pair_violates_institution_rule_via_merge`
（两名被锁评审人同机构，后者由机构归并造成）、`lock_pair_violates_expert_rule`
（两名被锁评审人均不擅长该论文主题）、`lock_assignments_exceed_capacity`
（该评审人被锁定的论文数累计超过其容量）、`no_valid_pair_containing_lock_reviewer`
（单人锁定时含锁定者的合法对不存在）、`all_lock_compatible_pairs_blocked_by_capacity`
（与锁定者相容的评审人对均被容量阻塞）。

## 项目结构

```
app/
  main.py     # FastAPI 路由、鉴权、发布/补位发布事务、评审人决定与正式评语、匿名反馈快照、评审资格停用/启用、机构归并组、已交评语更正、普通分配评审人锁定表、作者异议提交/查询/驳回/受理
  solver.py   # 分配求解器 + 固定槽位求解引擎（补位=失效释放 / 普通锁定=失效只报错不释放；容量比例、字典序、部分方案与诊断；停用者直接排除；机构冲突一律按归并组判定）
  db.py       # SQLite 连接、schema 初始化/迁移、修订号、发布序号、读写事务、评审资格状态与变更记录、机构归并边与并查集分组、评语更正记录、普通分配锁定表、作者异议记录
  schemas.py  # 请求体模型与校验
tests/test_api.py                # 原有接口与求解器测试（含并发发布测试）
tests/test_backfill.py           # 确认/回避/硬回避/补位预演与发布测试
tests/test_reviews.py            # 正式评分/评语、收据幂等与冲突、进度与归档、补位沿用测试
tests/test_snapshots.py          # 匿名反馈快照：发布门槛、幂等原码、新版本旧码失效、统一 404、追溯
tests/test_eligibility.py        # 评审资格停用/启用：修订号核对、幂等/冲突、即时 403、槽位释放与恢复、变更记录
tests/test_institution_merges.py # 本轮机构归并：修订号核对、传递不可拆分、幂等、归并组冲突判定、预演归并标注、补位失效不固定、评语继承
tests/test_corrections.py        # 本轮评语更正：发起校验与幂等、访问码即时失效、待更正任务、更正提交幂等/冲突、快照重发、序号变化失效、补位沿用/归档
tests/test_locks.py              # 本轮普通分配锁定表：整表替换/修订号/幂等/404/422、槽位保留与专长由搭档满足、失格只报错不释放、归并/停用/删除/硬回避/容量诊断、发布拒绝不改当前版、补位不读锁定表、删论文级联
tests/test_objections.py         # 本轮作者异议：提交校验与统一 404、同标号幂等/冲突与并发唯一、查询凭据（码失效后仍可查）、作者视图不泄露、会务方查看冻结原反馈、驳回不影响快照、受理原子核对与既有更正流程衔接、序号变化过期与历史追溯
examples/demo.sh          # 端到端 curl 示例（录入 -> 预演 -> 发布 -> 取稿）
examples/backfill.sh      # 确认/回避 -> 补位预演 -> 双版本复核发布示例
examples/reviews.sh       # 正式评语收集端到端示例（确认 -> 提交评语 -> 进度查看 -> 补位沿用/归档）
examples/snapshots.sh     # 匿名反馈快照端到端示例（发布快照 -> 持码读取 -> 幂等 -> 序号变化旧码失效 -> 追溯）
examples/eligibility.sh   # 评审资格停用/启用端到端示例（停用 -> 即时断稿/排除 -> 补位释放 -> 重新启用 -> 变更记录）
examples/institutions.sh  # 机构归并端到端示例（机构归并 -> 普通分配按组冲突 -> 已确认槽位失效 -> 补位双版本发布 -> 归并组查看）
examples/corrections.sh   # 本轮端到端示例（发起更正 -> 访问码失效 -> 评审人更正 -> 快照重发 -> 补位沿用/待更正失效）
examples/locks.sh         # 本轮端到端示例（锁定整表提交/幂等 -> 普通预演保留槽位 -> 失格诊断与发布拒绝 -> 补位不读锁定 -> 清除锁定）
examples/objections.sh    # 本轮端到端示例（作者凭码提交异议 -> 幂等/冲突 -> 会务方受理走更正流程/驳回 -> 查询凭据 -> 序号变化过期与追溯）
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```
