#!/usr/bin/env bash
# 批量变更影响预演调用示例（只读评估，不产生任何副作用）。
#
# 预演输入与 POST /api/regions/batch 相同（expected_revision、按序
# operations、trajectories），在同一已提交快照上评估「若现在提交会怎样」：
# 各轨迹在拟变更完整区域集合下的安全结论与穿越明细、若提交将冲突的有效
# 通行凭证、变更前仍有未来安全时刻而变更后被清空的择时承诺、有效观察的
# 结论净翻转（旧/新结论）。拟登记区域以操作下标标识，不分配正式编号；
# 预演不改变区域、凭证、承诺、观察、审计与版本，也不占用编号。
#
# 演示：
#   1. 登记区域 A、B；签发凭证（y=150 路径）、择时承诺与安全观察
#   2. 无害批次预演：全安全、无冲突、无翻转
#   3. 影响预演：create D（挡住 y=150 路径）+ delete A →
#      轨迹穿越明细（拟登记区域以操作下标标识）、凭证冲突、承诺被清空、
#      观察结论 safe→unsafe / unsafe→safe 净翻转
#   4. 错误契约：revision 过期 409、重复区域 422、未知区域 404、
#      非法操作 422、非法轨迹 422
#   5. 预演无副作用：revision、区域、审计、观察结论均不变，编号不被占用
#   6. 与正式提交对照：同一批正式提交被 pass_conflict 拒绝（与预演一致）
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_batch_preview.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

VF="2030-01-01T00:00:00Z"; VT="2030-12-31T00:00:00Z"
# 各高度的水平直航轨迹（2030-06-01 10:00 出发，60 秒飞 200 单位）
SAFE_TRAJ='[{"x":-50,"y":500,"t":"2030-06-01T10:00:00Z"},{"x":150,"y":500,"t":"2030-06-01T10:01:00Z"}]'
PASS_TRAJ='[{"x":-50,"y":150,"t":"2030-06-01T10:00:00Z"},{"x":150,"y":150,"t":"2030-06-01T10:01:00Z"}]'

preview() {  # $1=expected_revision  $2=operations JSON  $3=trajectories JSON
  curl -fsS -X POST "$BASE_URL/api/regions/batch/preview" \
    -H 'Content-Type: application/json' \
    -d "{\"expected_revision\": $1, \"operations\": $2, \"trajectories\": $3}"
}

echo "== 1. 准备：批量登记区域 A、B，并签发凭证/承诺/观察 =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions/batch" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": 0,
       \"operations\": [
         {\"action\":\"create\",\"name\":\"A区\",\"polygon\":[[0,0],[100,0],[100,100],[0,100]],
          \"valid_from\":\"$VF\",\"valid_to\":\"$VT\"},
         {\"action\":\"create\",\"name\":\"B区\",\"polygon\":[[200,0],[300,0],[300,100],[200,100]],
          \"valid_from\":\"$VF\",\"valid_to\":\"$VT\"}],
       \"trajectories\": [$SAFE_TRAJ]}")
ID_A=$(echo "$R" | J "['operations'][0]['region_id']")
REV=$(echo "$R" | J "['revision']")
echo "区域 A id=$ID_A，当前 revision=$REV"

PASS=$(curl -fsS -X POST "$BASE_URL/api/passes" -H 'Content-Type: application/json' \
  -d "{\"points\": $PASS_TRAJ}" | J "['id']")
COM=$(curl -fsS -X POST "$BASE_URL/api/commitments" -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":150,"t":0},{"x":150,"y":150,"t":60}],
       "window_start":"2030-06-02T10:00:00Z","window_end":"2030-06-02T11:00:00Z"}' \
  | J "['id']")
OBS=$(curl -fsS -X POST "$BASE_URL/api/observations" -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":150,"t":"2030-06-03T10:00:00Z"},
                 {"x":150,"y":150,"t":"2030-06-03T10:01:00Z"}]}' | J "['id']")
echo "凭证 id=$PASS，承诺 id=$COM，观察 id=$OBS（均沿 y=150 路径）"

echo "== 2. 无害批次预演：修订 B 区名称 =="
preview "$REV" "[{\"action\":\"update\",\"id\":2,\"name\":\"B区-改名\"}]" "[$SAFE_TRAJ]" \
  | python3 -m json.tool

echo "== 3. 影响预演：create D（[0,100]×[120,180]，挡住 y=150 路径）+ delete A =="
OPS="[{\"action\":\"create\",\"name\":\"D区\",\"polygon\":[[0,120],[100,120],[100,180],[0,180]],
        \"valid_from\":\"$VF\",\"valid_to\":\"$VT\"},
       {\"action\":\"delete\",\"id\":$ID_A}]"
preview "$REV" "$OPS" "[$SAFE_TRAJ, $PASS_TRAJ]" | python3 -m json.tool
echo "   ↑ 拟登记 D 以 operation_index=0 标识（region_id 为 null）；"
echo "     pass_conflicts / commitment_conflicts / observation_flips 即"
echo "     正式提交时将触发的 pass_conflict / commitment_conflict 与观察翻转事件"

echo "== 4. 错误契约（与正式批量一致）=="
echo "-- 4a. revision 过期 → 409 revision_conflict（details 带当前值）--"
curl -sS -o /tmp/bp_409.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": 0,
       \"operations\": [{\"action\":\"update\",\"id\":$ID_A,\"name\":\"x\"}],
       \"trajectories\": [$SAFE_TRAJ]}"
python3 -m json.tool /tmp/bp_409.json

echo "-- 4b. 同一区域重复操作 → 422 duplicate_region --"
curl -sS -o /tmp/bp_dup.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"update\",\"id\":$ID_A,\"name\":\"x\"},
                        {\"action\":\"delete\",\"id\":$ID_A}],
       \"trajectories\": [$SAFE_TRAJ]}"
python3 -m json.tool /tmp/bp_dup.json

echo "-- 4c. 未知区域 → 404 region_not_found --"
curl -sS -o /tmp/bp_404.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"delete\",\"id\":999999}],
       \"trajectories\": [$SAFE_TRAJ]}"
python3 -m json.tool /tmp/bp_404.json

echo "-- 4d. 非法操作（修订后时段倒序）→ 422 invalid_operation --"
curl -sS -o /tmp/bp_op.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"update\",\"id\":$ID_A,
                         \"valid_from\":\"2030-11-01T00:00:00Z\",
                         \"valid_to\":\"2030-10-01T00:00:00Z\"}],
       \"trajectories\": [$SAFE_TRAJ]}"
python3 -m json.tool /tmp/bp_op.json

echo "-- 4e. 非法轨迹（时刻逆序）→ 422 invalid_trajectory --"
curl -sS -o /tmp/bp_traj.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"update\",\"id\":$ID_A,\"name\":\"x\"}],
       \"trajectories\": [[{\"x\":0,\"y\":0,\"t\":\"2030-06-01T10:00:10Z\"},
                           {\"x\":1,\"y\":1,\"t\":\"2030-06-01T10:00:00Z\"}]]}"
python3 -m json.tool /tmp/bp_traj.json

echo "== 5. 预演无副作用：revision、区域、审计、观察结论均不变 =="
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
echo "区域数仍为：$(curl -fsS "$BASE_URL/api/regions" | J ".__len__()")"
echo "审计记录仍为：$(curl -fsS "$BASE_URL/api/regions/changes" | J "['changes'].__len__()") 条"
echo "观察 $OBS 结论仍为：$(curl -fsS "$BASE_URL/api/observations/$OBS" | J "['conclusion']")"
echo "观察 $OBS 事件数仍为：$(curl -fsS "$BASE_URL/api/observations/$OBS/events" | J "['events'].__len__()")"

echo "== 6. 与正式提交对照：同一批正式提交被 pass_conflict 拒绝（与预演一致）=="
curl -sS -o /tmp/bp_commit.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV, \"operations\": $OPS,
       \"trajectories\": [$SAFE_TRAJ]}"
python3 -m json.tool /tmp/bp_commit.json
echo "正式提交被拒绝后 revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"

echo "完成。提示：示例区域/凭证/承诺/观察会保留，可用对应 DELETE/cancel 接口清理。"
