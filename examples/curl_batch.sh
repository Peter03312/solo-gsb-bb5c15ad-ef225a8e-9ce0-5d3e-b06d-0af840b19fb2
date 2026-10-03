#!/usr/bin/env bash
# 批量原子变更调用示例（POST /api/regions/batch）。
#
# 演示：
#   1. 取当前 revision
#   2. 一次批次登记 A/B 两个区域 + 一条安全轨迹 → 成功，revision 只 +1
#   3. 用"穿越扩大后区域"的轨迹提交修订 → 422 unsafe_trajectory，整批不落库
#   4. 用过期 revision 提交 → 409 revision_conflict；带新 revision 重放成功
#   5. 混合批次（修订 + 删除 + 新建）一次提交
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_batch.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

echo "== 1. 当前全局 revision =="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "expected_revision = $REV"

echo "== 2. 一批登记 A、B 两个区域，轨迹从区域上方（y=200）安全经过 =="
BATCH=$(curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"批量-A区\",
           \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
           \"valid_from\": \"2026-09-01T00:00:00Z\",
           \"valid_to\":   \"2026-10-01T00:00:00Z\"},
          {\"action\": \"create\", \"name\": \"批量-B区\",
           \"polygon\": [[200,0],[300,0],[300,100],[200,100]],
           \"valid_from\": \"2026-09-01T00:00:00Z\",
           \"valid_to\":   \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2026-09-30T00:00:00Z\"},
           {\"x\": 350, \"y\": 200, \"t\": \"2026-09-30T00:02:00Z\"}]
        ]
      }")
echo "$BATCH" | python3 -m json.tool
NEW_REV=$(echo "$BATCH" | J "['revision']")
A_ID=$(echo "$BATCH" | J "['operations'][0]['region_id']")
B_ID=$(echo "$BATCH" | J "['operations'][1]['region_id']")
echo "新区域：A=$A_ID B=$B_ID；整批后 revision=$NEW_REV（只 +1）"

echo "== 3. 不安全轨迹：把 A 区扩大到 0..200，轨迹 y=50 直穿 → 422，整批不落库 =="
curl -sS -o /tmp/batch_unsafe.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $NEW_REV,
        \"operations\": [
          {\"action\": \"update\", \"id\": $A_ID,
           \"polygon\": [[0,0],[200,0],[200,100],[0,100]]}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 50, \"t\": \"2026-09-30T00:00:00Z\"},
           {\"x\": 250, \"y\": 50, \"t\": \"2026-09-30T00:01:00Z\"}]
        ]
      }"
python3 -m json.tool /tmp/batch_unsafe.json
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（未推进）"

echo "== 4. revision 过期：仍用旧值 $REV 提交 → 409 =="
curl -sS -o /tmp/batch_conflict.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"update\", \"id\": $A_ID, \"name\": \"批量-A区-改名\"}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2026-09-30T00:00:00Z\"},
           {\"x\": 350, \"y\": 200, \"t\": \"2026-09-30T00:02:00Z\"}]
        ]
      }"
python3 -m json.tool /tmp/batch_conflict.json
CURRENT=$(python3 -c 'import json;print(json.load(open("/tmp/batch_conflict.json"))["error"]["details"][0]["current_revision"])')
echo "服务端告知当前 revision=$CURRENT，用它重放……"
curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $CURRENT,
        \"operations\": [
          {\"action\": \"update\", \"id\": $A_ID, \"name\": \"批量-A区-改名\"}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2026-09-30T00:00:00Z\"},
           {\"x\": 350, \"y\": 200, \"t\": \"2026-09-30T00:02:00Z\"}]
        ]
      }" | python3 -m json.tool

echo "== 5. 混合批次：修订 A（version+1）+ 删除 B + 新建 C，revision 仍只 +1 =="
BEFORE=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $BEFORE,
        \"operations\": [
          {\"action\": \"update\", \"id\": $A_ID,
           \"valid_to\": \"2026-12-01T00:00:00Z\"},
          {\"action\": \"delete\", \"id\": $B_ID},
          {\"action\": \"create\", \"name\": \"批量-C区\",
           \"polygon\": [[400,0],[500,0],[500,100],[400,100]],
           \"valid_from\": \"2026-09-01T00:00:00Z\",
           \"valid_to\":   \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 200, \"t\": \"2026-09-30T00:00:00Z\"},
           {\"x\": 550, \"y\": 200, \"t\": \"2026-09-30T00:02:00Z\"}]
        ]
      }" | python3 -m json.tool

echo "完成。提示：示例数据会保留，可用 DELETE /api/regions/{id} 清理。"
