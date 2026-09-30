#!/usr/bin/env bash
# 频点占用请求示例（需服务已启动，默认 http://localhost:8000）
#
# 演示：现场其他设备按时段占用场次频点 —— 登记 / 查询 / 撤销占用记录，
# 以及占用对规划发布与逐组切换的影响。已发布版不因占用变化而重写。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 创建场次（可用频点 1、4）并录入两个设备组 =="
SESSION=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "占用演示", "frequencies": [1, 4]}')
echo "$SESSION"
SID=$(echo "$SESSION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
HA=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "音响", "start": 0, "end": 120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
HB=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "灯光", "start": 60, "end": 180}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $HA, \"group_b\": $HB, \"min_separation\": 3}"; echo

echo "== 2. 首次发布（base_version=0）：分配 (音响=1, 灯光=4) =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 0, "locks": []}'; echo

echo "== 3. 登记占用：频点 4 在 [0,60) 被其他设备占用 =="
echo "   （与音响 [0,120) 相交；与灯光 [60,180) 端点相接，不算相交）"
OCC=$(curl -s -X POST "$BASE/sessions/$SID/occupancies" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 4, "start": 0, "end": 60}')
echo "$OCC"
O1=$(echo "$OCC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 4. 查询当前占用（场次详情中亦包含） =="
curl -s "$BASE/sessions/$SID/occupancies"; echo

echo "== 5. 再次发布：灯光与占用端点相接仍可用频点 4，分配不变、改频 0 =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "locks": []}'; echo

echo "== 6. 再登记占用：频点 1 在 [30,90) 被占用（与两组均相交） =="
OCC=$(curl -s -X POST "$BASE/sessions/$SID/occupancies" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 1, "start": 30, "end": 90}')
echo "$OCC"
O2=$(echo "$OCC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 7. 占用导致无解：发布返回 422 并指出相关占用，发布版不变 =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 2, "locks": []}' | python3 -m json.tool
curl -s "$BASE/sessions/$SID/plans"; echo

echo "== 8. 撤销占用记录 $O2 后恢复可解；重复撤销返回 404 =="
curl -s -X DELETE "$BASE/sessions/$SID/occupancies/$O2"; echo
curl -s -o /dev/null -w '重复撤销: HTTP %{http_code}\n' \
  -X DELETE "$BASE/sessions/$SID/occupancies/$O2"
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 2, "locks": []}'; echo

echo "== 9. 已发布版不可变：版本 1 仍为 (音响=1, 灯光=4) =="
curl -s "$BASE/sessions/$SID/plans/1"; echo

echo "== 10. 校验失败示例（均拒绝整次操作） =="
curl -s -o /dev/null -w '起点不早于终点: HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/occupancies" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 4, "start": 10, "end": 10}'
curl -s -o /dev/null -w '频点不属于场次: HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/occupancies" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 99, "start": 0, "end": 60}'
curl -s -X DELETE "$BASE/sessions/$SID/occupancies/$O1" > /dev/null
