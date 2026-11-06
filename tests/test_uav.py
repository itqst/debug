"""无人机第一期：用板上真实抓包验证状态卡、保留指令告警、VFC 指令和 Bridge 命令的对应、只读；不连真实 Broker"""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from dog_debug.app import Studio
from dog_debug.core import Event
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
        self.sent = []

    def publish(self, topic, text, qos, retain):
        self.sent.append((topic, text))
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
    window.control.uav_send.setChecked(True)
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
    window.control.uav_send.setChecked(True)
    window.mqtt.client = None
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: None)
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("直连模块")
    window.connection._connect()
    assert not window.control.uav_send.isChecked() and not window.mqtt.uav_send
