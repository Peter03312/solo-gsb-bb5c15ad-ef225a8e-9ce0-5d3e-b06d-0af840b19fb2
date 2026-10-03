# 临时限行核验服务

面向场地安全员的临时限行区域登记、轨迹穿越核验与安全出发时刻调度服务。
安全员在平面直角坐标系中登记带 UTC 生效时段的简单多边形区域；系统对按时刻
严格递增、相邻点直线匀速运动的连续轨迹做整段核验，返回每个区域的穿越起止
时刻以及总体是否安全；也可以对一条固定行程（相对秒数轨迹）在带时区的候选
出发时段内，精确求出所有不会触碰限行区的出发区间。此外，安全员可以为指定
通行轨迹**签发可取消的通行凭证**：未取消且行程未结束的凭证会约束后续一切
区域变更，任何会使其轨迹不再安全的登记/修订/删除/批量变更都会被整体拒绝。
区域写操作（登记/修订/删除/批量变更）支持携带 **`Idempotency-Key` 幂等键**：
网络超时后安全重试同一请求，不会重复登记、重复推进版本或重复分配区域 ID。
安全员还可以为**相对秒数固定行程**与**带时区候选出发窗口申请择时通行
承诺**：签发时给出当前左闭右开的安全出发区间；未取消且窗口未结束的承诺
会约束后续一切区域变更——变更前窗口内仍有未来安全时刻、变更后一点不剩的
登记/修订/删除/批量变更都会被整体拒绝（409）。安全员可把**尚未取消、窗口
未结束的承诺兑现为一张固定时刻通行凭证**：输入承诺编号与带时区的出发时刻，
系统在与区域变更、承诺取消及凭证签发共用的写事务内重验该时刻晚于受理时刻、
落在原候选窗口与按当前区域集合重算的安全出发区间内，按承诺的相对轨迹生成
绝对时刻轨迹；成功后原承诺按既有取消语义失效、**只生成一张凭证**（区域
revision 不变），并发兑现至多一次成功。安全员还可为**带时区绝对
时刻轨迹登记可取消的安全状态观察**：安全或不安全均可登记，登记返回观察
编号、当前安全结论与区域 revision；单区及批量区域变更成功时，对未取消且
末点未过的观察比较变更前后安全结论，**仅结论翻转**才在**同一事务**留一条
含观察编号、旧/新结论与新 revision 的事件（整批至多一条/观察）；观察
**不阻止**区域变更，也不改变通行凭证与择时承诺的保护。**每次成功的区域集合变更**（单区登记/修订/删除与批量变更）还会在**同一事务**追加一条**只读审计记录**（revision、发生时间、变更类型、涉及区域编号与批量操作摘要），安全员可按全局 revision 游标升序分页读取。此外，安全员可为一条固定行程做**最小区域豁免预演**（只读）：输入相对秒数轨迹、带时区候选出发窗口、可豁免的互异现有区域编号与已读 revision，系统在候选区域的子集中找出使窗口内至少存在安全出发时刻的方案（豁免数最少、最早安全时刻、编号字典序），返回所据 revision、豁免编号、最早安全时刻与完整安全出发区间；预演不改变任何数据。安全员还可在正式提交多区调整前做**批量变更影响预演**（只读）：输入与批量变更相同（预期 revision、按序登记/修订/删除操作、待核验轨迹），基于同一已提交快照报告各轨迹在拟变更完整区域集合下的安全结论与穿越明细、若提交将冲突的有效通行凭证、变更前仍有未来安全时刻而变更后被清空的择时承诺，以及有效观察的结论净翻转（旧/新结论）；拟登记区域以操作下标标识、不分配正式编号，预演不改变任何数据、不占用编号。

## 核验规则

* **轨迹**：至少 2 个点 `(x, y, t)`；时刻必须严格递增（重复/逆序报错且不处理）；
  相邻点之间按**直线匀速**运动。
* **交点插值**：交点时刻通过线段参数插值得到，**不做采样点检查**——即使所有提交
  的采样点都在区域外，只要两点连线穿过区域就会被检出。
* **边界即穿越**：沿边界运动、与顶点单点接触都算穿越；单点接触返回
  `entry_time == exit_time` 的退化区间，并置 `point_contact: true`。
* **区间合并**：同一区域相接（端点相同）或重叠的穿越段合并为一段；
  不同区域分别列出。
* **生效时段左闭右开**：区域仅在 `[valid_from, valid_to)` 内参与核验。
  * 接触时刻恰为 `valid_from`：参与（左端闭）；
  * 接触时刻恰为 `valid_to`：不参与（右端开，此时区域已失效）；
  * 正长度穿越跨过 `valid_to` 时，区间在 `valid_to` 处截断，
    `exit_inclusive=false`。
* **非法输入不保存**：非法多边形（自相交、退化、面积为 0）、非有限坐标
  （NaN/Infinity）、`valid_from >= valid_to` 均返回 422 且不落库。
* **一致性快照**：一次核验在单个 `REPEATABLE READ READ ONLY` 事务中读取区域集合，
  即使核验期间有区域被并发登记/修订/删除，本次核验仍使用同一已提交版本；
  响应中的 `revision` 标明所用集合版本。

## 批量原子变更规则（`POST /api/regions/batch`）

安全员可以把**多处**限行区域的登记/修订/删除放在**一次变更**里提交，并要求
一条或多条指定通行轨迹在变更后的完整区域集合下仍然安全：

* **请求**：`expected_revision`（预期全局 revision）、`operations`（非空，
  元素为 `create` / `update` / `delete`，按数组顺序执行）、`trajectories`
  （至少 1 条带时区绝对时刻轨迹，结构与 `/api/verify` 相同；每条 ≥2 点、
  时刻严格递增）。
* **同一现有区域在一批中只能出现一次**：对同一 `id` 两次 update、
  update 与 delete 混用等都返回 422 `duplicate_region`（create 不占现有 id，
  可任意多个）。
* **对变更后完整集合核验**：未被本批涉及的区域原样保留，删除的区域不参与，
  新建/修订后的区域以新形态参与；沿用直线匀速、**边界接触即穿越**、
  生效时段 **左闭右开** 的既有规则。所有轨迹必须全部安全。
* **原子提交**：仅当 revision 匹配、全部操作合法且全部轨迹安全时才落库；
  已有区域修订沿用 `version = version + 1`，**整批全局 revision 只递增一次**。
* **并发互斥**：批量接口与单个登记/修订/删除共用同一把全局元数据行锁，
  写事务在此串行化；基于过期 revision 的并发提交无法绕过校验，直接返回 409。
* **成功响应**：新 `revision`、逐操作结果（create 带**新区域 ID** 与
  `version=1`，update 带修订后 `version`，delete 回显被删 id 与删除前
  version），以及逐轨迹核验结果（含每条轨迹对每个区域的穿越明细）。
* **失败不落库，原因可区分**：

| 状态码 | error.code | 触发条件 |
| --- | --- | --- |
| 409 | `revision_conflict` | `expected_revision` 与当前 revision 不符（details 带当前值） |
| 404 | `region_not_found` | update/delete 引用了不存在的区域（带操作下标与 id） |
| 422 | `duplicate_region` | 同一现有区域在一批中出现多次 |
| 422 | `invalid_operation` | 单条操作语义非法（如修订后 `valid_from >= valid_to`），带操作下标 |
| 422 | `unsafe_trajectory` | 存在轨迹在变更后集合下穿越；details 列出轨迹下标与穿越起止明细 |
| 409 | `pass_conflict` | 变更后集合将使未取消且未结束的凭证不安全；details 列出冲突凭证 id |
| 409 | `commitment_conflict` | 变更后集合将抹掉未取消且窗口未结束承诺的全部未来安全出发时刻；details 列出冲突承诺 id |
| 422 | `validation_error` / `invalid_polygon` / `invalid_trajectory` | 结构/多边形/轨迹非法（同既有接口） |

## 批量变更影响预演规则（`POST /api/regions/batch/preview`）

安全员在**正式提交多区调整前**，可以先用同一份请求做一次**只读影响预演**：
输入与 `POST /api/regions/batch` 完全相同（`expected_revision`、按序
`operations`、`trajectories`），系统在**同一已提交快照**上评估
「若现在提交会怎样」并给出报告，但**不提交任何变更**。

* **请求**：与批量变更一致——`expected_revision`（预期全局 revision）、
  `operations`（非空，`create`/`update`/`delete`，按数组顺序）、
  `trajectories`（至少 1 条带时区绝对时刻轨迹）。
* **报告内容**（HTTP 200）：
  * `revision`：预演所依据的已提交区域集合版本（与 `expected_revision`
    一致）；`evaluated_at`：本次预演的**基准时刻**——凭证/承诺/观察的
    有效性判定与承诺安全区间裁剪共用该时刻；
  * `trajectories`：每条待核验轨迹在**拟变更完整区域集合**下的安全结论
    `safe` 与逐区域穿越明细（沿用直线匀速、**边界接触即穿越**、生效时段
    **左闭右开**的既有规则；未被本批涉及的区域原样保留，删除的区域不参与）；
  * `pass_conflicts`：若提交将冲突的**有效通行凭证**（未取消且末点时刻
    未过、其轨迹在拟变更集合下不再安全）；
  * `commitment_conflicts`：**变更前仍有未来安全时刻、变更后一点不剩**
    的未取消且窗口未结束承诺（与 `commitment_conflict` 同一判定）；
  * `observation_flips`：未取消且末点时刻未过观察的**结论净翻转**
    （`old_conclusion`/`new_conclusion`，整批前后净效果，与正式提交时
    会记录的事件一一对应）。
* **拟登记区域以操作下标标识**：预演不为 `create` 分配正式区域编号
  （不占用 id 序列）；报告中拟登记区域 `region_id` 为 `null`、
  `operation_index` 为其操作下标，其余区域 `operation_index` 为 `null`。
* **只读保证**：预演在单个 `REPEATABLE READ READ ONLY` 事务中读取同一
  已提交快照（与 `/api/verify` 相同），**不改变**区域、凭证、承诺、
  观察、审计记录与全局 revision，也**不占用区域编号**；预演期间并发的
  区域变更不影响本次计算。
* **错误约定与正式批量一致**：过期 revision → 409 `revision_conflict`
  （details 带当前值）；未知区域 → 404 `region_not_found`；重复区域 →
  422 `duplicate_region`；非法操作 → 422 `invalid_operation`；非法
  轨迹/多边形/结构 → 422 `invalid_trajectory`/`validation_error`。
  与正式提交不同：**轨迹不安全、凭证/承诺冲突不作为错误**，只写入报告。
* **不保证之后可提交**：报告基于出具时刻的快照；正式提交仍须按现有
  保护规则（revision 比对、轨迹核验、凭证/承诺约束）在写事务内重验。

预演报告示例（拟登记区域 D 挡住凭证路径、删除区域 A 之后）：

```json
{
  "revision": 1,
  "evaluated_at": "2026-10-03T04:31:13.853848Z",
  "trajectories": [
    {
      "index": 0,
      "safe": false,
      "results": [
        {"region_id": 2, "operation_index": null, "region_name": "B区", "crossings": []},
        {"region_id": null, "operation_index": 0, "region_name": "D区",
         "crossings": [
           {"entry_time": "2030-06-01T10:00:15Z",
            "exit_time": "2030-06-01T10:00:45Z",
            "entry_inclusive": true, "exit_inclusive": true,
            "point_contact": false}
         ]}
      ]
    }
  ],
  "pass_conflicts": [{"pass_id": 1}],
  "commitment_conflicts": [{"commitment_id": 1}],
  "observation_flips": [
    {"observation_id": 1, "old_conclusion": "safe", "new_conclusion": "unsafe"}
  ]
}
```

```bash
# 用与正式批量相同的请求体预演（只读，可反复调用）
curl -X POST http://localhost:8000/api/regions/batch/preview \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1,
       "operations": [
         {"action":"create","name":"D区",
          "polygon":[[0,120],[100,120],[100,180],[0,180]],
          "valid_from":"2030-01-01T00:00:00Z","valid_to":"2030-12-31T00:00:00Z"},
         {"action":"delete","id":1}],
       "trajectories": [[
         {"x":-50,"y":150,"t":"2030-06-01T10:00:00Z"},
         {"x":150,"y":150,"t":"2030-06-01T10:01:00Z"}]]}'
```

## 通行凭证规则（签发 / 查询 / 取消）

安全员可以为一条指定通行轨迹签发**可取消的通行凭证**；未取消且行程未结束
的凭证会约束后续所有区域写入，保证已放行的轨迹不会因区域变更而变得不安全。

* **签发 `POST /api/passes`**：请求体与 `/api/verify` 相同（`points` 至少
  2 点、带时区绝对时刻、时刻严格递增、相邻点直线匀速），且**首点时刻必须
  晚于签发时刻**（以服务端拿到写锁后的当前时间为准；已开始的轨迹拒绝）。
  签发在持有全局元数据行锁的写事务内完成，按**当前完整区域集合**核验，
  沿用边界接触即违规、生效期左闭右开规则：
  * 安全：持久化并返回 **201**，响应含凭证 `id`、`status`、`revision`
    （签发时核验所用的区域集合版本）、回显轨迹与 `issued_at`；
    签发不推进区域 revision；
  * 不安全：**422** `unsafe_trajectory`，details 列出穿越区域与起止明细，
    不落库；
  * 非法（结构/时刻问题）或已开始：**422** `invalid_trajectory` /
    `validation_error`，不落库。
* **查询 `GET /api/passes/{id}`**：返回凭证（含 `status`：
  `active`/`cancelled`）；未知 ID 返回 **404** `not_found`。
* **取消 `POST /api/passes/{id}/cancel`**：置为 `cancelled` 并返回凭证状态；
  **重复取消幂等**（结果不变，仍为 200）；未知 ID 返回 **404**。
  取消不推进区域 revision。
* **凭证约束区域变更**：**未取消且末点时刻未过**的凭证，在单区登记、修订、
  删除及批量变更**提交前**都会被重新核验——用变更后的完整区域集合检查这些
  凭证的轨迹是否仍然安全；任一凭证不再安全则返回 **409** `pass_conflict`
  （details 逐条列出冲突凭证 `pass_id`），**整笔回滚**，区域 `version` 与
  全局 `revision` 均不变。已取消或末点时刻已过的凭证不再约束。
* **串行化**：签发、取消与所有区域写入（含批量）共用同一把全局元数据行锁，
  完全串行化——不存在"核验通过到落库之间被并发变更插队"的漏检窗口。

## 择时通行承诺规则（签发 / 查询 / 取消）

安全员手里只有一条**固定行程**（相对首点的秒数轨迹）与一个未来的**候选
出发窗口**时，可以申请择时通行承诺：系统按当前完整区域集合算出窗口内
左闭右开的安全出发区间并持久化；只要承诺未取消、窗口未结束，后续任何会
**抹掉其全部未来安全出发时刻**的区域变更都会被整体拒绝。

* **签发 `POST /api/commitments`**：请求体为相对秒数轨迹
  `points`（首点 `t` 必须为 `0`、其余严格递增，语义与 `/api/schedule`
  相同）加带时区候选出发窗口 `window_start` / `window_end`（左闭右开，
  服务端归一化为 UTC）；**窗口起点必须晚于签发时刻**（以服务端拿到写锁
  后的当前时间为准）。签发在持有全局元数据行锁的写事务内完成，按
  **当前完整区域集合**用与 `/api/schedule` 完全相同的精确区间运算
  （接触 `[a,b]` 闭、生效 `[vf,vt)` 左闭右开 → 禁止出发 `[vf-b, vt-a)`）
  求窗口内的安全出发区间：
  * 存在安全区间：持久化并返回 **201**，响应含承诺 `id`、`status`、
    `revision`（签发时所用区域集合版本）、相对秒数轨迹、候选窗口、
    当前 `intervals`（左闭右开安全区间）与 `issued_at`；签发不推进
    区域 revision；
  * 区间为空：**422** `unsafe_trajectory`，**拒签且不保存**；
  * 轨迹/窗口非法（点数不足、坐标或相对时刻非有限、首点非 0、相对时刻
    不严格递增、窗口倒序、窗口不带时区）或窗口起点不晚于当前时刻：
    **422** `invalid_trajectory` / `invalid_input` / `validation_error`，
    不落库。
* **查询 `GET /api/commitments/{id}`**：返回承诺及其**当前可用**安全
  区间——按查询时刻的完整区域集合与当前时刻重算（早于当前时刻的窗口
  部分不再出现；窗口已结束时区间为空）；`status` 仍为 `active/cancelled`，
  `revision` 仍是签发时版本。已取消承诺的 `intervals` 恒为空。
  未知 ID 返回 **404** `not_found`。
* **取消 `POST /api/commitments/{id}/cancel`**：置为 `cancelled` 并返回
  承诺状态（`intervals` 恒为空）；**重复取消幂等**（结果不变，仍 200）；
  未知 ID 返回 **404**。取消不推进区域 revision，取消后承诺不再约束
  任何区域变更。
* **承诺约束区域变更**：**未取消且窗口未结束**（`window_end` 晚于当前
  时刻）的承诺，在单区登记、修订、删除及批量变更**提交前**都会在
  **同一写事务**内用**变更后集合**重算当前时刻之后的安全区间：
  * 变更前窗口内**仍有未来安全时刻**、变更后**一点不剩** → 返回
    **409** `commitment_conflict`（details 逐条列出冲突承诺
    `commitment_id`），**整笔回滚**，区域 `version` 与全局 `revision`
    均不变；
  * 变更前已无未来安全时刻（例如窗口大部分时间已流逝且剩余部分全被
    禁止）的承诺**不再约束**；
  * 只挡住窗口一部分、仍留有安全时刻的变更正常放行，下次查询即可看到
    收缩后的区间。
  * 该检查与通行凭证保护 `pass_conflict` 并列：任一检查失败都整笔回滚。
* **串行化**：承诺签发、取消与签发凭证、所有区域写入（含批量）共用同一把
  全局元数据行锁，完全串行化——不存在"区间算好到落库之间被并发变更
  插队"的漏检窗口。

## 承诺兑现规则（兑现为固定时刻通行凭证）

安全员可把一张**尚未取消、候选窗口未结束**的择时通行承诺，按选定的出发
时刻**兑现**为一张**固定时刻通行凭证**：
`POST /api/commitments/{id}/fulfill`，请求体仅含带时区的
`departure_time`（服务端归一化为 UTC）。

* **兑现前提（在与区域变更、承诺取消及凭证签发共用的写事务内，拿到全局
  元数据行锁后逐一重验）**：
  * 承诺存在且**未取消**；已被普通取消的承诺返回 **422**
    `commitment_cancelled`；
  * 候选**窗口未结束**（`window_end` 晚于服务端受理时刻）；窗口已结束
    返回 **422** `commitment_expired`（过期判定先于出发时刻判定，过期
    承诺不改变状态）；
  * `departure_time` **晚于服务端受理时刻**（以拿到写锁后的当前服务
    时间为准）；不晚于受理时刻返回 **422** `departure_not_in_future`；
  * 出发时刻落在承诺**原候选窗口** `[window_start, window_end)` 内
    （左闭右开：恰为起点可兑现、恰为终点不可）；越界返回 **422**
    `departure_out_of_window`；
  * 出发时刻落在**按当前区域集合重算**的安全出发区间内（与
    `/api/schedule` 完全相同的精确区间运算，左闭右开——注意不是沿用
    签发时快照，区域集合可能已变化）；不安全返回 **422**
    `unsafe_trajectory`，details 回显窗口内当前安全区间。
  * 上述任一失败**均不生成凭证、不改变承诺**（active 仍为 active、
    revision 与区间不变）。
* **绝对时刻轨迹**：兑现按承诺存储的相对秒数轨迹生成绝对时刻轨迹——
  点 `i` 的绝对时刻 = `departure_time + t_i`（首点即出发时刻），坐标
  与相邻点直线匀速语义不变。
* **成功响应（201）**：
  ```json
  {
    "commitment_id": 7,
    "revision": 3,
    "pass": {
      "id": 12,
      "status": "active",
      "revision": 3,
      "trajectory": [
        {"x": -50.0, "y": 50.0, "t": "2030-06-01T10:00:00Z"},
        {"x": 150.0, "y": 50.0, "t": "2030-06-01T10:01:00Z"}
      ],
      "issued_at": "2030-06-01T09:55:00Z",
      "cancelled_at": null
    }
  }
  ```
  * **只生成一张凭证**：`pass` 结构与 `GET /api/passes/{id}` 完全一致，
    即刻受既有 `pass_conflict` 规则保护（此后任何危及该轨迹的区域变更
    都会被 409 整体拒绝；凭证可查询、可取消）；
  * 原承诺**按既有取消语义失效**：`status` 置 `cancelled`、
    `cancelled_at` 置位；`GET /api/commitments/{id}` 响应新增可空字段
    **`fulfilled_pass_id`**（兑现时为所生成凭证编号，普通取消/未取消
    为 `null`），`intervals` 与其它已取消承诺一样恒为空；
  * **区域 revision 不因兑现改变**：响应中的 `revision`（及凭证的
    `revision`）是本次重验所据的当前区域集合版本，不推进版本号、不写
    区域变更审计。
* **未知承诺**：返回 **404** `not_found`。
* **并发语义**：兑现与取消、区域变更、凭证签发在同一把全局元数据行锁上
  串行化。同一承诺的并发兑现**至多一次成功**；后到者得到 **409**
  `commitment_already_fulfilled`（details 带已生成的 `pass_id`），
  与「先被普通取消」的失败（422 `commitment_cancelled`）**原因可区分**。
  兑现与取消并发时：兑现先成功则取消对已失效承诺幂等返回 200（保留
  `fulfilled_pass_id`）；取消先成功则兑现得到 422 `commitment_cancelled`。
* **兼容性**：承诺与凭证的查询、取消响应及区域保护规则保持不变——
  已兑现承诺不再以承诺身份约束区域变更（其安全已由新凭证的
  `pass_conflict` 保护接管）；对已兑现承诺重复调用取消接口仍幂等返回
  200。

```bash
# 兑现：输入承诺编号与带时区出发时刻（可提交 +08:00 等任意时区，服务端归一化）
curl -X POST http://localhost:8000/api/commitments/7/fulfill \
  -H 'Content-Type: application/json' \
  -d '{"departure_time": "2030-06-01T10:00:00Z"}'
# 重复兑现 → 409 commitment_already_fulfilled（details 带 pass_id）
# 普通取消 → 422 commitment_cancelled；窗口已结束 → 422 commitment_expired；
# 不晚于受理时刻 → 422 departure_not_in_future；越窗 → 422 departure_out_of_window；
# 不在当前安全区间 → 422 unsafe_trajectory（details 回显当前安全区间）
```

## 安全状态观察规则（登记 / 查询 / 取消 / 事件）

安全员可以为一条**带时区绝对时刻轨迹**登记**可取消的安全状态观察**：
系统按登记时的完整区域集合给出当前安全结论；此后每次成功的区域变更都会
在同一写事务内复核未结束观察的结论是否翻转，并留下可分页读取的事件流。
观察是**纯通知**：不阻止任何区域变更，也不改变通行凭证与择时承诺的保护。

* **登记 `POST /api/observations`**：请求体与 `/api/verify` 相同
  （`points` 至少 2 点、带时区绝对时刻、时刻严格递增、相邻点直线匀速），
  且**首点时刻必须晚于登记时刻**（以服务端拿到写锁后的当前时间为准）。
  登记在持有全局元数据行锁的写事务内完成，按**当前完整区域集合**核验，
  沿用直线匀速、**边界接触即穿越**、生效期**左闭右开**规则：
  * **安全与不安全均可登记**，均返回 **201**，响应含观察 `id`、`status`
    （`active`）、当前结论 `conclusion`（`safe`/`unsafe`）、该结论对应
    的区域 `revision`、回显轨迹与 `registered_at`；
  * 登记**不推进**区域 revision，也不产生事件；
  * 轨迹非法（点数不足/坐标非有限/时刻不严格递增/不带时区）或首点不晚于
    当前时刻：**422** `invalid_trajectory` / `validation_error`，不落库。
* **查询 `GET /api/observations/{id}`**：返回观察的轨迹、状态
  （`active`/`cancelled`）、**最近结论** `conclusion` 及该结论对应的
  `revision`；未知编号返回 **404** `not_found`。
* **取消 `POST /api/observations/{id}/cancel`**：置为 `cancelled` 并
  返回观察状态；**重复取消幂等**（结果不变，仍为 200）；未知编号 **404**。
  取消不推进区域 revision。
* **区域变更产生翻转事件**：单区登记/修订/删除与**批量变更成功提交时**，
  系统在**同一写事务**内对每张**未取消且末点时刻未过**（末点晚于当前
  时刻）的观察，分别用**变更前**与**变更后**的完整区域集合重新核验：
  * **仅结论翻转**（`safe→unsafe` 或 `unsafe→safe`）才追加一条事件，
    事件含观察编号、观察内事件序号 `seq`（从 1 开始连续递增）、
    **旧结论**、**新结论**与变更后的**新 revision**；同时更新观察的
    最近结论与对应 revision；
  * 结论不变（仍安全或仍不安全）**不产生事件**；批量变更比较的是
    **整批变更前 vs 整批变更后**的净效果，**整批对同一观察至多一条**
    事件，与批内操作数量无关；
  * **已取消或末点时刻已过**的观察不再核验、不再产生事件；
  * 核验完全沿用直线匀速、边界接触即穿越、生效期左闭右开规则；
  * **观察不阻止区域变更**：不存在观察专属的 409；凭证 `pass_conflict`
    与承诺 `commitment_conflict` 的保护不受观察影响；
  * 区域变更失败回滚（422/404/409）时事件与结论更新一并回滚；携带
    `Idempotency-Key` 的变更重试只回放首次响应，**不会重复产生事件**。
* **事件分页 `GET /api/observations/{id}/events`**：按观察编号与
  **事件序号游标升序**读取，查询参数：
  * `cursor`：事件序号游标，只返回 `seq` 严格大于游标的事件；首页传 `0`
    或不传（默认 0，不可为负）；
  * `limit`：每页事件数，1~500，默认 100；
  * 响应含 `observation_id`、`events`（按 `seq` 升序，每条含
    `seq/old_conclusion/new_conclusion/revision/occurred_at`）与
    `next_cursor`（下一页游标，即本页最后一条事件的 `seq`；没有更多
    事件时为 `null`）；观察存在但从未翻转时返回空页（`events: []`、
    `next_cursor: null`），观察编号未知返回 **404**。
* **串行化**：观察登记、取消与所有区域写入（含批量）、凭证/承诺签发共用
  同一把全局元数据行锁，完全串行化——不存在"结论算好到落库之间被并发
  变更插队"的漏检窗口。

```bash
# 登记观察：安全或不安全均可，返回编号、当前结论与 revision
curl -X POST http://localhost:8000/api/observations \
  -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":50,"t":"2026-10-02T12:00:00Z"},
                 {"x":350,"y":50,"t":"2026-10-02T12:05:00Z"}]}'
# 查询观察（轨迹/状态/最近结论/revision）与事件（游标升序分页）
curl http://localhost:8000/api/observations/1
curl 'http://localhost:8000/api/observations/1/events?limit=100&cursor=0'
# 取消（幂等）
curl -X POST http://localhost:8000/api/observations/1/cancel
```


curl -X POST http://localhost:8000/api/commitments \
  -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":50,"t":0},{"x":150,"y":50,"t":60}],
       "window_start":"2026-10-01T12:10:00Z",
       "window_end":"2026-10-01T12:15:00Z"}'
# 查询当前可用区间 / 幂等取消
curl http://localhost:8000/api/commitments/1
curl -X POST http://localhost:8000/api/commitments/1/cancel
```

## 区域集合变更审计历史（只读）

每次**成功提交**的区域写事务都在**同一事务**追加一条审计记录，安全员可
按全局 revision 游标**升序分页只读**（`GET /api/regions/changes`），
用于回看区域集合何时、以何种方式发生过变更：

* **记录范围**：成功的单区**登记**（`create`）、**修订**（`update`）、
  **删除**（`delete`）与**批量变更**（`batch`，整批一行、revision 只算
  一次）。每条含 `revision`（变更后的全局版本号，也是分页游标）、
  `occurred_at`（发生时间，UTC）、`change_type`、`region_ids`（本次涉及
  的区域编号；批量按请求操作顺序，create 为新分配编号）与 `summary`
  （批量操作摘要：逐操作 `index/action/region_id/version` 与
  `total`、`create_count`/`update_count`/`delete_count`；单区变更同样
  返回该结构，恰有一项 `index=0` 的操作，delete 的 `version` 为删除前
  版本）。
* **不留记录的情形**：失败事务（422/404/409，含校验失败、区域不存在、
  revision 冲突、`pass_conflict`、`commitment_conflict`、轨迹不安全等）
  整笔回滚，审计行一并消失；携带 `Idempotency-Key` 的重试只回放首次响应，
  **不新增记录**；通行凭证、择时承诺、安全状态观察的签发与取消**不产生**
  区域变更记录，也不推进 revision。
* **游标分页**：`cursor` 为非负 revision 游标（默认 `0`，可省略），只返回
  `revision` 严格大于游标的记录；`limit` 为每页条数（1~500，默认 100）。
  响应为 `changes` 数组与 `next_cursor`（本页最后一条记录的 revision，
  没有更多时为 `null`）；下一页携带 `?cursor=<next_cursor>` 继续。
  从无变更或游标超出最大 revision 时返回 `"changes": []`、
  `"next_cursor": null`。`cursor<0`、非整数、`limit` 越界（<1 或 >500）
  一律返回 **422** `validation_error`（统一校验错误信封）。
* **一致性**：查询在单个 `REPEATABLE READ READ ONLY` 事务内按 revision
  键集读取；每次成功变更的 revision 单调递增且一行一 revision，因此查询
  期间即使有新变更提交，已取页与后续页也不会重复或遗漏（新提交只会出现在
  后续翻页/重新全量遍历时）。

```bash
# 首页（可省略 cursor）
curl 'http://localhost:8000/api/regions/changes?limit=100&cursor=0'
# 用 next_cursor 翻下一页
curl 'http://localhost:8000/api/regions/changes?limit=100&cursor=3'
```

响应示例：

```json
{
  "changes": [
    {
      "revision": 1,
      "occurred_at": "2026-10-02T11:25:31.555133Z",
      "change_type": "create",
      "region_ids": [1],
      "summary": {
        "operations": [
          {"index": 0, "action": "create", "region_id": 1, "version": 1}
        ],
        "total": 1,
        "create_count": 1,
        "update_count": 0,
        "delete_count": 0
      }
    },
    {
      "revision": 3,
      "occurred_at": "2026-10-02T11:25:31.881347Z",
      "change_type": "batch",
      "region_ids": [2, 1],
      "summary": {
        "operations": [
          {"index": 0, "action": "create", "region_id": 2, "version": 1},
          {"index": 1, "action": "delete", "region_id": 1, "version": 2}
        ],
        "total": 2,
        "create_count": 1,
        "update_count": 0,
        "delete_count": 1
      }
    }
  ],
  "next_cursor": null
}
```

## 幂等重试规则（`Idempotency-Key` 请求头）

安全员在**网络超时**后需要重试区域变更时，可以为以下四个写接口携带
`Idempotency-Key` 请求头，服务端保证同一请求**只实际提交一次**：

* `POST /api/regions`（登记）、`PATCH /api/regions/{id}`（修订）、
  `DELETE /api/regions/{id}`（删除）、`POST /api/regions/batch`（批量变更）。

规则：

* **键的合法性**：非空（纯空白视为空）且最长 128 字符；非法键返回
  **422** `invalid_idempotency_key`，不产生任何变更。
* **全局唯一键空间**：同一键在上述四类写操作间共享；以**操作类型 +
  目标区域 + 规范化请求内容**（时刻归一化为 UTC、多边形归一化为逆时针
  闭合环后的请求体）判定是否同一次请求——JSON 键序、时区写法
  （如 `+08:00` 与 `Z`）、多边形是否显式闭合不影响判定。
* **首次成功提交**：变更结果与键、原 HTTP 状态码、原响应体在**同一事务**
  内落库，不存在"变更已提交但响应未保存"的窗口。
* **相同请求重试**：直接返回**原状态码与原响应体**（逐字节一致）——即使
  期间区域 revision 已被其它变更推进、批量请求的 `expected_revision`
  早已过期；不会再次推进 revision/version，也不会重新分配区域 ID。
  删除重试返回原 200 而非 404。
* **并发同键**：并发到达的同键请求在数据库主键上互斥，全库只有
  **一次实际提交**；其余请求等待后回放同一响应。
* **同键不同请求**：返回 **409** `idempotency_key_conflict`，不变更任何数据
  （包括同键跨操作类型，如登记用过的键再用于删除）。
* **失败的业务请求不占键**：凡是未成功提交的请求（422 校验失败、404 区域
  不存在、409 revision 冲突 / 凭证冲突等），其键随事务回滚一并释放，
  修正请求内容后可用同一键重新提交。
* **无键请求**：不携带该头时保持既有行为，完全不受影响；凭证保护、批量
  原子性与既有错误契约对带键请求同样适用。

```bash
curl -X POST http://localhost:8000/api/regions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7f3c2a-siteA-weld-0001' \
  -d '{"name":"A区","polygon":[[0,0],[100,0],[100,100],[0,100]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
# 超时后原样重发（同键同内容）→ 返回同一 201 响应，不重复登记
```

建议键内携带业务含义与唯一标识（如 `场地-操作-UUID`），由客户端为
**每一次逻辑变更**生成一个新键，重试时沿用同一键。

## 出发时刻调度规则（`POST /api/schedule`）

* **输入**：固定行程用**相对秒数**给出 `{"x","y","t"}`，首点相对秒数必须为 `0`，
  其余时刻严格递增；候选出发时段 `[window_start, window_end)` 带时区，服务端
  归一化为 UTC。相邻点之间仍按直线匀速运动。
* **精确区间运算，不做离散抽样**：对每个区域，先求轨迹与多边形（**含边界**）
  的全部几何接触，得到相对时刻闭区间 `[a, b]`（单点接触时 `a == b`）。
  设出发时刻为 `s`、区域生效窗口为 `[vf, vt)`，则该接触造成违规当且仅当
  `存在 τ∈[a,b] 使 vf ≤ s+τ < vt`，等价于
  **`s ∈ [vf-b, vt-a)`（左闭右开）**。该公式对正长度接触与单点接触统一成立。
* **端点语义准确**：
  * 恰在 `valid_from` 接触 → 对应出发时刻禁止（禁止区间左端闭）；
  * 恰在 `valid_to` 接触 → 对应出发时刻安全（禁止区间右端开）；
  * 沿边界整段运动按闭区间 `[a,b]` 参与计算，边界即违规；
  * 各区域禁止区间取并集（相接或重叠即合并），再从候选窗口（左闭右开）中扣除。
* **输出**：按起点排序的安全出发区间，端点统一为 UTC ISO8601 并带
  `start_inclusive/end_inclusive` 开闭标记（当前规则下恒为左闭右开）；
  无任何可行时刻时返回 `"intervals": []`。响应带 `revision`。
* **拒绝整个请求（422）**：轨迹点少于 2 个；坐标或相对时刻非有限
  （NaN/Infinity）；首点相对秒数不为 0 或相对时刻不严格递增；
  `window_start >= window_end`；候选时段不带时区。
* **一致性快照**：与 `/api/verify` 相同，整个计算在单个
  `REPEATABLE READ READ ONLY` 事务中完成，区域并发修订时本请求使用同一已提交
  快照。

## 最小区域豁免预演规则（`POST /api/exemptions/preview`）

安全员为一条固定行程评估「最少豁免哪些现有区域，候选窗口内才有安全出发
时刻」。预演是**只读评估**：只在本次计算中忽略被选区域，不改变区域、
凭证、承诺、观察、审计记录与 revision，也不影响既有核验与调度结果。

* **输入**：相对秒数轨迹 `points`（首点 `t=0`、其余严格递增，语义与
  `/api/schedule` 相同）、带时区候选出发窗口
  `[window_start, window_end)`、可豁免的互异现有区域编号
  `candidate_region_ids`（空列表表示不允许豁免任何区域）与客户端已读的
  区域 `expected_revision`。
* **计算**：沿用直线匀速、边界接触即违规、生效时段左闭右开的既有规则
  （与 `/api/schedule` 同一精确区间运算），从候选区域的子集中找出使窗口内
  **至少存在一个安全出发时刻**的方案，依次取：
  1. **豁免区域数最少**；
  2. 该方案下**最早安全时刻最早**；
  3. 仍并列时**豁免编号升序序列字典序最小**。
* **输出**：`feasible`、所据 `revision`、豁免编号
  `exempted_region_ids`（升序）、最早安全时刻 `earliest_departure` 与该
  方案的完整安全出发区间 `intervals`（左闭右开）。**原本安全则豁免集为空**
  （`[]`）；豁免全部候选区域仍无安全时刻时返回明确的不可行结果
  （`feasible=false`，豁免编号与最早时刻为 `null`、`intervals` 为空）。
* **拒绝与冲突**：候选编号重复 → 422 `duplicate_region`；引用不存在的
  区域 → 404 `region_not_found`；轨迹/窗口非法 → 422
  `invalid_trajectory`/`invalid_input`/`validation_error`；
  `expected_revision` 与当前 revision 不符 → 409 `revision_conflict`
  （details 带当前值，客户端用新 revision 重新提交即可）。
* **一致性快照**：整个预演在单个 `REPEATABLE READ READ ONLY` 事务中读取
  同一已提交区域快照（与 `/api/verify`、`/api/schedule` 相同），预演期间
  并发的区域变更不影响本次计算。

请求（候选窗口 2030-06-02 10:00~11:00 UTC，候选区域 1、2、3）：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60}
  ],
  "window_start": "2030-06-02T10:00:00Z",
  "window_end":   "2030-06-02T11:00:00Z",
  "candidate_region_ids": [1, 2, 3],
  "expected_revision": 3
}
```

可行响应（HTTP 200）：豁免区域 1、3 后窗口前半段 `[10:00,10:30)` 安全：

```json
{
  "feasible": true,
  "revision": 3,
  "window_start": "2030-06-02T10:00:00Z",
  "window_end":   "2030-06-02T11:00:00Z",
  "exempted_region_ids": [1, 3],
  "earliest_departure": "2030-06-02T10:00:00Z",
  "intervals": [
    {
      "start": "2030-06-02T10:00:00Z",
      "end":   "2030-06-02T10:30:00Z",
      "start_inclusive": true,
      "end_inclusive": false
    }
  ]
}
```

不可行响应（HTTP 200，豁免全部候选区域仍无安全时刻）：

```json
{
  "feasible": false,
  "revision": 3,
  "window_start": "2030-06-02T10:00:00Z",
  "window_end":   "2030-06-02T11:00:00Z",
  "exempted_region_ids": null,
  "earliest_departure": null,
  "intervals": []
}
```

## 技术栈

Python 3.11 · FastAPI · PostgreSQL 16 · Shapely 2（GEOS）· psycopg3 · Uvicorn

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根路径（端点列表） |
| http://localhost:8000/docs | Swagger UI（可在线调用） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查 |
| PostgreSQL | localhost:5432（用户/库/密码均为 restrict） |

首次启动时应用会自动建表（`regions`、`region_meta`、`passes`、
`commitments`（含兑现标记列 `fulfilled_at`/`fulfilled_pass_id`）、
`observations`、`observation_events`、
`idempotency_keys`、`region_change_audit`）并初始化全局版本号，无需手工执行 SQL；
既有库再次启动会自动补齐新增列（不改变任何现有行）。数据库未就绪时应用会
自动重试连接（默认 30 次、间隔 1 秒）。

停止并清理数据卷：

```bash
docker compose down -v
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://restrict:restrict@localhost:5432/restrict` | PostgreSQL DSN |
| `API_PORT` | `8000` | Compose 映射到宿主机的 API 端口 |
| `POSTGRES_PORT` | `5432` | Compose 映射到宿主机的数据库端口 |
| `DB_CONNECT_RETRIES` | `30` | 启动时连接数据库的重试次数 |
| `DB_CONNECT_INTERVAL` | `1.0` | 重试间隔（秒） |

例如改用 18000 端口：`API_PORT=18000 docker compose up --build`。

## 本地（无 Docker）运行

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://restrict:restrict@localhost:5432/restrict"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## HTTP API

时间字段一律使用带时区的 ISO8601，建议统一 `...Z`（UTC）。
多边形顶点按 `[[x, y], ...]` 提交，首尾点可显式闭合也可不闭合；
服务端校验为简单多边形后归一化为逆时针闭合环存储。

### 区域管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/regions` | 登记区域（201）；支持 `Idempotency-Key` 幂等重试 |
| GET | `/api/regions` | 列出全部区域 |
| GET | `/api/regions/{id}` | 查询单个区域 |
| PATCH | `/api/regions/{id}` | 修订区域（字段任意组合，部分更新）；`version` +1；支持幂等键 |
| DELETE | `/api/regions/{id}` | 删除区域；支持幂等键 |
| POST | `/api/regions/batch` | 多处区域登记/修订/删除一次原子提交，并核验指定轨迹全部安全；支持幂等键 |
| POST | `/api/regions/batch/preview` | 批量变更影响预演（只读）：报告轨迹结论与穿越明细、将冲突凭证、将被清空承诺、观察净翻转 |
| GET | `/api/regions/changes` | 区域集合变更只读审计历史：按全局 revision 游标升序分页 |
| GET | `/api/revision` | 当前区域集合全局版本号（任意写操作后单调 +1） |
| POST | `/api/verify` | 对一条绝对时刻轨迹做整段穿越核验 |
| POST | `/api/schedule` | 对一条相对秒数固定行程，求候选时段内全部安全出发区间 |
| POST | `/api/exemptions/preview` | 最小区域豁免预演（只读）：求使窗口内存在安全出发时刻的最优豁免方案 |
| POST | `/api/passes` | 为一条绝对时刻轨迹签发可取消的通行凭证（201） |
| GET | `/api/passes/{id}` | 按 ID 查询凭证（含状态） |
| POST | `/api/passes/{id}/cancel` | 取消凭证（幂等），返回状态 |
| POST | `/api/commitments` | 为相对秒数固定行程+候选窗口签发择时通行承诺（201） |
| GET | `/api/commitments/{id}` | 按 ID 查询承诺与当前可用安全出发区间（已兑现时 `fulfilled_pass_id` 指向凭证） |
| POST | `/api/commitments/{id}/cancel` | 取消承诺（幂等），返回状态 |
| POST | `/api/commitments/{id}/fulfill` | 把未取消且窗口未结束的承诺兑现为一张固定时刻通行凭证（201） |
| POST | `/api/observations` | 为带时区绝对时刻轨迹登记安全状态观察（201；安全/不安全均可） |
| GET | `/api/observations/{id}` | 按 ID 查询观察（轨迹/状态/最近结论/revision） |
| POST | `/api/observations/{id}/cancel` | 取消观察（幂等），返回状态 |
| GET | `/api/observations/{id}/events` | 按事件序号游标升序分页读取结论翻转事件 |

登记示例：

```bash
curl -X POST http://localhost:8000/api/regions \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }'
```

### 轨迹核验 `POST /api/verify`

请求：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 150, "y": 50, "t": "2026-09-30T00:01:00Z"}
  ]
}
```

响应（该轨迹在 00:00:15 进入、00:00:45 离开 0~100 的区域，时刻由插值得到）：

```json
{
  "safe": false,
  "revision": 1,
  "checked_at": "2026-09-30T03:40:00.000000Z",
  "results": [
    {
      "region_id": 1,
      "region_name": "A区-焊接作业面",
      "crossings": [
        {
          "entry_time": "2026-09-30T00:00:15Z",
          "exit_time": "2026-09-30T00:00:45Z",
          "entry_inclusive": true,
          "exit_inclusive": true,
          "point_contact": false
        }
      ]
    }
  ]
}
```

* `safe=true` 当且仅当所有区域的 `crossings` 均为空。
* `entry_inclusive/exit_inclusive` 标明端点开闭；穿越被生效窗口右端截断时
  `exit_inclusive=false`。
* 无穿越的区域也会出现在 `results` 中（`crossings: []`），保证逐区域可核对。

### 批量原子变更 `POST /api/regions/batch`

把多处区域变更与"这些变更后轨迹必须安全"的前置条件合并为一次原子提交。
操作按数组顺序在内存中构造**变更后完整区域集合**，再对每条轨迹核验；
全部通过后才一次性写入，全局 revision 只推进一次。

请求（`update`/`delete` 用 `id` 引用现有区域；`update` 字段任意组合的部分更新）：

```json
{
  "expected_revision": 2,
  "operations": [
    {
      "action": "update",
      "id": 1,
      "name": "A区-缩小",
      "polygon": [[0, 0], [50, 0], [50, 50], [0, 50]]
    },
    {
      "action": "delete",
      "id": 2
    },
    {
      "action": "create",
      "name": "C区-新作业面",
      "polygon": [[400, 0], [500, 0], [500, 100], [400, 100]],
      "valid_from": "2026-10-01T00:00:00Z",
      "valid_to":   "2026-11-01T00:00:00Z"
    }
  ],
  "trajectories": [
    [
      {"x": -50, "y": 200, "t": "2026-10-02T00:00:00Z"},
      {"x": 900, "y": 200, "t": "2026-10-02T00:02:00Z"}
    ]
  ]
}
```

成功响应（HTTP 200）：

```json
{
  "revision": 3,
  "operations": [
    {"index": 0, "action": "update", "region_id": 1, "version": 3},
    {"index": 1, "action": "delete", "region_id": 2, "version": 4},
    {"index": 2, "action": "create", "region_id": 5, "version": 1}
  ],
  "trajectories": [
    {
      "index": 0,
      "safe": true,
      "results": [
        {"region_id": 1, "region_name": "A区-缩小", "crossings": []},
        {"region_id": 5, "region_name": "C区-新作业面", "crossings": []}
      ]
    }
  ]
}
```

* `operations[].index` 回显操作在请求数组中的下标；create 返回新分配的
  `region_id`；update 的 `version` 是修订后的新版本（原版本 +1）；
  delete 回显删除前 version。
* `trajectories[].results` 与 `/api/verify` 的逐区域结构一致（包含边界
  点接触、被 `valid_to` 截断等开闭标记），但区域集合是**变更后**的集合。

不安全时（HTTP 422，整批取消、不留任何痕迹）只列出发生穿越的轨迹与区域：

```json
{
  "error": {
    "code": "unsafe_trajectory",
    "message": "存在轨迹在变更后区域集合下发生穿越，整批变更已取消且不落库",
    "details": [
      {
        "trajectory_index": 0,
        "results": [
          {
            "region_id": 5,
            "region_name": "C区-新作业面",
            "crossings": [
              {
                "entry_time": "2026-10-02T00:01:10Z",
                "exit_time": "2026-10-02T00:01:30Z",
                "entry_inclusive": true,
                "exit_inclusive": true,
                "point_contact": false
              }
            ]
          }
        ]
      }
    ]
  }
}
```

* revision 过期（期间有其他登记/修订/删除已提交）返回 **409**
  `revision_conflict`，`details[0].current_revision` 给出当前值；
  客户端用新 revision 重新提交即可（乐观并发）。
* 引用不存在区域返回 **404** `region_not_found`；同一区域在一批中出现
  多次返回 **422** `duplicate_region`；修订后时段非法等返回 **422**
  `invalid_operation`（均带操作下标）。上述任一失败整批不落库、
  revision 不变。
* 若变更后集合会使某张未取消且未结束的通行凭证不再安全，返回 **409**
  `pass_conflict`（见下节），整批回滚。
* 正式提交前可用 `POST /api/regions/batch/preview` 做**只读影响预演**
  （见「批量变更影响预演规则」）：同一请求体、同一套校验与核验规则，
  报告各轨迹结论、将冲突的凭证/承诺与观察净翻转，但不落库、不占编号。

### 通行凭证 `POST /api/passes`

为指定通行轨迹签发可取消的凭证。请求体与 `/api/verify` 相同，但首点时刻
必须晚于签发时刻（已开始的轨迹拒绝）：

```json
{
  "points": [
    {"x": -50, "y": 200, "t": "2026-10-01T12:00:00Z"},
    {"x": 350, "y": 200, "t": "2026-10-01T12:05:00Z"}
  ]
}
```

安全时返回 **201**（`revision` 为签发时核验所用的区域集合版本）：

```json
{
  "id": 1,
  "status": "active",
  "revision": 3,
  "trajectory": [
    {"x": -50, "y": 200, "t": "2026-10-01T12:00:00Z"},
    {"x": 350, "y": 200, "t": "2026-10-01T12:05:00Z"}
  ],
  "issued_at": "2026-10-01T11:55:00Z",
  "cancelled_at": null
}
```

不安全时返回 **422** `unsafe_trajectory`，details 直接列出有穿越的区域
（结构与 `/api/verify` 的 `results` 相同，只含违规区域），凭证不落库：

```json
{
  "error": {
    "code": "unsafe_trajectory",
    "message": "轨迹在当前区域集合下发生穿越，凭证未签发且不落库",
    "details": [
      {
        "region_id": 1,
        "region_name": "A区-焊接作业面",
        "crossings": [
          {
            "entry_time": "2026-10-01T12:00:15Z",
            "exit_time": "2026-10-01T12:00:45Z",
            "entry_inclusive": true,
            "exit_inclusive": true,
            "point_contact": false
          }
        ]
      }
    ]
  }
}
```

查询与取消：

```bash
curl http://localhost:8000/api/passes/1          # 查询，未知 ID 返回 404
curl -X POST http://localhost:8000/api/passes/1/cancel   # 取消，幂等
```

取消成功返回同一凭证结构，`status` 变为 `cancelled` 并带 `cancelled_at`；
对已取消的凭证再次取消返回相同结果（幂等），未知 ID 返回 404。

凭证约束区域变更的冲突响应（单区登记/修订/删除与批量变更一致）为
**409** `pass_conflict`，details 逐条列出冲突凭证，整笔回滚：

```json
{
  "error": {
    "code": "pass_conflict",
    "message": "变更后区域集合与 1 张未取消通行凭证冲突，整笔变更已回滚",
    "details": [
      {"pass_id": 1, "message": "凭证 1（未取消且末点时刻未过）的轨迹在变更后区域集合下不再安全"}
    ]
  }
}
```

### 择时通行承诺 `POST /api/commitments`

为相对秒数固定行程与带时区候选出发窗口签发承诺。请求与 `/api/schedule`
相同（首点相对秒数为 `0`、其余严格递增），另要求**窗口起点晚于签发时刻**：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60}
  ],
  "window_start": "2026-10-01T12:10:00Z",
  "window_end":   "2026-10-01T12:15:00Z"
}
```

安全（窗口内存在安全出发时刻）时返回 **201**，`intervals` 为按当前完整
区域集合算出的左闭右开安全出发区间（结构与 `/api/schedule` 一致）：

```json
{
  "id": 1,
  "status": "active",
  "revision": 3,
  "trajectory": [
    {"x": -50.0, "y": 50.0, "t": 0.0},
    {"x": 150.0, "y": 50.0, "t": 60.0}
  ],
  "window_start": "2026-10-01T12:10:00Z",
  "window_end":   "2026-10-01T12:15:00Z",
  "intervals": [
    {
      "start": "2026-10-01T12:10:00Z",
      "end":   "2026-10-01T12:12:30Z",
      "start_inclusive": true,
      "end_inclusive": false
    }
  ],
  "issued_at": "2026-10-01T11:55:00Z",
  "cancelled_at": null
}
```

窗口内当前没有任何安全出发时刻时返回 **422** `unsafe_trajectory`，
承诺不落库；窗口起点不晚于签发时刻、相对轨迹/窗口非法时返回 **422**
`invalid_trajectory` / `invalid_input`。

查询与取消：

```bash
curl http://localhost:8000/api/commitments/1                 # 当前可用区间；未知 ID 404
curl -X POST http://localhost:8000/api/commitments/1/cancel  # 取消，幂等
```

`GET` 返回的 `intervals` 是**查询时刻**的可用区间（按当前区域集合与
当前时刻重算；已流逝的窗口部分与已结束窗口返回空区间）；取消响应的
`intervals` 恒为空。

区域变更抹掉承诺窗口结束前全部安全时刻的冲突响应（单区登记/修订/删除
与批量变更一致）为 **409** `commitment_conflict`，details 逐条列出冲突
承诺，整笔回滚：

```json
{
  "error": {
    "code": "commitment_conflict",
    "message": "变更后区域集合与 1 张未取消择时通行承诺冲突，其窗口结束前将不再有任何安全出发时刻，整笔变更已回滚",
    "details": [
      {"commitment_id": 1, "message": "承诺 1（未取消且窗口未结束）在变更前仍有未来安全出发时刻，变更后窗口结束前将不再有任何安全出发时刻"}
    ]
  }
}
```

### 承诺兑现 `POST /api/commitments/{id}/fulfill`

把一张尚未取消、窗口未结束的承诺按选定的带时区出发时刻兑现为一张固定
时刻通行凭证。请求体只有 `departure_time`（可提交任意带时区 ISO8601，
服务端归一化为 UTC）：

```json
{"departure_time": "2030-06-01T10:00:00Z"}
```

成功返回 **201**：承诺编号、所据区域 `revision` 与一张凭证
（结构同 `GET /api/passes/{id}`）；原承诺按取消语义失效（`GET` 承诺时
`status=cancelled`、`fulfilled_pass_id` 为该凭证编号、`intervals` 为空），
区域 revision 不推进、不写变更审计：

```json
{
  "commitment_id": 7,
  "revision": 3,
  "pass": {
    "id": 12,
    "status": "active",
    "revision": 3,
    "trajectory": [
      {"x": -50.0, "y": 50.0, "t": "2030-06-01T10:00:00Z"},
      {"x": 150.0, "y": 50.0, "t": "2030-06-01T10:01:00Z"}
    ],
    "issued_at": "2030-06-01T09:55:00Z",
    "cancelled_at": null
  }
}
```

出发时刻须：晚于服务端受理时刻；落在原候选窗口 `[window_start,
window_end)`（左闭右开）；落在按**当前**区域集合重算的安全出发区间内
（绝对时刻轨迹由 `departure_time + 相对秒数` 生成）。失败不给凭证且不
改变承诺：

| 状态码 | error.code | 触发条件 |
| --- | --- | --- |
| 404 | `not_found` | 未知承诺编号 |
| 409 | `commitment_already_fulfilled` | 承诺已被兑现（details 带已生成的 `pass_id`，并发重复兑现归此） |
| 422 | `commitment_cancelled` | 承诺已被普通取消 |
| 422 | `commitment_expired` | 候选窗口已结束（`window_end` 不晚于受理时刻） |
| 422 | `departure_not_in_future` | 出发时刻不晚于服务端受理时刻 |
| 422 | `departure_out_of_window` | 出发时刻越出原候选窗口 |
| 422 | `unsafe_trajectory` | 出发时刻不在当前安全出发区间（details 回显当前安全区间） |
| 422 | `validation_error` | 请求结构非法（如出发时刻不带时区、多余字段） |

并发兑现/取消在同一把全局行锁上串行化：同一承诺至多一次兑现成功，失败
方可用 `commitment_already_fulfilled`（409）与 `commitment_cancelled`
（422）区分终态原因。

```bash
curl -X POST http://localhost:8000/api/commitments/7/fulfill \
  -H 'Content-Type: application/json' \
  -d '{"departure_time": "2030-06-01T18:00:00+08:00"}'
```

### 安全状态观察 `POST /api/observations`

为带时区绝对时刻轨迹登记可取消的安全状态观察。请求体与 `/api/verify`
相同，但首点时刻必须晚于登记时刻（已开始的轨迹拒绝）；**安全或不安全
均可登记**：

```json
{
  "points": [
    {"x": -50, "y": 200, "t": "2026-10-02T12:00:00Z"},
    {"x": 350, "y": 200, "t": "2026-10-02T12:05:00Z"}
  ]
}
```

返回 **201**（`revision` 为登记时结论对应的区域集合版本；登记不推进
revision）：

```json
{
  "id": 1,
  "status": "active",
  "trajectory": [
    {"x": -50.0, "y": 200.0, "t": "2026-10-02T12:00:00Z"},
    {"x": 350.0, "y": 200.0, "t": "2026-10-02T12:05:00Z"}
  ],
  "conclusion": "safe",
  "revision": 3,
  "registered_at": "2026-10-01T11:55:00Z",
  "cancelled_at": null
}
```

轨迹直穿区域时同样返回 **201**，只是 `conclusion` 为 `"unsafe"`；
轨迹非法或首点不晚于当前时刻返回 **422** `invalid_trajectory`，不落库。

查询与取消（未知编号 404，重复取消幂等）：

```bash
curl http://localhost:8000/api/observations/1
curl -X POST http://localhost:8000/api/observations/1/cancel
```

区域变更成功后，观察的最近结论 `conclusion` 与对应 `revision` 会随翻转
事件更新（见下）；已取消或末点时刻已过的观察不再更新。

### 观察事件分页 `GET /api/observations/{id}/events`

单区登记/修订/删除与批量变更**成功提交**时，未取消且末点时刻未过的观察
若变更前后安全结论翻转，会在**同一事务**追加一条事件。按观察编号与事件
序号游标**升序分页**读取（`cursor` 默认 0、只取 `seq > cursor`；
`limit` 默认 100、范围 1~500；`next_cursor` 为下一页游标，末页为
`null`）：

```bash
curl 'http://localhost:8000/api/observations/1/events?limit=100&cursor=0'
```

```json
{
  "observation_id": 1,
  "events": [
    {
      "seq": 1,
      "old_conclusion": "safe",
      "new_conclusion": "unsafe",
      "revision": 4,
      "occurred_at": "2026-10-01T12:00:00Z"
    }
  ],
  "next_cursor": 1
}
```

* 批量变更比较整批变更前后的净效果，**整批对同一观察至多一条事件**；
  结论不变不产生事件；区域变更失败回滚或幂等重试回放不会重复产生事件。
* 观察存在但从未翻转时返回空页（非 404）；观察编号未知返回 **404**
  `not_found`；`cursor<0`、`limit` 越界返回 **422**。
* 观察不阻止区域变更（没有观察专属冲突），也不影响 `pass_conflict` /
  `commitment_conflict` 的既有保护。

### 出发时刻调度 `POST /api/schedule`

请求：固定行程用相对首点的秒数（首点 `t=0`、严格递增），候选时段带时区：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60}
  ],
  "window_start": "2026-08-31T23:58:00Z",
  "window_end":   "2026-09-01T00:02:00Z"
}
```

假设某区域为 `[0,100]×[0,100]`、仅在 `2026-09-01T00:00:00Z` 至
`2026-09-01T00:01:00Z` 生效，则轨迹在相对 `15s~45s` 与区域接触（含边界），
禁止出发区间为 `[vf-45s, vt-15s) = [2026-08-31T23:59:15Z, 2026-09-01T00:00:45Z)`，
候选窗口被切成两段安全区间：

```json
{
  "revision": 1,
  "window_start": "2026-08-31T23:58:00Z",
  "window_end": "2026-09-01T00:02:00Z",
  "intervals": [
    {
      "start": "2026-08-31T23:58:00Z",
      "end": "2026-08-31T23:59:15Z",
      "start_inclusive": true,
      "end_inclusive": false
    },
    {
      "start": "2026-09-01T00:00:45Z",
      "end": "2026-09-01T00:02:00Z",
      "start_inclusive": true,
      "end_inclusive": false
    }
  ]
}
```

* 区间端点为 UTC，恒为左闭右开；`intervals` 为空表示候选时段内无安全出发时刻。
* 时区处理：`window_start/window_end` 可提交任意带时区的 ISO8601（如
  `2026-09-01T08:00:00+08:00`），服务端归一化为 UTC；不带时区返回 422。
* 几何接触为单点时（如擦过顶点），接触区间退化为 `[a,a]`，禁止区间长度恰为
  区域生效窗口长度；接触恰落在 `valid_to` 的那次出发安全（右端开），
  落在 `valid_from` 的那次出发禁止（左端闭）。

### 错误响应

统一使用 `422`（语义错误）、`404`（区域/凭证不存在）或 `409`（批量提交的
revision 冲突、区域变更与未取消凭证冲突、幂等键冲突）。除通用的
`validation_error` 外，
轨迹类错误使用专用错误码：`/api/verify` 与 `/api/passes` 为
`invalid_trajectory`，`/api/schedule` 的相对轨迹问题同样返回
`invalid_trajectory`，候选时段不递增返回 `invalid_input`；
`/api/regions/batch` 另用 `revision_conflict`（409）、`region_not_found`（404）、
`duplicate_region`、`invalid_operation`、`unsafe_trajectory`（均 422）；
`/api/regions/batch/preview` 沿用批量变更的同一组错误码（轨迹不安全与
凭证/承诺冲突在预演中不作为错误，只写入报告）；
凭证相关另用 `pass_conflict`（409，区域变更危及未取消凭证）与
`not_found`（404，未知凭证 ID）；
择时承诺另用 `commitment_conflict`（409，区域变更抹掉未结束承诺窗口内的
全部安全出发时刻）与 `not_found`（404，未知承诺 ID）；
承诺兑现另用 `commitment_already_fulfilled`（409，承诺已被兑现，details
带已生成的 `pass_id`）、`commitment_cancelled`/`commitment_expired`
（422，已普通取消/窗口已结束）、`departure_not_in_future`/
`departure_out_of_window`（422，出发时刻不晚于受理时刻/越出候选窗口），
兑现时刻不在当前安全出发区间时复用 `unsafe_trajectory`（422）；
安全状态观察另用 `not_found`（404，未知观察编号；观察登记复用
`invalid_trajectory`）；
最小区域豁免预演另用 `duplicate_region`（422，候选编号重复）、
`region_not_found`（404，候选编号引用不存在的区域）与
`revision_conflict`（409，`expected_revision` 已过期）；
幂等键相关另用 `invalid_idempotency_key`（422，键为空或超过 128 字符）与
`idempotency_key_conflict`（409，同键被内容不同的请求复用）：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [{"loc": ["body", "polygon"], "message": "...", "type": "..."}]
  }
}
```

## 调用示例

```bash
# 端到端 curl 示例：安全出发区间（含单点接触/生效端点）、登记区域、
# 核验穿越、修订与删除
bash examples/curl_examples.sh

# 批量原子变更（登记/修订/删除 + 轨迹核验；含不安全拒绝、revision 冲突重放）
bash examples/curl_batch.sh

# 通行凭证（签发/查询/取消；含 409 pass_conflict 约束、幂等取消、
# 不安全与已开始轨迹拒签）
bash examples/curl_passes.sh

# 择时通行承诺（签发/查询当前区间/取消；含 409 commitment_conflict 约束、
# 幂等取消、无安全时刻与窗口起点已过拒签）
bash examples/curl_commitments.sh

# 承诺兑现（未取消且窗口未结束 → 固定时刻凭证；含越界/过期/非未来/不安全/
# 已取消/未知契约、重复兑现 409 与已取消 422 的区分、revision 不推进）
bash examples/curl_commitment_fulfill.sh

# 安全状态观察（登记/查询/取消；安全与不安全均可登记；区域变更后的
# 结论翻转事件、事件游标分页、取消幂等与错误契约）
bash examples/curl_observations.sh

# 幂等重试（Idempotency-Key：同键重试回放原响应、同键不同内容 409、
# 非法键 422、失败不占键、带键删除/批量重试）
bash examples/curl_idempotency.sh

# 区域集合变更只读审计历史（登记/修订/删除/批量各一条；失败与幂等重试
# 不留记录；revision 游标升序分页、next_cursor、游标非法 422）
bash examples/curl_region_changes.sh

# 最小区域豁免预演（只读：原本安全豁免集为空、最少豁免数、最早安全时刻、
# 明确不可行结果、重复/未知编号与 revision 过期契约、预演无副作用）
bash examples/curl_exemptions.sh

# 批量变更影响预演（只读：轨迹结论与穿越明细、拟登记区域以操作下标标识、
# 凭证冲突/承诺清空/观察净翻转报告、错误契约、预演无副作用、与正式提交对照）
bash examples/curl_batch_preview.sh

# 仅用 Python 标准库的调用示例（含调度两段安全区间与通行凭证演示）
python3 examples/python_client.py
```

## 测试

```bash
# 纯几何引擎：穿越核验用例，不需要数据库
python3 tests/test_geometry.py

# 纯几何引擎：安全出发区间调度用例（精确端点、单点接触、沿边界、
# 生效窗口端点、多区域并集等），不需要数据库
python3 tests/test_schedule_geometry.py

# 纯几何引擎：最小区域豁免预演用例（原本安全、最少豁免数、最早安全时刻、
# 不可行、端点开闭、与调度引擎一致），不需要数据库
python3 tests/test_exemption_geometry.py

# HTTP + PostgreSQL 集成测试（需要可连接的数据库，会清空 regions 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_api.py

# 批量原子变更 API 集成测试（成功/各种拒绝原因/左闭右开端点/并发竞争，
# 同样会清空 regions 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_batch_api.py

# 通行凭证 API 集成测试（签发/查询/取消、凭证约束区域变更、过期放行、
# 签发与区域写入并发串行化，会清空 regions/passes 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_passes_api.py

# 择时通行承诺 API 集成测试（签发/查询/取消、承诺约束区域变更的
# 「变更前有、变更后没有」语义、窗口结束放行、左闭右开端点、
# 签发与区域写入并发串行化，会清空 regions/commitments 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_commitments_api.py

# 承诺兑现 API 集成测试（窗口/安全区间/受理时刻重验、相对轨迹转绝对轨迹、
# 成功只生成一张凭证且 revision 不变、未知/已取消/过期/越界/非未来/不安全
# 不给凭证且不改变承诺、重复兑现 409 与普通取消 422 可区分、兑现与取消/
# 区域变更并发串行化、兑现凭证即刻受 pass_conflict 保护，会清空全部业务表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_commitment_fulfill_api.py

# 幂等键 API 集成测试（同键重试回放原响应、并发同键只提交一次、
# 同键不同内容 409、非法键 422、失败不占键，会清空 regions/passes/idempotency_keys 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_idempotency_api.py

# 安全状态观察 API 集成测试（安全/不安全均可登记、单区与批量翻转事件、
# 整批至多一条/观察、失败回滚与幂等回放不重复产生事件、取消与过期不产生
# 事件、事件游标分页、观察不影响凭证/承诺保护，会清空全部业务表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_observations_api.py

# 区域集合变更审计历史 API 集成测试（四类变更各一条同事务记录、字段与
# 批量摘要正确、失败事务/幂等重试/凭证承诺观察签发取消均不留记录、
# 游标分页不重不漏、游标非法 422、并发写与查询期间新增不重不漏，
# 会清空全部业务表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_region_changes_api.py

# 最小区域豁免预演 API 集成测试（原本安全豁免集为空、最少豁免数与最早
# 安全时刻择优、明确不可行结果、重复/未知编号 422/404、revision 过期 409、
# 轨迹窗口非法 422、预演不改变区域/审计/凭证/承诺/观察与 revision、
# 既有核验与调度不受影响，会清空全部业务表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_exemptions_api.py

# 批量变更影响预演 API 集成测试（轨迹结论与穿越明细、拟登记区域以操作下标
# 标识不占用编号、凭证冲突/承诺清空/观察净翻转报告、与批量变更一致的错误
# 契约、预演全程无副作用、预演结论与正式提交保护规则一致，会清空全部业务表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_batch_preview_api.py
```

## 项目结构

```
app/
  main.py        # FastAPI 路由、事务边界、错误处理（verify、schedule、最小区域豁免预演、batch、批量变更影响预演、passes、commitments（含承诺兑现为固定时刻凭证）、observations、区域变更审计历史、幂等键）
  schemas.py     # Pydantic 请求/响应模型与输入校验（含批量操作判别联合、批量变更影响预演报告模型、承诺兑现请求/响应模型、观察与事件分页模型、区域变更审计分页模型、豁免预演模型）
  geometry.py    # 穿越核验、安全出发区间与最小区域豁免预演引擎（插值、裁剪、合并、区间差、等覆盖基本段枚举）
  repository.py  # 区域 CRUD、一致性快照读取、批量原子变更事务、批量变更只读影响预演、通行凭证/择时承诺（含承诺兑现事务）/安全状态观察签发与取消、区域写入前的凭证与承诺保护、写入成功后的观察结论翻转事件与区域变更审计追加、幂等键占位/回放
  db.py          # psycopg3 连接池、建表初始化（regions/region_meta/passes/commitments（含兑现标记列 fulfilled_at/fulfilled_pass_id）/observations/observation_events/idempotency_keys/region_change_audit；既有库自动补列）、启动重试
examples/        # curl 与 Python 调用示例（含批量原子变更、批量变更影响预演、通行凭证、择时承诺及其兑现、安全状态观察、区域集合变更审计历史、最小区域豁免预演与幂等重试）
tests/           # 几何离线测试与 API/批量变更/批量变更影响预演/通行凭证/择时承诺/承诺兑现/安全状态观察/区域变更审计历史/最小区域豁免预演/幂等键集成测试
Dockerfile, docker-compose.yml
```
