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
    """模块卡片的正文（一行一类状态）、提示和右上角时间"""
    state, when, body, hint = window.uav.cards[module]
    return body.text(), hint.text(), when.text()


def test_uav_page_shows_cards_from_active_connection(window):
    """状态总览按模块分卡片：Bridge 一张（各领域一行）、VFC 一张；GW、Vision 没接时也占位"""
    for e in capture("idle.jsonl"):
        window._ingest(e)
    window.uav.render()
    bridge, _, _ = card_text(window, "bridge")
    for line in ("任务环境</b>　<b style='color:#34C759'>READY", "定位</b>", "STALE", "建图</b>", "DISABLED", "在线</b>", "ONLINE"):
        assert line in bridge, line
    assert "航点共 —" in bridge and "最后完成编号 -1" in bridge
    vfc, _, _ = card_text(window, "vfc")
    assert "StandBy" in vfc and "在线" in vfc
    _, hint, when = card_text(window, "gw")
    assert "还没接入" in hint and when == "未观测"
    shown = window.uav.grid.cards
    assert [f for f in shown] == [window.uav.frames[m] for m in ("bridge", "vfc", "gw", "vision")]
    # 断开后卡片不再当作实时
    window.mqtt.connected = False
    window.uav.render()
    body, _, when = card_text(window, "bridge")
    assert body == "" and when == "已断开"


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
    transport._connected(client, None, None, 0, None)
    assert topics == [[("local/uav/#", 0)]]


def test_uav_needs_direct_mode(window, monkeypatch):
    window.mqtt.client = None
    called = []
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: called.append(a))
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("云端 → 真实 GW")
    window.connection._connect()
    assert called == []
    window.mode_box.setCurrentText("直连模块")
    window.connection._connect()
    assert called and called[0][-1] == "uav"


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
    window.uav.render()
    assert "5 · 到点，等待放行" in card_text(window, "bridge")[0]


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


def test_sidebar_follows_device_and_locks_while_connected(window, monkeypatch):
    """选无人机后侧栏只剩无人机能用的页，日志页不给取设备日志；连上后不能换设备，断开才放开"""
    rows = lambda: [window.nav.item(i).text() for i in range(window.nav.count())]
    assert "遥控" in rows() and not window.uav_send.isVisible()
    window.device_box.setCurrentText("无人机")
    assert rows() == ["控制调试", "航线任务", "状态总览", "日志排障"]
    assert not window.uav_send.isHidden() and window.logs.fetch_logs.isHidden() and window.logs.live_follow.isHidden()
    window._go(window.UAV_MISSION)
    assert window.stack.currentIndex() == window.UAV_MISSION
    # 切回狗，当前页狗没有，落到狗的第一页
    window.device_box.setCurrentText("机器狗")
    assert rows()[0] == "遥控" and window.stack.currentIndex() == window.REMOTE and window.uav_send.isHidden()
    assert not window.logs.fetch_logs.isHidden()
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.mqtt.client = None
    monkeypatch.setattr(window.mqtt, "connect", lambda *a, **k: None)
    window.connection._connect()
    assert not window.device_box.isEnabled()
    window.mqtt.client = object()
    monkeypatch.setattr(window.mqtt, "close", lambda: None)
    window.connection._connect()
    assert window.device_box.isEnabled()



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


def test_board_with_ros_down_and_no_vfc_explains_empty_cards(window):
    """板上 ROS 没起、VFC 没跑时（11-09 板上实际情况）：在线不标“保留快照”，没收到的写可能原因"""
    import time
    now = time.time()
    window.uav.since = now - 10
    window._ingest(Event("local/uav/bridge/heartbeat", {"sn": "", "type": "online", "state": 1,
                         "info": {"result": 0, "msg": "机载服务在线", "data": "ONLINE"}}, sn="", session="s1", retained=True, timestamp=now - 10))
    window._ingest(Event("local/uav/bridge/state/system", {"sn": "", "type": "system", "state": 1, "info": {"result": 0, "msg": "环境空闲，可以启动",
                         "data": "READY", "detail": {"ros": "DOWN"}}}, sn="", session="s1", timestamp=now))
    window.uav.since = now - 10
    window.uav.render()
    body, hint, _ = card_text(window, "bridge")
    assert "ONLINE" in body and "保留快照" not in body.split("<br>")[0]
    assert "Bridge 报 ros DOWN，没有位姿可发" in hint
    assert "VFC 服务可能没在跑" in card_text(window, "vfc")[1]


def test_card_click_opens_detail_with_fields_changes_and_related_commands(window):
    """和狗的状态总览一样点模块卡片看详情：各类状态最新一条完整字段、变了的标橙、这次连接里的变化、相关指令点开看整条请求"""
    for e in capture("commands.jsonl"):
        window._ingest(e)
    window.store.flush()
    window.uav.render()
    window.uav.frames["bridge"].clicked.emit()
    assert not window.uav.detail_card.isHidden() and window.uav.detail_title.text() == "Bridge 机载服务 详情"
    texts = [window.uav.detail_box.itemAt(i).widget().text() for i in range(window.uav.detail_box.count())
             if window.uav.detail_box.itemAt(i).widget()]
    assert any(t.startswith("<b>任务环境</b>") and "info.data" in t for t in texts)
    assert "local/uav/bridge/state/system" in window.uav.detail_topic.text()
    assert any(t.startswith("这次连接里的变化") for t in texts)
    window.uav.detail_view.set_index(1)
    rows = window.uav.detail_model.rows
    assert rows and all(e.module == "bridge" for e, *_ in rows)
    window.uav._detail_open(window.uav.detail_model.index(0, 0))
    assert window.uav.detail_pages.currentIndex() == 2 and window.uav.detail_trace.selected is rows[0][0]
    # VFC 卡片列的是 VFC 指令
    window.uav.frames["vfc"].clicked.emit()
    assert window.uav.detail_model.rows and all(e.module == "vfc" for e, *_ in window.uav.detail_model.rows)
    window.uav.detail_view.set_index(0)
    window.uav._open_detail(None)
    assert window.uav.detail_card.isHidden()


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
    assert window.nav.item(0).text() == "控制调试"
    window.mqtt.client = None
    window.connection._profile_chosen(index)
    assert window.device_box.currentText() == "机器狗" and window.host.text() == "10.0.0.2"


def test_related_commands_not_pushed_out_by_other_modules(window):
    """VFC 详情的相关指令在 SQL 里按模块筛：之后来 610 条 Bridge 命令也挤不掉那条 VFC 指令"""
    window._ingest(Event("local/uav/vfc/command", {"method": "pause_nav", "tid": "p1", "bid": "b", "data": {}}, sn="", session="s1"))
    for i in range(610):
        window._ingest(Event("local/uav/bridge/command", {"sn": "", "id": f"s{i}", "type": "system", "action": "system.status", "client": "vfc"},
                             sn="", session="s1"))
    window.store.flush()
    window.uav._open_detail("vfc")
    window.uav.detail_view.set_index(1)
    assert [e.method for e, *_ in window.uav.detail_model.rows] == ["pause_nav"]


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
    for method in ("start_nav_return_home", "land"):
        window.control._open_command(method)
        window.control._send()
        assert json.loads(client.sent[-1][1])["bid"] == "11111"


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
    for method in ("land", "system.stop"):
        window.control._open_command(method)
        assert button.isHidden()
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
    assert set(options) == {"保存地图", "不保存"} and not any(o.isChecked() for o in options.values())
    options["不保存"].setChecked(True)
    window.control._send()
    sent = json.loads(client.sent[-1][1])
    assert sent["method"] == "stop_mapping" and sent["data"] == {"save": False} and sent["bid"] == "MAP-1"
    # 航线任务页：开始建图新 bid，停止两个按钮各自带明确的 save
    window.uav._start_mapping()
    assert json.loads(client.sent[-1][1])["bid"] != "MAP-1"
    window.uav._stop_mapping(True)
    sent = json.loads(client.sent[-1][1])
    assert sent["data"] == {"save": True} and sent["bid"] == "MAP-1"


def test_mapping_status_card_shows_data_msg_session(window):
    """mapping_status（VFC 1.0.0）：data 是协议词、raw 是 Bridge 原词、result 是协议码；卡片显示词、说明、原词和 session_id；
    保存结果醒目记一条；VFC 自报 Unknown 的原因看说明；session 能复制"""
    window._ingest(mapping_state("Passive", "当前不在建图", raw="DISABLED", result=2))
    window._ingest(mapping_state("MappingRunning", "正在建图", "20261109_140005", raw="MAPPING", result=5))
    window._ingest(mapping_state("MappingSaveBegin", "正在保存地图", "20261109_140005", raw="SAVING", result=7))
    window._ingest(mapping_state("MappingSaveEnd", "地图已保存", "20261109_140005", raw="SAVED", result=8))
    window.uav.render()
    body = card_text(window, "vfc")[0]
    assert "MappingSaveEnd" in body and "地图已保存" in body and "Bridge SAVED" in body and "session 20261109_140005" in body
    assert any(loud and text == "VFC 建图 MappingSaveEnd：地图已保存" for _, text, loud in window.uav.log)
    assert window.uav.mapping_session() == "20261109_140005" and "MappingSaveEnd" in window.uav.mapping_line.text()
    from dog_debug.uav import quality
    assert quality(mapping_state("MappError", "保存失败", raw="SAVE_FAILED", result=6)) == "失败"
    assert quality(mapping_state("MappingRunning", "正在建图", result=5)) == "正常"
    window._ingest(mapping_state("Unknown", "Bridge 建图状态过期", result=1))
    window.uav.render()
    assert "Bridge 建图状态过期" in card_text(window, "vfc")[0]


def test_vfc_osd_card_shows_raw_values_and_age(window):
    """VFC OSD（local/uav/vfc/osd）：缺的键显示“—”不当 0，原值显示；写明最近一次收到的时间和距今秒数；不驱动狗的 3D"""
    import time
    window.uav.render()
    window._ingest(Event("local/uav/vfc/state", {"name": "vfc", "type": "nav_status", "info": {"result": 0, "msg": "", "data": "StandBy"}},
                         sn="", session="s1"))
    window.uav.render()
    assert "收不到是正常的" in card_text(window, "vfc")[1]
    at = time.time() - 4
    window._ingest(Event("local/uav/vfc/osd", {"sn": "", "ts": 1793760000000, "data": {"sn": "", "x": 0.42, "y": 3.1, "z": 1.2, "speed": 0.98,
                                                                                  "heading": 81, "ts": 1793760000000}},
                         sn="", session="s1", timestamp=at))
    window.uav.render()
    body = card_text(window, "vfc")[0]
    assert "电量 —" in body and "x 0.42　y 3.1　z 1.2（米）" in body and "速度 0.98 m/s · 航向 81°" in body
    # 4 秒没新的就标橙（每 1 s 一条），时间和秒数照样写
    assert time.strftime("%H:%M:%S", time.localtime(at)) + " 收到 · 4 秒前" in body
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
    rows = window.store.query(kind="状态", sn="TEST_DOG_001", live=True, dog_only=True)
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
    transport._message(None, None, message)
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
    window._ingest(vfc_state("loc_status", {"result": 2, "msg": "定位未启动", "data": "Init", "raw": {"localization": "STALE", "relocalization": "STALE"}}))
    window._ingest(vfc_state("loc_status", {"result": 1, "msg": "定位丢失", "data": "LocLost", "raw": {"localization": "LOST", "relocalization": "LOST"}}))
    window._ingest(vfc_state("map_status", {"result": 0, "msg": "", "data": "IDLE", "map_busy": False}))
    window._ingest(vfc_state("map_status", {"result": 0, "msg": "地图资产已就绪", "data": "MAP_ASSETS_READY", "map_id": "0000009_20261111_082057",
                                            "auto": True, "map_busy": False}))
    window._ingest(vfc_state("nav_action_progress", {"result": 1, "msg": "本期不支持航点动作，已跳过", "flight_id": "T1",
                                                     "data": {"point_index": 0, "actions": [{"action": "take_photo", "state": "skipped"}]}}))
    window.uav.render()
    body = card_text(window, "vfc")[0]
    assert "LocLost" in body and "Bridge 定位 LOST · 重定位 LOST" in body
    assert "MAP_ASSETS_READY" in body and "地图 0000009_20261111_082057" in body and "自动建资产" in body
    assert "第 0 点" in body and "take_photo skipped" in body
    loud = {text for _, text, flag in window.uav.log if flag}
    assert "VFC 定位 Init → LocLost：定位丢失" in loud and "VFC 地图 MAP_ASSETS_READY：地图资产已就绪" in loud
    from dog_debug.uav import quality
    assert quality(window.uav.latest["nav_action_progress"]) == "未知"


def test_vfc_alarms_raise_and_clear(window):
    """VFC 告警（local/uav/vfc/events，type=alarm）：进行中的在前、已解除的灰；VFC 卡片写进行中几条；关键变化醒目"""
    alarm = lambda code, state, title, **more: Event("local/uav/vfc/events", {"name": "vfc", "state": 1, "type": "alarm",
        "info": {"code": code, "level": "warning", "state": state, "title": title, "advice": "检查定位", **more}}, sn="", session="s1")
    window._ingest(alarm("ALM-3101", "raise", "定位丢失", x=1.0, y=2.0, z=1.2))
    window._ingest(alarm("ALM-2207", "raise", "避障触发"))
    window._ingest(alarm("ALM-2207", "clear", "避障触发"))
    window.uav.render()
    assert window.uav.alarm_count.text() == "1 条进行中 · 已解除 1 条"
    text = window.uav.alarm_text.text()
    assert text.index("ALM-3101") < text.index("ALM-2207") and "进行中" in text and "已解除" in text and "位置 x 1.0 y 2.0 z 1.2" in text
    assert "告警</b>" in card_text(window, "vfc")[0] and "1 条进行中" in card_text(window, "vfc")[0]
    assert any(loud and t == "告警解除 ALM-2207 避障触发" for _, t, loud in window.uav.log)


def test_other_uav_modules_get_their_own_card(window):
    """无人机上的其他模块（如 FSVR）收到状态后出现自己的卡片，按通用格式看；GW、Vision 没收到也占位"""
    window._ingest(Event("local/uav/fsvr/state", {"data": {"info": [], "name": "fsvr", "state": 1, "version": "1.0.0"}, "sn": "",
                                                  "type": "subsystem_state"}, sn="", session="s1"))
    window._ingest(Event("local/uav/fsvr/state", {"method": "storage_status", "data": {"upload_remain": 0, "total": 100, "used": 3}},
                         sn="", session="s1"))
    window.uav.render()
    assert window.uav.frames["fsvr"] in window.uav.grid.cards and window.uav.frames["ptz"] not in window.uav.grid.cards
    body = card_text(window, "fsvr")[0]
    assert body.index("在线</b>") < body.index("storage_status") and "v1.0.0" in body and "used=3" in body
    assert body.count("storage_status") == 1
    window.uav._open_detail("fsvr")
    assert window.uav.detail_title.text() == "FSVR 文件 详情" and "local/uav/fsvr/state" in window.uav.detail_topic.text()


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
    # 航线用这张图：已知地图、map_id 填上，发出时顶层带 profile
    window.uav._route_map()
    window.uav._send_route()
    route = sent()
    assert route["method"] == "start_multi_nav_by_points" and route["profile"] == "known_navigation" and route["data"]["map_id"] == "M10"
    window.uav.route.profile.set_index(1)
    window.uav._send_route()
    assert sent()["profile"] == "online_navigation" and "map_id" not in sent()["data"]
