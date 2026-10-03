#!/usr/bin/env bash
# 幂等键（Idempotency-Key）调用示例：网络超时后的安全重试。
#
# 演示：
#   1. 带键登记区域（201）
#   2. 同键同内容重试 → 返回原 201 与原响应体，revision 不推进、不重复登记
#   3. 期间其它变更推进 revision 后，重试仍返回原响应
#   4. 同键不同内容 → 409 idempotency_key_conflict，不变更
#   5. 非法键（空/超长）→ 422 invalid_idempotency_key
#   6. 失败的业务请求不占键（422 后同键修正重试成功）
#   7. 带键删除：重试返回原 200（而非 404）
#   8. 带键批量变更：重试返回原响应，revision 只推进一次、不重新分配区域 ID
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_idempotency.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

echo "== 1. 带键登记区域（Idempotency-Key: demo-create-001） =="
RESP=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-001' \
  -d '{
        "name": "幂等演示区",
        "polygon": [[0,0],[50,0],[50,50],[0,50]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }')
echo "$RESP" | python3 -m json.tool
REGION_ID=$(echo "$RESP" | J "['region']['id']")

echo
echo "== 2. 网络超时后同键同内容重试 → 原 201 与原响应，不重复登记 =="
curl -sS -o /tmp/idem_retry.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-001' \
  -d '{
        "name": "幂等演示区",
        "polygon": [[0,0],[50,0],[50,50],[0,50]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }'
python3 -m json.tool /tmp/idem_retry.json
echo "区域总数（仍只有 1 个）：$(curl -fsS "$BASE_URL/api/regions" | J ".__len__()")"
echo "当前 revision（未再推进）：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"

echo
echo "== 3. 其它变更推进 revision 后，重试仍返回原响应 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{"name":"其它区域","polygon":[[100,0],[110,0],[110,10],[100,10]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}' > /dev/null
echo "其它登记后 revision：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
curl -sS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-001' \
  -d '{"name":"幂等演示区","polygon":[[0,0],[50,0],[50,50],[0,50]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}' \
  | python3 -m json.tool
echo "↑ 响应中的 revision 仍是首次提交时的值，区域未重复登记"

echo
echo "== 4. 同键不同内容 → 409 idempotency_key_conflict，不变更 =="
curl -sS -o /tmp/idem_409.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-001' \
  -d '{"name":"同名键但内容不同","polygon":[[0,0],[5,0],[5,5],[0,5]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_409.json

echo
echo "== 5. 非法键 → 422 invalid_idempotency_key =="
curl -sS -o /tmp/idem_badkey.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'print("x"*129)')" \
  -d '{"name":"超长键","polygon":[[0,0],[5,0],[5,5],[0,5]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_badkey.json

echo
echo "== 6. 失败的业务请求不占键：先 422，再同键修正重试成功 =="
curl -sS -o /tmp/idem_422.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-002' \
  -d '{"name":"自相交多边形","polygon":[[0,0],[10,10],[10,0],[0,10]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_422.json
echo "-- 同键修正后重试 --"
curl -sS -o /tmp/idem_fixed.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-002' \
  -d '{"name":"修正后的区域","polygon":[[200,0],[210,0],[210,10],[200,10]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_fixed.json

echo
echo "== 7. 带键删除：重试返回原 200（而非 404） =="
curl -sS -o /tmp/idem_del.json -w 'HTTP %{http_code}\n' -X DELETE \
  "$BASE_URL/api/regions/$REGION_ID" -H 'Idempotency-Key: demo-delete-001'
python3 -m json.tool /tmp/idem_del.json
echo "-- 同键重试删除（区域已不存在，仍返回原 200） --"
curl -sS -o /tmp/idem_del2.json -w 'HTTP %{http_code}\n' -X DELETE \
  "$BASE_URL/api/regions/$REGION_ID" -H 'Idempotency-Key: demo-delete-001'
python3 -m json.tool /tmp/idem_del2.json

echo
echo "== 8. 带键批量变更：重试返回原响应，revision 只推进一次 =="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
BATCH_RESP=$(curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-batch-001' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"批量幂等区\",
           \"polygon\": [[300,0],[310,0],[310,10],[300,10]],
           \"valid_from\": \"2026-09-01T00:00:00Z\",
           \"valid_to\":   \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [[
          {\"x\": -50, \"y\": 500, \"t\": \"2026-09-15T00:00:00Z\"},
          {\"x\": 900, \"y\": 500, \"t\": \"2026-09-15T00:02:00Z\"}
        ]]
      }")
echo "$BATCH_RESP" | python3 -m json.tool
echo "-- 同键重试（expected_revision 已过期也无所谓，直接回放原响应） --"
curl -sS -o /tmp/idem_batch2.json -w 'HTTP %{http_code}\n' -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-batch-001' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"批量幂等区\",
           \"polygon\": [[300,0],[310,0],[310,10],[300,10]],
           \"valid_from\": \"2026-09-01T00:00:00Z\",
           \"valid_to\":   \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [[
          {\"x\": -50, \"y\": 500, \"t\": \"2026-09-15T00:00:00Z\"},
          {\"x\": 900, \"y\": 500, \"t\": \"2026-09-15T00:02:00Z\"}
        ]]
      }"
python3 -m json.tool /tmp/idem_batch2.json
echo "重试后 revision（只推进过一次）：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
echo "名为「批量幂等区」的区域数（只有 1 个）：$(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print(sum(1 for r in json.load(sys.stdin) if r["name"]=="批量幂等区"))')"
