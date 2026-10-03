#!/usr/bin/env bash
# 择时通行承诺调用示例（签发 / 查询当前可用区间 / 幂等取消 / 区域变更约束）。
#
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s，对 [0,100]×[0,100]
# 区域的几何接触为相对 [15s,45s]；区域生效 [vf,vt) 时，禁止出发区间恰为
# 左闭右开的 [vf-45s, vt-15s)。
#
# 演示：
#   1. 登记一个一分钟生效区域，其禁止出发区间只覆盖候选窗口中段
#   2. 签发承诺 → 返回窗口两端的两段安全出发区间（精确端点，左闭右开）
#   3. 查询承诺的当前可用区间（按当前区域集合与当前时刻重算）
#   4. 再登记一个长期生效区域试图抹掉全部安全时刻 → 409 commitment_conflict
#   5. 取消承诺（重复取消幂等）后再登记 → 201
#   6. 无安全时刻 / 窗口起点已过 / 非法相对轨迹 / 未知 ID → 422/404
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_commitments.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
iso_after() { python3 -c "
from datetime import datetime,timedelta,timezone
print((datetime.now(timezone.utc)+timedelta(seconds=$1)).isoformat().replace('+00:00','Z'))"; }

WS=$(iso_after 600)   # 候选窗口起点：10 分钟后
WE=$(iso_after 900)   # 候选窗口终点：15 分钟后

echo "== 1. 登记一分钟生效区域：vf=窗口起点+90s，禁止区间只覆盖窗口中段 =="
VF=$(iso_after 690); VT=$(iso_after 750)
# 禁止出发 = [vf-45s, vt-15s) = [起+45s, 起+135s)，窗口 [起, 起+300s)
# 被切成两段：[起, 起+45s) 与 [起+135s, 起+300s)
REGION=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"临检区-一分钟\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$VF\",
        \"valid_to\":   \"$VT\"
      }")
RID=$(echo "$REGION" | J "['region']['id']")
echo "区域 id=$RID，当前 revision=$(echo "$REGION" | J "['revision']")"

echo "== 2. 签发承诺：返回两段安全出发区间（精确区间运算，非抽样） =="
COMMIT=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
echo "$COMMIT" | python3 -m json.tool
CID=$(echo "$COMMIT" | J "['id']")
echo "承诺 id=$CID，签发 revision=$(echo "$COMMIT" | J "['revision']")，安全区间："
echo "$COMMIT" | python3 -c "
import json,sys
c = json.load(sys.stdin)
for iv in c['intervals']:
    lb = '[' if iv['start_inclusive'] else '('
    rb = ']' if iv['end_inclusive'] else ')'
    print(f'  可出发：{lb}{iv[\"start\"]}, {iv[\"end\"]}{rb}')"

echo "== 3. 查询承诺当前可用区间（随区域集合/当前时刻重算） =="
curl -fsS "$BASE_URL/api/commitments/$CID" | python3 -m json.tool

echo "== 4. 登记长期生效区域，抹掉窗口内全部安全时刻 → 409，整笔回滚 =="
curl -sS -o /tmp/commit_conflict.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "全时段管控区",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2032-01-01T00:00:00Z"
      }'
python3 -m json.tool /tmp/commit_conflict.json
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（整笔回滚，未推进）"

echo "== 5. 取消承诺（两次结果相同，幂等），随后登记该区域 → 201 =="
curl -fsS -X POST "$BASE_URL/api/commitments/$CID/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/commitments/$CID/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "全时段管控区",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to":   "2032-01-01T00:00:00Z"
      }' | python3 -m json.tool

echo "== 6. 拒签与非法输入 =="
echo "-- 6a. 窗口内无任何安全出发时刻 → 422 unsafe_trajectory，不落库 --"
curl -sS -o /tmp/commit_empty.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$(iso_after 600)\",
        \"window_end\":   \"$(iso_after 660)\"
      }"
python3 -m json.tool /tmp/commit_empty.json

echo "-- 6b. 窗口起点不晚于签发时刻 → 422 invalid_trajectory --"
PAST=$(iso_after -10)
curl -sS -o /tmp/commit_past.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$PAST\",
        \"window_end\":   \"$(iso_after 60)\"
      }"
python3 -m json.tool /tmp/commit_past.json

echo "-- 6c. 首点相对秒数非 0 → 422 invalid_trajectory --"
curl -sS -o /tmp/commit_badtraj.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 1},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$(iso_after 60)\",
        \"window_end\":   \"$(iso_after 120)\"
      }"
python3 -m json.tool /tmp/commit_badtraj.json

echo "-- 6d. 未知承诺 → 404 --"
curl -sS -o /tmp/commit_404.json -w 'HTTP %{http_code}\n' "$BASE_URL/api/commitments/999999"
python3 -m json.tool /tmp/commit_404.json

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/{id} 清理。"
