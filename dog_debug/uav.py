"""室内巡检无人机 MQTT 的解读：各模块（Bridge、VFC、GW、Vision、FSVR 等）的状态、VFC 告警、残留保留指令、云端与本地协议映射

依据《SLAM无人机-MQTT通信协议.md》与《MQTT接口字段说明_20261105_源码版(3).md》（Bridge 0.2.0-rc2）。
板上时钟不准，时效一律按本机收到的时间算，报文里的 ts 只用来显示。
"""

import dataclasses
import math
import re

from .core import BRIDGE

VFC = "local/uav/vfc/"
# 往这两个主题发的是指令；上面的保留消息在 VFC 或 Bridge 重启、重连时会被再收到一遍，VFC 分不出来会照样执行
COMMAND_TOPICS = (VFC + "command", BRIDGE + "command")

# 开始新任务的 VFC 指令：bid 新生成（按钮是“换新 bid”）；其他 VFC 指令带当前任务的 bid
# 返航、降落、录制航线、自检也是新任务
NEW_TASK = ("start_multi_nav_by_points", "start_mapping", "start_nav_return_home", "land", "start_record", "start_self_check")

# 状态：键 → (名称, 多久没新消息算过期（秒，None 为不按时效判断）)；键的顺序就是模块卡片里各行的顺序
DOMAINS = {
    "online": ("在线", None),
    "system": ("任务环境", 3),
    "localization": ("定位", 3),
    "relocalization": ("重定位", 3),
    "navigation": ("导航", 3),
    "mapping": ("建图", 3),
    "flight": ("飞控桥", 3),
    "fcu": ("飞控实时", 2),
    "pose": ("位姿", 2),
    "subsystem_status": ("在线", 5),
    "nav_status": ("任务", 3),
    "nav_status_progress": ("航点进度", None),
    "nav_action_progress": ("航点动作", None),
    "mapping_status": ("建图", 3),
    "loc_status": ("定位", 3),
    "map_status": ("地图", 3),
    "sync_map_file_status": ("地图同步", None),
    "self_check_result": ("自检结果", None),
    "pcd": ("稀疏点云", 3),
    "vfc_osd": ("OSD", 3),
    "flyState": ("飞行状态", 3),
}
MODULES = ("bridge", "vfc", "gw", "vision", "ptz", "vtc", "fsvr")
GENERIC_ONLINE = ("subsystem_status", "subsystem_state")
EVENTS = VFC + "events"

# VFC 收到这些 GW 指令时会给 Bridge 发对应的命令
VFC_TO_BRIDGE = {"start_multi_nav_by_points": "system.start", "pause_nav": "navigation.pause",
                 "continue_nav": "navigation.resume", "stop_nav": "navigation.pause",
                 "start_nav_return_home": "navigation.return_home", "land": "flight.land",
                 "start_mapping": "system.start", "stop_mapping": "system.stop"}
VFC_AUTO = {"navigation.next", "system.stop", "system.status"}
LINK_SECONDS = 5


# 云端报文（tta/uav/{sn}/...）：services_reply 的 result/msg 在 data 里，state 是 method+data，osd 字段平铺
CLOUD = re.compile(r"^tta/uav/[^/]+/(services|services_reply|state|osd|events)$")
# 按《SLAM无人机-MQTT通信协议.md》：地图管理（get_map_list / sync_map_file / remove_map_by_id / rename_map_name）云端 target 为 fsvr，
# 其余任务/建图/导航/定位/自检指令 target 为 vfc；用户可在指令页 target 下拉框随时改（如旧 GW 需改 vfc / gw）
CLOUD_TARGETS = {
    "get_map_list": "fsvr",
    "sync_map_file": "fsvr",
    "remove_map_by_id": "fsvr",
    "rename_map_name": "fsvr",
}
GW_TARGETS = ("vfc", "fsvr", "gw", "ptz", "vision", "audio")
GW_STATES = ("storage_status",)


def cloud_topic(sn):
    return f"tta/uav/{sn}/services"


def cloud_command(payload):
    """本地格式的 VFC 指令加上云端要的 target（GW 转给 VFC 时会去掉）；已经填了 target 的不动"""
    if isinstance(payload, dict) and "target" not in payload:
        payload["target"] = CLOUD_TARGETS.get(payload.get("method"), "vfc")
    return payload


def local_view(event):
    """云端报文换成本地等价的样子（主题和外壳），后面的解读只认本地格式；不是云端报文原样返回。原始报文照样入库，对照时看原文"""
    match = CLOUD.match(event.topic)
    if not match:
        return event
    kind, p = match[1], event.payload
    if kind == "services":
        payload = {k: v for k, v in p.items() if k != "target"}
        return dataclasses.replace(event, topic=VFC + "command", payload=payload)
    if kind == "services_reply":
        body = p.get("data") if isinstance(p.get("data"), dict) else {}
        payload = {k: v for k, v in p.items() if k not in ("data", "target")}
        payload.update(result=body.get("result", 2), msg=body.get("msg", ""), data=body.get("data"))
        for extra_key in ("quota", "selection_error", "results"):
            if extra_key in body and extra_key not in payload:
                payload[extra_key] = body[extra_key]
        return dataclasses.replace(event, topic=VFC + "ack", payload=payload)
    if kind == "state":
        method, body = p.get("method"), p.get("data")
        if method == "subsystem_status" and isinstance(body, list):
            entry = next((m for m in body if isinstance(m, dict) and m.get("name") == "vfc"), {"name": "vfc", "state": 0})
            return dataclasses.replace(event, topic=VFC + "state", payload={"type": method, "data": entry})
        if method == "alarm":
            # 《SLAM无人机-MQTT通信协议.md》中飞控告警发在 tta/uav/{sn}/state，method="alarm"
            return dataclasses.replace(event, topic=EVENTS, payload={"type": "alarm", "info": body if isinstance(body, dict) else {}})
        if method in GW_STATES:
            return dataclasses.replace(event, topic="local/uav/gw/state", payload={"type": method, "info": body})
        body = body if isinstance(body, dict) else {}
        return dataclasses.replace(event, topic=VFC + "state", payload={"type": method, "info": body, "data": body})
    if kind == "osd":
        return dataclasses.replace(event, topic=VFC + "osd", payload={"data": p})
    return dataclasses.replace(event, topic=EVENTS,
                               payload={"type": p.get("method") or p.get("type"), "info": p.get("data") or p.get("info")})


def title_of(key):
    if key in DOMAINS:
        return DOMAINS[key][0]
    kind = key.split(":", 1)[1]
    return "在线" if kind in GENERIC_ONLINE else kind


def domain(event):
    """这条消息是哪一类状态：Bridge 领域、VFC 状态，或别的模块的“模块:类型”；不是无人机状态返回 None"""
    event = local_view(event)
    topic = event.topic
    if topic.startswith(BRIDGE):
        rest = topic[len(BRIDGE):]
        if rest == "heartbeat":
            return "online"
        if rest == "data":
            return "pose"
        if rest == "fcu":
            return "fcu"
        if rest.startswith("state/"):
            return rest[len("state/"):]
        return None
    if topic == VFC + "osd":
        return "vfc_osd"
    if topic == VFC + "state":
        kind = event.payload.get("type")
        return "subsystem_status" if kind == "subsystem_state" else kind if kind in DOMAINS else None
    parts = topic.split("/")
    if len(parts) == 4 and parts[3] == "state" and parts[2] not in ("vfc", "bridge"):
        kind = event.payload.get("type") or event.payload.get("method")
        return f"{parts[2]}:{kind}" if isinstance(kind, str) and kind else None
    return None


# FCU 实时状态枚举（Bridge 0.2.0-rc2 4.8 节）
FCU_LANDED = {0: "未知", 1: "地面", 2: "空中", 3: "起飞中", 4: "降落中"}
FCU_SYS_STATUS = {3: "待命", 4: "运行", 5: "紧急", 6: "严重故障"}

# 自检项与分级（SLAM无人机-MQTT通信协议.md）
SELF_CHECK_NAMES = {
    "SELFTEST-001": "飞控",
    "SELFTEST-002": "激光雷达",
    "SELFTEST-003": "电池供电",
    "SELFTEST-004": "电机",
    "SELFTEST-005": "载荷",
    "SELFTEST-006": "定位系统",
}
SELF_CHECK_ORDER = {"pass": 0, "warn": 1, "degraded": 2, "ban": 3}
SELF_CHECK_LABELS = {"pass": "通过", "warn": "仅告警", "degraded": "降级可飞", "ban": "禁飞"}


def self_check_verdict(items):
    """按协议规则取已知自检项中最重的一级（未知 code 忽略）"""
    worst, rank = "pass", 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("code") not in SELF_CHECK_NAMES:
            continue
        grade = str(item.get("grade") or "")
        score = SELF_CHECK_ORDER.get(grade, -1)
        if score > rank:
            worst, rank = grade, score
    return worst


def reading(event):
    """状态卡上显示的内容：(状态词, 说明, result, 附加信息)；result 0 正常、1 失败、2 未知或过期"""
    event = local_view(event)
    payload = event.payload
    key = domain(event)
    if key == "vfc_osd":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        measured = lambda name: isinstance(data.get(name), (int, float)) and not isinstance(data[name], bool) and math.isfinite(data[name])
        value = lambda name, unit="": f"{data[name]}{unit}" if measured(name) else "—"
        bad = [name for name in OSD_KEYS if name in data and not measured(name)]
        extra = f"速度 {value('speed', ' m/s')} · 航向 {value('heading', '°')}" + (f"（{'、'.join(bad)} 不是数字）" if bad else "")
        flags = [f"{text} {_yes(data[name]) if isinstance(data[name], bool) else data[name]}" for name, text in OSD_FLAGS if name in data]
        if flags:
            extra += " · " + " · ".join(flags)
        return f"电量 {value('batteryRemain', '%')}", f"x {value('x')}　y {value('y')}　z {value('z')}（米）", 2 if bad else 0, extra
    if key == "flyState":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        flying = {1: "飞行中", 0: "未飞行"}.get(info.get("flightState"), "—")
        ctrl = CTRL_MODES.get(info.get("ctrlMode"), "—" if info.get("ctrlMode") is None else str(info.get("ctrlMode")))
        return flying, f"控制模式 {ctrl}", 0, f"飞控模式 {info.get('mode') or '—'} · 已飞 {fly_time(info.get('flyingTime'))}"
    if key == "subsystem_status":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return "在线" if data.get("state") == 1 else f"state {data.get('state')}", f"v{data.get('version', '?')}", 0, ""
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    result, msg = info.get("result", 2), str(info.get("msg", ""))
    if key == "nav_status_progress":
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        return f"{data.get('current_index', '?')} / {data.get('count', '?')}", msg, result, f"任务 {info.get('flight_id') or '—'}"
    if key == "nav_status":
        reason = f" · 原因 {REASONS.get(info['reason'], info['reason'])}" if info.get("reason") else ""
        return str(info.get("data", "")), msg, result, f"任务 {info.get('flight_id') or '—'}" + reason
    if key == "mapping_status":
        raw = info.get("raw")
        extra = " · ".join(part for part in (f"Bridge {raw}" if raw else "", f"session {info.get('session_id') or '—'}",
                                             f"资产 {info.get('assets')}" if info.get("assets") else "",
                                             f"资产任务 {info['map_job_id']}" if info.get("map_job_id") else "") if part)
        return str(info.get("data", "")), msg, result, extra
    if key == "loc_status":
        raw = info.get("raw") if isinstance(info.get("raw"), dict) else {}
        return (str(info.get("data", "")), msg, result,
                f"Bridge 定位 {raw.get('localization') or '—'} · 重定位 {raw.get('relocalization') or '—'}")
    if key == "map_status":
        parts = [f"地图 {info['map_id']}" if info.get("map_id") else "", f"会话 {info['session_id']}" if info.get("session_id") else "",
                 "保存后自动建资产" if info.get("auto") else "", "自动建资产任务" if info.get("map_job_auto") else "",
                 f"资产任务 {info['map_job_id']}" if info.get("map_job_id") else "",
                 f"地图忙（{info.get('map_busy_reason') or '原因未写'}）" if info.get("map_busy") else "",
                 f"原因 {info['reason']}" if info.get("reason") else "", f"阶段 {info['stage']}" if info.get("stage") else "",
                 f"退出码 {info['exit_code']}" if info.get("exit_code") not in (None, "") else "",
                 f"日志 {info['job_log']}" if info.get("job_log") else "",
                 "已清掉原选中的同名地图" if info.get("selection_cleared") else ""]
        return str(info.get("data", "")), msg, result, " · ".join(p for p in parts if p)
    if key == "sync_map_file_status":
        state = str(info.get("state", ""))
        percent = info.get("percent")
        maps = info.get("data") if isinstance(info.get("data"), list) else []
        word = "上传中" if state == "1" else ("同步完成" if result == 0 else "同步失败") if state == "0" else (state or "—")
        pct = f"{percent}%" if isinstance(percent, (int, float)) and not isinstance(percent, bool) else "—"
        extra = f"进度 {pct} · 共 {info.get('total', len(maps))} 张" + (f" · {', '.join(str(m) for m in maps)}" if maps else "")
        return word, msg, result, extra
    if key == "self_check_result":
        items = info.get("items") if isinstance(info.get("items"), list) else []
        finished = bool(info.get("finished"))
        verdict = self_check_verdict(items)
        word = f"{'完成' if finished else '自检中'} · {SELF_CHECK_LABELS.get(verdict, verdict)}"
        bad = [f"{SELF_CHECK_NAMES.get(i.get('code'), i.get('code'))}({SELF_CHECK_LABELS.get(i.get('grade'), i.get('grade'))})"
               for i in items if isinstance(i, dict) and i.get("code") in SELF_CHECK_NAMES and i.get("grade") != "pass"]
        extra = f"已完成 {len(items)} 项" + (f" · 异常：{'、'.join(bad)}" if bad else " · 全部正常") + (f" · bid {info.get('bid')}" if info.get("bid") else "")
        out_res = 1 if verdict == "ban" else 2 if verdict in ("degraded", "warn") else 0
        return word, msg, out_res, extra
    if key == "pcd":
        src = info.get("source") or "lidar"
        cnt, src_cnt, leaf, seq = info.get("count", "—"), info.get("src_count", "—"), info.get("leaf", "—"), info.get("seq", "—")
        enc = info.get("encoding") or "—"
        return f"{cnt} 点", f"{src} · seq {seq}", 0 if enc == "xyz_i16_i8" else 2, f"原始 {src_cnt} · leaf {leaf}m · 编码 {enc}"
    if key == "nav_action_progress":
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        actions = data.get("actions") if isinstance(data.get("actions"), list) else []
        done = "、".join(action_text(a) for a in actions if isinstance(a, dict))
        index = data.get("point_index")
        where = f"第 {index + 1} 个航点" if isinstance(index, int) and not isinstance(index, bool) else "第 ? 个航点"
        return where, msg, result, (done or "没有动作") + f" · 任务 {info.get('flight_id') or '—'}"
    if ":" in str(key):
        return _generic_reading(payload)
    detail = info.get("detail") if isinstance(info.get("detail"), dict) else {}
    extra = ""
    if key == "navigation":
        extra = (f"会话 {detail.get('session_id') or '—'} · 航点共 {detail.get('waypoint_total') if detail.get('waypoint_total') is not None else '—'}"
                 f" · 最后完成编号 {detail.get('waypoint_last_completed', '—')}（从 0 起，-1 为还没有）")
    elif key == "system":
        parts = [f"{name} {detail[name]}" for name in ("profile", "stage", "session_id", "ros") if detail.get(name)]
        if detail.get("map_busy"):
            parts.append(f"地图忙({detail.get('map_busy_reason') or '—'}" + ("/自动" if detail.get("map_job_auto") else "") + ")")
        fc = detail.get("flight_commands")
        if isinstance(fc, dict):
            parts.append(f"MQTT飞控 {'开' if fc.get('mqtt_flight_control') else '关'}")
        extra = " · ".join(parts)
    elif key == "mapping" and detail:
        parts = [f"会话 {detail['session_id']}" if detail.get("session_id") else "",
                 f"资产 {detail['assets']}" if detail.get("assets") else "",
                 f"任务 {detail['map_job_id']}" if detail.get("map_job_id") else ""]
        extra = " · ".join(p for p in parts if p)
    elif key == "flight" and detail:
        battery = detail.get("battery_percent")
        extra = (f"模式 {detail.get('mode') or '—'} · 解锁 {_yes(detail.get('armed'))} · 落地 {_yes(detail.get('landed'))}"
                 f" · 起飞完成 {_yes(detail.get('takeoff_complete'))}"
                 + (f" · 电量 {battery:.0f}%" if isinstance(battery, (int, float)) else ""))
        if info.get("data") == "STALE":
            extra += "（整体 STALE，detail 仍在更新）"
    elif key == "fcu" and detail:
        battery = detail.get("battery_percent")
        volt = detail.get("battery_voltage")
        landed = FCU_LANDED.get(detail.get("landed_state"), "—")
        sys_st = FCU_SYS_STATUS.get(detail.get("system_status"), "—")
        stale = detail.get("stale") if isinstance(detail.get("stale"), list) else []
        parts = [f"模式 {detail.get('mode') or '—'}", f"解锁 {_yes(detail.get('armed'))}", f"状态 {landed}/{sys_st}"]
        if isinstance(battery, (int, float)):
            parts.append(f"电量 {battery:.0f}%" + (f"({volt:.2f}V)" if isinstance(volt, (int, float)) else ""))
        if stale:
            parts.append("过期:" + ",".join(str(s) for s in stale))
        extra = " · ".join(parts)
    elif key == "localization" and detail:
        extra = f"对齐 {detail.get('aligned')} · 坐标锁定 {detail.get('frame_locked')}"
    elif key == "relocalization" and detail.get("text"):
        extra = str(detail["text"])
    elif key == "pose" and detail:
        extra = " ".join(f"{name}={detail[name]:.2f}" if isinstance(detail[name], (int, float)) else f"{name}={detail[name]}"
                         for name in ("x", "y", "z", "yaw_deg", "speed") if name in detail)
    return str(info.get("data", "")), msg, result, extra


def fly_time(value):
    """flyingTime（秒，VFC 按离地时刻算，在地面是 0）显示成 mm:ss；没有这个字段是“—”"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "—"
    return f"{int(value) // 60:02d}:{int(value) % 60:02d}"


def action_text(action):
    """一个航点动作：名字、状态（中文）、用时；停留（pause）写成“停留 已过/共 s”"""
    name, state = str(action.get("action", "?")), str(action.get("state", ""))
    word = ACTION_STATES.get(state, state)
    elapsed = action.get("elapsed_ms")
    seconds = elapsed / 1000 if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else None
    param = action.get("param") if isinstance(action.get("param"), list) else []
    if name == "pause":
        total = param[0] if param and isinstance(param[0], (int, float)) and not isinstance(param[0], bool) else None
        clock = f"{seconds:.0f}" if seconds is not None else "—"
        return f"停留 {clock}/{total if total is not None else '?'} s {word}"
    return f"{name} {word}" + (f" {seconds:.1f} s" if seconds else "")


def _generic_reading(payload):
    """还没专门解读的模块状态：在线消息看 data.state，其余取 info / data 里的 data、state、result、msg，再摘几个字段"""
    kind = payload.get("type") or payload.get("method")
    body = payload.get("info") if isinstance(payload.get("info"), dict) else payload.get("data")
    body = body if isinstance(body, dict) else {}
    if kind in GENERIC_ONLINE:
        state = body.get("state")
        return ("在线" if state == 1 else f"state {state}"), f"v{body.get('version', '?')}", 0 if state == 1 else 2, ""
    word = body.get("data") if isinstance(body.get("data"), (str, int, float)) else body.get("state", "")
    result = body.get("result", 0) if isinstance(body.get("result"), int) else 0
    brief = " · ".join(f"{k}={v}" for k, v in body.items() if k not in ("data", "msg", "result") and isinstance(v, (str, int, float, bool)))[:120]
    return str(word), str(body.get("msg", "")), result, brief


def _yes(value):
    return {True: "是", False: "否", None: "—"}.get(value, str(value))


OSD_KEYS = ("x", "y", "z", "speed", "heading", "batteryRemain")
OSD_FLAGS = (("flightMode", "模式"), ("armed", "解锁"), ("landed", "落地"), ("takeoffComplete", "起飞完成"),
             ("mapId", "地图"), ("batteryVoltage", "电压"))
ACTION_STATES = {"pending": "待做", "running": "进行中", "done": "完成", "failed": "失败", "skipped": "跳过"}
CTRL_MODES = {0: "姿态", 1: "定高", 2: "定点", 3: "offboard"}
REASONS = {
    "stop": "结束航线",
    "return_home": "返航",
    "land": "降落",
    "stick": "摇杆接管",
    "low_battery": "低电量",
    "loc_lost": "定位丢失",
    "link_lost": "通信中断超时",
}
WAITING_NEXT = "WAYPOINT_COMPLETE_WAITING_EXTERNAL_TRIGGER"
YAW_DONE = {"HOLD", "WAITING_TRAJECTORY"}
FLIGHT_FLAGS = (("armed", "解锁"), ("landed", "落地"), ("mode", "模式"), ("takeoff_complete", "起飞完成"))


def changes(before, event):
    """和这一领域的上一条比，有哪些值得记下来的变化：[(文字, 是否醒目)]；before 为 None 时是第一次收到"""
    event, before = local_view(event), before and local_view(before)
    key = domain(event)
    name = DOMAINS.get(key, (key, None))[0]
    word, msg, _, extra = reading(event)
    old = reading(before) if before else None
    out = []
    if key in ("nav_status", "nav_status_progress"):
        if key == "nav_status_progress":
            out.append((f"航点进度 {word}：{msg}", True))
        elif not old or (word, msg) != old[:2]:
            loud = "已到达" in msg or "航线完成" in msg or "返航完成" in msg or "打断" in msg or word in ("Succeeded", "Failed", "Cancelled")
            out.append((f"VFC 任务 {word}：{msg}", loud))
        return out
    if key == "flyState":
        if not old or (word, msg, extra) != (old[0], old[1], old[3]):
            out.append((f"飞行状态 {word} · {msg} · {extra}", bool(old) and word != old[0]))
        return out
    if key in ("subsystem_status", "pose", "vfc_osd", "fcu", "pcd"):
        return out
    if key == "mapping_status":
        if not old or (word, msg) != old[:2]:
            out.append((f"VFC 建图 {word}：{msg}", word in ("MappingSaveEnd", "MappError")))
        return out
    if key == "loc_status":
        if not old or word != old[0]:
            out.append((f"VFC 定位 {old[0] if old else '—'} → {word}：{msg}", word in ("LocLost", "Error")))
        return out
    if key == "map_status":
        if not old or (word, msg) != old[:2]:
            out.append((f"VFC 地图 {word}" + (f"：{msg}" if msg else ""), word not in ("IDLE", "BUILDING", "MAP_AUTO_BUILD_WAITING")))
        return out
    if key == "sync_map_file_status":
        if not old or word != old[0]:
            out.append((f"地图同步 {word}：{extra}" + (f"（{msg}）" if msg else ""), word in ("同步完成", "同步失败")))
        return out
    if key == "self_check_result":
        if not old or (word, extra) != (old[0], old[3]):
            out.append((f"自检结果 {word}：{extra}", word.startswith("完成") or "禁飞" in word or "降级" in word))
        return out
    if key == "nav_action_progress":
        states = lambda e: [(a.get("action"), a.get("state")) for a in
                            ((((e.payload.get("info") or {}).get("data") or {}).get("actions")) or []) if isinstance(a, dict)]
        if not old or (word, states(event)) != (old[0], states(before)):
            failed = any(state == "failed" for _, state in states(event))
            out.append((f"航点动作 {word}：{msg or ''} {extra}".strip(), failed))
        return out
    if ":" in str(key):
        if old and word != old[0]:
            out.append((f"{key.split(':')[0].upper()} {title_of(key)} {old[0]} → {word}", False))
        return out
    if not old or word != old[0]:
        text = f"{name} {old[0] if old else '—'} → {word}"
        if key == "navigation" and word == WAITING_NEXT:
            text += "（到点，等待放行）"
        if key == "flight" and old and old[0] == "PRE_YAW" and word in YAW_DONE:
            text += "（转向完成，可以放行）"
        out.append((text, key == "navigation" and word == WAITING_NEXT or key == "flight" and bool(old) and old[0] == "PRE_YAW"))
    info = event.payload.get("info") if isinstance(event.payload.get("info"), dict) else {}
    detail = info.get("detail") if isinstance(info.get("detail"), dict) else {}
    was = ((before.payload.get("info") or {}).get("detail") or {}) if before else {}
    if key == "navigation" and "waypoint_last_completed" in detail and detail.get("waypoint_last_completed") != was.get("waypoint_last_completed"):
        out.append((f"最后完成航点编号 {was.get('waypoint_last_completed', '—')} → {detail['waypoint_last_completed']}", True))
    if key == "flight":
        for field, label in FLIGHT_FLAGS:
            if before and field in detail and was.get(field) != detail[field]:
                out.append((f"飞控{label} {_yes(was.get(field))} → {_yes(detail[field])}", False))
    return out


def freshness(event, now):
    """收到上报的时效（和数据本身好不好分开看）：“保留快照” / “超时没收到” / “实时”"""
    if event.retained:
        return "保留快照"
    limit = DOMAINS.get(domain(event), (None, None))[1]
    return "超时没收到" if limit and now - event.timestamp > limit else "实时"


def quality(event):
    """数据本身：“正常” / “失败” / “未知” / “不适用”；Bridge 的 DISABLED 是这一领域当前不适用，不算未知"""
    event = local_view(event)
    word, _, result, _ = reading(event)
    key = domain(event)
    if key == "mapping_status":
        return {"MappError": "失败", "Unknown": "未知", "Passive": "不适用"}.get(word, "正常")
    if key == "map_status":
        verdict = {"MAP_BUILD_FAILED": "失败", "MAP_BUILD_TIMEOUT": "失败", "MAP_AUTO_BUILD_UNCERTAIN": "未知",
                   "MAP_AUTO_BUILD_SKIPPED": "不适用"}.get(word)
        if verdict:
            return verdict
    if key == "self_check_result":
        return "失败" if result == 1 else "未知" if result == 2 else "正常"
    if key == "nav_action_progress":
        info = event.payload.get("info") if isinstance(event.payload.get("info"), dict) else {}
        actions = ((info.get("data") or {}) if isinstance(info.get("data"), dict) else {}).get("actions") or []
        if any(isinstance(a, dict) and a.get("state") == "failed" for a in actions):
            return "失败"
        return "正常" if result == 0 else "未知"
    if word == "DISABLED":
        return "不适用"
    if word == "OFFLINE" or result == 1:
        return "失败"
    return "未知" if word in ("STALE", "UNKNOWN") or result == 2 else "正常"


def alarm_of(event):
    """VFC 告警的内容（code、level、state raise/clear、title、advice、x/y/z、detail）；不是告警返回 None"""
    event = local_view(event)
    if event.topic != EVENTS or event.payload.get("type") != "alarm":
        return None
    info = event.payload.get("info")
    return info if isinstance(info, dict) and info.get("code") else None


def retained_command(event):
    """指令主题上收到的保留消息（非空）：Broker 会把它补发给每个新订阅者，执行方重启后会再执行一遍"""
    return local_view(event).topic in COMMAND_TOPICS and event.retained and bool(event.payload or event.raw.strip())


def link(events):
    """把 VFC 指令和它触发的 Bridge 命令排成时间线，按时间先后返回行"""
    rows, by_key = [], {}
    for e in sorted((local_view(e) for e in events), key=lambda e: e.timestamp):
        if e.kind not in ("指令", "ACK") or not e.tid:
            continue
        source = "Bridge" if e.topic.startswith(BRIDGE) else "VFC"
        key = (source, e.session, e.tid)
        row = by_key.get(key)
        if row is None:
            row = by_key[key] = {"key": key, "source": source, "action": e.method, "events": [], "parent": None,
                                 "origin": "", "confirmed": False, "at": e.timestamp}
            rows.append(row)
        row["events"].append(e)
    vfc_rows = [r for r in rows if r["source"] == "VFC"]
    linked = set()
    for row in rows:
        if row["source"] != "Bridge":
            continue
        command = next((e for e in row["events"] if e.kind == "指令"), None)
        if command is None:
            row["origin"] = "只收到回执"
            continue
        if command.payload.get("client") != "vfc":
            row["origin"] = f"外部（{command.payload.get('client') or '未写 client'}）"
            continue
        candidates = [r for r in vfc_rows if r["key"] not in linked and r["key"][1] == row["key"][1]
                      and VFC_TO_BRIDGE.get(r["action"]) == row["action"] and 0 <= row["at"] - r["at"] <= LINK_SECONDS]
        if candidates:
            parent = candidates[-1]
            linked.add(parent["key"])
            row["parent"], row["origin"] = parent["key"], f"VFC 指令 {parent['key'][2]}"
            row["confirmed"] = _quoted(parent, row)
        else:
            row["origin"] = "VFC 自动" if row["action"] in VFC_AUTO else "未对上 VFC 指令"
    return rows


def _quoted(parent, row):
    final = next((e for e in reversed(row["events"]) if e.kind == "ACK" and e.payload.get("final")), None)
    ack = next((e for e in reversed(parent["events"]) if e.kind == "ACK"), None)
    if not final or not ack:
        return False
    info = final.payload.get("info") if isinstance(final.payload.get("info"), dict) else {}
    text = str(ack.payload.get("msg", ""))
    return str(final.payload.get("status", "")) in text and str(info.get("data", "")) in text


def outcome(row):
    """时间线一行的结果：VFC 看 ACK 的 result 和 msg；Bridge 把各次回执的 status 串起来"""
    acks = [e for e in row["events"] if e.kind == "ACK"]
    if not acks:
        return "没收到回执", 2
    if row["source"] == "VFC":
        ack = acks[-1]
        result = ack.payload.get("result", 2)
        return ("受理" if result == 0 else "拒绝" if result == 1 else "未知") + (f"：{ack.payload.get('msg')}" if ack.payload.get("msg") else ""), result
    final = next((e for e in reversed(acks) if e.payload.get("final")), acks[-1]).payload
    info = final.get("info") if isinstance(final.get("info"), dict) else {}
    chain = " → ".join(dict.fromkeys(str(e.payload.get("status")) for e in acks))
    status = {2: "结果不确定"}.get(info.get("result"), "")
    return (chain + (f"（{info.get('data')}：{info.get('msg')}）" if info.get("data") else "")
            + (f"　{status}" if status else "")), info.get("result", 2)
