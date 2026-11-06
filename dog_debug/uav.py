"""室内巡检无人机本地 MQTT 的解读：VFC 和 Bridge 各领域的状态、残留的保留指令、VFC 指令和 Bridge 命令的对应

依据 nav/MQTT接口字段说明_20261105_源码版.md（Bridge）和 nav/tta_vfc 的 TTA_VFC_MqttProtocol.cpp（VFC）。
板上时钟不准（实测慢约 36 h），时效一律按本机收到的时间算，报文里的 ts 只用来显示
"""

from .core import BRIDGE

VFC = "local/uav/vfc/"
# 往这两个主题发的是指令；上面的保留消息在 VFC 或 Bridge 重启、重连时会被再收到一遍，VFC 分不出来会照样执行
COMMAND_TOPICS = (VFC + "command", BRIDGE + "command")

# 状态卡：键 → (名称, 多久没新消息算过期（秒，None 为不按时效判断）)
# Bridge 各领域没变化也至少每秒重发一次；VFC 的 subsystem_status 每 2 s 一次，nav_status 每 1 s 一次；
# 在线消息只在变化时发（保留），航点进度只在到点时发，这两类不按时效判断；位姿约 2 Hz，只在里程计新鲜时发
DOMAINS = {
    "online": ("Bridge 在线", None),
    "system": ("任务环境", 3),
    "localization": ("定位", 3),
    "relocalization": ("重定位", 3),
    "navigation": ("导航", 3),
    "mapping": ("建图", 3),
    "flight": ("飞控", 3),
    "pose": ("位姿", 2),
    "subsystem_status": ("VFC 在线", 5),
    "nav_status": ("VFC 任务", 3),
    "nav_status_progress": ("航点进度", None),
}

# VFC 收到这些 GW 指令时会给 Bridge 发对应的命令（nav 指挥家 11-06 按 VFC 源码给出）
VFC_TO_BRIDGE = {"start_multi_nav_by_points": "system.start", "pause_nav": "navigation.pause",
                 "continue_nav": "navigation.resume", "stop_nav": "navigation.pause",
                 "start_nav_return_home": "navigation.return_home", "land": "flight.land"}
# VFC 自己发起、没有对应 GW 指令的 Bridge 命令：放行下一个航点、任务结束停环境、启动时读状态快照
VFC_AUTO = {"navigation.next", "system.stop", "system.status"}
# VFC 单线程按顺序处理，同一条指令触发的 Bridge 命令实测 20 ms 内跟上；留足余量，又不至于串到下一条
LINK_SECONDS = 5


def domain(event):
    """这条消息属于哪张状态卡；不是无人机状态返回 None"""
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
    if topic == VFC + "state":
        kind = event.payload.get("type")
        # GW 也认旧名 subsystem_state
        return "subsystem_status" if kind == "subsystem_state" else kind if kind in DOMAINS else None
    return None


def reading(event):
    """状态卡上显示的内容：(状态词, 说明, result, 附加信息)；result 0 正常、1 失败、2 未知或过期"""
    payload = event.payload
    key = domain(event)
    if key == "subsystem_status":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return "在线" if data.get("state") == 1 else f"state {data.get('state')}", f"v{data.get('version', '?')}", 0, ""
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    result, msg = info.get("result", 2), str(info.get("msg", ""))
    if key == "nav_status_progress":
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        return f"{data.get('current_index', '?')} / {data.get('count', '?')}", msg, result, f"任务 {info.get('flight_id') or '—'}"
    if key == "nav_status":
        return str(info.get("data", "")), msg, result, f"任务 {info.get('flight_id') or '—'}"
    detail = info.get("detail") if isinstance(info.get("detail"), dict) else {}
    extra = ""
    if key == "navigation":
        extra = (f"会话 {detail.get('session_id') or '—'} · 航点共 {detail.get('waypoint_total') if detail.get('waypoint_total') is not None else '—'}"
                 f" · 最后完成编号 {detail.get('waypoint_last_completed', '—')}（从 0 起，-1 为还没有）")
    elif key == "system":
        extra = " · ".join(f"{name} {detail[name]}" for name in ("profile", "stage", "session_id", "ros") if detail.get(name))
    elif key == "flight" and detail:
        battery = detail.get("battery_percent")
        extra = (f"模式 {detail.get('mode') or '—'} · 解锁 {detail.get('armed')} · 落地 {detail.get('landed')}"
                 + (f" · 电量 {battery:.0f}%" if isinstance(battery, (int, float)) else ""))
    elif key == "localization" and detail:
        extra = f"对齐 {detail.get('aligned')} · 坐标锁定 {detail.get('frame_locked')}"
    elif key == "relocalization" and detail.get("text"):
        extra = str(detail["text"])
    elif key == "pose" and detail:
        extra = " ".join(f"{name}={detail[name]}" for name in ("x", "y", "z", "yaw_deg", "speed") if name in detail)
    return str(info.get("data", "")), msg, result, extra


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
    if word == "DISABLED":
        return "不适用"
    if word == "OFFLINE" or result == 1:
        return "失败"
    return "未知" if word in ("STALE", "UNKNOWN") or result == 2 else "正常"


def retained_command(event):
    """指令主题上收到的保留消息（非空）：Broker 会把它补发给每个新订阅者，执行方重启后会再执行一遍"""
    return event.topic in COMMAND_TOPICS and event.retained and bool(event.payload or event.raw.strip())


def link(events):
    """把 VFC 指令和它触发的 Bridge 命令排成时间线，按时间先后返回行：

    {"key", "source"（"VFC" / "Bridge"）, "action", "events"（这一请求的全部消息，按时间）, "parent"（VFC 行的 key）,
     "origin"（Bridge 行：对应的 VFC 指令 / "VFC 自动" / "外部" / "未对上"）, "confirmed"（VFC 的 ACK 里带了这条 Bridge 回执的状态和原因码）}

    Bridge 命令不带 VFC 的 tid，只能按动作对应、时间就近配：同一动作、5 s 内、还没配过的 VFC 指令里取最近的一条
    """
    rows, by_key = [], {}
    for e in sorted(events, key=lambda e: e.timestamp):
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
