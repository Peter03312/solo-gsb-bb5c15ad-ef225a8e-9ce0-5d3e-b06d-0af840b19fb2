#!/usr/bin/env bash
# 临时限行核验服务调用示例（curl）。
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

echo "== 1. 健康检查 =="
curl -fsS "$BASE_URL/health"; echo

echo "== 2. 出发时刻调度：登记一个仅 09-01 [00:00:00,00:01:00) 生效的临检区 =="
# 轨迹：相对 0s 位于 (-50,50)，60s 后到 (150,50)；与方区的几何接触为
# 闭区间 [15s,45s]（由线段插值得到，非抽样）。
# 禁止出发区间 = [vf-45s, vt-15s)
#             = [2026-08-31T23:59:15Z, 2026-09-01T00:00:45Z)（左闭右开）。
# 候选窗口 [08-31 23:58, 09-01 00:02) 被切成两段安全：
#   [23:58:00,23:59:15) 与 [00:00:45,00:02:00)，均为左闭右开。
# 23:59:15 出发会在 valid_from 时刻进入区域（左端闭，违规）；
# 00:00:45 出发则在 valid_to 时刻离开（右端开，安全）。
C_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "C区-一分钟临检",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-09-01T00:01:00Z"
      }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["region"]["id"])')

echo "== 3. 求安全出发区间（精确区间运算，无离散抽样） =="
curl -fsS -X POST "$BASE_URL/api/schedule" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 50, "t": 0},
          {"x": 150, "y": 50, "t": 60}
        ],
        "window_start": "2026-08-31T23:58:00Z",
        "window_end":   "2026-09-01T00:02:00Z"
      }'; echo

echo "== 4. 单点接触 + 生效端点：相对 5s 擦过右上角 (100,100) =="
# 接触长度为 0（退化点）。区域 [09-01 00:00,09-02 00:00) 生效时禁止
# [08-31T23:59:55Z, 09-01T23:59:55Z)；窗口 [09-01 00:00, 09-02 00:00) 内
# 安全段为 [09-01T23:59:55Z, 09-02T00:00:00Z)——23:59:55 这次出发恰使接触
# 时刻 == valid_to（右端点开），故安全、左端闭。
curl -fsS -X PATCH "$BASE_URL/api/regions/$C_ID" \
  -H 'Content-Type: application/json' \
  -d '{
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-09-02T00:00:00Z"
      }' >/dev/null
curl -fsS -X POST "$BASE_URL/api/schedule" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x":  50, "y": 150, "t": 0},
          {"x": 150, "y":  50, "t": 10}
        ],
        "window_start": "2026-09-01T00:00:00Z",
        "window_end":   "2026-09-02T00:00:00Z"
      }'; echo
curl -fsS -X DELETE "$BASE_URL/api/regions/$C_ID" >/dev/null

echo "== 5. 登记限行区域 A：正方形 [0,0]-[100,100]，9 月全月生效 =="
A_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }' | python3 -c 'import sys,json; print(json.load(sys.stdin)["region"]["id"])')
echo "A 区 id=$A_ID"

echo "== 6. 登记限行区域 B：右侧 200~300 的带状区域 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "B区-吊装半径",
        "polygon": [[200,0],[300,0],[300,100],[200,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }'; echo

echo "== 7. 查询当前区域集合版本号 =="
curl -fsS "$BASE_URL/api/revision"; echo

echo "== 8. 列出全部区域 =="
curl -fsS "$BASE_URL/api/regions"; echo

echo "== 9. 核验轨迹：3 个点，直线匀速，穿过 A 区但不碰 B 区 =="
# 09-30 00:00:00 位于 (-50,50)；1 分钟后到 (150,50)；2 分钟后到 (350,50)
# A 区穿越由线段插值得到：进入 00:00:15（x=0），离开 00:00:45（x=100）
curl -fsS -X POST "$BASE_URL/api/verify" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
          {"x": 150, "y": 50, "t": "2026-09-30T00:01:00Z"},
          {"x": 350, "y": 50, "t": "2026-09-30T00:02:00Z"}
        ]
      }'; echo

echo "== 10. 核验轨迹：斜向擦过 A 区右上角 (100,100)，应为单点接触 =="
curl -fsS -X POST "$BASE_URL/api/verify" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x":  50, "y": 150, "t": "2026-09-30T00:00:00Z"},
          {"x": 150, "y":  50, "t": "2026-09-30T00:00:10Z"}
        ]
      }'; echo

echo "== 11. 修订区域 A（改名 + 扩大到 120x120） =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-扩大后",
        "polygon": [[0,0],[120,0],[120,120],[0,120]]
      }'; echo

echo "== 12. 删除区域 B（动态取 id） =="
B_ID=$(curl -fsS "$BASE_URL/api/regions" \
  | python3 -c 'import sys,json; print(next(r["id"] for r in json.load(sys.stdin) if r["name"]=="B区-吊装半径"))')
curl -fsS -X DELETE "$BASE_URL/api/regions/$B_ID"; echo

echo "完成。"
