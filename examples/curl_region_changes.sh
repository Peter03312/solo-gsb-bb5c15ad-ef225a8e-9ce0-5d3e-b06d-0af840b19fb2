#!/usr/bin/env bash
# 区域集合变更只读审计历史调用示例。
#
# 演示：
#   1. 空历史返回 changes=[] 与 next_cursor=null
#   2. 单区登记/修订/删除与批量变更各产生一条按 revision 升序的记录
#   3. 失败事务（非法时段 422、删除不存在 404）不留记录
#   4. 幂等重试回放不新增记录
#   5. 凭证/承诺/观察的签发与取消不产生区域变更记录
#   6. cursor/limit 游标分页（next_cursor）与游标非法的统一 422
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_region_changes.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
WIDE_FROM="2020-01-01T00:00:00Z"
WIDE_TO="2030-01-01T00:00:00Z"
T1=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
T2=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat().replace("+00:00","Z"))')

echo "== 0. 清理：删除全部既有区域（404 可忽略），避免历史串台 =="
for id in $(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print(" ".join(str(r["id"]) for r in json.load(sys.stdin)))' 2>/dev/null); do
  curl -sS -o /dev/null -X DELETE "$BASE_URL/api/regions/$id" || true
done

echo "== 1. 空历史（从未发生过变更时）：空数组 + null =="
curl -fsS "$BASE_URL/api/regions/changes" | python3 -m json.tool

echo "== 2. 登记 A 区 → 一条 create 记录（revision/type/region_ids/summary） =="
A=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"A区-审计演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$WIDE_FROM\",
        \"valid_to\":   \"$WIDE_TO\"
      }")
A_ID=$(echo "$A" | J "['region']['id']")
echo "新区域编号=$A_ID，新 revision=$(echo "$A" | J "['revision']")"

echo "== 3. 修订 A 区 → update（摘要带修订后 version） =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{"name": "A区-改名"}' | python3 -m json.tool

echo "== 4. 非法时段登记 422：事务回滚，不留审计记录 =="
curl -sS -o /tmp/rc_bad.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d "{\"name\":\"非法\",\"polygon\":[[0,0],[10,0],[10,10],[0,10]],
       \"valid_from\":\"$WIDE_TO\",\"valid_to\":\"$WIDE_FROM\"}"
python3 -m json.tool /tmp/rc_bad.json

echo "== 5. 删除不存在区域 404：同样不留记录 =="
curl -sS -o /tmp/rc_404.json -w 'HTTP %{http_code}\n' -X DELETE "$BASE_URL/api/regions/999999"
python3 -m json.tool /tmp/rc_404.json

echo "== 6. 批量变更（create+delete）：整批一行 batch，涉及编号按操作顺序 =="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"B区\",
           \"polygon\": [[400,0],[500,0],[500,100],[400,100]],
           \"valid_from\": \"$WIDE_FROM\", \"valid_to\": \"$WIDE_TO\"},
          {\"action\": \"delete\", \"id\": $A_ID}
        ],
        \"trajectories\": [[
          {\"x\": -50, \"y\": 300, \"t\": \"$T1\"},
          {\"x\": 900, \"y\": 300, \"t\": \"$T2\"}
        ]]
      }" | python3 -m json.tool

echo "== 7. 幂等登记 + 同键重试：只回放原响应，不新增审计记录 =="
for try in 1 2; do
  curl -sS -o /dev/null -w "第 $try 次 HTTP %{http_code}\n" \
    -X POST "$BASE_URL/api/regions" \
    -H 'Content-Type: application/json' -H 'Idempotency-Key: rc-demo-key-0001' \
    -d "{\"name\":\"幂等区\",\"polygon\":[[0,0],[10,0],[10,10],[0,10]],
         \"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}"
done

echo "== 8. 凭证签发/取消不产生区域变更记录 =="
PID=$(curl -fsS -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d "{\"points\":[{\"x\":-50,\"y\":800,\"t\":\"$T1\"},{\"x\":350,\"y\":800,\"t\":\"$T2\"}]}" \
  | J "['id']")
curl -fsS -o /dev/null -X POST "$BASE_URL/api/passes/$PID/cancel"
echo "凭证 $PID 已签发并取消（revision 不变、无审计记录）"

echo "== 9. 读取整段历史（按 revision 升序，limit=100） =="
curl -fsS "$BASE_URL/api/regions/changes?limit=100" | python3 -m json.tool

echo "== 10. 分页：每页 2 条，用 next_cursor 翻页直到 null =="
CURSOR=0
PAGE=1
while :; do
  RESP=$(curl -fsS "$BASE_URL/api/regions/changes?limit=2&cursor=$CURSOR")
  echo "--- 第 $PAGE 页（cursor=$CURSOR） ---"
  echo "$RESP" | python3 -c 'import sys,json; d=json.load(sys.stdin); print([(c["revision"], c["change_type"], c["region_ids"]) for c in d["changes"]]); print("next_cursor =", d["next_cursor"])'
  NEXT=$(echo "$RESP" | J "['next_cursor']" || echo None)
  [ "$NEXT" = "None" ] && break
  CURSOR=$NEXT
  PAGE=$((PAGE + 1))
done

echo "== 11. 游标非法 / 页大小越界：统一 422 validation_error =="
curl -sS -o /tmp/rc_c1.json -w 'cursor=-1  → HTTP %{http_code}\n' "$BASE_URL/api/regions/changes?cursor=-1"
python3 -m json.tool /tmp/rc_c1.json
curl -sS -o /tmp/rc_c2.json -w 'limit=501  → HTTP %{http_code}\n' "$BASE_URL/api/regions/changes?limit=501"
python3 -m json.tool /tmp/rc_c2.json

echo "完成。审计历史为只读：GET /api/regions/changes?cursor=<revision>&limit=<1~500>"
