"""无人机第一期：用板上真实抓包验证状态卡、保留指令告警、VFC 指令和 Bridge 命令的对应、只读；不连真实 Broker"""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from dog_debug.app import Studio
from dog_debug.core import Event, summarize
from dog_debug.uav import freshness, link, outcome, quality, reading

DATA = Path(__file__).parent / "data" / "uav"


def capture(name, session="s1", base=1000.0):
    """抓包文件转成事件：时间戳按抓包里的相对时间排在 base 之后，和上位机收到时一样记本机时间"""
    events = []
    for line in (DATA / name).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if "topic" not in row:
            continue
        at = row["t"] if "t" in row else row["mono"]
        events.append((at, Event(row["topic"], row["body"], sn="", session=session, retained=row["retain"])))
    first = events[0][0]
    for at, e in events:
        e.timestamp = base + at - first
    return [e for _, e in events]


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    win = Studio(":memory:")
    win.mqtt.connected, win.mqtt.device, win.mqtt.session, win.mqtt.host = True, "uav", "s1", "192.168.1.230"
    win.mqtt.client = StubClient()
    yield win
    win.mqtt.client = None
    win.close()
    app.processEvents()


class StubClient:
    def __init__(self):
        self.sent, self.options = [], []

    def publish(self, topic, text, qos, retain):
        self.sent.append((topic, text))
        self.options.append((qos, retain))
        return type("Result", (), {"rc": 0})()

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def test_bridge_messages_parse_as_requests_and_states():
    ack = Event("local/uav/bridge/ack", {"sn": "", "id": "vfc-1-2", "action": "system.start", "status": "rejected", "final": True})
    state = Event("local/uav/bridge/state/navigation", {"sn": "", "type": "navigation", "state": 0, "info": {}})
    # Bridge 的 id 带前缀当请求号，和 VFC 的 tid 分开
    assert (ack.kind, ack.method, ack.tid) == ("ACK", "system.start", "bridge:vfc-1-2")
    assert (state.kind, state.method) == ("状态", "navigation")


def test_vfc_commands_linked_to_bridge_commands_from_board_capture():
    rows = {row["key"][2]: row for row in link(capture("commands.jsonl"))}
    children = {row["parent"][2]: row for row in rows.values() if row["parent"]}
    # 中间插了一条未知指令（bt-005），system.start 仍配到发出航线的 bt-004
    assert children["bt-004"]["action"] == "system.start" and children["bt-004"]["confirmed"]
    assert children["bt-006"]["key"][2] == "bridge:vfc-1793736584573-3" and children["bt-006"]["confirmed"]
    assert children["bt-007"]["action"] == "navigation.return_home" and children["bt-008"]["action"] == "flight.land"
    assert not {"bt-001", "bt-002", "bt-003", "bt-005"} & set(children)
    assert outcome(children["bt-004"])[1] == 1 and "BAD_WAYPOINTS" in outcome(children["bt-004"])[0]
    assert outcome(rows["bt-005"]) == ("未知：未知指令：no_such_method", 2)
    assert outcome(rows["bt-001"])[0].startswith("拒绝：当前状态 StandBy")


def test_vfc_own_bridge_commands_marked_auto():
    rows = link(capture("restart.jsonl"))
    status = next(r for r in rows if r["action"] == "system.status")
    assert status["origin"] == "VFC 自动" and status["parent"] is None


def test_bridge_final_result_not_reverted_by_late_accepted():
    command = Event("local/uav/bridge/command", {"id": "x1", "action": "system.start", "client": "MQTTX"}, session="s", timestamp=1)
    final = Event("local/uav/bridge/ack", {"id": "x1", "action": "system.start", "status": "succeeded", "final": True,
                                          "info": {"result": 0, "data": "NAVIGATION_READY", "msg": "环境就绪"}}, session="s", timestamp=2)
    late = Event("local/uav/bridge/ack", {"id": "x1", "action": "system.start", "status": "accepted", "final": False,
                                         "info": {"result": 0, "data": "JOB_ACCEPTED"}}, session="s", timestamp=3)
    row = link([command, final, late])[0]
    assert row["origin"] == "外部（MQTTX）" and outcome(row)[1] == 0 and "NAVIGATION_READY" in outcome(row)[0]
    uncertain = Event("local/uav/bridge/ack", {"id": "x2", "action": "navigation.next", "status": "uncertain", "final": True,
                                              "info": {"result": 2, "data": "NO_RECEIPT", "msg": "没收到回执"}}, session="s", timestamp=4)
    assert outcome(link([uncertain])[0])[0].endswith("结果不确定")


def test_card_freshness_and_quality_are_separate():
    retained, live = capture("idle.jsonl")[1:3]
    assert freshness(retained, retained.timestamp) == "保留快照"
    navigation = next(e for e in capture("idle.jsonl") if e.topic.endswith("navigation") and not e.retained)
    # 一直在实时重发，但数据本身是 STALE：收到是实时的，内容是未知
    assert freshness(navigation, navigation.timestamp + 1) == "实时" and quality(navigation) == "未知"
    assert freshness(navigation, navigation.timestamp + 10) == "超时没收到"
    mapping = next(e for e in capture("idle.jsonl") if e.topic.endswith("mapping"))
    assert quality(mapping) == "不适用"
    will = Event("local/uav/bridge/heartbeat", {"sn": "", "type": "online", "state": 0,
                                                "info": {"result": 2, "msg": "机载服务离线", "data": "OFFLINE"}})
    assert reading(will)[0] == "OFFLINE" and quality(will) == "失败" and freshness(will, will.timestamp + 600) == "实时"


def card_text(window, module):
    """状态总览（和狗同一页）里这个模块卡片的正文和右上角时间；先按当前设备刷新"""
    window.store.flush()
    window.status._refresh_status()
    state, when, content = window.status.module_cards[module]
    return content.text(), when.text()


def test_uav_overview_reuses_dog_status_page(window):
    """无人机的总览和狗用同一个状态页：一个模块一张卡片（Bridge、VFC、GW、Vision、PTZ、VTC、FSVR），卡片里是各类状态的方法名、时间和字段摘要"""
    window.device_box.setCurrentText("无人机")
    for e in capture("idle.jsonl"):
        window._ingest(e)
    bridge, when = card_text(window, "bridge")
    assert "system" in bridge and "data=READY" in bridge and "localization" in bridge and when != "未观测"
    vfc, _ = card_text(window, "vfc")
    assert "nav_status" in vfc and "StandBy" in vfc
    assert "还没有收到 GW" in card_text(window, "gw")[0]
    assert window.status.status_grid.cards == [window.status.module_frames[m] for m in ("bridge", "vfc", "gw", "vision", "ptz", "vtc", "fsvr")]
    assert window.status.heading.text() == "无人机 · 状态总览与遥测"
    # 狗的直播和音视频预览不显示，换成无人机的告警、关键变化、指令时间线
    assert window.status.live_card[0].isHidden() and not window.status.uav_extras.isHidden()
    window.device_box.setCurrentText("机器狗")
    assert window.status.status_grid.cards[0] is window.status.module_frames["gw"] and window.status.uav_extras.isHidden()


def test_retained_command_alert_and_new_connection_resets(window):
    window._ingest(Event("local/uav/vfc/command", {"method": "start_multi_nav_by_points", "tid": "old", "bid": "b"},
                         session="s1", retained=True))
    window.uav.render()
    assert not window.uav.banner.isHidden() and "local/uav/vfc/command" in window.uav.banner.text()
    assert "start_multi_nav_by_points" in window.uav.banner.text()
    window.mqtt.session = "s2"
    window._ingest(Event("local/uav/bridge/heartbeat", {"type": "online", "info": {"result": 0, "data": "ONLINE"}}, session="s2"))
    window.uav.render()
    assert window.uav.banner.isHidden()


def test_nothing_is_published_on_a_uav_connection(window):
    client = window.mqtt.client
    window.mode_box.setCurrentText("直连模块")
    window.teleop.remote.press("motion", "fwd")
    window.teleop.remote.release("motion", "fwd")
    assert not window._quick_send("take_photo", {})
    window.control._open_command("take_photo")
    window.control._send()
    assert client.sent == []


def test_uav_connection_subscribes_local_uav_only():
    import queue
    from dog_debug.transport import MqttTransport
    transport = MqttTransport(queue.Queue())
    transport.device = "uav"
    topics = []
    client = type("Client", (), {"subscribe": lambda self, t: topics.append(t)})()
    transport.client = client
    transport._connected(client, None, None, 0, None)
    assert topics == [[("local/uav/#", 0)]]


def test_uav_connect_modes(window, monkeypatch):
    """无人机两种方式：“云端 → GW”是模拟云端（带无人机 SN 连），“直连模块”是模拟 GW（本地主题）；沙盒不连"""
    window.mqtt.client = None
    called = []
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: called.append(a))
    window.device_box.setCurrentText("无人机")
    window.connection._connect()
    assert called == []
    window.mode_box.setCurrentText("云端 → 真实 GW")
    window.uav_sn.setText("1209039909594066")
    window.connection._connect()
    assert called[-1][-2:] == ("uav", "1209039909594066")
    window.mqtt.client = None
    window.mode_box.setCurrentText("直连模块")
    window.connection._connect()
    assert called[-1][-2:] == ("uav", "")


def test_bridge_ack_diagnosis_and_final_wins():
    from dog_debug.core import diagnose, summarize
    ack = lambda status, final, result, data, at: Event("local/uav/bridge/ack", {
        "sn": "", "id": "p1", "action": "navigation.pause", "status": status, "final": final,
        "info": {"result": result, "data": data, "msg": "说明"}}, session="s", timestamp=at)
    assert diagnose(ack("rejected", True, 1, "WRONG_STATE", 1)).status == "失败"
    assert diagnose(ack("accepted", False, 0, "JOB_ACCEPTED", 1)).status == "受理中"
    assert diagnose(ack("uncertain", True, 2, "NO_RECEIPT", 1)).status == "结果不确定"
    assert diagnose(ack("confirmed", True, 0, "FSM_ACCEPTED", 1)).status == "请求已处理"
    command = Event("local/uav/bridge/command", {"sn": "", "id": "p1", "action": "navigation.pause"}, session="s", timestamp=0)
    late = [command, ack("succeeded", True, 0, "READY", 2), ack("accepted", False, 0, "JOB_ACCEPTED", 3)]
    assert summarize(late).status == "完成"


def test_vfc_tid_and_bridge_id_with_same_string_stay_separate():
    from dog_debug.core import Store, requests
    store = Store(":memory:")
    for e in (Event("local/uav/vfc/command", {"method": "pause_nav", "tid": "shared-id"}, sn="", session="s"),
              Event("local/uav/vfc/ack", {"method": "pause_nav", "tid": "shared-id", "result": 1, "msg": "不能暂停"}, sn="", session="s"),
              Event("local/uav/bridge/ack", {"sn": "", "id": "shared-id", "action": "navigation.pause", "status": "rejected",
                                             "final": True, "info": {"result": 1, "data": "WRONG_STATE", "msg": "状态不允许暂停"}},
                    sn="", session="s")):
        store.add(e)
    store.flush()
    found = requests(store, store.query(limit=10))
    assert len(found) == 2
    bridge = next(r for r in found if r.events[0].topic.startswith("local/uav/bridge/"))
    assert [e.topic for e in bridge.events] == ["local/uav/bridge/ack"] and bridge.status == "失败"


def test_timeline_only_shows_current_connection(window):
    window._ingest(Event("local/uav/vfc/command", {"method": "pause_nav", "tid": "a1"}, sn="", session="s1"))
    window.uav.render()
    assert [r["key"][2] for r in window.uav.rows] == ["a1"]
    window.mqtt.session = "s2"
    window._ingest(Event("local/uav/vfc/command", {"method": "stop_nav", "tid": "b1"}, sn="", session="s2"))
    window.uav.render()
    assert [r["key"][2] for r in window.uav.rows] == ["b1"]


def test_dog_connection_explains_how_to_see_uav(window):
    window.mqtt.device = "dog"
    window.uav.render()
    assert "按机器狗订阅" in window.uav.link_note.text()


def test_old_database_bridge_ids_upgraded_once(tmp_path):
    """旧版库里 Bridge 的请求号没有前缀：打开时补上，证据链、请求列表、备注都对得上；同串的 VFC 请求和它的备注不动；再打开不重复改"""
    from dog_debug.core import Store, requests
    path = tmp_path / "old.sqlite3"
    store = Store(path)
    rows = [Store.row(e) for e in (
        Event("local/uav/bridge/command", {"sn": "", "id": "old-id", "action": "navigation.pause"}, sn="", session="s"),
        Event("local/uav/bridge/ack", {"sn": "", "id": "old-id", "action": "navigation.pause", "status": "rejected", "final": True,
                                       "info": {"result": 1, "data": "WRONG_STATE", "msg": "不允许"}}, sn="", session="s"),
        Event("local/uav/vfc/command", {"method": "pause_nav", "tid": "same"}, sn="", session="s"),
        Event("local/uav/bridge/ack", {"sn": "", "id": "same", "action": "navigation.pause", "status": "rejected", "final": True,
                                       "info": {"result": 1, "data": "X", "msg": "x"}}, sn="", session="s"))]
    # 旧版的 tid 列是不带前缀的原 id
    rows = [row[:5] + (row[5].removeprefix("bridge:"),) + row[6:] for row in rows]
    store.add_rows(rows)
    store.set_note(("", "s", "old-id"), {"remark": "Bridge 的备注"})
    store.set_note(("", "s", "same"), {"remark": "分不清是谁的"})
    store.flush()
    store.close()
    for _ in range(2):
        store = Store(path)
        found = {r.key: r for r in requests(store, store.query(limit=10))}
        assert found[("", "s", "bridge:old-id")].status == "失败" and len(found[("", "s", "bridge:old-id")].events) == 2
        assert ("", "s", "same") in found and ("", "s", "bridge:same") in found
        assert store.note(("", "s", "bridge:old-id"))["remark"] == "Bridge 的备注"
        assert store.note(("", "s", "same"))["remark"] == "分不清是谁的"
        reader = store.reader()
        assert len(reader.trace(found[("", "s", "bridge:old-id")].events[0])) == 2
        reader.db.close()
        store.close()


def test_control_page_lists_uav_commands_with_their_templates(window):
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    methods = {window.control.commands.item(i).data(0x0100)["method"] for i in range(window.control.commands.count())
               if window.control.commands.item(i).data(0x0100)}
    assert {"start_multi_nav_by_points", "land", "system.start", "navigation.next"} <= methods and "take_photo" not in methods
    window.control._open_command("pause_nav")
    payload = json.loads(window.control.editor.toPlainText())
    assert window.control.topic.text() == "local/uav/vfc/command" and "target" not in payload and payload["tid"] and payload["ts"]
    window.control._open_command("system.start")
    payload = json.loads(window.control.editor.toPlainText())
    # Bridge 不认多余的顶层字段：只有 id，没有 tid
    assert window.control.topic.text() == "local/uav/bridge/command" and payload["id"].startswith("kunkun-") and "tid" not in payload
    assert set(payload) == {"sn", "id", "type", "action", "args", "client", "ts"}
    window.control._form_edited("profile", "known_navigation")
    assert json.loads(window.control.editor.toPlainText())["args"]["profile"] == "known_navigation"
    window.device_box.setCurrentText("机器狗")
    assert window.control.selected["module"] != "bridge"


def test_uav_send_needs_switch_and_only_goes_to_command_topics(window):
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("pause_nav")
    window.control._send()
    assert client.sent == []
    window.uav_send.setChecked(True)
    window.control._send()
    assert [topic for topic, _ in client.sent] == ["local/uav/vfc/command"]
    sent = json.loads(client.sent[0][1])
    # VFC 回了 ACK，本次结果里看得到
    window._ingest(Event("local/uav/vfc/ack", {"method": "pause_nav", "tid": sent["tid"], "result": 1, "msg": "当前状态 StandBy 不能暂停"},
                         sn="", session="s1"))
    window._refresh_tail()
    assert "不能暂停" in window.control.result.text()
    window.control._open_command("system.status")
    window.control._send()
    assert client.sent[-1][0] == "local/uav/bridge/command"
    # 狗的指令即使开了开关也不会发到无人机的 Broker
    assert not window._quick_send("take_photo", {}) and len(client.sent) == 2


def test_new_connection_turns_uav_send_off(window, monkeypatch):
    window.uav_send.setChecked(True)
    window.mqtt.client = None
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: None)
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.connection._connect()
    assert not window.uav_send.isChecked() and not window.mqtt.uav_send


def test_flight_capture_changes_include_arming_takeoff_and_mode(window):
    """同事真飞的抓包（只定位模式）：flight 整体一直 STALE，解锁、离地、落地、模式变化照样记下来"""
    for e in capture("flight.jsonl"):
        window._ingest(e)
    texts = [text for _, text, _ in window.uav.log]
    assert "飞控解锁 否 → 是" in texts and "飞控落地 是 → 否" in texts and "飞控落地 否 → 是" in texts
    assert "飞控模式 OFFBOARD → POSCTL" in texts and "任务环境 STOPPING → READY" in texts
    assert any(text.startswith("VFC 任务 Failed：VFC 重启后不续飞") for text in texts)
    flying = next(e for e in capture("flight.jsonl") if e.topic.endswith("state/flight") and e.payload["info"].get("detail", {}).get("armed"))
    extra = reading(flying)[3]
    assert reading(flying)[0] == "STALE" and "解锁 是" in extra and "整体 STALE，detail 仍在更新" in extra


def test_waypoint_release_moments_are_loud(window):
    nav = lambda state, word, last: Event("local/uav/bridge/state/navigation", {"sn": "", "type": "navigation", "state": state,
        "info": {"result": 0, "msg": "", "data": word, "detail": {"session_id": "s", "waypoint_total": 3, "waypoint_last_completed": last}}},
        sn="", session="s1")
    flight = lambda word: Event("local/uav/bridge/state/flight", {"sn": "", "type": "flight", "state": 6,
        "info": {"result": 0, "msg": "", "data": word, "detail": {"armed": True}}}, sn="", session="s1")
    for e in (nav(4, "ACTIVE", -1), nav(5, "WAYPOINT_COMPLETE_WAITING_EXTERNAL_TRIGGER", 0), flight("PRE_YAW"), flight("HOLD"),
              Event("local/uav/bridge/command", {"sn": "", "id": "vfc-1-9", "action": "navigation.next", "client": "vfc"}, sn="", session="s1"),
              Event("local/uav/bridge/events", {"sn": "", "type": "waypoint_complete", "state": 1,
                                                "info": {"result": 0, "data": "waypoint=1 arrival_yaw_complete"}}, sn="", session="s1")):
        window._ingest(e)
    loud = {text for _, text, flag in window.uav.log if flag}
    assert "导航 ACTIVE → WAYPOINT_COMPLETE_WAITING_EXTERNAL_TRIGGER（到点，等待放行）" in loud
    assert "最后完成航点编号 -1 → 0" in loud and "飞控 PRE_YAW → HOLD（转向完成，可以放行）" in loud
    assert "发出 navigation.next（vfc，id vfc-1-9）" in loud and "Bridge 到点事件：waypoint=1 arrival_yaw_complete" in loud


def test_quick_buttons_send_with_current_task_bid_at_qos1(window):
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window._ingest(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "msg": "", "data": "Active",
                                                                                              "flight_id": "task-7"}}, sn="", session="s1"))
    window.uav._quick("pause_nav")
    assert client.sent == []
    window.uav_send.setChecked(True)
    window.uav._quick("pause_nav")
    topic, text = client.sent[-1]
    payload = json.loads(text)
    assert topic == "local/uav/vfc/command" and payload["method"] == "pause_nav" and payload["bid"] == "task-7" and payload["tid"]
    # VFC 和 Bridge 的指令主题按协议 QoS 1，不带 retain
    assert client.options[-1] == (1, False)


def test_route_editor_converts_yaw_and_actions(window):
    from dog_debug.pages.uav import parse_actions, quaternion
    assert quaternion(90) == {"x": 0.0, "y": 0.0, "z": 0.707107, "w": 0.707107}
    assert parse_actions("pause 5; take_photo; gimbal_action 1,1") == [
        {"id": "a1", "action": "pause", "param": [5]}, {"id": "a2", "action": "take_photo", "param": []},
        {"id": "a3", "action": "gimbal_action", "param": [1, 1]}]
    editor = window.uav.route
    editor._add(("2", "3", "1.5", "180", "take_photo"))
    # 已知地图一定要带 map_id（第六轮模式由 map_id 定）
    editor.map_id.setText("")
    with pytest.raises(ValueError):
        editor.route()
    editor.map_id.setText("M1")
    data, bid = editor.route()
    assert len(data["points"]) == 2 and data["points"][1]["position"] == {"x": 2.0, "y": 3.0, "z": 1.5}
    assert data["points"][1]["orientation"]["w"] == 0.0 and data["points"][1]["orientation"]["z"] == 1.0 and bid
    editor.table.item(0, 0).setText("abc")
    window.uav._send_route()
    assert "第 1 个航点" in editor.error.text()
    editor.table.item(0, 0).setText("0")
    # 点“发送航线”后发出的是 start_multi_nav_by_points，bid 用编辑器里的
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window.uav._send_route()
    payload = json.loads(client.sent[-1][1])
    # 默认那个点航向 0：四元数 (0,0,0,1)，不是全 0；动作默认为空（VFC 现在不支持航点动作）
    assert payload["method"] == "start_multi_nav_by_points"
    assert payload["data"]["points"][0]["orientation"] == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0} and payload["data"]["points"][0]["actions"] == []
    assert payload["bid"] == editor.bid.text().strip() and not editor.error.text()


def test_export_session_messages_as_jsonl(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog
    for e in capture("commands.jsonl"):
        window._ingest(e)
    window.store.flush()
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    window.uav._export()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(capture("commands.jsonl")) and rows[0]["topic"] == "local/uav/bridge/state/system" and rows[0]["retain"]


def test_capture_jsonl_imports_on_logs_page(tmp_path):
    from dog_debug.logs import import_log
    events = list(import_log(DATA / "commands.jsonl", "", "import-x"))
    acks = [e for e in events if e.topic == "local/uav/bridge/ack"]
    assert acks and acks[0].tid.startswith("bridge:vfc-") and acks[0].source_time == "14:51:21"
    assert events[0].retained and events[0].module == "bridge"


def test_sidebar_stays_and_pages_switch_content_with_device(window, monkeypatch):
    """狗和无人机共用 5 个页，换设备不换页、只换内容；顶栏出现“允许下发”；连上后不能换设备，断开才放开"""
    rows = lambda: [window.nav.item(i).text() for i in range(window.nav.count())]
    assert rows() == ["总览与遥测", "遥控与载荷", "指令与链路", "任务与文件", "日志与配置"] and window.uav_lock.isHidden()
    window._go(window.TELEOP)
    window.device_box.setCurrentText("无人机")
    assert rows() == ["总览与遥测", "遥控与载荷", "指令与链路", "任务与文件", "日志与配置"]
    assert window.stack.currentIndex() == window.TELEOP and window.teleop_stack.stack.currentIndex() == 1
    assert window.mission_stack.stack.currentIndex() == 1 and window.device_seg.index() == 1
    assert not window.uav_lock.isHidden() and window.logs.fetch_logs.isHidden() and window.logs.live_follow.isHidden()
    window.device_seg.set_index(0)
    assert window.device_box.currentText() == "机器狗" and window.teleop_stack.stack.currentIndex() == 0 and window.uav_lock.isHidden()
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.mqtt.client = None
    monkeypatch.setattr(window.mqtt, "connect", lambda *a, **k: None)
    window.connection._connect()
    assert not window.device_box.isEnabled() and not window.device_seg.isEnabled()
    window.mqtt.client = object()
    monkeypatch.setattr(window.mqtt, "close", lambda: None)
    window.connection._connect()
    assert window.device_box.isEnabled() and window.device_seg.isEnabled()


def test_bridge_send_warns_when_vfc_online(window, monkeypatch):
    """VFC 在线时直接发 Bridge 命令提示会绕开 VFC，不拦；VFC 指令不提示"""
    import time
    notes = []
    monkeypatch.setattr(window, "notify", notes.append)
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window.control._open_command("system.status")
    window.control._send()
    assert "绕开 VFC" not in notes[-1]
    window._ingest(Event("local/uav/vfc/state", {"sn": "", "type": "subsystem_status", "data": {"name": "vfc", "state": 1, "version": "1.0.0"}},
                         sn="", session="s1", timestamp=time.time()))
    window.control._send()
    assert "绕开 VFC" in notes[-1] and client.sent[-1][0] == "local/uav/bridge/command"
    window.control._open_command("pause_nav")
    window.control._send()
    assert "绕开 VFC" not in notes[-1]


def test_uav_module_detail_like_dog(window):
    """点无人机的模块卡片，和狗一样右侧展开详情：模块上报的各类状态和指令与回复"""
    window.device_box.setCurrentText("无人机")
    for e in capture("commands.jsonl"):
        window._ingest(e)
    card_text(window, "bridge")
    window.status.module_frames["bridge"].clicked.emit()
    assert not window.status.detail_card.isHidden() and window.status.detail_title.text() == "BRIDGE 详情"
    window.status.detail_view.set_index(1)
    rows = window.status.detail_model.rows
    assert rows and all(e.module == "bridge" for e, *_ in rows)
    window.status.module_frames["vfc"].clicked.emit()
    assert window.status.detail_model.rows and all(e.module == "vfc" for e, *_ in window.status.detail_model.rows)


def test_saved_dog_profile_cannot_switch_device_while_uav_connected(window, monkeypatch):
    """连着无人机时选保存的狗配置：不应用、配置下拉退回去，侧栏和连接身份不分裂；断开后可以选"""
    window.store.save_connection("狗", {"mode": "direct", "host": "10.0.0.2", "port": 1883, "sn": "D1", "user": "", "tls": False, "device": "dog"})
    window.connection._load_profiles()
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.mqtt.client = None
    monkeypatch.setattr(window.mqtt, "connect", lambda *a, **k: None)
    window.connection._connect()
    window.mqtt.client = object()
    index = window.profile_box.findText("狗")
    window.profile_box.setCurrentIndex(index)
    window.connection._profile_chosen(index)
    assert window.device_box.currentText() == "无人机" and window.profile_box.currentIndex() == 0
    assert window.nav.item(0).text() == "总览与遥测" and window.device_seg.index() == 1
    window.mqtt.client = None
    window.connection._profile_chosen(index)
    assert window.device_box.currentText() == "机器狗" and window.host.text() == "10.0.0.2"


def test_related_commands_not_pushed_out_by_other_modules(window):
    """VFC 详情的指令与回复在 SQL 里按模块筛：之后来 610 条 Bridge 命令也挤不掉那条 VFC 指令"""
    window.device_box.setCurrentText("无人机")
    window._ingest(Event("local/uav/vfc/command", {"method": "pause_nav", "tid": "p1", "bid": "b", "data": {}}, sn="", session="s1"))
    for i in range(610):
        window._ingest(Event("local/uav/bridge/command", {"sn": "", "id": f"s{i}", "type": "system", "action": "system.status", "client": "vfc"},
                             sn="", session="s1"))
    card_text(window, "vfc")
    window.status._open_module("vfc")
    window.status.detail_view.set_index(1)
    assert [e.method for e, *_ in window.status.detail_model.rows] == ["pause_nav"]


def test_every_uav_send_gets_new_id_and_current_ts(window):
    """11-09 真机：同一条 stop_nav 原样发了 5 次、system.stop 带着上午的 id 和 ts，Bridge 当重复请求回了旧结果。
    每次点发送都换新 tid / id 和当前 ts；VFC 的 bid 是任务 ID，不换"""
    import time
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window.control._open_command("stop_nav")
    old = json.loads(window.control.editor.toPlainText())
    old["ts"] = 1794195129773
    window.control.editor.setPlainText(json.dumps(old))
    window.control._send()
    window.control._send()
    first, second = (json.loads(text) for _, text in client.sent[-2:])
    assert first["tid"] != second["tid"] != old["tid"] and first["bid"] == second["bid"] == old["bid"]
    assert abs(first["ts"] / 1000 - time.time()) < 5
    assert json.loads(window.control.editor.toPlainText())["tid"] == second["tid"]
    window.control._open_command("system.stop")
    window.control._send()
    window.control._send()
    a, b = (json.loads(text) for _, text in client.sent[-2:])
    assert a["id"] != b["id"] and a["id"].startswith("kunkun-") and "tid" not in a and abs(b["ts"] / 1000 - time.time()) < 5


def test_bridge_duplicate_ack_is_flagged_not_executed(window):
    """Bridge 回执带 duplicate: true：结论标红“重复请求未执行”，关键变化里醒目写明"""
    from dog_debug.core import diagnose
    cmd = Event("local/uav/bridge/command", {"sn": "", "id": "kunkun-5fc28597c47b471f", "type": "system", "action": "system.stop",
                                            "args": {"confirm": True}, "client": "kunkun"}, sn="", session="s1")
    ack = Event("local/uav/bridge/ack", {"sn": "", "id": "kunkun-5fc28597c47b471f", "type": "system", "state": 4, "action": "system.stop",
                                        "status": "succeeded", "final": True, "job_id": "job-20261104-073246-7558b8",
                                        "info": {"result": 0, "msg": "环境已停止", "data": "READY"}, "duplicate": True}, sn="", session="s1")
    for e in (cmd, ack):
        window._ingest(e)
    window.store.flush()
    assert diagnose(ack).status == "重复请求未执行"
    assert summarize(window.store.trace(cmd)).status == "重复请求未执行"
    assert any(loud and "当重复请求" in text for _, text, loud in window.uav.log)


def test_control_page_vfc_bid_auto_filled(window):
    """11-09 真飞：手编的航线没带 bid 被 VFC 拒（缺少字段或为空：bid），返航、降落带的是模板随机 bid 不是当前任务的。
    开始航线没填 bid 自动生成；其他 VFC 指令带当前任务的 bid；航线模板航向 0、动作为空"""
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window.control._open_command("start_multi_nav_by_points")
    template = json.loads(window.control.editor.toPlainText())
    assert template["bid"] and template["data"]["points"][0]["orientation"] == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    assert template["data"]["points"][0]["actions"] == []
    del template["bid"]
    window.control.editor.setPlainText(json.dumps(template))
    window.control._send()
    assert json.loads(client.sent[-1][1])["bid"]
    window._ingest(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "msg": "", "data": "Active",
                                                                                              "flight_id": "11111"}}, sn="", session="s1"))
    for method in ("pause_nav", "stop_nav"):
        window.control._open_command(method)
        window.control._send()
        assert json.loads(client.sent[-1][1])["bid"] == "11111"
    # 返航、降落是新任务（VFC 994c1ae），bid 新生成，不用当前任务的
    for method in ("start_nav_return_home", "land"):
        window.control._open_command(method)
        window.control._send()
        assert json.loads(client.sent[-1][1])["bid"] not in ("", "11111")


def test_control_page_route_edits_yaw_not_quaternion(window):
    """控制调试页的开始航线：points 用航点表格编，只填航向角；改了同步成四元数写回 JSON，填错不写回"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("start_multi_nav_by_points")
    table = window.control.points_table
    assert not table.isHidden() and table.table.rowCount() == 1 and table.table.item(0, 3).text() == "0"
    labels = [window.control.form_grid.itemAt(i).widget().text() for i in range(window.control.form_grid.count())]
    assert "points" not in labels and "map_id" in labels
    table.table.item(0, 3).setText("90")
    point = json.loads(window.control.editor.toPlainText())["data"]["points"][0]
    assert point["orientation"] == {"x": 0.0, "y": 0.0, "z": 0.707107, "w": 0.707107} and point["actions"] == []
    table._add(("-5", "-9", "1.2", "180", ""))
    assert len(json.loads(window.control.editor.toPlainText())["data"]["points"]) == 2
    table.table.item(1, 0).setText("abc")
    assert "第 2 个航点" in table.error.text() and json.loads(window.control.editor.toPlainText())["data"]["points"][1]["position"]["x"] == -5.0
    # 手改 JSON 里的四元数，表格按航向角显示
    payload = json.loads(window.control.editor.toPlainText())
    payload["data"]["points"][0]["orientation"] = {"x": 0, "y": 0, "z": 1.0, "w": 0.0}
    window.control.editor.setPlainText(json.dumps(payload))
    window.control._rebuild_form()
    assert window.control.points_table.table.item(0, 3).text() == "180"
    # 别的指令不显示航点表格
    window.control._open_command("land")
    assert window.control.points_table.isHidden()


def test_new_id_button_follows_command(window):
    """无人机发送时自动换 tid / id，开始航线的按钮是“换新 bid”、只换 bid；其他无人机指令不显示；狗照旧“换新 tid”"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("start_multi_nav_by_points")
    button = window.control.new_tid_btn
    assert button.text() == "换新 bid" and not button.isHidden()
    before = json.loads(window.control.editor.toPlainText())
    window.control._new_tid()
    after = json.loads(window.control.editor.toPlainText())
    assert after["bid"] != before["bid"] and after["tid"] == before["tid"] and after["data"] == before["data"]
    for method in ("stop_nav", "system.stop"):
        window.control._open_command(method)
        assert button.isHidden()
    window.control._open_command("land")
    assert button.text() == "换新 bid" and not button.isHidden()
    window.device_box.setCurrentText("机器狗")
    window.control._open_command("take_photo")
    assert button.text() == "换新 tid" and not button.isHidden()


def test_uav_bid_rules_do_not_touch_dog_vfc(window):
    """狗也有 VFC 模块：狗的 move 模板不加 bid、不出航点表格，按钮照旧“换新 tid”"""
    window.device_box.setCurrentText("机器狗")
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("move")
    payload = json.loads(window.control.editor.toPlainText())
    assert "bid" not in payload and window.control.points_table.isHidden()
    assert window.control.new_tid_btn.text() == "换新 tid" and not window.control.new_tid_btn.isHidden()


def test_points_table_survives_bad_points_in_json(window):
    """手改 JSON 成表格认不出的 points（null、非数字四元数）：表格收起、JSON 原样保留、不报错；改回能认的表格接着用"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("start_multi_nav_by_points")
    good = json.loads(window.control.editor.toPlainText())
    for bad in ([None], [{"position": {"x": 0, "y": 0, "z": 1}, "orientation": {"z": "abc"}, "actions": []}]):
        payload = dict(good, data=dict(good["data"], points=bad))
        text = json.dumps(payload)
        window.control.editor.setPlainText(text)
        window.control._rebuild_form()
        assert window.control.points_table.isHidden() and "航点表格暂停" in window.control.form_note.text()
        assert window.control.editor.toPlainText() == text
        assert not window.control.points_table.table.signalsBlocked()
    window.control.editor.setPlainText(json.dumps(good))
    window.control._rebuild_form()
    table = window.control.points_table
    assert not table.isHidden()
    table.table.item(0, 3).setText("90")
    assert json.loads(window.control.editor.toPlainText())["data"]["points"][0]["orientation"]["z"] == 0.707107


def test_task_bid_not_carried_over_from_previous_connection(window):
    """上一个连接的任务状态不当成当前任务：新连接还没收到 nav_status 时，降落不带旧 bid"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window._ingest(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "msg": "", "data": "Active",
                                                                                              "flight_id": "OLD_TASK"}}, sn="", session="s1"))
    assert window.uav.current_task() == "OLD_TASK"
    window.mqtt.session = "s2"
    window.control._open_command("land")
    assert json.loads(window.control.editor.toPlainText())["bid"] != "OLD_TASK"
    assert window.uav.current_task() == "" and window.uav._task_bid() != "OLD_TASK"


def mapping_state(data, msg, session_id="", result=0, raw=""):
    return Event("local/uav/vfc/state", {"name": "vfc", "version": "1.0.0", "state": 1, "type": "mapping_status",
                                         "info": {"result": result, "msg": msg, "data": data, "raw": raw, "session_id": session_id}},
                 sn="", session="s1")


def test_mapping_commands_follow_vfc_format(window):
    """建图：start_mapping 新 bid、data {}；stop_mapping 带当前任务 bid，save 必须由人选“保存地图”或“不保存”才发得出去"""
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window._ingest(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "msg": "建图中（session 待确认）",
                                                                                              "data": "Active", "flight_id": "MAP-1"}}, sn="", session="s1"))
    window.control._open_command("start_mapping")
    assert window.control.new_tid_btn.text() == "换新 bid"
    window.control._send()
    sent = json.loads(client.sent[-1][1])
    assert sent["method"] == "start_mapping" and sent["data"] == {} and sent["bid"] and sent["bid"] != "MAP-1" and sent["tid"]
    assert client.options[-1] == (1, False)
    window.control._open_command("stop_mapping")
    template = json.loads(window.control.editor.toPlainText())
    assert template["data"] == {"save": None} and template["bid"] == "MAP-1"
    count = len(client.sent)
    window.control._send()
    assert len(client.sent) == count
    from PySide6.QtWidgets import QRadioButton
    options = {o.text(): o for o in window.control.form_area.findChildren(QRadioButton)}
    assert set(options) == {"保存地图", "不保存", "按默认（保存）"} and not any(o.isChecked() for o in options.values())
    options["不保存"].setChecked(True)
    window.control._send()
    sent = json.loads(client.sent[-1][1])
    assert sent["method"] == "stop_mapping" and sent["data"] == {"save": False} and sent["bid"] == "MAP-1"
    # 按默认：data 里不带 save（VFC 按保存），表单重建后仍显示选的是“按默认”
    options["按默认（保存）"].setChecked(True)
    assert json.loads(window.control.editor.toPlainText())["data"] == {}
    window.control._rebuild_form()
    options = {o.text(): o for o in window.control.form_area.findChildren(QRadioButton) if not o.isHidden()}
    assert options["按默认（保存）"].isChecked()
    window.control._send()
    assert json.loads(client.sent[-1][1])["data"] == {}
    # 航线任务页：开始建图新 bid，停止两个按钮各自带明确的 save
    window.uav._start_mapping()
    assert json.loads(client.sent[-1][1])["bid"] != "MAP-1"
    window.uav._stop_mapping(True)
    sent = json.loads(client.sent[-1][1])
    assert sent["data"] == {"save": True} and sent["bid"] == "MAP-1"
    window.uav._stop_mapping(None)
    assert json.loads(client.sent[-1][1])["data"] == {}


def test_mapping_status_card_shows_data_msg_session(window):
    """mapping_status（VFC 1.0.0）：data 是协议词、raw 是 Bridge 原词、result 是协议码；VFC 卡片里看得到词、原词和 session_id；
    保存结果醒目记一条；session 能复制"""
    window.device_box.setCurrentText("无人机")
    window._ingest(mapping_state("Passive", "当前不在建图", raw="DISABLED", result=2))
    window._ingest(mapping_state("MappingRunning", "正在建图", "20261109_140005", raw="MAPPING", result=5))
    window._ingest(mapping_state("MappingSaveBegin", "正在保存地图", "20261109_140005", raw="SAVING", result=7))
    window._ingest(mapping_state("MappingSaveEnd", "地图已保存", "20261109_140005", raw="SAVED", result=8))
    window.uav.render()
    body = card_text(window, "vfc")[0]
    assert "data=MappingSaveEnd" in body and "raw=SAVED" in body and "session_id=20261109_140005" in body
    assert any(loud and text == "VFC 建图 MappingSaveEnd：地图已保存" for _, text, loud in window.uav.log)
    assert window.uav.mapping_session() == "20261109_140005" and "MappingSaveEnd" in window.uav.mapping_line.text()
    from dog_debug.uav import quality
    assert quality(mapping_state("MappError", "保存失败", raw="SAVE_FAILED", result=6)) == "失败"
    assert quality(mapping_state("MappingRunning", "正在建图", result=5)) == "正常"


def test_vfc_osd_shown_raw_and_not_driving_dog_scene(window):
    """VFC OSD（local/uav/vfc/osd）：卡片里原值显示；解读里缺的键是“—”不当 0；不驱动狗的 3D"""
    from dog_debug.uav import reading
    window.device_box.setCurrentText("无人机")
    e = Event("local/uav/vfc/osd", {"sn": "", "ts": 1793760000000, "data": {"sn": "", "x": 0.42, "y": 3.1, "z": 1.2, "speed": 0.98,
                                                                          "heading": 81, "ts": 1793760000000}}, sn="", session="s1")
    window._ingest(e)
    body = card_text(window, "vfc")[0]
    assert "osd" in body and "x=0.42" in body and "heading=81" in body
    word, msg, _, extra = reading(e)
    assert word == "电量 —" and msg == "x 0.42　y 3.1　z 1.2（米）" and extra == "速度 0.98 m/s · 航向 81°"
    assert window.scene.osd == {}
    assert not any("OSD" in text for _, text, _ in window.uav.log)


def test_osd_non_numbers_shown_as_dash_and_unknown(window):
    """OSD 里类型不对的值（false、null、字符串、true）不当测量值：显示“—”、写明哪几项不是数字、数据标未知"""
    from dog_debug.uav import quality, reading
    e = Event("local/uav/vfc/osd", {"sn": "", "data": {"x": False, "y": None, "z": "oops", "speed": True, "heading": None,
                                                       "batteryRemain": None}}, sn="", session="s1")
    word, msg, result, extra = reading(e)
    assert word == "电量 —" and msg == "x —　y —　z —（米）" and extra.startswith("速度 — · 航向 —")
    assert "x、y、z、speed、heading、batteryRemain 不是数字" in extra and quality(e) == "未知"
    ok = Event("local/uav/vfc/osd", {"sn": "", "data": {"x": 0, "batteryRemain": 72}}, sn="", session="s1")
    assert reading(ok)[0] == "电量 72%" and reading(ok)[1].startswith("x 0　y —") and quality(ok) == "正常"


def test_uav_messages_do_not_show_on_dog_status(window, monkeypatch):
    """连无人机时连接卡里隐藏着狗的 SN：无人机消息不再记这个 SN；旧库里已经记了狗 SN 的无人机消息，狗的状态总览也不取"""
    window.store.add(Event("local/uav/vfc/osd", {"sn": "", "data": {"x": 15, "y": 20}}, sn="TEST_DOG_001", session="old-uav"))
    window.store.add(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "data": "Active"}},
                           sn="TEST_DOG_001", session="old-uav"))
    window.store.add(Event("local/robotdog/vfc/state", {"type": "subsystem_status", "data": {"state": 1}}, sn="TEST_DOG_001", session="dog"))
    window.store.flush()
    rows = window.store.query(kind="状态", sn="TEST_DOG_001", live=True, device="dog")
    assert [e.topic for _, e in rows] == ["local/robotdog/vfc/state"]
    assert len(window.store.query(kind="状态", sn="TEST_DOG_001", live=True)) == 3
    # 新连接：无人机的消息 sn 为空
    import queue
    import paho.mqtt.client as mqtt
    from dog_debug.transport import MqttTransport

    class Client:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(mqtt, "Client", Client)
    inbox = queue.Queue()
    transport = MqttTransport(inbox)
    transport.connect("192.168.1.230", 1883, "TEST_DOG_001", device="uav")
    message = type("M", (), {"topic": "local/uav/vfc/osd", "payload": b'{"data": {"x": 1}}', "qos": 0, "retain": False, "dup": False})()
    transport._message(transport.client, None, message)
    events = [item[1] for item in list(inbox.queue) if item[0] == "event"]
    assert transport.sn == "" and events and events[-1].sn == ""


def test_mosquitto_sub_json_capture_imports(tmp_path):
    """nav 板上验收用 mosquitto_sub -F %j 抓的包（payload 跨多行）：主题、retain、原文时间、模块都认出来"""
    from dog_debug.logs import import_log
    path = tmp_path / "板上.jsonl"
    path.write_text('{"tst":"2026-11-11T09:48:09+0800","topic":"local/uav/bridge/heartbeat","qos":1,"retain":1,"payloadlen":10,"payload":{\n'
                    '  "sn": "",\n  "type": "online",\n  "info": {"result": 0, "data": "ONLINE"}\n}}\n'
                    '{"tst":"2026-11-11T09:48:10+0800","topic":"local/uav/vfc/state","qos":1,"retain":0,"payload":{"type":"loc_status",'
                    '"info":{"result":2,"msg":"定位未启动","data":"Init"}}}\n', encoding="utf-8")
    events = list(import_log(path, "", "import-x"))
    assert [e.topic for e in events] == ["local/uav/bridge/heartbeat", "local/uav/vfc/state"]
    assert events[0].retained and not events[1].retained and events[0].source_time == "2026-11-11 09:48:09"
    assert events[0].module == "bridge" and events[1].module == "vfc" and events[1].payload["type"] == "loc_status"


def vfc_state(kind, info):
    return Event("local/uav/vfc/state", {"name": "vfc", "version": "1.0.0", "state": 1, "type": kind, "info": info}, sn="", session="s1")


def test_vfc_loc_map_and_action_states_on_vfc_card(window):
    """VFC 1.0.0 的定位（loc_status）、地图（map_status）、航点动作（nav_action_progress）进 VFC 卡片；定位丢失、地图结局醒目记一条"""
    window.device_box.setCurrentText("无人机")
    window._ingest(vfc_state("loc_status", {"result": 2, "msg": "定位未启动", "data": "Init", "raw": {"localization": "STALE", "relocalization": "STALE"}}))
    window._ingest(vfc_state("loc_status", {"result": 1, "msg": "定位丢失", "data": "LocLost", "raw": {"localization": "LOST", "relocalization": "LOST"}}))
    window._ingest(vfc_state("map_status", {"result": 0, "msg": "", "data": "IDLE", "map_busy": False}))
    window._ingest(vfc_state("map_status", {"result": 0, "msg": "地图资产已就绪", "data": "MAP_ASSETS_READY", "map_id": "0000009_20261111_082057",
                                            "auto": True, "map_busy": False}))
    window._ingest(vfc_state("nav_action_progress", {"result": 1, "msg": "本期不支持航点动作，已跳过", "flight_id": "T1",
                                                     "data": {"point_index": 0, "actions": [{"action": "take_photo", "state": "skipped"}]}}))
    body = card_text(window, "vfc")[0]
    assert "data=LocLost" in body and "data=MAP_ASSETS_READY" in body and "nav_action_progress" in body
    loud = {text for _, text, flag in window.uav.log if flag}
    assert "VFC 定位 Init → LocLost：定位丢失" in loud and "VFC 地图 MAP_ASSETS_READY：地图资产已就绪" in loud
    from dog_debug.uav import quality, reading
    assert quality(window.uav.latest["nav_action_progress"]) == "未知" and "take_photo 跳过" in reading(window.uav.latest["nav_action_progress"])[3]


def test_vfc_alarms_raise_and_clear(window):
    """VFC 告警（local/uav/vfc/events，type=alarm）：总览下面告警分段里进行中的在前、已解除的灰；关键变化醒目"""
    alarm = lambda code, state, title, **more: Event("local/uav/vfc/events", {"name": "vfc", "state": 1, "type": "alarm",
        "info": {"code": code, "level": "warning", "state": state, "title": title, "advice": "检查定位", **more}}, sn="", session="s1")
    window._ingest(alarm("ALM-3101", "raise", "定位丢失", x=1.0, y=2.0, z=1.2))
    window._ingest(alarm("ALM-2207", "raise", "避障触发"))
    window._ingest(alarm("ALM-2207", "clear", "避障触发"))
    window.uav.render()
    assert window.uav.alarm_count.text() == "告警 1 条进行中 · 已解除 1 条"
    text = window.uav.alarm_text.text()
    assert text.index("ALM-3101") < text.index("ALM-2207") and "进行中" in text and "已解除" in text and "位置 x 1.0 y 2.0 z 1.2" in text
    assert any(loud and t == "告警解除 ALM-2207 避障触发" for _, t, loud in window.uav.log)


def test_other_uav_modules_on_their_own_card(window):
    """无人机上的其他模块（如 FSVR）按和狗一样的写法进自己的卡片；PTZ 等没收到时写还没有收到"""
    window.device_box.setCurrentText("无人机")
    window._ingest(Event("local/uav/fsvr/state", {"data": {"info": [], "name": "fsvr", "state": 1, "version": "1.0.0"}, "sn": "",
                                                  "type": "subsystem_state"}, sn="", session="s1"))
    window._ingest(Event("local/uav/fsvr/state", {"method": "storage_status", "data": {"upload_remain": 0, "total": 100, "used": 3}},
                         sn="", session="s1"))
    body, when = card_text(window, "fsvr")
    assert "storage_status" in body and "used=3" in body and "v1.0.0" in when
    assert "还没有收到 PTZ" in card_text(window, "ptz")[0]
    window.status._open_module("fsvr")
    assert window.status.detail_title.text() == "FSVR 详情"


def test_map_card_lists_maps_and_sends_map_commands(window, monkeypatch):
    """地图卡：get_map_list 的回执填表；选用、生成资产、改名、删除（问一次）发对应的 VFC 地图指令，bid 新生成；航线可以用选中的图"""
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    window.uav._map_command("get_map_list", {})
    assert json.loads(client.sent[-1][1])["method"] == "get_map_list"
    maps = [{"map_id": "M10", "name": "VFC验收", "size": 13.1, "selected": False, "has_assets": False, "has_raw": True},
            {"map_id": "M9", "name": "一楼", "size": 14.6, "selected": True, "has_assets": True, "has_raw": True}]
    window._ingest(Event("local/uav/vfc/ack", {"method": "get_map_list", "tid": "g1", "bid": "b", "result": 0, "msg": "", "data": maps},
                         sn="", session="s1"))
    window.uav.render()
    table = window.uav.map_table
    assert table.rowCount() == 2 and table.item(1, 3).text() == "✓" and table.item(0, 4).text() == "没有"
    table.selectRow(0)
    sent = lambda: json.loads(client.sent[-1][1])
    window.uav._select_map()
    assert sent()["method"] == "loc_load_map" and sent()["data"] == {"map_id": "M10"} and sent()["bid"]
    window.uav._build_map()
    assert sent()["method"] == "build_map" and sent()["data"] == {"session_id": "M10"}
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("新名字", True))
    window.uav._rename_map()
    assert sent()["data"] == {"map_id": "M10", "new_name": "新名字"}
    count = len(client.sent)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    window.uav._delete_map()
    assert len(client.sent) == count
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.uav._delete_map()
    assert sent()["method"] == "remove_map_by_id" and sent()["data"] == {"map_ids": ["M10"]}
    # 航线用这张图：已知地图带 map_id；第六轮模式由 map_id 定，顶层不再带 profile，未知环境不带 map_id
    window.uav._route_map()
    window.uav._send_route()
    route = sent()
    assert route["method"] == "start_multi_nav_by_points" and "profile" not in route and route["data"]["map_id"] == "M10"
    window.uav.route.profile.set_index(1)
    window.uav._send_route()
    assert "profile" not in sent() and "map_id" not in sent()["data"]


def test_key_telemetry_card_shared_by_dog_and_uav(window):
    """总览顶上的关键遥测卡：狗按设备 OSD、无人机按 VFC OSD 显示数字；没报的“—”不当 0；姿态角没报就不出格子"""
    tiles = lambda: {key: value.text() for key, (tile, value) in window.status.telemetry.tiles.items() if not tile.isHidden()}
    # 狗的实时 OSD 只认活动连接，这里直接交给 scene，等同连着时收到
    window.scene.telemetry(Event("tta/robotdog/TEST_DOG_001/osd", {"batteryRemain": 80, "speed": 0.25, "heading": 90, "x": 1, "y": 2},
                                 sn="TEST_DOG_001", session="s1"))
    window.status.telemetry.update("dog")
    shown = tiles()
    assert shown["batteryRemain"] == "80%" and shown["speed"] == "0.25 m/s" and shown["position"] == "1.00, 2.00"
    assert shown["temperature"] == "—" and "pitch" not in shown and "z" not in shown
    assert window.status.telemetry.state.text() == "OSD 实时"
    window.device_box.setCurrentText("无人机")
    # 无人机卡片只认活动连接的消息：给一条当前会话的实时 OSD
    window._ingest(Event("local/uav/vfc/osd", {"sn": "", "data": {"z": 1.2, "speed": "fast", "heading": 81, "pitch": 3}},
                         sn="", session=window.mqtt.session, source="mqtt"))
    window.status.telemetry.update("uav")
    shown = tiles()
    assert shown["z"] == "1.20 m" and shown["speed"] == "—" and shown["heading"] == "81°" and shown["pitch"] == "3°"
    assert "roll" not in shown and "temperature" not in shown


def test_clear_retained_publishes_empty_retained(window, monkeypatch):
    """保留消息告警旁的按钮：往每个主题发空的保留消息，发成功的从告警里去掉；被只读闸挡住就提示、告警留着"""
    window._ingest(Event("local/uav/vfc/command", {"method": "start_multi_nav_by_points", "tid": "old", "bid": "b"},
                         session=window.mqtt.session, source="mqtt", retained=True))
    window.mqtt.connected = True
    window.uav.render()
    assert not window.uav.clear_btn.isHidden()
    sent, notes = [], []
    monkeypatch.setattr(window, "notify", notes.append)
    monkeypatch.setattr(window.mqtt, "publish", lambda topic, payload, retain=False: (_ for _ in ()).throw(RuntimeError("默认只读")))
    window.uav._clear_retained()
    assert "local/uav/vfc/command" in window.uav.alerts and "默认只读" in notes[-1]
    monkeypatch.setattr(window.mqtt, "publish", lambda topic, payload, retain=False: sent.append((topic, payload, retain)))
    window.uav._clear_retained()
    assert sent == [("local/uav/vfc/command", "", True)] and not window.uav.alerts
    assert window.uav.banner.isHidden() and window.uav.clear_btn.isHidden()
    # 断开后旧告警不能清，按钮也藏起来
    window._ingest(Event("local/uav/vfc/command", {"method": "land", "tid": "t2"}, session=window.mqtt.session, source="mqtt", retained=True))
    window.mqtt.connected = False
    window.uav.render()
    assert not window.uav.banner.isHidden() and window.uav.clear_btn.isHidden()
    window.uav._clear_retained()
    assert len(sent) == 1 and "之前的连接" in notes[-1]


def test_stale_client_callbacks_ignored():
    """断开后马上重连：旧客户端晚到的断开、消息回调不影响新连接"""
    import queue
    from dog_debug.transport import MqttTransport
    inbox = queue.Queue()
    t = MqttTransport(inbox)
    old, new = object(), object()
    t.client, t.connected = new, True
    t._disconnected(old, None, None, 0, None)
    t._message(old, None, type("M", (), {"payload": b"{}", "topic": "x", "qos": 0, "retain": False, "dup": False})())
    assert t.connected and inbox.empty()
    t._disconnected(new, None, None, 0, None)
    assert not t.connected and inbox.get_nowait()[0] == "disconnect"


def test_telemetry_not_live_after_disconnect(window):
    """无人机 OSD 刚到、连接一断就标“已停”，不等 3 秒时效；旧连接的数在新连接收到第一条前不显示"""
    window.device_box.setCurrentText("无人机")
    window.mqtt.connected = True
    window._ingest(Event("local/uav/vfc/osd", {"sn": "", "data": {"z": 2.0}}, sn="", session=window.mqtt.session, source="mqtt"))
    card = window.status.telemetry
    card.update("uav")
    assert card.state.text() == "OSD 实时"
    window.mqtt.connected = False
    card.update("uav")
    assert card.state.text() == "OSD 已停" and card.tiles["z"][1].text() == "2.00 m"
    card.sparks["z"].add(1.0, 2.0)
    card.sparks["z"].add(2.0, 2.5)
    window.mqtt.session = "new-session"
    card.update("uav")
    assert card.state.text() == "未收到 OSD" and card.tiles["z"][1].text() == "—"
    # 换了连接，走势线也从头画
    assert card.sparks["z"].points == []


SN = "1209039909594066"


def cloud(window):
    """设备无人机、方式“云端 → GW”，假装已按模拟云端连上（订阅 tta/uav/{sn}/#）"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("云端 → 真实 GW")
    window.uav_sn.setText(SN)
    window.mqtt.cloud_sn = SN
    window.uav_send.setChecked(True)


def test_cloud_views_match_local_format():
    """GW 转上来的云端报文换成本地的样子再解读：回执 result 从 data 里提出来，状态 method+data，OSD 平铺"""
    from dog_debug.uav import domain, local_view, reading
    reply = local_view(Event(f"tta/uav/{SN}/services_reply", {"method": "land", "tid": "t1", "bid": "b1", "target": "vfc",
                                                              "data": {"result": 1, "msg": "降落中不能结束航线", "data": {"x": 1}}}))
    assert reply.topic == "local/uav/vfc/ack" and reply.payload["result"] == 1 and reply.payload["msg"] == "降落中不能结束航线"
    assert reply.payload["data"] == {"x": 1} and "target" not in reply.payload and reply.tid == "t1"
    state = Event(f"tta/uav/{SN}/state", {"ts": 1, "method": "nav_status", "data": {"result": 0, "msg": "降落中", "data": "Active",
                                                                                  "flight_id": "L1"}})
    assert domain(state) == "nav_status" and reading(state)[:2] == ("Active", "降落中")
    # GW 汇总的各模块在线是个列表，取 VFC 那项；没列 VFC 就是不在线
    online = Event(f"tta/uav/{SN}/state", {"method": "subsystem_status", "data": [{"name": "gw", "state": 1},
                                                                                {"name": "vfc", "state": 1, "info": []}]})
    assert domain(online) == "subsystem_status" and reading(online)[0] == "在线"
    gone = Event(f"tta/uav/{SN}/state", {"method": "subsystem_status", "data": [{"name": "gw", "state": 1}]})
    assert reading(gone)[0] == "state 0"
    # storage_status 是 GW 自己报的
    from dog_debug.core import infer_module
    storage = Event(f"tta/uav/{SN}/state", {"method": "storage_status", "data": {"total": 1, "used": 0}})
    assert domain(storage) == "gw:storage_status" and infer_module(storage.topic, storage.payload) == "gw"
    osd = Event(f"tta/uav/{SN}/osd", {"sn": SN, "x": 1.5, "z": 1.2, "batteryRemain": 70, "functionMode": 0})
    assert domain(osd) == "vfc_osd" and reading(osd)[0] == "电量 70%"
    # 本地报文原样不动
    local = Event("local/uav/vfc/ack", {"result": 0})
    assert local_view(local) is local


def test_cloud_mode_sends_services_with_target(window):
    """模拟云端：指令发 tta/uav/{sn}/services、带 target（默认 vfc，可以改），QoS 1；目录里不列 Bridge 命令"""
    cloud(window)
    client = window.mqtt.client
    window.control._open_command("pause_nav")
    assert window.control.topic.text() == f"tta/uav/{SN}/services"
    assert json.loads(window.control.editor.toPlainText())["target"] == "vfc" and not window.control.target_box.isHidden()
    window.control.target_box.setCurrentText("fsvr")
    assert json.loads(window.control.editor.toPlainText())["target"] == "fsvr"
    window.control._send()
    topic, text = client.sent[-1]
    assert topic == f"tta/uav/{SN}/services" and json.loads(text)["target"] == "fsvr" and client.options[-1] == (1, False)
    assert all(e["module"] != "bridge" for e in window.control._catalog())
    # 航线任务页的快捷指令也走云端
    window.uav._quick("land")
    topic, text = client.sent[-1]
    assert topic == f"tta/uav/{SN}/services" and json.loads(text)["target"] == "vfc" and json.loads(text)["method"] == "land"
    # 换回直连就是模拟 GW：本地主题、不带 target，Bridge 命令回来
    window.mqtt.cloud_sn = ""
    window.mode_box.setCurrentText("直连模块")
    window.control._open_command("pause_nav")
    assert window.control.topic.text() == "local/uav/vfc/command" and "target" not in json.loads(window.control.editor.toPlainText())
    assert any(e["module"] == "bridge" for e in window.control._catalog())


def test_cloud_mode_publish_whitelist():
    """模拟云端连接只放行这台的 services；本地主题、别的 SN 都不发；应答只放行上线和心跳应答"""
    import queue
    from dog_debug.transport import MqttTransport
    t = MqttTransport(queue.Queue())
    t.client, t.connected, t.device, t.cloud_sn, t.uav_send = StubClient(), True, "uav", SN, True
    t.publish(f"tta/uav/{SN}/services", {"method": "land"})
    assert t.client.options[-1] == (1, False)
    for topic in ("local/uav/vfc/command", "tta/uav/OTHER/services"):
        with pytest.raises(RuntimeError):
            t.publish(topic, {})
    t.uav_send = False
    with pytest.raises(RuntimeError):
        t.publish(f"tta/uav/{SN}/services", {})
    # 应答不受允许下发管，但只能是上线和心跳应答
    t.reply(f"tta/uav/{SN}/heartbeat_reply", {"seq": 1})
    with pytest.raises(RuntimeError):
        t.reply(f"tta/uav/{SN}/services", {})


def test_cloud_mode_answers_online_and_heartbeat(window):
    """扮演云端：本机 SN 的上线回 online_reply、心跳回 heartbeat_reply（seq 原样）；别的 SN 不回；开关关了不回；应答入库"""
    cloud(window)
    client = window.mqtt.client
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    window._ingest(Event("tta/uav/status/online", {"sn": SN, "type": "uav"}, **live))
    topic, text = client.sent[-1]
    assert topic == "tta/uav/status/online_reply" and {k: v for k, v in json.loads(text).items() if k != "ts"} == {"sn": SN, "type": "uav", "result": 0}
    window._ingest(Event(f"tta/uav/{SN}/heartbeat", {"seq": 42, "ts": 1}, **live))
    topic, text = client.sent[-1]
    assert topic == f"tta/uav/{SN}/heartbeat_reply" and json.loads(text)["seq"] == 42
    count = len(client.sent)
    window._ingest(Event("tta/uav/status/online", {"sn": "OTHER"}, **live))
    window.cloud_answer.setChecked(False)
    window._ingest(Event(f"tta/uav/{SN}/heartbeat", {"seq": 43}, **live))
    assert len(client.sent) == count
    window.store.flush()
    assert any(e.topic.endswith("heartbeat_reply") for _, e in window.store.query(kind=("ACK",), limit=20))


def test_cloud_reports_show_on_cards_and_timeline(window):
    """模拟云端收到的状态、OSD、回执：卡片、遥测、时间线都按本地格式解读；双击看的还是原始云端报文；GW 不转的显示没收到"""
    cloud(window)
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    window._ingest(Event(f"tta/uav/{SN}/state", {"ts": 1, "method": "nav_status",
                                                 "data": {"result": 0, "msg": "降落中", "data": "Active", "flight_id": "L1"}}, **live))
    window._ingest(Event(f"tta/uav/{SN}/osd", {"sn": SN, "z": 1.2, "batteryRemain": 66}, **live))
    window._ingest(Event(f"tta/uav/{SN}/services", {"method": "stop_nav", "tid": "t9", "bid": "L1", "target": "vfc", "data": {}},
                         direction="发送", **live))
    window._ingest(Event(f"tta/uav/{SN}/services_reply", {"method": "stop_nav", "tid": "t9", "bid": "L1", "target": "vfc",
                                                         "data": {"result": 1, "msg": "降落中不能结束航线"}}, **live))
    body, when = card_text(window, "vfc")
    assert "nav_status" in body and when != "未观测"
    assert window.uav.current_task() == "L1"
    window.status.telemetry.update("uav")
    assert window.status.telemetry.tiles["z"][1].text() == "1.20 m"
    window.uav.render()
    window.uav._render_timeline()
    row = next(r for r in window.uav.rows if r["action"] == "stop_nav")
    from dog_debug.uav import outcome
    assert outcome(row) == ("拒绝：降落中不能结束航线", 1)
    opened = []
    window.open_request = lambda first, events: opened.append(events)
    window.uav._open_row(window.uav.rows.index(row), 0)
    assert all(e.topic.startswith("tta/uav/") for e in opened[0])
    # 狗的状态不混进无人机的云端消息
    window.store.flush()
    assert not window.store.query(kind="状态", live=True, device="dog")
    assert window.store.query(kind="状态", live=True, device="uav")



def test_record_route_and_load_map_wait(window):
    """第六轮录制航线：无图 data {}；有图 map_id 是一项的数组，默认用加载成功的那张；加载地图 600 s 内算加载中；新 bid"""
    client = window.mqtt.client
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(True)
    sent = lambda: json.loads(client.sent[-1][1])
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    window.uav._start_record(False)
    assert sent()["method"] == "start_record" and sent()["data"] == {} and sent()["bid"]
    count = len(client.sent)
    window.uav._start_record(True)
    assert len(client.sent) == count
    # 加载地图：发出（Broker 回显）后在等，回执 0 后记成已加载
    window._ingest(Event("local/uav/vfc/command", {"method": "loc_load_map", "tid": "L1", "bid": "b", "data": {"map_id": "M7"}}, **live))
    assert any("加载地图 M7 中" in line for line in window.uav._loading_lines())
    window.uav.render()
    window.uav._render_timeline()
    from dog_debug.uav import outcome
    row = next(r for r in window.uav.rows if r["action"] == "loc_load_map")
    assert window.uav.table.item(window.uav.rows.index(row), 5).text().startswith("加载中")
    window._ingest(Event("local/uav/vfc/ack", {"method": "loc_load_map", "tid": "L1", "result": 0, "msg": "地图 M7 已加载，定位就绪"}, **live))
    assert window.uav.loaded() == "M7" and not window.uav.loading
    window.uav._start_record(True)
    assert sent()["data"] == {"map_id": ["M7"]}
    window.control._open_command("start_record")
    assert json.loads(window.control.editor.toPlainText())["data"] == {"map_id": ["M7"]}
    window.uav._stop_record()
    assert sent()["method"] == "stop_record" and sent()["data"] == {}


def test_fly_state_card_and_osd_without_flight_state(window):
    """第六轮 flyState：VFC 卡片一行、关键遥测三格；字段没有显示“—”（AUTO.LAND 只有 mode）；云端 method+data 也认；OSD 不再看 flightState"""
    from dog_debug.uav import domain, reading
    window.device_box.setCurrentText("无人机")
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    fly = Event("local/uav/vfc/state", {"type": "flyState", "info": {"sn": "", "flightState": 1, "ctrlMode": 3, "mode": "OFFBOARD"}}, **live)
    assert domain(fly) == "flyState" and reading(fly) == ("飞行中", "控制模式 offboard", 0, "飞控模式 OFFBOARD · 已飞 —")
    land = Event("local/uav/vfc/state", {"type": "flyState", "info": {"flightState": 1, "mode": "AUTO.LAND"}})
    assert reading(land)[1] == "控制模式 —"
    cloud = Event(f"tta/uav/{SN}/state", {"method": "flyState", "data": {"flightState": 0, "ctrlMode": 2}})
    assert reading(cloud)[:2] == ("未飞行", "控制模式 定点")
    window._ingest(fly)
    window._ingest(Event("local/uav/vfc/osd", {"data": {"z": 1.0, "flightState": 0}}, **live))
    window.status.telemetry.update("uav")
    tiles = {k: v.text() for k, (t, v) in window.status.telemetry.tiles.items()}
    assert tiles["flying"] == "飞行中" and tiles["ctrlMode"] == "offboard" and tiles["mode"] == "OFFBOARD"
    assert "飞行中" not in reading(Event("local/uav/vfc/osd", {"data": {"flightState": 1, "z": 1}}))[3]
    body = card_text(window, "vfc")[0]
    assert "flyState" in body



def test_vfc_rejection_noted_as_is(window):
    """门槛提示照实显示：VFC 拒绝的回执原文进关键变化（醒目）"""
    window.device_box.setCurrentText("无人机")
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    msg = "上一个任务的环境还没停：录制环境请先发 stop_record"
    window._ingest(Event("local/uav/vfc/ack", {"method": "start_multi_nav_by_points", "tid": "r1", "result": 1, "msg": msg}, **live))
    assert window.uav.log[0][1] == f"VFC 拒绝 start_multi_nav_by_points：{msg}" and window.uav.log[0][2]


def test_load_map_read_only_keeps_gate_notice(window, monkeypatch):
    """只读时点“加载地图”：出口拦下，只留拦截提示，不再接着报“已发出”（GPT e71ea30 实测）"""
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.uav_send.setChecked(False)
    window.uav.maps = (Event("local/uav/vfc/ack", {}), [{"map_id": "M1", "selected": True}])
    window.uav._render_maps()
    window.uav.map_table.setCurrentCell(0, 0)
    notes = []
    monkeypatch.setattr(window, "notify", notes.append)
    count = len(window.mqtt.client.sent)
    window.uav._select_map()
    assert len(window.mqtt.client.sent) == count and notes and not any(n.startswith("已发出") for n in notes)



def test_round7_fly_time_actions_and_auto_takeoff(window):
    """第七轮：flyState 的 flyingTime 显示“已飞 mm:ss”（地面 0、没有是“—”）；航点动作进度带状态和用时，停留按秒走、
    只有状态变了才记关键变化；自动起飞 Initializing 的 msg 照实显示，被人工打断醒目"""
    from dog_debug.uav import reading
    window.device_box.setCurrentText("无人机")
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    fly = Event("local/uav/vfc/state", {"type": "flyState", "info": {"flightState": 1, "ctrlMode": 3, "mode": "OFFBOARD", "flyingTime": 75}}, **live)
    assert reading(fly)[3] == "飞控模式 OFFBOARD · 已飞 01:15"
    ground = Event(f"tta/uav/{SN}/state", {"method": "flyState", "data": {"flightState": 0, "flyingTime": 0}})
    assert reading(ground)[3].endswith("已飞 00:00")
    window._ingest(fly)
    window.status.telemetry.update("uav")
    assert window.status.telemetry.tiles["flyTime"][1].text() == "01:15"
    # 自动起飞：全程 Initializing，按 msg 显示；被人工打断醒目
    state = lambda msg: Event("local/uav/vfc/state", {"type": "nav_status", "info": {"result": 0, "msg": msg, "data": "Initializing",
                                                                                     "flight_id": "F7"}}, **live)
    for msg in ("等待控制桥就绪后自动起飞", "自动解锁中", "已解锁，正在切 OFFBOARD", "自动起飞中"):
        window._ingest(state(msg))
    window._ingest(state("自动起飞已被人工操作打断，等待飞手接管"))
    texts = [(text, loud) for _, text, loud in window.uav.log]
    assert ("VFC 任务 Initializing：自动解锁中", False) in texts and ("VFC 任务 Initializing：自动起飞已被人工操作打断，等待飞手接管", True) in texts
    window.uav._render_task()
    assert "看 msg 走到哪一步" in window.uav.task_line.text()
    # 航点动作：停留每秒一次，状态没变不刷关键变化；载荷 skipped
    progress = lambda elapsed, state, photo="pending": Event("local/uav/vfc/state", {"type": "nav_action_progress", "info": {
        "flight_id": "F7", "result": 0, "msg": "", "data": {"point_index": 0, "actions": [
            {"action": "pause", "param": [5], "state": state, "elapsed_ms": elapsed},
            {"action": "take_photo", "param": [], "state": photo, "elapsed_ms": 0}]}}}, **live)
    before = len(window.uav.log)
    for elapsed in (1000, 2000, 3000):
        window._ingest(progress(elapsed, "running"))
    assert len(window.uav.log) == before + 1
    assert reading(progress(3000, "running"))[:1] == ("第 1 个航点",) and "停留 3/5 s 进行中" in reading(progress(3000, "running"))[3]
    window._ingest(progress(5000, "done", "skipped"))
    assert len(window.uav.log) == before + 2 and "take_photo 跳过" in window.uav.log[0][1]
    window.uav._render_task()
    assert "停留 5/5 s 完成" in window.uav.task_line.text()


def test_cloud_online_belongs_to_gw():
    """GW 往云端报的上线：算 GW 的状态（GW 卡片里能看到），method 记 online，不记到 VFC 名下"""
    from dog_debug.core import infer_module
    online = Event("tta/uav/status/online", {"sn": SN, "type": "uav", "data": [], "ts": 1})
    assert online.kind == "状态" and online.method == "online" and infer_module(online.topic, online.payload) == "gw"


def test_cloud_warns_when_gw_not_on_this_broker(window, monkeypatch):
    """模拟云端连上 Broker 过了 GW_WAIT 秒还没收到本机 SN 的上线和心跳：明确提示 GW 没连到这个 Broker（GatewayOnlineInfo.cfg），
    别的 SN 在上线时提醒 SN 可能填错；收到后提示撤掉、连接状态恢复"""
    import time as clock
    from dog_debug.pages import uav as page
    cloud(window)
    notes = []
    monkeypatch.setattr(window, "notify", notes.append)
    window.mqtt.port, window.mqtt.connected_at = 1883, clock.time()
    assert window.uav.gw_missing() == ""
    window.mqtt.connected_at = clock.time() - page.GW_WAIT - 1
    live = dict(session=window.mqtt.session, source="mqtt", sn="")
    window._ingest(Event("tta/uav/status/online", {"sn": "OTHER-SN", "type": "uav"}, **live))
    text = window.uav.gw_missing()
    assert "GatewayOnlineInfo.cfg" in text and "192.168.1.230:1883" in text and "OTHER-SN" in text
    window.uav.check_gw()
    window.uav.check_gw()
    assert len([n for n in notes if "没收到 GW" in n]) == 1 and "GatewayOnlineInfo" in window.link.text()
    window.uav.render()
    assert "GatewayOnlineInfo" in window.uav.link_note.text()
    window._ingest(Event(f"tta/uav/{SN}/heartbeat", {"seq": 1, "ts": 1}, **live))
    assert window.uav.gw_missing() == ""
    window.uav.check_gw()
    assert "GatewayOnlineInfo" not in window.link.text()
