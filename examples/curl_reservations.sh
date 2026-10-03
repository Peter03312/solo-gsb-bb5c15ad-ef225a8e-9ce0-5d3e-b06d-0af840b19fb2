#!/usr/bin/env bash
# 批量限行区域调整预约调用示例。
#
# 安全员预约一次未来的批量限行区域调整：提交带时区执行时刻 execute_at、
# 预期 revision 以及与 POST /api/regions/batch 一致的 operations/trajectories，
# 取得预约编号与待执行状态（pending）。受理时只校验输入格式，不预占
# revision、不保证届时可提交；预约持久保存，到期由后台执行器按
# (execute_at, id) 顺序在与区域写入共用的事务锁内重验 revision、操作、
# 轨迹安全与凭证/承诺保护：成功才一次性提交（区域/revision/观察事件/审计），
# 失败记录沿用批量变更错误码的可查询原因且不改变区域集合；同一预约无论
# 重启或并发处理都至多提交一次。待执行预约可取消，取消与执行竞争只产生
# 一个终态。
#
# 演示：
#   1. 登记区域 A；预约 5 秒后的批量调整（改名 + 新建 C 区）→ 201 pending
#   2. 查询：pending；等待后台执行器到期自动执行 → committed + 提交结果
#   3. 区域已变更、审计含 batch 记录（与区域写入同一事务）
#   4. 失败可查询：revision 过期 → failed/revision_conflict（区域不变）
#   5. 失败可查询：轨迹穿越拟变更集合 → failed/unsafe_trajectory
#   6. 取消：pending → cancelled（幂等不适用：终态再取消 409）
#   7. 错误契约：不带时区 422 validation_error、非未来 422
#      execution_not_in_future、未知编号 404 not_found、终态取消 409
#      reservation_already_terminal
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_reservations.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
AT() { python3 -c "from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(seconds=$1)).isoformat().replace('+00:00','Z'))"; }

WIDE_FROM="2020-01-01T00:00:00Z"; WIDE_TO="2031-01-01T00:00:00Z"
SAFE_TRAJ='[{"x":-50,"y":500,"t":"2030-06-01T10:00:00Z"},{"x":450,"y":500,"t":"2030-06-01T10:01:00Z"}]'

echo "== 0. 准备：登记区域 A（当前 revision=1） =="
curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d "{\"name\":\"A区\",\"polygon\":[[0,0],[100,0],[100,100],[0,100]],
       \"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}" | python3 -m json.tool

echo "== 1. 预约 5 秒后的批量调整（A 改名 + 新建 C 区） → 201 pending =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d "{\"execute_at\": \"$(AT 5)\",
       \"expected_revision\": 1,
       \"operations\": [
         {\"action\":\"update\",\"id\":1,\"name\":\"A区-夜间限制作业\"},
         {\"action\":\"create\",\"name\":\"C区\",\"polygon\":[[400,0],[500,0],[500,100],[400,100]],
          \"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}],
       \"trajectories\": [$SAFE_TRAJ]}")
echo "$R" | python3 -m json.tool
RID=$(echo "$R" | J "['id']")
echo "预约编号=$RID，状态=$(echo "$R" | J "['status']")（受理不预占 revision：当前仍=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")）"

echo "== 2. 到期前查询 → pending；等待后台执行器到期自动执行 =="
curl -fsS "$BASE_URL/api/regions/batch/reservations/$RID" | J "['status']"
sleep 7
R=$(curl -fsS "$BASE_URL/api/regions/batch/reservations/$RID")
echo "$R" | python3 -m json.tool
echo "执行后状态=$(echo "$R" | J "['status']")，新 revision=$(echo "$R" | J "['result']['revision']")"

echo "== 3. 区域已变更、审计含 batch 记录（与区域写入同一事务） =="
curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print([(r["id"], r["name"], r["version"]) for r in json.load(sys.stdin)])'
curl -fsS "$BASE_URL/api/regions/changes" | python3 -c 'import sys,json; print([(c["revision"], c["change_type"]) for c in json.load(sys.stdin)["changes"]])'

echo "== 4. 失败可查询：预期 revision 过期 → failed/revision_conflict（区域不变） =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d "{\"execute_at\": \"$(AT 4)\",
       \"expected_revision\": 1,
       \"operations\": [{\"action\":\"update\",\"id\":1,\"name\":\"A区-不会生效\"}],
       \"trajectories\": [$SAFE_TRAJ]}")
RID2=$(echo "$R" | J "['id']")
echo "预约编号=$RID2（expected_revision=1，已过期）"
sleep 6
curl -fsS "$BASE_URL/api/regions/batch/reservations/$RID2" | python3 -m json.tool
echo "区域名称仍为：$(curl -fsS "$BASE_URL/api/regions/1" | J "['name']")"

echo "== 5. 失败可查询：轨迹穿越拟变更集合 → failed/unsafe_trajectory =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d "{\"execute_at\": \"$(AT 4)\",
       \"expected_revision\": 2,
       \"operations\": [{\"action\":\"create\",\"name\":\"D区\",\"polygon\":[[0,20],[100,20],[100,80],[0,80]],
                         \"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}],
       \"trajectories\": [[{\"x\":-50,\"y\":50,\"t\":\"2030-06-01T10:00:00Z\"},
                          {\"x\":450,\"y\":50,\"t\":\"2030-06-01T10:01:40Z\"}]]}")
RID3=$(echo "$R" | J "['id']")
echo "预约编号=$RID3（自带轨迹将穿越拟建 D 区）"
sleep 6
curl -fsS "$BASE_URL/api/regions/batch/reservations/$RID3" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], "->", d["failure"]["code"]); print(json.dumps(d["failure"]["details"], ensure_ascii=False, indent=2))'

echo "== 6. 取消：pending → cancelled；终态再取消 → 409 =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d "{\"execute_at\": \"$(AT 3600)\",
       \"expected_revision\": 2,
       \"operations\": [{\"action\":\"delete\",\"id\":1}],
       \"trajectories\": [$SAFE_TRAJ]}")
RID4=$(echo "$R" | J "['id']")
curl -fsS -X POST "$BASE_URL/api/regions/batch/reservations/$RID4/cancel" | python3 -m json.tool
echo "-- 已取消预约不会执行；对终态预约再取消："
curl -sS -X POST "$BASE_URL/api/regions/batch/reservations/$RID4/cancel" | python3 -m json.tool

echo "== 7. 错误契约：不带时区 / 非未来 / 未知编号 =="
curl -sS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d '{"execute_at": "2030-06-01T10:00:00",
       "expected_revision": 2,
       "operations": [{"action":"delete","id":1}],
       "trajectories": [[{"x":0,"y":0,"t":"2030-06-01T10:00:00Z"},{"x":1,"y":1,"t":"2030-06-01T10:01:00Z"}]]}' \
  -o /tmp/rsv_bad1.json -w '不带时区 HTTP %{http_code} ' || true
python3 -c 'import json; print(json.load(open("/tmp/rsv_bad1.json"))["error"]["code"])'
curl -sS -X POST "$BASE_URL/api/regions/batch/reservations" \
  -H 'Content-Type: application/json' \
  -d "{\"execute_at\": \"$(AT -10)\",
       \"expected_revision\": 2,
       \"operations\": [{\"action\":\"delete\",\"id\":1}],
       \"trajectories\": [[{\"x\":0,\"y\":0,\"t\":\"2030-06-01T10:00:00Z\"},{\"x\":1,\"y\":1,\"t\":\"2030-06-01T10:01:00Z\"}]]}" \
  -o /tmp/rsv_bad2.json -w '非未来时刻 HTTP %{http_code} ' || true
python3 -c 'import json; print(json.load(open("/tmp/rsv_bad2.json"))["error"]["code"])'
curl -sS "$BASE_URL/api/regions/batch/reservations/999999" -o /tmp/rsv_bad3.json \
  -w '未知编号 HTTP %{http_code} ' || true
python3 -c 'import json; print(json.load(open("/tmp/rsv_bad3.json"))["error"]["code"])'

echo "完成。提示：预约持久保存，服务重启后到期仍会执行且至多提交一次。"
