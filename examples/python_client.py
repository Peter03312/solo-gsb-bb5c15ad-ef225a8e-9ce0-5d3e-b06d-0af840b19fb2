"""临时限行核验服务调用示例（Python 标准库，无需第三方依赖）。

用法：
    python3 examples/python_client.py
可用环境变量 BASE_URL 覆盖服务地址。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def show(title: str, obj: dict) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    show("健康检查", call("GET", "/health"))

    # ---- 出发时刻调度（先做，避免其它区域干扰本演示） ----
    # 登记一个仅在 09-01 [00:00:00,00:01:00) 生效的临检区。
    window_region = call("POST", "/api/regions", {
        "name": "C区-一分钟临检",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": "2026-09-01T00:01:00Z",
    })

    # 固定行程：相对 0s 位于 (-50,50)，60s 后到 (150,50)。
    # 几何接触为闭区间 [15s,45s]（线段插值，非抽样）：
    #   禁止出发 = [vf-45s, vt-15s)
    #           = [2026-08-31T23:59:15Z, 2026-09-01T00:00:45Z)（左闭右开）。
    # 候选窗口 [08-31 23:58, 09-01 00:02) 被切成两段安全。
    schedule = call("POST", "/api/schedule", {
        "points": [
            {"x": -50, "y": 50, "t": 0},
            {"x": 150, "y": 50, "t": 60},
        ],
        "window_start": "2026-08-31T23:58:00Z",
        "window_end": "2026-09-01T00:02:00Z",
    })
    show("安全出发区间（精确区间，非抽样）", schedule)
    for iv in schedule["intervals"]:
        lb = "[" if iv["start_inclusive"] else "("
        rb = "]" if iv["end_inclusive"] else ")"
        print(f"  可出发：{lb}{iv['start']}, {iv['end']}{rb}")
    call("DELETE", f"/api/regions/{window_region['region']['id']}")

    # ---- 区域登记 + 轨迹穿越核验 ----
    region = call("POST", "/api/regions", {
        "name": "A区-示例",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to": "2026-10-01T00:00:00Z",
    })
    show("登记区域", region)

    # 核验一条直线匀速轨迹：(-50,50) -> (150,50)
    # 插值交点：x=0 时为 00:00:15，x=100 时为 00:00:45
    verdict = call("POST", "/api/verify", {
        "points": [
            {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
            {"x": 150, "y": 50, "t": "2026-09-30T00:01:00Z"},
        ]
    })
    show("核验结果", verdict)

    if not verdict["safe"]:
        for r in verdict["results"]:
            for c in r["crossings"]:
                print(
                    f"  ! 区域 {r['region_name']} 被穿越："
                    f"{c['entry_time']} → {c['exit_time']}"
                    f"{'（单点接触）' if c['point_contact'] else ''}"
                )

    show("删除示例区域", call("DELETE", f"/api/regions/{region['region']['id']}"))

    # ---- 批量原子变更：多个登记/修订/删除一次提交 + 轨迹安全核验 ----
    revision = call("GET", "/api/revision")["revision"]
    batch_payload = {
        "expected_revision": revision,
        "operations": [
            {"action": "create", "name": "批量-A区",
             "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
             "valid_from": "2026-09-01T00:00:00Z",
             "valid_to": "2026-10-01T00:00:00Z"},
            {"action": "create", "name": "批量-B区",
             "polygon": [[200, 0], [300, 0], [300, 100], [200, 100]],
             "valid_from": "2026-09-01T00:00:00Z",
             "valid_to": "2026-10-01T00:00:00Z"},
        ],
        # 从区域上方安全经过：整批才会提交；改成 y=50 直穿则会收到
        # 422 unsafe_trajectory，且整批不落库（revision 不推进）
        "trajectories": [
            [{"x": -50, "y": 200, "t": "2026-09-30T00:00:00Z"},
             {"x": 350, "y": 200, "t": "2026-09-30T00:02:00Z"}],
        ],
    }
    try:
        batch_result = call("POST", "/api/regions/batch", batch_payload)
    except urllib.error.HTTPError as exc:
        err = json.loads(exc.read().decode())
        if err["error"]["code"] == "revision_conflict":
            # 并发变更导致过期：用服务端给出的当前 revision 重放一次
            current = err["error"]["details"][0]["current_revision"]
            print(f"revision 过期，改用 {current} 重放")
            batch_payload["expected_revision"] = current
            batch_result = call("POST", "/api/regions/batch", batch_payload)
        else:
            raise
    show("批量原子变更（整批 revision 只 +1）", batch_result)
    for op in batch_result["operations"]:
        print(f"  · {op['action']} -> region_id={op['region_id']} "
              f"version={op['version']}")
    for created in batch_result["operations"]:
        if created["action"] == "create":
            call("DELETE", f"/api/regions/{created['region_id']}")
    show("批量示例区域已清理", {"revision": call("GET", "/api/revision")["revision"]})

    # ---- 通行凭证：签发 / 查询 / 取消，以及凭证对区域变更的约束 ----
    # 轨迹时刻必须在签发时刻之后，这里用当前时间 +5/+10 分钟
    t1 = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    t2 = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    pass_traj = [
        {"x": -50, "y": 200, "t": t1},
        {"x": 350, "y": 200, "t": t2},
    ]
    issued = call("POST", "/api/passes", {"points": pass_traj})
    show("签发通行凭证（安全轨迹才持久化）", issued)
    pass_id = issued["id"]

    show("按 ID 查询凭证状态", call("GET", f"/api/passes/{pass_id}"))

    # 未取消且末点时刻未过的凭证约束区域变更：压住该轨迹的登记会被 409 拒绝
    try:
        call("POST", "/api/regions", {
            "name": "挡路区",
            "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_to": "2030-01-01T00:00:00Z",
        })
    except urllib.error.HTTPError as exc:
        err = json.loads(exc.read().decode())
        show("凭证约束生效：409 pass_conflict", err)
        conflicting = [d["pass_id"] for d in err["error"]["details"]]
        print(f"  冲突凭证 id：{conflicting}")

    # 取消（幂等，重复取消结果不变）；取消后该区域即可登记
    show("取消凭证", call("POST", f"/api/passes/{pass_id}/cancel"))
    show("重复取消（结果不变）", call("POST", f"/api/passes/{pass_id}/cancel"))
    created = call("POST", "/api/regions", {
        "name": "挡路区",
        "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to": "2030-01-01T00:00:00Z",
    })
    show("取消后原冲突区域可登记", created)
    call("DELETE", f"/api/regions/{created['region']['id']}")

    # ---- 择时通行承诺：相对秒数固定行程 + 带时区候选出发窗口 ----
    # 先登记一个短时生效区域，使禁止出发区间只覆盖候选窗口中段
    t_win = datetime.now(timezone.utc) + timedelta(seconds=600)
    commit_region = call("POST", "/api/regions", {
        "name": "承诺演示-一分钟",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "valid_from": (t_win + timedelta(seconds=90)).isoformat(),
        "valid_to": (t_win + timedelta(seconds=150)).isoformat(),
    })
    # 固定行程 (-50,50) t=0 → (150,50) t=60，几何接触为相对 [15,45]；
    # 禁止出发区间 [vf-45s, vt-15s) 落在窗口内，把窗口切成两段安全区间
    issued_commit = call("POST", "/api/commitments", {
        "points": [
            {"x": -50, "y": 50, "t": 0},
            {"x": 150, "y": 50, "t": 60},
        ],
        "window_start": t_win.isoformat(),
        "window_end": (t_win + timedelta(seconds=300)).isoformat(),
    })
    show("签发择时通行承诺（两段当前可用安全区间）", issued_commit)
    commitment_id = issued_commit["id"]
    for iv in issued_commit["intervals"]:
        lb = "[" if iv["start_inclusive"] else "("
        rb = "]" if iv["end_inclusive"] else ")"
        print(f"  可出发：{lb}{iv['start']}, {iv['end']}{rb}")

    show("查询承诺当前可用区间", call("GET", f"/api/commitments/{commitment_id}"))

    # 未取消且窗口未结束的承诺约束区域变更：抹掉全部未来安全时刻会被 409 拒绝
    try:
        call("POST", "/api/regions", {
            "name": "承诺演示-全时段",
            "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_to": "2032-01-01T00:00:00Z",
        })
    except urllib.error.HTTPError as exc:
        err = json.loads(exc.read().decode())
        show("承诺约束生效：409 commitment_conflict", err)
        print(f"  冲突承诺 id：{[d['commitment_id'] for d in err['error']['details']]}")

    # 取消（幂等）；取消后该区域即可登记
    show("取消承诺", call("POST", f"/api/commitments/{commitment_id}/cancel"))
    show("重复取消（结果不变）", call("POST", f"/api/commitments/{commitment_id}/cancel"))
    unblocked = call("POST", "/api/regions", {
        "name": "承诺演示-全时段",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_to": "2032-01-01T00:00:00Z",
    })
    show("取消后原冲突区域可登记", unblocked)
    # 清理本演示产生的区域
    call("DELETE", f"/api/regions/{commit_region['region']['id']}")
    call("DELETE", f"/api/regions/{unblocked['region']['id']}")

    # ---- 安全状态观察：登记 / 查询 / 取消 + 结论翻转事件分页 ----
    wide = {"valid_from": "2020-01-01T00:00:00Z", "valid_to": "2030-01-01T00:00:00Z"}
    # 观察 1：从区域上方 y=200 经过；此刻演示区域均已清理，结论 safe
    obs_safe = call("POST", "/api/observations", {"points": [
        {"x": -50, "y": 200, "t": t1},
        {"x": 350, "y": 200, "t": t2},
    ]})
    show("登记观察（上方安全轨迹，安全/不安全均可登记）", obs_safe)
    obs1_id = obs_safe["id"]

    obs_region = call("POST", "/api/regions", {
        "name": "观察演示-A区",
        "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
        **wide,
    })
    obs_region_id = obs_region["region"]["id"]

    # 观察 2：直穿 A 区的轨迹——不安全同样可以登记（登记不阻止任何变更）
    obs_unsafe = call("POST", "/api/observations", {"points": [
        {"x": -50, "y": 50, "t": t1},
        {"x": 350, "y": 50, "t": t2},
    ]})
    show("登记观察（直穿区域，结论 unsafe 同样登记）", obs_unsafe)
    obs2_id = obs_unsafe["id"]

    # 扩大 A 区到覆盖 y=200：观察 1 safe→unsafe（一条事件）；观察 2 结论不变
    call("PATCH", f"/api/regions/{obs_region_id}",
         {"polygon": [[0, 0], [400, 0], [400, 300], [0, 300]]})
    # 删除 A 区：两条观察都 unsafe→safe（各再追加一条事件）
    call("DELETE", f"/api/regions/{obs_region_id}")

    show("按编号查询观察（轨迹/状态/最近结论/revision）",
         call("GET", f"/api/observations/{obs1_id}"))

    # 事件按观察内序号游标升序分页：limit=1 翻页直到 next_cursor 为 None
    print("\n== 观察 1 的翻转事件（游标分页，每页 1 条） ==")
    cursor = 0
    while True:
        page = call("GET", f"/api/observations/{obs1_id}/events?limit=1&cursor={cursor}")
        for e in page["events"]:
            print(f"  seq={e['seq']} {e['old_conclusion']}→{e['new_conclusion']}"
                  f" @revision={e['revision']}")
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]

    # 取消（幂等，重复取消结果不变）；取消后不再产生事件
    show("取消观察 1", call("POST", f"/api/observations/{obs1_id}/cancel"))
    show("重复取消（结果不变）", call("POST", f"/api/observations/{obs1_id}/cancel"))
    show("取消观察 2", call("POST", f"/api/observations/{obs2_id}/cancel"))


if __name__ == "__main__":
    main()
