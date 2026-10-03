#!/usr/bin/env bash
# 预约未来批量限行区域调整调用示例
# （POST /api/regions/batch/schedule、GET 查询、POST cancel）。
#
# 演示：
#   1. 先登记一个区域（revision=1），并申请一个受保护的未来通行凭证
#   2. 预约一笔「未来 1 分钟删除该区域」的批量变更（带时区执行时刻、
#      expected_revision、有序 operations 与 trajectories）→ 201 pending
#   3. 查询预约：等待后台执行器到期处理 → succeeded，返回提交结果
#   4. 再预约一笔 revision 已过期的变更，到期后 failed/revision_conflict，
#      区域集合不变、失败原因可查询
#   5. 预约一笔更远的变更后立即取消（幂等）；终态（成功/失败）取消 → 409
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_scheduled_batch.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

echo "== 1. 当前 revision 与准备区域 =="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "当前 expected_revision = $REV"
A_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"预约演示-A区\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"2030-01-01T00:00:00Z\",
        \"valid_to\":   \"2031-01-01T00:00:00Z\"
      }" | J "['region']['id']")
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "已登记区域 id=$A_ID，revision=$REV"

# 执行时刻：当前时刻 + 60 秒（Python 生成带时区 ISO8601）
EXEC_AT=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=60)).isoformat())')

echo
echo "== 2. 预约未来批量变更（删除 A 区，轨迹从 y=200 安全经过） =="
S1=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/schedule" \
  -H 'Content-Type: application/json' \
  -d "{
        \"execute_at\": \"$EXEC_AT\",
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"delete\", \"id\": $A_ID}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2030-06-01T00:00:00Z\"},
           {\"x\": 900, \"y\": 200, \"t\": \"2030-06-01T00:02:00Z\"}]
        ]
      }")
echo "$S1" | python3 -m json.tool
S1_ID=$(echo "$S1" | J "['id']")
echo "预约编号=$S1_ID，状态=pending；受理不预占 revision、不保证届时可提交"

echo
echo "== 3. 轮询查询，等待到期执行（后台按 execute_at、编号顺序处理） =="
for _ in $(seq 1 40); do
  G=$(curl -fsS "$BASE_URL/api/regions/batch/schedule/$S1_ID")
  STATUS=$(echo "$G" | J "['status']")
  [ "$STATUS" != "pending" ] && break
  sleep 2
done
echo "$G" | python3 -m json.tool
echo "最终状态：$STATUS"

echo
echo "== 4. 对已成功的终态预约再次取消 → 409 scheduled_batch_terminal =="
curl -sS -o /tmp/sched_terminal.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/schedule/$S1_ID/cancel"
python3 -m json.tool /tmp/sched_terminal.json

echo
echo "== 5. 预约一笔 revision 已过期的变更：到期 failed、区域不变、原因可查 =="
EXEC2=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=3)).isoformat())')
S2=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/schedule" \
  -H 'Content-Type: application/json' \
  -d "{
        \"execute_at\": \"$EXEC2\",
        \"expected_revision\": 999999,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"预约演示-过期rev\",
           \"polygon\": [[0,0],[10,0],[10,10],[0,10]],
           \"valid_from\": \"2030-01-01T00:00:00Z\",
           \"valid_to\": \"2031-01-01T00:00:00Z\"}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2030-06-01T00:00:00Z\"},
           {\"x\": 900, \"y\": 200, \"t\": \"2030-06-01T00:02:00Z\"}]
        ]
      }")
S2_ID=$(echo "$S2" | J "['id']")
for _ in $(seq 1 20); do
  G2=$(curl -fsS "$BASE_URL/api/regions/batch/schedule/$S2_ID")
  [ "$(echo "$G2" | J "['status']")" != "pending" ] && break
  sleep 1
done
echo "$G2" | python3 -m json.tool

echo
echo "== 6. 受理错误示例：非未来时刻 / 不带时区 / 重复区域 =="
curl -sS -o /tmp/sched_past.json -w '过去时刻 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/schedule" \
  -H 'Content-Type: application/json' \
  -d "{
        \"execute_at\": \"2020-01-01T00:00:00Z\",
        \"expected_revision\": 0,
        \"operations\": [{\"action\": \"delete\", \"id\": 1}],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2030-06-01T00:00:00Z\"},
           {\"x\": 900, \"y\": 200, \"t\": \"2030-06-01T00:02:00Z\"}]
        ]
      }"
python3 -m json.tool /tmp/sched_past.json | head -12

curl -sS -o /tmp/sched_notz.json -w '不带时区 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/schedule" \
  -H 'Content-Type: application/json' \
  -d "{
        \"execute_at\": \"2030-01-01T00:00:00\",
        \"expected_revision\": 0,
        \"operations\": [{\"action\": \"delete\", \"id\": 1}],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2030-06-01T00:00:00Z\"},
           {\"x\": 900, \"y\": 200, \"t\": \"2030-06-01T00:02:00Z\"}]
        ]
      }"
python3 -m json.tool /tmp/sched_notz.json | head -12

echo
echo "== 7. 预约后立即取消（幂等）；未知编号 404 =="
EXEC3=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())')
S3_ID=$(curl -fsS -X POST "$BASE_URL/api/regions/batch/schedule" \
  -H 'Content-Type: application/json' \
  -d "{
        \"execute_at\": \"$EXEC3\",
        \"expected_revision\": 0,
        \"operations\": [{\"action\": \"delete\", \"id\": 1}],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2030-06-01T00:00:00Z\"},
           {\"x\": 900, \"y\": 200, \"t\": \"2030-06-01T00:02:00Z\"}]
        ]
      }" | J "['id']")
curl -fsS -X POST "$BASE_URL/api/regions/batch/schedule/$S3_ID/cancel" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("首次取消:",d["status"],"cancelled=",d["cancelled"])'
curl -fsS -X POST "$BASE_URL/api/regions/batch/schedule/$S3_ID/cancel" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("再次取消（幂等）:",d["status"],"cancelled=",d["cancelled"])'
curl -sS -o /tmp/sched_404.json -w '取消未知编号 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/schedule/999999/cancel"
python3 -m json.tool /tmp/sched_404.json

echo
echo "完成。可随时 GET $BASE_URL/api/regions/batch/schedule/{id} 查询状态、"
echo "提交结果（result）或失败原因（error）。"
