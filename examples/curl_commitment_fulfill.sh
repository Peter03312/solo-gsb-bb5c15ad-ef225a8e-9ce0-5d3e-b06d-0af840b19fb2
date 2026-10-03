#!/usr/bin/env bash
# 择时通行承诺「兑现」调用示例：把一张尚未取消、窗口未结束的承诺兑现为
# 一张固定时刻通行凭证（POST /api/commitments/{id}/fulfill）。
#
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s，对 [0,100]×[0,100]
# 区域的几何接触为相对 [15s,45s]；区域生效 [vf,vt) 时禁止出发区间恰为
# 左闭右开的 [vf-45s, vt-15s)。
#
# 演示：
#   1. 登记一分钟生效区域，把候选窗口切成两段安全区间
#   2. 签发承诺并在安全时刻兑现 → 201：承诺编号 + 固定时刻凭证 + revision
#   3. 重复兑现 → 409 commitment_already_fulfilled（与普通取消区分）
#   4. 另一张承诺：在禁止区间内时刻兑现 → 422 unsafe_trajectory（回显安全区间）
#   5. 越出窗口 / 不晚于当前时刻 / 普通取消 / 未知承诺 → 各类错误码
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_commitment_fulfill.sh
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
        \"name\": \"临检区-兑现演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$VF\",
        \"valid_to\":   \"$VT\"
      }")
RID=$(echo "$REGION" | J "['region']['id']")
echo "区域 id=$RID，当前 revision=$(echo "$REGION" | J "['revision']")"

echo "== 2. 签发承诺并在安全时刻（窗口起点）兑现 → 201 =="
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
CID=$(echo "$COMMIT" | J "['id']")
echo "承诺 id=$CID，两段安全区间："
echo "$COMMIT" | python3 -c "
import json,sys
for iv in json.load(sys.stdin)['intervals']:
    print(f'  [{iv[\"start\"]}, {iv[\"end\"]})')"

FULFILLED=$(curl -fsS -X POST "$BASE_URL/api/commitments/$CID/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WS\"}")
echo "$FULFILLED" | python3 -m json.tool
PID=$(echo "$FULFILLED" | J "['pass']['id']")
echo "兑现成功：承诺 $CID → 凭证 $PID，所据 revision=$(echo "$FULFILLED" | J "['revision']")"
echo "区域 revision 未因兑现改变：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"

echo "== 2b. 承诺查询：已按取消语义失效，fulfilled_pass_id 指向凭证 =="
curl -fsS "$BASE_URL/api/commitments/$CID" | python3 -m json.tool
echo "== 2c. 凭证可经既有接口查询（即刻受 pass_conflict 保护） =="
curl -fsS "$BASE_URL/api/passes/$PID" | python3 -m json.tool

echo "== 3. 重复兑现 → 409 commitment_already_fulfilled（区别于普通取消） =="
curl -sS -o /tmp/fulfill_again.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WS\"}"
python3 -m json.tool /tmp/fulfill_again.json

echo "== 4. 第二张承诺：禁止区间内时刻兑现 → 422 unsafe_trajectory =="
COMMIT2=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID2=$(echo "$COMMIT2" | J "['id']")
MID=$(iso_after 700)   # 起+100s，落在禁止区间 [起+45s,起+135s) 内
curl -sS -o /tmp/fulfill_unsafe.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID2/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$MID\"}"
python3 -m json.tool /tmp/fulfill_unsafe.json
echo "不安全兑现不改变承诺（仍 active）："
curl -fsS "$BASE_URL/api/commitments/$CID2" \
  | python3 -c "import json,sys; c=json.load(sys.stdin); print('status =', c['status'], '; 安全区间段数 =', len(c['intervals']))"
echo "改取安全区间起点（起+135s）后兑现成功："
SAFE=$(iso_after 735)
curl -fsS -X POST "$BASE_URL/api/commitments/$CID2/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$SAFE\"}" | python3 -m json.tool

echo "== 5. 错误契约 =="
echo "-- 5a. 越出候选窗口（恰为 window_end，开端）→ 422 departure_out_of_window --"
COMMIT3=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID3=$(echo "$COMMIT3" | J "['id']")
curl -sS -o /tmp/fulfill_out.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WE\"}"
python3 -m json.tool /tmp/fulfill_out.json

echo "-- 5b. 出发时刻不晚于受理时刻 → 422 departure_not_in_future --"
curl -sS -o /tmp/fulfill_past.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$(iso_after -5)\"}"
python3 -m json.tool /tmp/fulfill_past.json

echo "-- 5c. 不带时区 → 422 validation_error --"
curl -sS -o /tmp/fulfill_notz.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/fulfill" \
  -H 'Content-Type: application/json' \
  -d '{"departure_time": "2030-06-01T10:00:00"}'
python3 -m json.tool /tmp/fulfill_notz.json

echo "-- 5d. 普通取消的承诺 → 422 commitment_cancelled --"
COMMIT4=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID4=$(echo "$COMMIT4" | J "['id']")
curl -fsS -X POST "$BASE_URL/api/commitments/$CID4/cancel" > /dev/null
curl -sS -o /tmp/fulfill_cancelled.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID4/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WS\"}"
python3 -m json.tool /tmp/fulfill_cancelled.json

echo "-- 5e. 未知承诺 → 404 not_found --"
curl -sS -o /tmp/fulfill_404.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/999999/fulfill" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WS\"}"
python3 -m json.tool /tmp/fulfill_404.json

echo "完成。提示：示例数据会保留，可用 DELETE /api/regions/{id} 清理。"
