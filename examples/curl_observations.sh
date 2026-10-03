#!/usr/bin/env bash
# 安全状态观察调用示例（登记 / 查询 / 取消 + 结论翻转事件分页）。
#
# 演示：
#   1. 登记区域 A（观察 o1：safe→unsafe 翻转事件）
#   2. 登记两条观察：安全（区域上方经过）与不安全（直穿区域），均可登记
#   3. 修订区域压住安全观察 → 200（观察不阻止变更），safe→unsafe 事件
#   4. 删除区域 → unsafe→safe 事件（结论不变的操作不产生事件）
#   5. 按序号游标升序分页读取事件（cursor/limit/next_cursor）
#   6. 取消观察（幂等），取消后不再产生事件
#   7. 非法轨迹、首点已过、未知编号的错误契约
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_observations.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
# 生成未来时刻（登记要求首点晚于登记时刻）
T1=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
T2=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat().replace("+00:00","Z"))')
WIDE_FROM="2020-01-01T00:00:00Z"
WIDE_TO="2030-01-01T00:00:00Z"

echo "== 0. 清理：删除全部既有区域（幂等重试友好；404 可忽略） =="
for id in $(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print(" ".join(str(r["id"]) for r in json.load(sys.stdin)))' 2>/dev/null); do
  curl -sS -o /dev/null -X DELETE "$BASE_URL/api/regions/$id" || true
done

echo "== 1. 登记观察 o1：轨迹 y=50，当前无区域 → 201 safe =="
OBS1=$(curl -fsS -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 50, \"t\": \"$T2\"}
        ]
      }")
echo "$OBS1" | python3 -m json.tool
O1=$(echo "$OBS1" | J "['id']")
echo "观察编号=$O1，初始结论=$(echo "$OBS1" | J "['conclusion']")，revision=$(echo "$OBS1" | J "['revision']")"

echo "== 2. 登记区域 A：[0,100]x[0,100]，长期生效 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"A区-观察演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$WIDE_FROM\",
        \"valid_to\":   \"$WIDE_TO\"
      }" | python3 -m json.tool

echo "== 3. o1 事件：安全结论翻转 safe→unsafe（同一事务，新 revision） =="
curl -fsS "$BASE_URL/api/observations/$O1/events" | python3 -m json.tool

echo "== 4. 再登记两条观察：安全（y=200 上方经过）与不安全（y=50 直穿） =="
OBS_S=$(curl -fsS -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 200, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 200, \"t\": \"$T2\"}
        ]
      }")
echo "$OBS_S" | python3 -m json.tool
OS=$(echo "$OBS_S" | J "['id']")

OBS_U=$(curl -fsS -X POST "$BASE_URL/api/observations" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": \"$T1\"},
          {\"x\": 350, \"y\": 50, \"t\": \"$T2\"}
        ]
      }")
echo "不安全观察同样 201：结论=$(echo "$OBS_U" | J "['conclusion']")，编号=$(echo "$OBS_U" | J "['id']")"

echo "== 5. 按编号查询 o1：轨迹、状态、最近结论及 revision =="
curl -fsS "$BASE_URL/api/observations/$O1" | python3 -m json.tool

echo "== 6. 修订 A 扩大到覆盖 y=200 → 200（观察不阻止区域变更） =="
# 先查出 A 的区域 id
A_ID=$(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print([r for r in json.load(sys.stdin) if r["name"]=="A区-观察演示"][0]["id"])')
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{"polygon": [[0,0],[400,0],[400,300],[0,300]]}' | python3 -m json.tool

echo "== 7. 安全观察 $OS 出现一条 safe→unsafe 事件（不安全观察结论不变，无事件） =="
curl -fsS "$BASE_URL/api/observations/$OS/events" | python3 -m json.tool

echo "== 8. 删除区域 A → 观察翻转回 safe =="
curl -fsS -X DELETE "$BASE_URL/api/regions/$A_ID" | python3 -m json.tool
echo "o1 的事件列表（应看到 unsafe→safe）："
curl -fsS "$BASE_URL/api/observations/$O1/events" | python3 -m json.tool

echo "== 9. 事件分页：首页 limit=1，用 next_cursor 翻下一页 =="
PAGE1=$(curl -fsS "$BASE_URL/api/observations/$O1/events?limit=1")
echo "$PAGE1" | python3 -m json.tool
CURSOR=$(echo "$PAGE1" | J "['next_cursor']")
if [ "$CURSOR" != "None" ]; then
  echo "下一页（cursor=$CURSOR）："
  curl -fsS "$BASE_URL/api/observations/$O1/events?limit=1&cursor=$CURSOR" | python3 -m json.tool
else
  echo "（事件不足两条，没有下一页）"
fi

echo "== 10. 取消观察 o1（重复取消幂等，两次结果相同） =="
curl -fsS -X POST "$BASE_URL/api/observations/$O1/cancel" | python3 -m json.tool
curl -fsS -X POST "$BASE_URL/api/observations/$O1/cancel" | python3 -m json.tool

echo "== 11. 取消后再登记区域：o1 不再产生事件（$OS 仍 active 会翻转） =="
BEFORE=$(curl -fsS "$BASE_URL/api/observations/$O1/events" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["events"]))')
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"B区-取消后\",
        \"polygon\": [[0,0],[400,0],[400,300],[0,300]],
        \"valid_from\": \"$WIDE_FROM\",
        \"valid_to\":   \"$WIDE_TO\"
      }" | python3 -m json.tool
AFTER=$(curl -fsS "$BASE_URL/api/observations/$O1/events" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["events"]))')
echo "已取消观察 o1 事件数：$BEFORE → $AFTER（应保持不变）"

echo "== 12. 错误契约：非法轨迹 / 首点已过 / 未知编号 =="
curl -sS -o /tmp/obs_bad.json -w '点数不足 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/observations" -H 'Content-Type: application/json' \
  -d "{\"points\": [{\"x\": 0, \"y\": 0, \"t\": \"$T1\"}]}"
python3 -m json.tool /tmp/obs_bad.json
PAST=$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')
curl -sS -o /tmp/obs_past.json -w '首点已过 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/observations" -H 'Content-Type: application/json' \
  -d "{\"points\": [{\"x\": 0, \"y\": 0, \"t\": \"$PAST\"},{\"x\": 1, \"y\": 1, \"t\": \"$T1\"}]}"
python3 -m json.tool /tmp/obs_past.json
curl -sS -o /tmp/obs_404.json -w '未知编号 HTTP %{http_code}\n' "$BASE_URL/api/observations/999999"
python3 -m json.tool /tmp/obs_404.json

echo "完成。提示：观察会保留，可用 POST /api/observations/{id}/cancel 取消。"
