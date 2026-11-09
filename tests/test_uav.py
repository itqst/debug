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


def test_uav_page_shows_cards_from_active_connection(window):
    for e in capture("idle.jsonl"):
        window._ingest(e)
    window.uav.render()
    word = lambda key: window.uav.cards[key][0].text()
    assert word("system") == "READY" and word("localization") == "STALE" and word("mapping") == "DISABLED"
    assert word("online") == "ONLINE" and word("subsystem_status") == "在线" and word("nav_status") == "StandBy"
    assert "航点共 —" in window.uav.cards["navigation"][2].text() and "最后完成编号 -1" in window.uav.cards["navigation"][2].text()
    # 断开后卡片不再当作实时
    window.mqtt.connected = False
    window.uav.render()
    assert word("system") == "已断开"


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
    assert window.uav.cards["navigation"][0].text() == "5 · 到点，等待放行"


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
    assert rows() == ["控制调试", "航线任务", "消息追踪", "状态总览", "日志排障"]
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
    """板上 ROS 没起、VFC 没跑时（11-09 板上实际情况）：在线卡不写“等实时上报”，没收到的卡写可能原因"""
    import time
    now = time.time()
    window._ingest(Event("local/uav/bridge/heartbeat", {"sn": "", "type": "online", "state": 1,
                         "info": {"result": 0, "msg": "机载服务在线", "data": "ONLINE"}}, sn="", session="s1", retained=True, timestamp=now - 10))
    window._ingest(Event("local/uav/bridge/state/system", {"sn": "", "type": "system", "state": 1, "info": {"result": 0, "msg": "环境空闲，可以启动",
                         "data": "READY", "detail": {"ros": "DOWN"}}}, sn="", session="s1", timestamp=now))
    window.uav.render()
    word, msg, _, age = window.uav.cards["online"]
    assert word.text() == "ONLINE" and "等实时上报" not in age.text() and "OFFLINE" in age.text()
    assert window.uav.cards["pose"][1].text() == "Bridge 报 ros DOWN，没有位姿可发"
    assert "VFC 服务可能没在跑" in window.uav.cards["subsystem_status"][1].text()


def test_card_click_opens_detail_with_fields_changes_and_related_commands(window):
    """和狗的状态总览一样点卡片看详情：最新一条完整字段、变了的标橙、这次连接里的变化、相关指令点开看整条请求"""
    for e in capture("commands.jsonl"):
        window._ingest(e)
    window.store.flush()
    window.uav.render()
    window.uav.frames["system"].clicked.emit()
    assert not window.uav.detail_card.isHidden() and window.uav.detail_title.text() == "任务环境 详情"
    texts = [window.uav.detail_box.itemAt(i).widget().text() for i in range(window.uav.detail_box.count())
             if window.uav.detail_box.itemAt(i).widget()]
    assert "info.data" in texts[0] and "local/uav/bridge/state/system" in window.uav.detail_topic.text()
    assert any(t.startswith("这次连接里的变化") for t in texts)
    window.uav.detail_view.set_index(1)
    rows = window.uav.detail_model.rows
    assert rows and all(e.method.startswith(("system.", "job.")) for e, *_ in rows)
    window.uav._detail_open(window.uav.detail_model.index(0, 0))
    assert window.uav.detail_pages.currentIndex() == 2 and window.uav.detail_trace.selected is rows[0][0]
    # VFC 卡片列的是 VFC 指令
    window.uav.frames["nav_status"].clicked.emit()
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


def test_related_commands_not_pushed_out_by_other_domains(window):
    """导航卡的相关指令在 SQL 里先按动作前缀筛：之后来 610 条 system.status 也挤不掉那条 navigation.pause"""
    window._ingest(Event("local/uav/bridge/state/navigation", {"sn": "", "type": "navigation", "state": 4, "info": {"result": 0, "data": "ACTIVE"}},
                         sn="", session="s1"))
    window._ingest(Event("local/uav/bridge/command", {"sn": "", "id": "p1", "type": "navigation", "action": "navigation.pause", "client": "vfc"},
                         sn="", session="s1"))
    for i in range(610):
        window._ingest(Event("local/uav/bridge/command", {"sn": "", "id": f"s{i}", "type": "system", "action": "system.status", "client": "vfc"},
                             sn="", session="s1"))
    window.store.flush()
    window.uav._open_detail("navigation")
    window.uav.detail_view.set_index(1)
    assert [e.method for e, *_ in window.uav.detail_model.rows] == ["navigation.pause"]


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
