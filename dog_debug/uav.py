"""室内巡检无人机本地 MQTT 的解读：各模块（Bridge、VFC、GW、Vision 等）的状态、VFC 告警、残留的保留指令、VFC 指令和 Bridge 命令的对应

依据 nav/MQTT接口字段说明_20261105_源码版.md（Bridge）和 nav/tta_vfc 的 TTA_VFC_MqttProtocol.cpp（VFC 1.0.0）。
板上时钟不准（实测慢约 36 h），时效一律按本机收到的时间算，报文里的 ts 只用来显示
"""

import dataclasses
import math
import re

from .core import BRIDGE

VFC = "local/uav/vfc/"
# 往这两个主题发的是指令；上面的保留消息在 VFC 或 Bridge 重启、重连时会被再收到一遍，VFC 分不出来会照样执行
COMMAND_TOPICS = (VFC + "command", BRIDGE + "command")

# 开始新任务的 VFC 指令：bid 新生成（按钮是“换新 bid”）；其他 VFC 指令带当前任务的 bid
# 返航、降落也是新任务（VFC 994c1ae：原任务报 Cancelled，再用新 bid 报 Active）
NEW_TASK = ("start_multi_nav_by_points", "start_mapping", "start_nav_return_home", "land")

# 状态：键 → (名称, 多久没新消息算过期（秒，None 为不按时效判断）)；键的顺序就是模块卡片里各行的顺序
# Bridge 各领域没变化也至少每秒重发一次；VFC 的 subsystem_status 每 2 s 一次，nav_status、mapping_status、loc_status、
# map_status 每 1 s 一次；在线消息只在变化时发（保留），航点进度和航点动作只在到点时发，这几类不按时效判断；
# 位姿约 2 Hz，只在里程计新鲜时发；VFC 的 OSD 每 1 s 一条，空闲（没有位姿和电量）时整条不发
DOMAINS = {
    "online": ("在线", None),
    "system": ("任务环境", 3),
    "localization": ("定位", 3),
    "relocalization": ("重定位", 3),
    "navigation": ("导航", 3),
    "mapping": ("建图", 3),
    "flight": ("飞控", 3),
    "pose": ("位姿", 2),
    "subsystem_status": ("在线", 5),
    "nav_status": ("任务", 3),
    "nav_status_progress": ("航点进度", None),
    "nav_action_progress": ("航点动作", None),
    "mapping_status": ("建图", 3),
    "loc_status": ("定位", 3),
    "map_status": ("地图", 3),
    "vfc_osd": ("OSD", 3),
}
# 无人机状态总览的模块（tta_vfc 11-17 按《室内巡检无人机-软件系统设计》答复）：MQTT 域的 GW、VTC（气体传感器和补光灯）、VFC、
# VISION、PTZ，桥接层 Bridge，支持服务 FSVR；现在实际在跑的是 Bridge、VFC、FSVR，其余没接时按通用格式看
MODULES = ("bridge", "vfc", "gw", "vision", "ptz", "vtc", "fsvr")
# 其他模块和 GW 一样按 type=subsystem_state 报在线，旧名、新名都认
GENERIC_ONLINE = ("subsystem_status", "subsystem_state")
# VFC 告警（local/uav/vfc/events，type=alarm）：raise、clear 成对，ALM-3101 定位丢失等持续性告警跟状态走
EVENTS = VFC + "events"

# VFC 收到这些 GW 指令时会给 Bridge 发对应的命令（nav 指挥家 11-06 按 VFC 源码给出）
VFC_TO_BRIDGE = {"start_multi_nav_by_points": "system.start", "pause_nav": "navigation.pause",
                 "continue_nav": "navigation.resume", "stop_nav": "navigation.pause",
                 "start_nav_return_home": "navigation.return_home", "land": "flight.land",
                 "start_mapping": "system.start", "stop_mapping": "system.stop"}
# VFC 自己发起、没有对应 GW 指令的 Bridge 命令：放行下一个航点、任务结束停环境、启动时读状态快照
VFC_AUTO = {"navigation.next", "system.stop", "system.status"}
# VFC 单线程按顺序处理，同一条指令触发的 Bridge 命令实测 20 ms 内跟上；留足余量，又不至于串到下一条
LINK_SECONDS = 5


# 模拟云端时收到的是 GW 转过的云端报文（tta/uav/{sn}/...），外壳和本地不一样（nav/docs/无人机MQTT协议_云端-GW-VFC_草案.md 第 2 节）：
# services_reply 的 result、msg 在 data 里，原来的 data 变成 data.data；state 是 method 加 data；osd 字段平铺
CLOUD = re.compile(r"^tta/uav/[^/]+/(services|services_reply|state|osd|events)$")
# 云端指令默认交给 VFC；sync_map_file 是 FSVR 的。地图列表、删除、改名云端协议写的是 fsvr，但现在是 VFC 在处理，也默认 vfc
CLOUD_TARGETS = {"sync_map_file": "fsvr"}


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
        return dataclasses.replace(event, topic=VFC + "ack", payload=payload)
    if kind == "state":
        body = p.get("data") if isinstance(p.get("data"), dict) else {}
        # subsystem_status 本地放在 data 里，其他放在 info 里；两个都给，解读按各自的键取
        return dataclasses.replace(event, topic=VFC + "state", payload={"type": p.get("method"), "info": body, "data": body})
    if kind == "osd":
        return dataclasses.replace(event, topic=VFC + "osd", payload={"data": p})
    # 告警 GW 现在不转；以后转了按状态的外壳认：method 是 alarm、内容在 data
    return dataclasses.replace(event, topic=VFC + "events",
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
        if rest.startswith("state/"):
            return rest[len("state/"):]
        return None
    if topic == VFC + "osd":
        return "vfc_osd"
    if topic == VFC + "state":
        kind = event.payload.get("type")
        # GW 也认旧名 subsystem_state
        return "subsystem_status" if kind == "subsystem_state" else kind if kind in DOMAINS else None
    # 其他模块按狗的写法：local/uav/<模块>/state，类型在 type 或 method
    parts = topic.split("/")
    if len(parts) == 4 and parts[3] == "state" and parts[2] not in ("vfc", "bridge"):
        kind = event.payload.get("type") or event.payload.get("method")
        return f"{parts[2]}:{kind}" if isinstance(kind, str) and kind else None
    return None


def reading(event):
    """状态卡上显示的内容：(状态词, 说明, result, 附加信息)；result 0 正常、1 失败、2 未知或过期"""
    event = local_view(event)
    payload = event.payload
    key = domain(event)
    if key == "vfc_osd":
        # 测量值在 data 里，没数据的键不出现；缺的显示“—”，不当成 0。坐标跟 Bridge 模式走、heading 零度方向未定，都原值显示
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        # 只有有限的数才算测量值；字符串、null、true/false 也显示“—”，并标成未知，原文到详情里看
        measured = lambda name: isinstance(data.get(name), (int, float)) and not isinstance(data[name], bool) and math.isfinite(data[name])
        value = lambda name, unit="": f"{data[name]}{unit}" if measured(name) else "—"
        bad = [name for name in OSD_KEYS if name in data and not measured(name)]
        extra = f"速度 {value('speed', ' m/s')} · 航向 {value('heading', '°')}" + (f"（{'、'.join(bad)} 不是数字）" if bad else "")
        # 飞控那几个（VFC 1.0.0 起 OSD 里带），没有的不写
        flags = [f"{text} {_yes(data[name]) if isinstance(data[name], bool) else data[name]}" for name, text in OSD_FLAGS if name in data]
        if flags:
            extra += " · " + " · ".join(flags)
        return f"电量 {value('batteryRemain', '%')}", f"x {value('x')}　y {value('y')}　z {value('z')}（米）", 2 if bad else 0, extra
    if key == "subsystem_status":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return "在线" if data.get("state") == 1 else f"state {data.get('state')}", f"v{data.get('version', '?')}", 0, ""
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    result, msg = info.get("result", 2), str(info.get("msg", ""))
    if key == "nav_status_progress":
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        return f"{data.get('current_index', '?')} / {data.get('count', '?')}", msg, result, f"任务 {info.get('flight_id') or '—'}"
    if key == "nav_status":
        # reason 只在 Cancelled 时有：stop、return_home、land（VFC 994c1ae 降落先取消原任务，再用新 bid 报降落中）
        reason = f" · 取消原因 {REASONS.get(info['reason'], info['reason'])}" if info.get("reason") else ""
        return str(info.get("data", "")), msg, result, f"任务 {info.get('flight_id') or '—'}" + reason
    if key == "mapping_status":
        # data 是云端协议的词（MappingRunning 等），raw 是 Bridge 原词；result 是协议码（5 建图中、7 保存中、8 已保存、6 出错、
        # 2 不在建图），不是成败。VFC 自己判断不了时报 Unknown，原因只在 msg 里。session_id 生成地图时要用
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
        # VFC 994c1ae：保存后 Bridge 自动建资产（auto）；失败时有 stage、exit_code、job_log
        parts = [f"地图 {info['map_id']}" if info.get("map_id") else "", f"会话 {info['session_id']}" if info.get("session_id") else "",
                 "保存后自动建资产" if info.get("auto") else "", "自动建资产任务" if info.get("map_job_auto") else "",
                 f"资产任务 {info['map_job_id']}" if info.get("map_job_id") else "",
                 f"地图忙（{info.get('map_busy_reason') or '原因未写'}）" if info.get("map_busy") else "",
                 f"原因 {info['reason']}" if info.get("reason") else "", f"阶段 {info['stage']}" if info.get("stage") else "",
                 f"退出码 {info['exit_code']}" if info.get("exit_code") not in (None, "") else "",
                 f"日志 {info['job_log']}" if info.get("job_log") else "",
                 "已清掉原选中的同名地图" if info.get("selection_cleared") else ""]
        return str(info.get("data", "")), msg, result, " · ".join(p for p in parts if p)
    if key == "nav_action_progress":
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        actions = data.get("actions") if isinstance(data.get("actions"), list) else []
        done = "、".join(f"{a.get('action', '?')} {a.get('state', '')}".strip() for a in actions if isinstance(a, dict))
        return f"第 {data.get('point_index', '?')} 点", msg, result, (done or "没有动作") + f" · 任务 {info.get('flight_id') or '—'}"
    if ":" in str(key):
        return _generic_reading(payload)
    detail = info.get("detail") if isinstance(info.get("detail"), dict) else {}
    extra = ""
    if key == "navigation":
        extra = (f"会话 {detail.get('session_id') or '—'} · 航点共 {detail.get('waypoint_total') if detail.get('waypoint_total') is not None else '—'}"
                 f" · 最后完成编号 {detail.get('waypoint_last_completed', '—')}（从 0 起，-1 为还没有）")
    elif key == "system":
        extra = " · ".join(f"{name} {detail[name]}" for name in ("profile", "stage", "session_id", "ros") if detail.get(name))
    elif key == "flight" and detail:
        battery = detail.get("battery_percent")
        extra = (f"模式 {detail.get('mode') or '—'} · 解锁 {_yes(detail.get('armed'))} · 落地 {_yes(detail.get('landed'))}"
                 f" · 起飞完成 {_yes(detail.get('takeoff_complete'))}"
                 + (f" · 电量 {battery:.0f}%" if isinstance(battery, (int, float)) else ""))
        # 只定位模式飞的时候整体一直是 STALE，detail 照样在更新
        if info.get("data") == "STALE":
            extra += "（整体 STALE，detail 仍在更新）"
    elif key == "localization" and detail:
        extra = f"对齐 {detail.get('aligned')} · 坐标锁定 {detail.get('frame_locked')}"
    elif key == "relocalization" and detail.get("text"):
        extra = str(detail["text"])
    elif key == "pose" and detail:
        extra = " ".join(f"{name}={detail[name]:.2f}" if isinstance(detail[name], (int, float)) else f"{name}={detail[name]}"
                         for name in ("x", "y", "z", "yaw_deg", "speed") if name in detail)
    return str(info.get("data", "")), msg, result, extra


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
    # 没有状态词的（如 storage_status 只有一组数）不另起一个词，名称已经是类型了
    return str(word), str(body.get("msg", "")), result, brief


def _yes(value):
    return {True: "是", False: "否", None: "—"}.get(value, str(value))


# VFC OSD 里的测量值
OSD_KEYS = ("x", "y", "z", "speed", "heading", "batteryRemain")
# OSD 里飞控的状态（VFC 1.0.0）
OSD_FLAGS = (("flightState", "飞行中"), ("flightMode", "模式"), ("armed", "解锁"), ("landed", "落地"), ("takeoffComplete", "起飞完成"),
             ("mapId", "地图"), ("batteryVoltage", "电压"))
# nav_status 取消原因
REASONS = {"stop": "结束航线", "return_home": "返航", "land": "降落"}
# 到点放行要醒目：航点完成等待放行、控制桥转向完成可以放行
WAITING_NEXT = "WAYPOINT_COMPLETE_WAITING_EXTERNAL_TRIGGER"
YAW_DONE = {"HOLD", "WAITING_TRAJECTORY"}
FLIGHT_FLAGS = (("armed", "解锁"), ("landed", "落地"), ("mode", "模式"), ("takeoff_complete", "起飞完成"))


def changes(before, event):
    """和这一领域的上一条比，有哪些值得记下来的变化：[(文字, 是否醒目)]；before 为 None 时是第一次收到

    只记状态词、导航航点进度、飞控的解锁/落地/模式/起飞、VFC 任务状态的说明这些人会关心的，电量和位姿每次都变不记
    """
    event, before = local_view(event), before and local_view(before)
    key = domain(event)
    name = DOMAINS.get(key, (key, None))[0]
    word, msg, _, _ = reading(event)
    old = reading(before) if before else None
    out = []
    if key in ("nav_status", "nav_status_progress"):
        if key == "nav_status_progress":
            out.append((f"航点进度 {word}：{msg}", True))
        elif not old or (word, msg) != old[:2]:
            out.append((f"VFC 任务 {word}：{msg}", "已到达" in msg))
        return out
    if key in ("subsystem_status", "pose", "vfc_osd"):
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
            # 进行中（BUILDING、等资产锁）不醒目，结局（资产就绪、失败、超时、不确定、跳过）醒目
            out.append((f"VFC 地图 {word}" + (f"：{msg}" if msg else ""), word not in ("IDLE", "BUILDING", "MAP_AUTO_BUILD_WAITING")))
        return out
    if key == "nav_action_progress":
        out.append((f"航点动作 {word}：{msg or ''} {reading(event)[3]}".strip(), False))
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
    """收到上报的时效（和数据本身好不好分开看）：“保留快照” / “超时没收到” / “实时”

    保留消息是订阅时 Broker 给的缓存，不代表设备现在还在报；在线消息和航点进度不是周期上报，不按时效判断
    """
    if event.retained:
        return "保留快照"
    limit = DOMAINS.get(domain(event), (None, None))[1]
    return "超时没收到" if limit and now - event.timestamp > limit else "实时"


def quality(event):
    """数据本身：“正常” / “失败” / “未知” / “不适用”；Bridge 的 DISABLED 是这一领域当前不适用，不算未知"""
    word, _, result, _ = reading(event)
    if domain(event) == "mapping_status":
        # 建图状态的 result 是协议码，按词判断
        return {"MappError": "失败", "Unknown": "未知", "Passive": "不适用"}.get(word, "正常")
    if domain(event) == "map_status":
        # 地图生成的结局按词判：失败、超时算失败，自动建资产结果不确定算未知，跳过的不适用
        verdict = {"MAP_BUILD_FAILED": "失败", "MAP_BUILD_TIMEOUT": "失败", "MAP_AUTO_BUILD_UNCERTAIN": "未知",
                   "MAP_AUTO_BUILD_SKIPPED": "不适用"}.get(word)
        if verdict:
            return verdict
    if domain(event) == "nav_action_progress":
        # 本期 VFC 不执行航点动作，全部报 skipped（result 1），是提醒不是失败
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
    """把 VFC 指令和它触发的 Bridge 命令排成时间线，按时间先后返回行：

    {"key", "source"（"VFC" / "Bridge"）, "action", "events"（这一请求的全部消息，按时间）, "parent"（VFC 行的 key）,
     "origin"（Bridge 行：对应的 VFC 指令 / "VFC 自动" / "外部" / "未对上"）, "confirmed"（VFC 的 ACK 里带了这条 Bridge 回执的状态和原因码）}

    Bridge 命令不带 VFC 的 tid，只能按动作对应、时间就近配：同一动作、5 s 内、还没配过的 VFC 指令里取最近的一条
    """
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
    """VFC 的 ACK 会把 Bridge 回执的状态和原因码原样写进 msg（如“rejected BAD_WAYPOINTS”），对得上就算确认是这一条"""
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
    # 结论取 final 的那条：重复回执、迟到的 accepted 都不把终态退回“受理中”
    final = next((e for e in reversed(acks) if e.payload.get("final")), acks[-1]).payload
    info = final.get("info") if isinstance(final.get("info"), dict) else {}
    chain = " → ".join(dict.fromkeys(str(e.payload.get("status")) for e in acks))
    status = {2: "结果不确定"}.get(info.get("result"), "")
    return (chain + (f"（{info.get('data')}：{info.get('msg')}）" if info.get("data") else "")
            + (f"　{status}" if status else "")), info.get("result", 2)
