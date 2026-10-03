#!/usr/bin/env bash
# 最小区域豁免预演调用示例（只读评估，不产生任何副作用）。
#
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s，对 [0,100]×[0,100]
# 区域的几何接触为相对 [15s,45s]；区域生效 [vf,vt) 时，禁止出发区间恰为
# 左闭右开的 [vf-45s, vt-15s)。预演在候选区域子集中找出使窗口内存在安全
# 出发时刻的方案：豁免数最少 → 最早安全时刻最早 → 编号字典序最小。
#
# 演示：
#   1. 登记全覆盖区（禁整个窗口）；原本安全的行程预演 → 豁免集为空
#   2. 只豁免全覆盖区 → 豁免 1 个即全窗口安全；候选为空 → 不可行
#   3. 再登记前半区/后半区（各禁窗口一半）：
#      两半区为候选（全覆盖区不可豁免）→ 明确不可行结果；
#      三区皆为候选 → 需豁免 2 个，取最早安全时刻更早的 {前半区, 全覆盖区}
#   4. 错误契约：重复编号 422、未知编号 404、revision 过期 409
#   5. 预演无副作用：revision、区域集合与审计历史均不变
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_exemptions.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

# 固定行程（相对秒数）与候选出发窗口（2030-06-02 10:00~11:00 UTC）
TRAJ='[{"x":-50,"y":50,"t":0},{"x":150,"y":50,"t":60}]'
# 从区域上方经过的行程（y=200），不与任何区域接触
TRAJ_HIGH='[{"x":-50,"y":200,"t":0},{"x":150,"y":200,"t":60}]'
WS="2030-06-02T10:00:00Z"; WE="2030-06-02T11:00:00Z"

preview() {  # $1=候选编号 JSON 数组  $2=expected_revision  $3=轨迹（可选）
  curl -fsS -X POST "$BASE_URL/api/exemptions/preview" \
    -H 'Content-Type: application/json' \
    -d "{\"points\": ${3:-$TRAJ},
         \"window_start\": \"$WS\", \"window_end\": \"$WE\",
         \"candidate_region_ids\": $1,
         \"expected_revision\": $2}"
}

echo "== 1. 登记全覆盖区（2020~2032 生效，禁止区间覆盖整个窗口）=="
R=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d '{"name":"全覆盖区","polygon":[[0,0],[100,0],[100,100],[0,100]],
       "valid_from":"2020-01-01T00:00:00Z","valid_to":"2032-01-01T00:00:00Z"}')
FULL=$(echo "$R" | J "['region']['id']")
REV=$(echo "$R" | J "['revision']")
echo "全覆盖区 id=$FULL，当前 revision=$REV"

echo "-- 1a. 不接触任何区域的行程：原本安全，豁免集为空 --"
preview "[]" "$REV" "$TRAJ_HIGH" | python3 -m json.tool

echo "== 2. 只豁免全覆盖区：豁免 1 个即全窗口安全；候选为空则不可行 =="
preview "[$FULL]" "$REV" | python3 -m json.tool
echo "-- 候选为空（不允许任何豁免）→ 明确不可行结果 --"
preview "[]" "$REV" | python3 -m json.tool

echo "== 3. 再登记前半区/后半区（禁止区间各覆盖窗口一半）=="
# 禁止区间 [vf-45s, vt-15s)：前半区禁 [10:00,10:30)，后半区禁 [10:30,11:00)
R=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d '{"name":"前半区","polygon":[[0,0],[100,0],[100,100],[0,100]],
       "valid_from":"2030-06-02T10:00:45Z","valid_to":"2030-06-02T10:30:15Z"}')
LO=$(echo "$R" | J "['region']['id']")
R=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d '{"name":"后半区","polygon":[[0,0],[100,0],[100,100],[0,100]],
       "valid_from":"2030-06-02T10:30:45Z","valid_to":"2030-06-02T11:00:15Z"}')
HI=$(echo "$R" | J "['region']['id']")
REV=$(echo "$R" | J "['revision']")
echo "前半区 id=$LO，后半区 id=$HI，当前 revision=$REV"

echo "-- 3a. 候选为两半区（全覆盖区不可豁免）→ 明确不可行结果 --"
preview "[$LO,$HI]" "$REV" | python3 -m json.tool

echo "-- 3b. 三区皆为候选：豁免数最少为 2，取最早安全时刻更早的 {前半区, 全覆盖区} --"
preview "[$LO,$HI,$FULL]" "$REV" | python3 -m json.tool

echo "== 4. 错误契约 =="
echo "-- 4a. 候选编号重复 → 422 duplicate_region --"
curl -sS -o /tmp/exempt_dup.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/exemptions/preview" -H 'Content-Type: application/json' \
  -d "{\"points\": $TRAJ, \"window_start\": \"$WS\", \"window_end\": \"$WE\",
       \"candidate_region_ids\": [$LO,$LO], \"expected_revision\": $REV}"
python3 -m json.tool /tmp/exempt_dup.json

echo "-- 4b. 候选编号未知 → 404 region_not_found --"
curl -sS -o /tmp/exempt_404.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/exemptions/preview" -H 'Content-Type: application/json' \
  -d "{\"points\": $TRAJ, \"window_start\": \"$WS\", \"window_end\": \"$WE\",
       \"candidate_region_ids\": [999999], \"expected_revision\": $REV}"
python3 -m json.tool /tmp/exempt_404.json

echo "-- 4c. revision 过期 → 409 revision_conflict（details 带当前值）--"
curl -sS -o /tmp/exempt_409.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/exemptions/preview" -H 'Content-Type: application/json' \
  -d "{\"points\": $TRAJ, \"window_start\": \"$WS\", \"window_end\": \"$WE\",
       \"candidate_region_ids\": [$LO], \"expected_revision\": 0}"
python3 -m json.tool /tmp/exempt_409.json

echo "== 5. 预演无副作用：revision、区域集合与审计历史均不变 =="
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
echo "区域数仍为：$(curl -fsS "$BASE_URL/api/regions" | J ".__len__()")"
echo "审计记录仍为：$(curl -fsS "$BASE_URL/api/regions/changes" | J "['changes'].__len__()") 条"

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/{id} 清理。"
