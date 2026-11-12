"""MQTT 输入输出与离线沙盒，不向业务报文注入调试字段"""

import copy
import json
import queue
import time
import uuid

from .core import Event
from .uav import COMMAND_TOPICS as UAV_COMMAND_TOPICS
from .pointcloud import demo_frame
from .registry import UPLOAD, local_payload, route


class MqttTransport:
    """回调只写线程安全队列，存储和 UI 由主线程负责"""

    def __init__(self, inbox):
        self.inbox = inbox
        self.client = None
        self.connected = False
        self.host = ""
        self.sn = ""
        self.session = ""
        self.device = "dog"
        self.uav_send = False
        self.dropped = 0

    def _offer(self, item):
        try:
            self.inbox.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def connect(self, host, port, sn, username="", password="", tls=False, device="dog"):
        """device 为 "dog" 时订阅狗的本地和云端主题，"uav" 时订阅无人机本地主题（VFC 和 Bridge），只读"""
        import paho.mqtt.client as mqtt
        self.close()
        # 无人机的主题不带 SN，连接卡里隐藏着的狗 SN 不能记到无人机的消息上，否则狗的状态总览按 SN 查会混进来
        self.host, self.sn, self.session, self.device = host, "" if device == "uav" else sn, str(uuid.uuid4()), device
        # 每次新连接都回到只读
        self.uav_send = False
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dog-debug-" + uuid.uuid4().hex[:12],
                             clean_session=True, reconnect_on_failure=False)
        if username:
            client.username_pw_set(username, password)
        if tls:
            client.tls_set()
        client.on_connect = self._connected
        client.on_disconnect = self._disconnected
        client.on_message = self._message
        client.on_publish = self._published
        client.on_subscribe = lambda c, u, mid, codes, p: self._offer(("transport", "订阅结果 " + str(codes)))
        client.on_connect_fail = lambda c, u: self._offer(("disconnect", "无法连接 Broker，请核地址、端口和网络"))
        self.client = client
        client.connect_async(host, port, keepalive=20)
        client.loop_start()

    def _connected(self, client, userdata, flags, reason, properties):
        self.connected = reason == 0
        self._offer(("connection", "已连接，只读" if self.connected else "连接失败 " + str(reason)))
        if self.connected:
            if self.device == "uav":
                client.subscribe([("local/uav/#", 0)])
            else:
                client.subscribe([(f"tta/robotdog/{self.sn}/#", 0), ("local/robotdog/#", 0)])

    def _disconnected(self, client, userdata, flags, reason, properties):
        self.connected = False
        self._offer(("disconnect", "已断开 " + str(reason)))

    def _message(self, client, userdata, message):
        raw = message.payload.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {}
        except ValueError:
            payload = {}
        event = Event(message.topic, payload, sn=self.sn, session=self.session,
                      source="mqtt", qos=message.qos, retained=message.retain,
                      duplicate=message.dup, raw=raw)
        self._offer(("event", event))

    def _published(self, client, userdata, mid, reason, properties):
        # QoS 0 只表示客户端写出去了；QoS 1 是收到 Broker 的 PUBACK 之后才回调。都不代表对端执行了
        self._offer(("transport", f"客户端发送完成 mid={mid}（QoS 0 只表示已写出，QoS 1 表示 Broker 已收到）"))

    def publish(self, topic, payload):
        """payload 为 str 时按原文发送，便于发异常报文；其他值序列化为 JSON"""
        if not self.client or not self.connected:
            raise RuntimeError("MQTT 未连接，不排队旧命令")
        # 无人机默认只读：所有发送都从这里出去（遥控、快捷键、切页补发的停止），一处挡住；
        # 打开“允许向无人机发送”后也只放行 VFC、Bridge 的指令主题，狗的指令不会发到无人机的 Broker 上
        if self.device == "uav":
            if not self.uav_send:
                raise RuntimeError("连着的是无人机，默认只读：要发指令先在控制调试页打开“允许向无人机发送”")
            if topic not in UAV_COMMAND_TOPICS:
                raise RuntimeError(f"无人机只往 VFC、Bridge 的指令主题发，{topic} 不发")
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        result = self.client.publish(topic, text, qos=self.qos_for(topic), retain=False)
        if result.rc != 0:
            raise RuntimeError("MQTT 未接受发布 rc=" + str(result.rc))

    @staticmethod
    def qos_for(topic):
        """无人机 VFC、Bridge 的指令主题按它们的协议用 QoS 1（Bridge 字段说明第 6 节；VFC 订阅和转发 Bridge 都是 1），狗照旧 0"""
        return 1 if topic in UAV_COMMAND_TOPICS else 0

    def close(self):
        client, self.client = self.client, None
        self.connected = False
        if client:
            client.disconnect()
            client.loop_stop()


# 沙盒成功 ACK 里附带的示例业务数据，让地图/文件列表和测温表单离线也能看到回填效果
MOCK_DATA = {
    "get_map_list": {"data": [{"map_id": "2026_07_25_13_53_48", "size": 12}, {"map_id": "2026_07_28_14_13_53", "size": 9}]},
    "get_media_filelist": {"data": [
        {"id": "d1", "parent_id": "", "file_name": "2026-10-28", "node_type": 1, "file_type": 0, "file_size": 0, "children": [
            {"id": "f1", "parent_id": "d1", "file_name": "IMG_0001.jpg", "node_type": 0, "file_type": 0, "file_size": 51200, "children": None},
            {"id": "f2", "parent_id": "d1", "file_name": "VID_0002.mp4", "node_type": 0, "file_type": 1, "file_size": 1048576, "children": None}]}]},
    "ir_metering_point_set": {"data": {"x": 0.5, "y": 0.5, "temperature": 36.5}},
    "ir_metering_area_set": {"data": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8, "aver_temperature": 35.2,
                                      "max_temperature_point": {"x": 0.3, "y": 0.4, "temperature": 42.1},
                                      "min_temperature_point": {"x": 0.7, "y": 0.2, "temperature": 28.3}}},
}


# VFC 用 _replyWithWsData 回复的方法（tta_vfc TTA_ZS_1W_VfcSession.cpp），result/msg 在根上
ROOT_RESULT = {"get_all_paths", "add_nav_path", "modify_nav_path", "delete_nav_path", "start_mapping", "stop_mapping",
               "get_mapping_status", "get_pgm_map", "get_all_compressed_pgm_map", "remove_map_by_id", "rename_map_name",
               "loc_load_map", "loc_reset", "start_nav", "start_multi_nav", "start_multi_nav_by_points",
               "start_nav_return_home", "stop_nav", "pause_nav", "continue_nav", "controllor", "ctrl_mode"}


def simulate(payload, sn, session, scenario="success"):
    """内存沙盒只演示明确标记的 Mock 链路，不接触网络或设备"""
    module, tid, method = route(payload), payload.get("tid", ""), payload.get("method", "")
    if method in {"oss_config_get", "online", "heartbeat"}:
        suffix = "request" if method == "oss_config_get" else method
        topic = "tta/robotdog/status/online" if method == "online" else f"tta/robotdog/{sn}/{suffix}"
        request = {"method": method, "tid": tid, "sn": sn, "seq": 1}
        response = {"method": method, "tid": tid, "sn": sn, "result": 0, "seq": 1,
                    "data": {"note": "Mock 云端应答，不是真实凭据或部署配置"}}
        return [Event(topic, request, "模拟设备上报", "sandbox", sn, session, "gw"),
                Event(topic + "_reply", response, "模拟云端应答", "sandbox", sn, session, "gw")]
    if method == "pcd_switch":
        return simulate_pcd_switch(payload, sn, session, scenario)
    command = local_payload(payload)
    events = [Event(f"tta/robotdog/{sn}/services", copy.deepcopy(payload), "发送", "sandbox", sn, session, "gw"),
              Event(f"local/robotdog/{module}/command", command, "模拟转发", "sandbox", sn, session, module)]
    if scenario == "no_ack":
        return events
    failure = scenario == "failure"
    result, msg = (1, "相机连接超时 camera connection timeout") if failure else (0, "")
    if method == "speaker_play_start" and failure:
        msg = "audio device is busy"
    business = None if failure else copy.deepcopy(MOCK_DATA.get(method, {}).get("data"))
    ts = payload.get("ts", int(time.time() * 1000)) + 100
    shell = {"method": method, "tid": tid, "bid": payload.get("bid", ""), "ts": ts}
    # 本地 ACK 按各模块源码的形状：VFC 导航/地图类结果在根上，业务数据在 data；其他模块结果在 data 里
    if method in ROOT_RESULT:
        ack = {**shell, "result": result, "msg": msg, "data": business}
    else:
        ack = {**shell, "data": {"result": result, "msg": msg}}
        if business is not None:
            ack["data"]["data"] = business
    events.append(Event(f"local/robotdog/{module}/ack", copy.deepcopy(ack), source="sandbox", sn=sn, session=session, module=module))
    reply = {**shell, "target": payload.get("target", module)}
    if module == "fsvr":
        # GW 对 FSVR 回执专门透传 data，不再包一层，上云 target 还原成 gw
        reply["data"] = copy.deepcopy(ack["data"])
        reply["data"].pop("ext", None)
    else:
        # GW 通用转发只读本地根上的 result/msg（没有就填 0 和空串），本地 data 整个放进 data.data
        reply["data"] = {"result": ack.get("result", 0), "msg": ack.get("msg", "")}
        if ack.get("data") is not None:
            reply["data"]["data"] = copy.deepcopy(ack["data"])
    if scenario == "nested_failure":
        reply["data"] = {"result": 0, "msg": "", "data": {"result": 1, "msg": "data.index maps to a disabled stream"}}
    events.append(Event(f"tta/robotdog/{sn}/services_reply", reply, source="sandbox", sn=sn, session=session, module="gw"))
    if scenario == "duplicate":
        events.append(copy.deepcopy(events[-1]))
        events[-1].event_id = str(uuid.uuid4())
        events[-1].duplicate = True
    if not failure and method == "camera_livestream":
        info = {"index": str(payload.get("data", {}).get("index", 1)), "sn": "002", "pullType": "webRTC",
                "type": "audio" if payload.get("data", {}).get("mode") == 1 else "video",
                "result": 0 if payload.get("data", {}).get("state") == 2 else 1, "msg": "Mock 状态"}
        events.append(Event(f"local/robotdog/media/state", {"type": "stream_status", "info": info}, source="sandbox", sn=sn, session=session, module="media"))
        events.append(Event(f"local/robotdog/media/state", {"type": "room_state", "info": {"result": 0, "info": [
            {"index": 0, "sn": "001", "room": f"{sn}_001", "userId": "001"},
            {"index": 1, "sn": "002", "room": f"{sn}_002", "userId": "visible"},
            {"index": 2, "sn": "002", "room": f"{sn}_002", "userId": "infrared"}]}}, source="sandbox", sn=sn, session=session, module="media"))
    if not failure and method in UPLOAD:
        state_method = "media_file_upload_status" if method == "sync_media_file" else "sync_map_file_status"
        for state, percent in (("1", 35), ("0", 100)):
            events.append(Event(f"local/robotdog/fsvr/state", {"method": state_method, "tid": tid,
                "data": {"result": 0, "state": state, "percent": percent, "msg": "Mock 上传"}},
                source="sandbox", sn=sn, session=session, module="fsvr"))
    return events


def simulate_detection(sn, session, stop_after="云端事件", fail_at=""):
    """沙盒里生成一条检测抓拍到上云的消息链，按真实契约的关联字段（tid、event_id、delivery_id、file_path）

    stop_after 之后的阶段不产生消息；fail_at 阶段返回失败。只用于演示和测试，不接触设备
    """
    from .core import DETECTION_STAGES
    event_id, tid, delivery = "evt-" + uuid.uuid4().hex[:8], "cap-" + uuid.uuid4().hex[:8], "md-" + uuid.uuid4().hex[:12]
    path = time.strftime("events/%Y-%m-%d/TTA_") + uuid.uuid4().hex[:10] + ".jpg"
    reg, upl = "reg-" + delivery, "upl-" + uuid.uuid4().hex[:8]
    fail = lambda stage: fail_at == stage
    steps = [
        ("抓拍请求", [("local/robotdog/media/detect/command", {"method": "capture_detection", "tid": tid, "requester": "vision",
                                                             "event_id": event_id, "data": {"stream_id": "video1"}}, "vision")]),
        ("JPEG 生成", [("local/robotdog/detect/ack", {"method": "capture_detection", "tid": tid, "result": 1 if fail("JPEG 生成") else 0,
                                                    "msg": "", "data": {"phase": "failed", "reason": "hold expired"} if fail("JPEG 生成") else
                                                    {"phase": "completed", "delivery_id": delivery, "file_path": path, "file_size": 57483}}, "media")]),
        ("FSVR 登记", [("local/robotdog/fsvr/command", {"method": "register_file", "tid": reg, "requester": "media", "target": "fsvr",
                                                      "data": {"delivery_id": delivery, "file_path": path, "event_id": event_id, "kind": "detection"}}, "fsvr"),
                      ("local/robotdog/fsvr/ack", {"method": "register_file", "tid": reg,
                                                   "data": {"result": 1 if fail("FSVR 登记") else 0, "msg": "file not found" if fail("FSVR 登记") else "",
                                                            "phase": "completed", "delivery_id": delivery}}, "fsvr")]),
        ("上传受理", [("local/robotdog/fsvr/command", {"method": "upload_detection_image", "tid": upl, "requester": "vision",
                                                     "event_id": event_id, "data": {"file_path": path}}, "fsvr"),
                     ("local/robotdog/fsvr/ack", {"method": "upload_detection_image", "tid": upl, "requester": "vision",
                                                  "event_id": event_id, "data": {"result": 0, "msg": "upload tasks accepted",
                                                                                 "phase": "accepted", "file_path": path}}, "fsvr")]),
        ("上传终态", [("local/robotdog/fsvr/state", {"method": "detection_upload_status", "tid": upl, "requester": "vision",
                                                   "event_id": event_id,
                                                   "data": {"result": 1 if fail("上传终态") else 0, "state": "0",
                                                            "msg": "PUT 403" if fail("上传终态") else ""}}, "fsvr")]),
        ("终态确认", [("local/robotdog/fsvr/command", {"method": "confirm_result", "tid": "upc-" + uuid.uuid4().hex[:8],
                                                     "requester": "vision", "data": {"request_tid": upl, "result_seq": 1}}, "fsvr")]),
        ("本地事件", [("local/robotdog/vision/events", {"method": "detect_result_event",
                                                      "data": {"stream_id": "video1", "type": 1, "msg": "", "bbox": [], "file_path": path,
                                                               "uploaded": True}}, "vision")]),
        ("云端事件", [(f"tta/robotdog/{sn}/events", {"method": "detect_result_event",
                                                   "data": {"file_name": path.rsplit("/", 1)[-1], "type": 1, "msg": "", "bbox": []}}, "gw")]),
    ]
    events, now = [], time.time()
    for index, (stage, messages) in enumerate(steps):
        if DETECTION_STAGES.index(stage) > DETECTION_STAGES.index(stop_after):
            break
        for topic, payload, module in messages:
            events.append(Event(topic, payload, source="sandbox", sn=sn, session=session, module=module,
                                timestamp=now + len(events) * 0.05))
        if fail(stage):
            break
    return events


def simulate_pcd_switch(payload, sn, session, scenario="success"):
    """pcd_switch 是本地指令（GW 建图时自己发），沙盒里没有云端那一段：本地指令 → ros2bridge 回执，成功时再给心跳，开启时再给几帧演示点云

    回执按 ros2bridge 的形状：result/msg 在根上，data.enable 是处理后的实际状态
    """
    tid, data = payload.get("tid", ""), payload.get("data")
    enable = data.get("enable") if isinstance(data, dict) else None
    events = [Event("local/robotdog/ros2bridge/command", copy.deepcopy(payload), "发送", "sandbox", sn, session, "ros2bridge")]
    if scenario == "no_ack":
        return events
    ts = payload.get("ts", int(time.time() * 1000)) + 20
    if not isinstance(enable, bool):
        ack = {"method": "pcd_switch", "ts": ts, "tid": tid, "result": 1, "msg": "missing or invalid field: enable", "data": {"enable": False}}
    elif scenario == "failure":
        ack = {"method": "pcd_switch", "ts": ts, "tid": tid, "result": 1, "msg": "start collecting failed", "data": {"enable": False}}
    else:
        ack = {"method": "pcd_switch", "ts": ts, "tid": tid, "result": 0, "msg": "", "data": {"enable": enable}}
    events.append(Event("local/robotdog/ros2bridge/ack", ack, source="sandbox", sn=sn, session=session, module="ros2bridge"))
    # 失败时采集状态没变，不另给心跳
    if ack["result"]:
        return events
    on = ack["data"]["enable"]
    events.append(Event("local/robotdog/ros2bridge/heartbeat", {"sn": sn, "ts": ts + 10, "data": {
        "enabled": on, "ros_ok": on, "source": "/front_lidar", "last_frame_age_ms": 30 if on else -1,
        "in_hz": 10.0 if on else 0.0, "out_hz": 2.0 if on else 0.0, "out_bytes_per_sec": 99800 if on else 0,
        "out_count": 5400 if on else 0, "leaf": 0.08 if on else 0.0}}, source="sandbox", sn=sn, session=session, module="ros2bridge"))
    if on:
        events += simulate_clouds(sn, session)
    return events


def simulate_clouds(sn, session, frames=3):
    """几帧演示点云（本地 data + GW 转发的云端 pcd），只用于离线看显示效果"""
    events, now = [], int(time.time() * 1000)
    for seq in range(1, frames + 1):
        data = demo_frame(seq)
        events.append(Event("local/robotdog/ros2bridge/data", {"sn": sn, "ts": now, "data": data},
                            source="sandbox", sn=sn, session=session, module="ros2bridge"))
        events.append(Event(f"tta/robotdog/{sn}/state", {"ts": now, "method": "pcd", "data": copy.deepcopy(data)},
                            source="sandbox", sn=sn, session=session, module="ros2bridge"))
    return events
