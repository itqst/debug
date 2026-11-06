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
    assert (ack.kind, ack.method, ack.tid) == ("ACK", "system.start", "vfc-1-2")
    assert (state.kind, state.method) == ("状态", "navigation")


def test_vfc_commands_linked_to_bridge_commands_from_board_capture():
    rows = {row["key"][2]: row for row in link(capture("commands.jsonl"))}
    children = {row["parent"][2]: row for row in rows.values() if row["parent"]}
    # 中间插了一条未知指令（bt-005），system.start 仍配到发出航线的 bt-004
    assert children["bt-004"]["action"] == "system.start" and children["bt-004"]["confirmed"]
    assert children["bt-006"]["key"][2] == "vfc-1793736584573-3" and children["bt-006"]["confirmed"]
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
