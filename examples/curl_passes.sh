#!/usr/bin/env bash
# 通行凭证调用示例（签发 / 查询 / 取消 + 凭证对区域变更的约束）。
#
# 演示：
#   1. 登记一个长期生效的限行区域 A
#   2. 为一条从区域上方经过的未来轨迹签发通行凭证（201，返回 id/revision/轨迹）
#   3. 按 ID 查询凭证状态
#   4. 登记一个压住该轨迹的新区域 → 409 pass_conflict（凭证未取消，约束生效）
#   5. 取消凭证（重复取消幂等，结果不变）
#   6. 取消后再次登记该区域 → 201
#   7. 不安全轨迹与已开始的轨迹 → 422，不落库
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_passes.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
# 生成未来时刻（签发要求首点晚于签发时刻）
T1=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
T2=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat().replace("+00:00","Z"))')

echo "== 1. 登记限行区域 A：正方形 [0,100]x[0,100]，长期生效 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 2. 签发通行凭证：轨迹从区域上方 y=200 安全经过（$T1 → $T2） =="
PASS=$(curl -fsS -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 200, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 200, \"t\": \"$T2\"}
        ]
      }")
echo "$PASS" | python3 -m json.tool
PASS_ID=$(echo "$PASS" | J "['id']")
echo "凭证 id=$PASS_ID，签发时区域 revision=$(echo "$PASS" | J "['revision']")"

echo "== 3. 按 ID 查询凭证状态 =="
curl -fsS "$BASE_URL/api/passes/$PASS_ID" | python3 -m json.tool

echo "== 4. 凭证未取消：登记压住该轨迹的区域 → 409 pass_conflict =="
curl -sS -o /tmp/pass_conflict.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "B区-吊装半径",
        "polygon": [[0,150],[100,150],[100,250],[0,250]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }'
python3 -m json.tool /tmp/pass_conflict.json
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（未推进，整笔回滚）"

echo "== 5. 取消凭证（重复取消幂等，两次结果相同） =="
curl -fsS -X POST "$BASE_URL/api/passes/$PASS_ID/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/passes/$PASS_ID/cancel" | python3 -m json.tool

echo "== 6. 取消后再次登记该区域 → 201 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "B区-吊装半径",
        "polygon": [[0,150],[100,150],[100,250],[0,250]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2030-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 7. 不安全轨迹（直穿 A 区）→ 422 unsafe_trajectory，不落库 =="
curl -sS -o /tmp/pass_unsafe.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 50, \"t\": \"$T2\"}
        ]
      }"
python3 -m json.tool /tmp/pass_unsafe.json

echo "== 8. 已开始的轨迹（首点在过去）→ 422 invalid_trajectory，不落库 =="
PAST=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
curl -sS -o /tmp/pass_started.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 500, \"t\": \"$PAST\"},
          {\"x\": 350, \"y\": 500, \"t\": \"$T2\"}
        ]
      }"
python3 -m json.tool /tmp/pass_started.json

echo "== 9. 未知凭证 → 404 =="
curl -sS -o /tmp/pass_404.json -w 'HTTP %{http_code}\n' "$BASE_URL/api/passes/999999"
python3 -m json.tool /tmp/pass_404.json

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/{id} 清理。"
