"""在 Qt 中验证真实控件操作与故障面板，不访问机器狗"""

import json
import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import urllib.request

import pytest
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QWidget
import dog_debug.pages.diagnostics as diagnostics_module
import dog_debug.pages.status as status_module
from dog_debug.app import Studio
from dog_debug.registry import MODULES
from dog_debug.registry import find, make_command
from dog_debug.core import Event


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    win = Studio(":memory:")
    yield win
    win.close()
    app.processEvents()


def select(window, method):
    window.control._open_command(method)
    assert window.control.selected["method"] == method


def test_demo_opens_actionable_trace(window):
    """演示故障定位单独开窗口看整条请求，不再跳到消息追踪"""
    from dog_debug.trace_view import TraceView
    before = window.stack.currentIndex()
    window.control._demo()
    views = [v for v in window.findChildren(TraceView) if v.window() is not window and v.window().isVisible()]
    text = views[-1].text()
    assert "MEDIA 返回失败" in text and "相机连接超时" in text
    assert "下一步" in text and "sandbox" in text
    assert window.stack.currentIndex() == before


def test_messages_page_removed(window):
    rows = [window.nav.item(i).text() for i in range(window.nav.count())]
    assert rows == ["总览与遥测", "遥控与载荷", "指令与链路", "任务与文件", "日志与配置"] and not hasattr(window, "messages")


def test_recent_commands_kept_per_device(window):
    """最近发过的按设备分开：切到无人机看不到狗的，切回来狗的还在"""
    select(window, "take_photo")
    window.control._remember(window.control.selected)
    window.device_box.setCurrentText("无人机")
    assert window.control.recent == []
    window.device_box.setCurrentText("机器狗")
    assert [r["method"] for r in window.control.recent] == ["take_photo"]


def test_all_control_modules_have_templates(window):
    modules = {window.control.commands.item(i).data(0x0100)["module"]
               for i in range(window.control.commands.count()) if window.control.commands.item(i).data(0x0100)}
    assert modules == {"gw", "vfc", "ptz", "media", "vision", "fsvr", "ros2bridge"}


def test_send_shows_result_on_control_page(window):
    select(window, "take_photo")
    window.control.scenario.set_index(1)
    window.control._send()
    assert window.stack.currentIndex() == window.CONTROL
    assert "相机连接超时" in window.control.result.text()


def test_motion_sends_without_gate(window):
    select(window, "move")
    window.control._send()
    assert "move" in window.control.result.text()


def test_direct_mode_uses_local_topic_and_reports_offline_broker(window):
    select(window, "take_photo")
    window.mode_box.setCurrentText("直连模块")
    assert window.control.topic.text() == "local/robotdog/media/command"
    window.control._send()
    assert window.toast.text() == "MQTT 未连接，不排队旧命令"


def test_builtin_suite(window):
    window.suite._run_suite()
    result = window.suite.suite_info.toPlainText()
    assert "FAIL" not in result
    assert result.count("PASS") == 9


def test_case_without_assertion_is_not_pass(window, tmp_path, monkeypatch):
    path = tmp_path / "case.yaml"
    path.write_text("payload:\n  method: take_photo\n  data: {}\nscenario: no_ack\n", encoding="utf-8")
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getOpenFileNames", lambda *a, **k: ([str(path)], ""))
    window.suite._load_case()
    result = window.suite.suite_info.toPlainText()
    assert "PASS" not in result and "仅执行" in result


@pytest.mark.parametrize("text", ["[]", "{not json"])
def test_non_object_payload_is_recorded_not_crash(window, text):
    window.control.editor.setPlainText(text)
    window.control._send()
    assert "无法按对象模拟" in window.toast.text()
    assert window.control.watch.raw == text


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


def test_send_records_active_connection_not_edited_sn(window):
    select(window, "take_photo")
    window.mode_box.setCurrentText("云端 → 真实 GW")
    client = StubClient()
    window.mqtt.client, window.mqtt.connected = client, True
    window.mqtt.host, window.mqtt.sn, window.mqtt.session = "127.0.0.1", "A", "live"
    window.sn.setText("B")
    assert "重连后生效" in window.link.text()
    window.control._send()
    assert client.sent[0][0] == "tta/robotdog/B/services"
    assert window.control.watch.sn == "A" and window.control.watch.session == "live"


def test_raw_text_published_as_is(window):
    window.mode_box.setCurrentText("直连模块")
    client = StubClient()
    window.mqtt.client, window.mqtt.connected, window.mqtt.sn = client, True, "A"
    window.control.editor.setPlainText("{broken")
    window.control._send()
    assert client.sent[0][1] == "{broken"


@pytest.fixture
def panel(window):
    from dog_debug.pages.teleop import RemotePanel
    sent = []
    result = {"ok": True}

    def send(method, data):
        sent.append((method, dict(data)))
        return result["ok"]

    widget = RemotePanel(send)
    widget.sent, widget.result = sent, result
    widget.show()
    yield widget
    widget.motion_timer.stop()
    widget.gimbal_timer.stop()


def test_hold_moves_then_release_sends_zero(panel):
    panel.press("motion", "fwd")
    assert panel.sent[-1][0] == "move" and panel.sent[-1][1]["vx"] > 0
    assert panel.motion_timer.isActive()
    panel.release("motion", "fwd")
    assert not panel.motion_timer.isActive()
    assert panel.sent[-1] == ("move", {"vx": 0, "vy": 0, "yaw_rate": 0})


def test_combined_keys_and_hide_halts(panel):
    panel.press("motion", "fwd")
    panel.press("motion", "turn_l")
    assert panel.sent[-1][1]["vx"] > 0 and panel.sent[-1][1]["yaw_rate"] > 0
    panel.hide()
    assert panel.sent[-1] == ("move", {"vx": 0, "vy": 0, "yaw_rate": 0})
    assert not panel.motion and not panel.motion_timer.isActive()


def test_send_failure_stops_without_extra_stop(panel):
    panel.result["ok"] = False
    panel.press("gimbal", "up")
    count = len(panel.sent)
    assert not panel.gimbal_timer.isActive() and not panel.gimbal
    panel.halt()
    assert len(panel.sent) == count


def test_gimbal_release_recenters(panel):
    panel.press("gimbal", "left")
    assert panel.sent[-1][1]["vy"] < 1500
    panel.release("gimbal", "left")
    assert panel.sent[-1][1]["vx"] == 1500 and panel.sent[-1][1]["vy"] == 1500


def test_quick_action_reports_ack(window):
    window._go(window.REMOTE)
    assert window._quick_send("stand_up", {})
    text = window.quick_views[0][2].text()
    assert "站立" in text and "stand_up" in text and "操作成功" in text


def test_one_x_uses_panel_camera_and_zoom_one(panel):
    from PySide6.QtWidgets import QPushButton
    next(b for b in panel.findChildren(QPushButton) if b.text() == "1×").click()
    assert panel.sent[-1] == ("camera_focal_length_set", {"index": 1, "sn": "002", "type": 4})


def test_halt_stops_zoom_on_original_camera(panel):
    panel.zoom(1)
    panel.camera_sn.setText("other")
    panel.halt()
    assert panel.sent[-1] == ("camera_focal_length_set", {"index": 1, "sn": "002", "type": 3})
    panel.halt()
    assert len([m for m in panel.sent if m[1].get("type") == 3]) == 1


def live_window(mode="直连模块"):
    window = Studio(":memory:")
    window.mode_box.setCurrentText(mode)
    client = StubClient()
    window.mqtt.client, window.mqtt.connected, window.mqtt.sn = client, True, "A"
    return window, client


def last_move(client):
    import json
    return json.loads([text for topic, text in client.sent if '"move"' in text][-1])["data"]


def test_close_window_sends_zero_before_disconnect():
    window, client = live_window()
    window.teleop.remote.press("motion", "fwd")
    assert last_move(client)["vx"] > 0
    window.close()
    assert last_move(client) == {"vx": 0, "vy": 0, "yaw_rate": 0}


def test_manual_disconnect_sends_zero_first(window):
    window.mode_box.setCurrentText("直连模块")
    client = StubClient()
    window.mqtt.client, window.mqtt.connected, window.mqtt.sn = client, True, "A"
    window.teleop.remote.press("motion", "fwd")
    window.connection._connect()
    assert last_move(client) == {"vx": 0, "vy": 0, "yaw_rate": 0}
    assert window.mqtt.client is None


def test_lost_connection_stops_locally_without_claiming_zero(window):
    window.mode_box.setCurrentText("直连模块")
    client = StubClient()
    window.mqtt.client, window.mqtt.connected, window.mqtt.sn = client, True, "A"
    window.teleop.remote.press("motion", "fwd")
    window.mqtt.connected = False
    window.inbox.put(("disconnect", "已断开 7"))
    window._drain()
    assert not window.teleop.remote.motion_timer.isActive()
    assert last_move(client)["vx"] > 0
    zero = [e for _, e in window.store.query(method="move") if e.payload.get("data", {}).get("vx") == 0]
    assert not zero


def test_map_list_ack_fills_combo_and_loads(window):
    window.mission.missions.send("get_map_list", {})
    assert window.mission.missions.map_box.count() == 2
    window.mission.missions.map_box.setCurrentIndex(1)
    window.mission.missions.send("loc_load_map", {"map_id": window.mission.missions.map_id()})
    assert window.quick.payload["data"]["map_id"] == "2026_07_28_14_13_53"


def test_single_nav_heading_becomes_quaternion():
    from dog_debug.pages.mission import pose
    goal = pose(1.0, 2.0, 90)
    assert goal["position"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert abs(goal["orientation"]["z"] - 0.707107) < 1e-6 and abs(goal["orientation"]["w"] - 0.707107) < 1e-6


def test_multi_nav_points_and_actions(window):
    m = window.mission.missions
    m.map_box.setEditText("map_a")
    m.add_point(3, 4, 0, 4, "10")
    data = m.multi_nav()
    assert data["map_id"] == "map_a" and len(data["points"]) == 2
    assert data["points"][0]["actions"] == []
    assert data["points"][1]["actions"] == [{"id": "", "action": "pause", "param": [10]}]


def test_media_list_fill_and_sync_selected(window):
    m = window.mission.missions
    m.send("get_media_filelist", {})
    assert m.files.count() == 2
    m.files.item(1).setSelected(True)
    m.upload_path.setText("http://upload.invalid/x")
    next(b for b in m.findChildren(type(window.control.send_btn)) if b.text() == "同步所选媒体").click()
    assert window.quick.payload["data"] == {"file_path": ["2026-10-28/VID_0002.mp4"], "upload_path": "http://upload.invalid/x"}


def test_ir_ack_shows_temperature(window):
    m = window.mission.missions
    m.send("ir_metering_point_set", {**m.camera(), "x": 0.5, "y": 0.5})
    assert "36.5 ℃" in m.temperature.text()
    m.send("ir_metering_area_set", {**m.camera(), "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8})
    assert "平均 35.2 ℃" in m.temperature.text()


def test_edit_payload_opens_control_page(window):
    window.mission._edit_payload("start_multi_nav_by_points", window.mission.missions.multi_nav())
    assert window.stack.currentIndex() == window.CONTROL
    assert '"task_name"' in window.control.editor.toPlainText()


def test_map_origin_yaw_rotates_projection(window, tmp_path):
    m = window.mission.missions
    m.open_map(make_map(tmp_path, "image: map_a.pgm\nresolution: 0.1\norigin: [-2.0, -3.0, 1.5707963267948966]\n"))
    view = m.map_view
    assert view.to_world(50, 40) == (-6.0, 2.0)
    u, v = view.to_pixel(-6.0, 2.0)
    assert abs(u - 50) < 1e-3 and abs(v - 40) < 1e-3
    view.set_robot(0, 0, math.pi / 2)
    assert abs(view.robot.rotation()) < 1e-3


def make_map(tmp_path, yaml_text=None):
    from PySide6.QtGui import QImage
    image = QImage(100, 80, QImage.Format_Grayscale8)
    image.fill(254)
    path = tmp_path / "map_a.pgm"
    assert image.save(str(path), "PGM")
    if yaml_text:
        (tmp_path / "map_a.yaml").write_text(yaml_text, encoding="utf-8")
    return str(path)


def test_map_yaml_sets_projection_and_click_adds_waypoint(window, tmp_path):
    m = window.mission.missions
    m.open_map(make_map(tmp_path, "image: map_a.pgm\nresolution: 0.1\norigin: [-2.0, -3.0, 0.0]\n"))
    assert m.resolution.value() == 0.1 and m.origin[0].value() == -2.0
    view = m.map_view
    assert view.to_world(0, 80) == (-2.0, -3.0) and view.to_world(50, 40) == (3.0, 1.0)
    before = m.points.rowCount()
    view.picked.emit(*view.to_world(50, 40))
    assert m.points.rowCount() == before + 1
    assert [m.points.cellWidget(before, c).value() for c in (0, 1)] == [3.0, 1.0]
    assert len([i for i in view.markers if i.flags() & i.GraphicsItemFlag.ItemIgnoresTransformations]) == m.points.rowCount()


def test_map_without_yaml_uses_map_list_info(window, tmp_path):
    m = window.mission.missions
    m.observe(Event("local/robotdog/vfc/ack", {"method": "get_all_compressed_pgm_map", "data": {"result": 0, "data": {
        "map_a": [{"info": {"resolution": 0.05, "origin": {"position": {"x": -13.65, "y": -10.45}}}}, []]}}}))
    m.open_map(make_map(tmp_path))
    assert m.resolution.value() == 0.05 and m.origin[0].value() == -13.65
    assert "地图列表应答" in m.map_hint.text()


def test_large_import_streams_batches_and_cancel_allows_restart(window, tmp_path):
    import json as _json
    import time as _time
    path = tmp_path / "big.log"
    with path.open("w", encoding="utf-8") as out:
        for i in range(12000):
            out.write(f"12:00:00 local/robotdog/media/ack " + _json.dumps({"method": "take_photo", "tid": f"t{i}", "data": {"result": 0}}) + "\n")
    window.logs.start_import([str(path)])
    window.logs.import_thread.join(10)
    for _ in range(50):
        window._drain()
    assert window.store.count() == 12000
    assert "导入完成：12000 条" in window.logs.import_info.toPlainText()
    window.logs.start_import([str(path)])
    window.import_cancel.set()
    window.logs.import_thread.join(10)
    assert not window.logs.import_thread.is_alive()
    for _ in range(50):
        window._drain()
    before = window.store.count()
    window.logs.start_import([str(path)])
    window.logs.import_thread.join(10)
    for _ in range(50):
        window._drain()
    assert window.store.count() == before + 12000


def test_export_preview_counts(window, tmp_path):
    from dog_debug.core import export_preview
    window.control._demo()
    window.store.add(Event("tta/robotdog/A/request_reply", {"method": "oss_config_get", "data": {"password": "x"}}, sn="A", session="z"))
    info = export_preview(window.store)
    assert info["events"] == window.store.count() and info["requests"] >= 1 and info["redacted"] == 1
    result = window.diagnostics.export_bundle(str(tmp_path / "b.zip"))
    assert result["events"] == info["events"] and result["listed"] == result["requests"]


def test_clear_store_after_one_confirm(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    window.control._demo()
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: QMessageBox.No)
    window.logs._clear_store()
    assert window.store.count() > 0
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", lambda *a, **k: QMessageBox.Yes)
    window.logs._clear_store()
    assert window.store.count() == 0 and "本地 0 条" in window.logs.store_size.text()


def disk_window(tmp_path, texts=("ALPHA", "BETA")):
    window = Studio(str(tmp_path / "late.sqlite3"))
    for i, text in enumerate(texts):
        window.store.add(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": f"t{i}", "data": {"result": 0, "msg": text}}, sn="A", session="s"))
    window.store.flush()
    return window


def test_export_preview_runs_off_gui_thread(tmp_path, monkeypatch):
    import threading
    import time as _time
    from PySide6.QtCore import QTimer
    import dog_debug.app as app_module
    window = disk_window(tmp_path)
    real, seen = diagnostics_module.export_preview, {}

    def probe(store):
        seen["main"] = threading.current_thread() is threading.main_thread()
        _time.sleep(0.6)
        return real(store)

    monkeypatch.setattr(diagnostics_module, "export_preview", probe)
    beat = {"last": _time.monotonic(), "gap": 0.0}

    def tick():
        now = _time.monotonic()
        beat["gap"], beat["last"] = max(beat["gap"], now - beat["last"]), now

    heart = QTimer()
    heart.timeout.connect(tick)
    heart.start(20)
    state = {}

    def check():
        dialog = window.diagnostics.export_dialog
        state["loading_first"] = state.get("loading_first", not dialog.confirm.isEnabled())
        if dialog.confirm.isEnabled():
            state["filled"] = True
            dialog.reject()
        else:
            QTimer.singleShot(100, check)

    QTimer.singleShot(50, check)
    window.diagnostics.export()
    heart.stop()
    assert seen["main"] is False and state["loading_first"] and state["filled"]
    assert beat["gap"] < 0.3
    window.close()


def test_run_cases_in_order_with_evidence(window, tmp_path):
    ok = tmp_path / "a_ok.yaml"
    ok.write_text("name: 拍照成功\npayload:\n  method: take_photo\n  data: {}\nscenario: success\nexpect:\n  ack_status: 操作成功\n", encoding="utf-8")
    bad = tmp_path / "b_bad.yaml"
    bad.write_text("name: 期望错误\npayload:\n  method: take_photo\n  data: {}\nscenario: failure\nexpect:\n  ack_status: 操作成功\n", encoding="utf-8")
    broken = tmp_path / "c_broken.yaml"
    broken.write_text("{not: [valid", encoding="utf-8")
    window.suite.run_cases([str(ok), str(bad), str(broken)])
    text = window.suite.suite_info.toPlainText()
    assert "3 条用例：PASS 1，FAIL 2" in text and "仅执行" not in text
    assert text.index("拍照成功") < text.index("期望错误") < text.index("c_broken")
    assert "期望 操作成功，实际 失败" in text and "用例无法执行" in text


def test_status_card_separates_expired_and_version(window):
    window.sn.setText("A")
    import time as _time
    window.store.add(Event("local/robotdog/vfc/heartbeat", {"data": {"version": "3.2.1"}}, sn="A", session="s",
                           timestamp=_time.time() - 60))
    window.store.add(Event("local/robotdog/fsvr/state", {"type": "subsystem_state", "data": {"name": "fsvr", "version": "1.0.0", "state": 1}},
                           sn="A", session="s"))
    window.store.flush()
    window._go(window.STATUS)
    assert "v3.2.1" in window.status.module_cards["vfc"][1].text() and "已过期" in window.status.module_cards["vfc"][1].text()
    assert "v1.0.0" in window.status.module_cards["fsvr"][1].text() and "已过期" not in window.status.module_cards["fsvr"][1].text()
    assert window.status.module_cards["ptz"][1].text() == "未观测"


def test_bad_case_does_not_stop_following_cases(window, tmp_path):
    bad = tmp_path / "a_wrong.yaml"
    bad.write_text("name: bad payload\npayload: []\n", encoding="utf-8")
    ok = tmp_path / "b_ok.yaml"
    ok.write_text("name: 拍照\npayload:\n  method: take_photo\n  data: {}\nexpect:\n  ack_status: 操作成功\n", encoding="utf-8")
    window.suite.run_cases([str(bad), str(ok)])
    text = window.suite.suite_info.toPlainText()
    assert "2 条用例：PASS 1，FAIL 1" in text and "用例无法执行" in text and "拍照" in text


def test_status_card_ages_without_new_messages(window, monkeypatch):
    window.sn.setText("A")
    import dog_debug.app as app_module
    clock = {"now": 1000.0}
    monkeypatch.setattr(app_module.time, "time", lambda: clock["now"])
    window.store.add(Event("local/robotdog/vfc/heartbeat", {"data": {"version": "3.2.1"}}, sn="A", session="s", timestamp=999.0))
    window.store.flush()
    window._go(window.STATUS)
    assert "v3.2.1" in window.status.module_cards["vfc"][1].text() and "已过期" not in window.status.module_cards["vfc"][1].text()
    clock["now"] = 1012.0
    window.status.status_aged -= 2
    window._drain()
    assert "已过期 13 秒" in window.status.module_cards["vfc"][1].text()


def test_status_card_shows_reported_version(window):
    window.sn.setText("A")
    window.store.add(Event("local/robotdog/fsvr/state", {"type": "subsystem_state", "data": {"name": "fsvr", "version": "1.0.0", "state": 1}},
                           sn="A", session="s"))
    window.store.add(Event("local/robotdog/vfc/heartbeat", {"data": {"version": "3.2.1"}}, sn="A", session="s"))
    window.store.flush()
    window._go(window.STATUS)
    assert window.status.module_cards["fsvr"][1].text().startswith("v1.0.0 · ")
    assert window.status.module_cards["vfc"][1].text().startswith("v3.2.1 · ")
    assert window.status.module_cards["ptz"][1].text() == "未观测"


def build_package(tmp_path):
    from test_models import URDF, cube, write_stl
    root = tmp_path / "工作区" / "my_pkg"
    (root / "urdf").mkdir(parents=True)
    (root / "meshes").mkdir()
    (root / "urdf" / "测试臂.urdf").write_text(URDF, encoding="utf-8")
    write_stl(root / "meshes" / "上臂.STL", cube(0.05))
    write_stl(root / "meshes" / "滑块.STL", cube(0.03))
    return root


def drain_until(window, done, seconds=60):
    import time as _time
    deadline = _time.monotonic() + seconds
    while not done() and _time.monotonic() < deadline:
        window._drain()
        if window.scene3d.importing and window.scene3d.importing.get("job"):
            window.scene3d._poll_step()
        _time.sleep(0.02)
    assert done()


def test_ui_urdf_import_missing_retry_and_switch_back(window, tmp_path):
    root = build_package(tmp_path)
    elsewhere = tmp_path / "网格"
    elsewhere.mkdir()
    (root / "meshes" / "上臂.STL").rename(elsewhere / "上臂.STL")
    window.scene.set_variant("L2 点足运动版")
    before = window.scene.robot
    window.scene3d.import_model("urdf", str(root))
    drain_until(window, lambda: not window.scene3d.importing)
    assert window.scene.robot is before and (not window.scene3d.import_banner.isHidden())
    assert "上臂.STL" in window.scene3d.import_text.text() and "当前场景未改变" in window.scene3d.import_text.text()
    window.scene3d.import_model("urdf", str(root), [str(elsewhere)])
    drain_until(window, lambda: not window.scene3d.importing)
    robot = window.scene.robot
    assert robot.kind == "URDF" and robot.name == "测试臂" and not (not window.scene3d.import_banner.isHidden())
    assert set(window.scene3d.joint_sliders) == {"shoulder", "rail", "wrist"} and not (not window.pose_card.isHidden())
    assert "导入 URDF" in window.scene3d.model_info.text() and window.scene3d.model_box.count() == 3
    window.scene3d._model_chosen(0)
    assert window.scene.robot.kind == "L2" and (not window.pose_card.isHidden())


def test_ui_step_import_and_cancel(window):
    from test_models import STEP, needs_ocp  # noqa: F401
    from dog_debug import cad
    if cad.available():
        pytest.skip("未安装 cadquery-ocp")
    window.scene.set_variant("L2 点足运动版")
    before = window.scene.robot
    window.scene3d.import_model("step", STEP)
    job = window.scene3d.importing["job"]
    window.scene3d.cancel_import()
    assert window.scene3d.importing is None and not job.process.is_alive() and window.scene.robot is before
    assert not (not window.scene3d.import_banner.isHidden())
    window.scene3d.import_model("step", STEP)
    drain_until(window, lambda: not window.scene3d.importing)
    robot = window.scene.robot
    assert robot.kind == "STEP" and window.scene3d.part_list.count() == 2 and "文件单位 mm" in window.scene3d.model_info.text()
    assert window.teleop.joint_title.text() == "部件" and "结构" in window.scene3d.joint_pill.text()


def test_form_and_json_stay_in_sync_without_dropping_fields(window):
    import json as _json
    select(window, "camera_livestream")
    fields = {window.control.form_grid.itemAtPosition(i // 2, (i % 2) * 2).widget().text():
              window.control.form_grid.itemAtPosition(i // 2, (i % 2) * 2 + 1).widget() for i in range(window.control.form_grid.count() // 2)}
    assert {"index", "state", "mode"} <= set(fields)
    payload = _json.loads(window.control.editor.toPlainText())
    payload["data"]["自定义"] = "保留我"
    window.control.editor.setPlainText(_json.dumps(payload, ensure_ascii=False))
    window.control._rebuild_form()
    state = next(window.control.form_grid.itemAt(i + 1).widget() for i in range(0, window.control.form_grid.count(), 2)
                 if window.control.form_grid.itemAt(i).widget().text() == "state")
    state.setText("2")
    state.textEdited.emit("2")
    after = _json.loads(window.control.editor.toPlainText())
    assert after["data"]["state"] == 2 and after["data"]["自定义"] == "保留我"
    state.textEdited.emit("abc")
    assert _json.loads(window.control.editor.toPlainText())["data"]["state"] == 2
    window.control.editor.setPlainText("{broken")
    window.control._rebuild_form()
    assert "暂停同步" in window.control.form_note.text() and window.control.editor.toPlainText() == "{broken"


def test_stream_table_separates_activity_and_room(window):
    window.sn.setText("A")
    window.store.add(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": "1", "sn": "002", "type": "video", "result": 1}},
                           sn="A", session="s", module="media"))
    window.store.add(Event("local/robotdog/media/state", {"type": "room_state", "info": {"result": 0, "info": [
        {"index": 0, "sn": "001", "room": "r0", "userId": "u0"}, {"index": 1, "sn": "002", "room": "r1", "userId": "u1"}]}},
        sn="A", session="s", module="media"))
    window.store.flush()
    window._go(window.STATUS)
    table = window.status.stream_table
    rows = {table.item(r, 0).text(): [table.item(r, c).text() for c in range(7)] for r in range(table.rowCount())}
    assert rows["1"][3] == "活跃" and rows["1"][4] == "r1" and rows["1"][5] == "u1"
    assert rows["0"][3] == "未观测" and rows["0"][4] == "r0"


def test_yaml_case_with_bom_and_chinese_path(window, tmp_path):
    folder = tmp_path / "用例 目录"
    folder.mkdir()
    case = folder / "拍照 用例.yaml"
    case.write_bytes("name: 中文用例\npayload:\n  method: take_photo\n  task_name: 西4楼\n  data: {}\nexpect:\n  ack_status: 操作成功\n".encode("utf-8-sig"))
    window.suite.run_cases([str(case)])
    assert "PASS" in window.suite.suite_info.toPlainText() and "中文用例" in window.suite.suite_info.toPlainText()


def test_diagnostic_zip_to_chinese_path_reopens(window, tmp_path):
    import zipfile
    window.control._demo()
    command = next(e for _, e in window.store.query(method="take_photo", kind="指令"))
    window.store.set_note((command.sn, command.session, command.tid), {"expected": "拍照成功", "actual": "相机超时", "remark": "中文备注"})
    path = tmp_path / "诊断 包" / "狗一号 诊断.zip"
    path.parent.mkdir()
    window.diagnostics.export_bundle(str(path))
    with zipfile.ZipFile(path) as archive:
        session = archive.read("session.jsonl").decode("utf-8")
        notes = archive.read("notes.json").decode("utf-8")
    assert "相机连接超时" in session and "中文备注" in notes
    extracted = tmp_path / "解开 的会话.jsonl"
    extracted.write_text(session, encoding="utf-8")
    window.logs.start_import([str(extracted)])
    window.logs.import_thread.join(10)
    for _ in range(20):
        window._drain()
    assert "编码 utf-8" in window.logs.import_info.toPlainText()
    assert window.store.query(text="相机连接超时")


def test_close_window_terminates_step_import():
    from test_models import STEP
    from dog_debug import cad
    if cad.available():
        pytest.skip("未安装 cadquery-ocp")
    window = Studio(":memory:")
    window.scene3d.import_model("step", STEP)
    job = window.scene3d.importing["job"]
    window.close()
    assert not job.process.is_alive() and window.scene3d.importing is None


def test_original_dialog_decodes_gbk_and_positions_cursor(window, tmp_path, monkeypatch):
    from dog_debug.logs import import_log
    path = tmp_path / "相机 日志.log"
    path.write_bytes(("前一行 中文\n" * 50 + "12:00:01 local/robotdog/media/ack {\"tid\":\"t\",\"data\":{\"result\":1,\"msg\":\"相机超时\"}}\n").encode("gbk"))
    event = list(import_log(path, "a", "s"))[-1]
    shown = {}
    monkeypatch.setattr("PySide6.QtWidgets.QDialog.exec", lambda d: shown.update(
        text=d.findChild(QPlainTextEdit).toPlainText(), line=d.findChild(QPlainTextEdit).textCursor().block().text()))
    window._original(event)
    assert "相机超时" in shown["text"] and "前一行 中文" in shown["text"]
    assert shown["line"].startswith("12:00:01")


def test_wheel_over_unfocused_slider_scrolls_page(window):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    window._go(window.REMOTE)
    slider = window.teleop.remote.vx
    before = slider.value()
    wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(slider, wheel)
    assert slider.value() == before


def test_command_list_groups_each_module_once(window):
    headers = [window.control.commands.item(i).text() for i in range(window.control.commands.count()) if not window.control.commands.item(i).data(0x0100)]
    assert len(headers) == len(set(headers)) == 7


def test_clicking_trace_step_with_real_mouse_does_not_crash(window):
    from PySide6.QtTest import QTest
    from dog_debug.trace_view import Step
    from dog_debug.trace_view import TraceView
    window.show()
    window.control._demo()
    view = [v for v in window.findChildren(TraceView) if v.window() is not window and v.window().isVisible()][-1]
    steps = [s for s in view.findChildren(Step) if s.isVisible()]
    QTest.mouseClick(steps[0], Qt.LeftButton)
    QApplication.processEvents()
    assert view.selected is not None and "证据链" in view.text()
    window.hide()


def test_command_list_searches_chinese_and_method(window):
    shown = lambda: [window.control.commands.item(i).data(Qt.UserRole)["method"] for i in range(window.control.commands.count())
                     if window.control.commands.item(i).data(Qt.UserRole)]
    window.control.command_search.setText("站立")
    assert "stand_up" in shown() and "move" not in shown()
    window.control.command_search.setText("stand_up")
    assert shown() == ["stand_up"] and window.control.selected["method"] != "stand_up"
    window.control._pick_first()
    assert window.control.selected["method"] == "stand_up" and "站立" in window.control.command_heading.text()


def test_draft_kept_when_switching_commands_and_recent_reopens(window):
    select(window, "move")
    edited = window.control.editor.toPlainText().replace('"vx": 0', '"vx": 0.42', 1)
    assert edited != window.control.editor.toPlainText()
    window.control.editor.setPlainText(edited)
    select(window, "stand_up")
    window.control._send()
    select(window, "move")
    assert window.control.editor.toPlainText() == edited
    window.control._template()
    assert '"vx": 0.42' not in window.control.editor.toPlainText()
    assert [r["method"] for r in window.control.recent] == ["stand_up"]
    window.control._open_command("stand_up")
    assert window.control.selected["method"] == "stand_up"


def test_result_card_uses_request_verdict_after_ack(window):
    select(window, "get_media_filelist")
    window.control._send()
    window._refresh_tail()
    text = window.control.result.text()
    assert "待判" not in text.split("证据链")[0] and "操作成功" in text.split("证据链")[0]


def test_form_accepts_decimal_for_integer_template_value(window):
    from PySide6.QtWidgets import QLineEdit
    select(window, "move")
    field = next(w for w in window.control.form_area.findChildren(QLineEdit) if w.text() == "0")
    field.textEdited.emit("0.42")
    assert json.loads(window.control.editor.toPlainText())["data"]["vx"] == 0.42


def test_draft_keeps_topic_and_new_tid_keeps_params(window):
    select(window, "move")
    window.control.topic.setText("custom/topic")
    window.control.editor.setPlainText(window.control.editor.toPlainText().replace('"vx": 0', '"vx": 0.3', 1))
    select(window, "stand_up")
    select(window, "move")
    assert window.control.topic.text() == "custom/topic"
    before = json.loads(window.control.editor.toPlainText())
    window.control._new_tid()
    after = json.loads(window.control.editor.toPlainText())
    assert after["tid"] != before["tid"] and after["data"] == before["data"]


def test_new_tid_on_non_object_keeps_text(window):
    select(window, "move")
    window.control.editor.setPlainText("[]")
    window.control._new_tid()
    assert window.control.editor.toPlainText() == "[]"


def test_connection_profile_saved_and_restored(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    window.mode_box.setCurrentText("直连模块")
    window.host.setText("10.0.0.8")
    window.sn.setText("DOG_A")
    window.password.setText("secret")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("板子本地", True))
    window.connection._save_profile()
    assert "secret" not in json.dumps(window.store.connections(), ensure_ascii=False)
    window.mode_box.setCurrentText("沙盒 · 全链路模拟")
    window.host.setText("127.0.0.1")
    window.connection._profile_chosen(window.profile_box.findText("板子本地"))
    assert (window.mode, window.host.text(), window.sn.text(), window.password.text()) == ("direct", "10.0.0.8", "DOG_A", "")
    window.connection._delete_profile()
    assert window.store.connections() == {}


def test_replay_osd_session_shows_frame_and_recent_messages(window):
    from dog_debug.core import Event
    events = []
    for i in range(5):
        events.append(Event("tta/robotdog/DOG/osd", {"x": i * 0.5, "y": 0.0, "heading": 90, "speed": 0.2}, sn="DOG", session="log1",
                            source="log:a.log", source_time=f"12:00:0{i}"))
        if i == 2:
            events.append(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "t1", "data": {"result": 1, "msg": "超时"}},
                                sn="DOG", session="log1", source="log:a.log", source_time="12:00:02"))
    for e in events:
        window.store.add(e)
    window.scene3d._replay_scan()
    window._drain()
    sessions = window.store.osd_sessions()
    assert sessions == [("DOG", "log1", 5)]
    window._background(lambda r: r.osd_frames("DOG", "log1"), "replay_frames", "DOG", "log1")
    window._drain()
    assert window.scene3d.replay and window.scene3d.replay_slider.maximum() == 4
    window.scene3d.replay_slider.setValue(3)
    assert window.scene.x == 1.5 and not window.scene.live and len(window.scene.trail) == 4
    assert "take_photo" in window.scene3d.replay_list.item(0).text() and window.scene3d.replay_list.item(0).font().bold()
    assert "回放" in window.scene3d.position_pill.text()
    window.scene3d._replay_exit()
    assert window.scene3d.replay is None and not window.scene.osd


def test_send_button_and_status_card_feedback(window):
    window.sn.setText("d")
    from dog_debug.core import Event
    select(window, "stand_up")
    window.control._send()
    assert window.control.send_btn.text() == "✓ 已发出"
    window._go(window.STATUS)
    window.store.add(Event("local/robotdog/ptz/state", {"type": "gimbal_status", "info": {"pitch": 0}}, sn="d", timestamp=1.0))
    window.status._refresh_status()
    assert not window.status.module_frames["ptz"].styleSheet()
    window.store.add(Event("local/robotdog/ptz/state", {"type": "gimbal_status", "info": {"pitch": 5}}, sn="d", timestamp=2.0))
    window.status._refresh_status()
    assert "border" in window.status.module_frames["ptz"].styleSheet()


def test_subsystem_status_array_projected_to_module_cards(window):
    window.sn.setText("A")
    from dog_debug.core import Event
    e = Event("tta/robotdog/A/state", {"method": "subsystem_status", "data": [
        {"name": "vision", "state": 1, "version": "2.1.0", "info": [{"index": 1, "sn": "002", "type": "camera"}]},
        {"name": "media", "state": 0}]}, sn="A", session="s")
    window.store.add(e)
    window.store.flush()
    window._go(window.STATUS)
    vision = window.status.status_latest["vision"][0]
    assert vision.summary_of.event_id == e.event_id and vision.payload["data"]["name"] == "vision"
    assert window.status.module_cards["vision"][1].text().startswith("v2.1.0")
    assert window.status.module_cards["media"][1].text() != "未观测"


def test_module_detail_shows_streams_and_commands_in_place(window):
    """MEDIA 详情：上报里有推流；“指令与回复”一次请求一行，点开在同一面板看指令和对应 ACK，不跳页"""
    from PySide6.QtWidgets import QLabel
    window.store.add(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1, "sn": "002", "result": 1}},
                           sn="TEST_DOG_001", session="s"))
    window._quick_send("take_photo", {})
    window.store.flush()
    window._go(window.STATUS)
    window.status._open_module("media")
    texts = [w.text() for w in window.status.detail_card.findChildren(QLabel) if not w.isHidden()]
    assert any("index 1" in t and "活跃" in t for t in texts)
    window.status.detail_view.set_index(1)
    rows = window.status.detail_model.rows
    # 转给 MEDIA 的指令和 MEDIA 的回复是同一次请求，合成一行；看到它的位置只在提示里
    assert len(rows) == 1 and rows[0][0].kind == "指令" and rows[0][1:] == ("沙盒", "操作成功", "观测到发往模块 · 模块回复")
    window.status._detail_open(window.status.detail_model.index(0, 0))
    assert window.status.detail_pages.currentIndex() == 2 and window.stack.currentIndex() == window.STATUS
    assert "证据链 · 4 条" in window.status.detail_trace.text() and "[media · ACK" in window.status.detail_trace.text()


def test_module_detail_redraws_on_late_ack_but_not_other_modules(window):
    window.sn.setText("A")
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "t1", "data": {}}, sn="A", session="s"))
    window._go(window.STATUS)
    window.status._open_module("media")
    window.status.detail_view.set_index(1)
    assert window.status.detail_model.rows[0][3] == "观测到发往模块" and window.status.detail_model.rows[0][2] == "无 ACK"
    window.status.detail_table.selectRow(0)
    window.status._detail_open(window.status.detail_model.index(0, 0))
    drawn = window.status.detail_drawn[1]
    # 别的模块来消息，当前详情不重画
    window._ingest(Event("local/robotdog/ptz/ack", {"method": "camera_switch", "tid": "x", "data": {"result": 0}}, sn="A", session="s"))
    window.status._refresh_status()
    assert window.status.detail_drawn[1] is drawn
    window._ingest(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "t1", "data": {"result": 1, "msg": "相机离线"}},
                         sn="A", session="s"))
    window.status._refresh_status()
    assert window.status.detail_model.rows[0][2] == "失败" and "相机离线" in window.status.detail_trace.text()
    assert window.status.detail_pages.currentIndex() == 2 and window.status.detail_table.currentIndex().row() == 0
    assert len(window.status.detail_model.rows) == 1 and window.status.detail_model.rows[0][3] == "观测到发往模块 · 模块回复"
    # 没观测到指令、只收到回复的也列出来
    window._ingest(Event("local/robotdog/media/ack", {"method": "camera_recording", "tid": "t2", "data": {"result": 0}},
                         sn="A", session="s"))
    window.status._refresh_status()
    assert window.status.detail_model.rows[0][0].tid == "t2" and window.status.detail_model.rows[0][1] == "仅观测到回复"


def test_status_page_only_shows_current_device_live_data(window):
    """导入的日志和别的设备不顶掉当前设备的实时状态；指令列表同样只看当前设备"""
    window.sn.setText("DOG_A")
    window._ingest(Event("local/robotdog/media/state", {"method": "subsystem_state", "data": {"version": "CURRENT"}}, sn="DOG_A", session="live"))
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "same", "data": {}}, sn="DOG_B", session="b"))
    window.store.add(Event("local/robotdog/media/state", {"method": "subsystem_state", "data": {"version": "OLD"}},
                           sn="DOG_A", session="import-x", source="log:a.log"))
    window.store.flush()
    window._go(window.STATUS)
    assert "CURRENT" in window.status.module_cards["media"][2].text() and "OLD" not in window.status.module_cards["media"][2].text()
    window.status._open_module("media")
    window.status.detail_view.set_index(1)
    assert window.status.detail_model.rows == []
    # 房间号是整机SN_相机SN，跟着 SN 和当前相机
    window.teleop.remote.camera_sn.setText("002")
    assert window.status.room.text() == "DOG_A_002"
    window.teleop.remote.camera_sn.setText("001")
    assert window.status.room.text() == "DOG_A_001"


def test_zlm_poll_shows_streams_or_error(window, monkeypatch):
    import dog_debug.app as app_module
    import io
    import time as _time
    replies = [{"code": 0, "data": [{"app": "live", "stream": "video0", "schema": "rtsp", "readerCount": 2, "bytesSpeed": 1000,
                                     "aliveSecond": 30, "originTypeStr": "rtp_push"},
                                    {"app": "live", "stream": "video0", "schema": "rtmp", "readerCount": 1}]},
               {"code": -100, "msg": "secret错误"}]

    def fake(url, timeout):
        reply = {"code": 0, "data": [{"rtsp.port": "8555"}]} if "getServerConfig" in url else replies.pop(0)
        return io.BytesIO(json.dumps(reply).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    window.mode_box.setCurrentText("直连模块")
    window._go(window.STATUS)
    window.status._open_module("zlm")
    window.status.zlm_host.setText("10.0.0.1:80")
    window.status.zlm_secret.setText("s")
    for expected in ("1 路流", "secret错误"):
        window.status._zlm_poll()
        deadline = _time.monotonic() + 3
        while window.status.zlm_busy and _time.monotonic() < deadline:
            window._drain()
            _time.sleep(0.02)
        assert expected in window.status.module_cards["zlm"][2].text()
    window.status.zlm_timer.stop()
    # RTSP 端口每个地址只查一次
    assert window.status.zlm_ports == {"10.0.0.1:80": {"rtsp": 8555}}


def test_bridged_osd_topic_matched_by_payload_sn(window):
    from dog_debug.core import Event
    window.mqtt.connected, window.mqtt.sn, window.mqtt.session = True, "DOG", "s"
    mine = Event("tta/robotdog/vfc/osd", {"sn": "DOG", "data": {"x": 1.0}}, source="mqtt", session="s")
    other = Event("tta/robotdog/vfc/osd", {"sn": "OTHER", "data": {"x": 1.0}}, source="mqtt", session="s")
    assert window._current_device(mine) and not window._current_device(other)
    window.mqtt.connected = False


def test_vfc_root_version_shown_on_card(window):
    window.sn.setText("D")
    from dog_debug.core import Event
    window.store.add(Event("local/robotdog/vfc/state", {"name": "vfc", "version": "3.0.1", "state": 1, "type": "loc_status",
                                                        "info": {"result": 1, "msg": "not connected", "data": None}}, sn="D", session="s"))
    window.store.flush()
    window._go(window.STATUS)
    assert window.status.module_cards["vfc"][1].text().startswith("v3.0.1")


def test_connection_remembered_and_restored():
    import json as _json
    from PySide6.QtCore import QSettings
    import dog_debug.app as app_module
    # 测试的设置必须落在临时 ini，不能碰本机注册表
    assert app_module.settings().format() == QSettings.IniFormat
    app_module.settings().setValue("connection", _json.dumps({"mode": "direct", "host": "10.1.1.1", "port": 1883, "sn": "DOG",
                                                              "user": "u", "password": "p", "tls": False}))
    window = Studio(":memory:")
    window.connection.restore_connection()
    assert (window.mode, window.host.text(), window.sn.text(), window.user.text(), window.password.text()) == ("direct", "10.1.1.1", "DOG", "u", "p")
    app_module.settings().remove("connection")
    window.close()


def test_stream_rows_name_cameras(window):
    window.sn.setText("D")
    from dog_debug.core import Event
    for index, sn in ((0, "001"), (1, "002")):
        window.store.add(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": index, "sn": sn, "result": 0}},
                               sn="D", session="s"))
    window.store.flush()
    window.status._refresh_status()
    assert [r[1] for r in window.status.stream_rows] == ["001（机体相机）", "002（云台相机）"]


def test_remote_page_no_longer_switches_lens(window):
    """云台固定主码流可见光、副码流红外，遥控页不再有切镜头按钮，变焦一直可用；目录里的 camera_switch 标明平台不应下发"""
    from dog_debug.registry import find
    remote = window.teleop.remote
    assert not hasattr(remote, "lens") and all(zoom.isEnabled() for zoom in remote.zoom_buttons)
    assert "平台不应下发" in find("camera_switch")["title"] and "打断两路直播" in find("camera_switch")["summary"]


def test_room_follows_reported_room_state(window):
    """房间号：没收到 room_state 时按“整机SN_相机SN”算；收到后用当前相机报的房间；手改过就不再跟"""
    window.sn.setText("10300262998249")
    window.teleop.remote.camera_sn.setText("002")
    assert window.status.room.text() == "10300262998249_002"
    window._ingest(Event("local/robotdog/media/state", {"type": "room_state", "info": {"result": 0, "info": [
        {"sn": "001", "index": 0, "room": "10300262998249_001", "userId": "001"},
        {"sn": "002", "index": 1, "room": "R-FROM-CLOUD", "userId": "visible"},
        {"sn": "002", "index": 2, "room": "R-FROM-CLOUD", "userId": "infrared"}]}}, sn="10300262998249", session="s"))
    window.store.flush()
    window.status._refresh_status()
    assert window.status.room.text() == "R-FROM-CLOUD"
    assert [(r[0], r[6]) for r in window.status.stream_rows] == [("0", "001"), ("1", "visible"), ("2", "infrared")]
    window.teleop.remote.camera_sn.setText("001")
    assert window.status.room.text() == "10300262998249_001"
    window.status.room.setText("手填的")
    window.teleop.remote.camera_sn.setText("002")
    assert window.status.room.text() == "手填的"


def test_live_lens_change_no_longer_offered(window):
    from dog_debug.registry import find
    assert find("live_lens_change") is None
    methods = {window.control.commands.item(i).data(0x0100)["method"] for i in range(window.control.commands.count()) if window.control.commands.item(i).data(0x0100)}
    assert "camera_switch" in methods and "live_lens_change" not in methods


def test_zlm_address_follows_direct_connection(window, monkeypatch):
    import dog_debug.app as app_module
    import io
    import time as _time
    asked = []

    def fake(url, timeout):
        asked.append(url.split("/index")[0])
        return io.BytesIO(json.dumps({"code": 0, "data": []}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    window.mode_box.setCurrentText("直连模块")
    window.status.zlm_secret.setText("s")
    window.mqtt.connected = True
    for host in ("192.168.234.119", "192.168.100.119"):
        window.mqtt.host = host
        window.inbox.put(("connection", "ok"))
        window._drain()
        deadline = _time.monotonic() + 3
        while (window.status.zlm_busy or not window.status.zlm_state) and _time.monotonic() < deadline:
            window._drain()
            _time.sleep(0.02)
        assert window.status.zlm_host.text() == f"{host}:80" and window.status.zlm_state["address"] == f"{host}:80"
    # 手填了独立地址后，换狗不再改
    window.status.zlm_host.setText("10.0.0.9:80")
    window.status.zlm_host.editingFinished.emit()
    window.mqtt.host = "192.168.1.5"
    window.inbox.put(("connection", "ok"))
    window._drain()
    assert window.status.zlm_host.text() == "10.0.0.9:80"


def test_zlm_late_result_for_old_address_is_dropped(window):
    window.status.zlm_host.setText("10.0.0.2:80")
    window.status.zlm_secret.setText("s")
    window.status.zlm_busy = True
    window.status._zlm_done({"at": 1, "error": "", "streams": {"live/old": {}}, "address": "10.0.0.1:80"})
    assert window.status.zlm_state is None


def test_status_filter_hides_types_and_modules_without_touching_data(window):
    window.sn.setText("A")
    from PySide6.QtWidgets import QLabel
    from dog_debug.core import Event
    for topic, payload in (("local/robotdog/gw/heartbeat", {"data": {"version": "3.0.1"}}),
                           ("local/robotdog/gw/state", {"type": "subsystem_state", "data": {"vfc": 1}}),
                           ("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1, "result": 1}}),
                           ("local/robotdog/media/state", {"type": "room_state", "info": {"info": []}})):
        window.store.add(Event(topic, payload, sn="A", session="s"))
    window.store.flush()
    window._go(window.STATUS)
    window.status._open_module("gw")
    count, seen = window.store.count(), dict(window.status.status_seen)
    card = lambda m: window.status.module_cards[m][2].text()
    detail = lambda: " ".join(w.text() for w in window.status.detail_card.findChildren(QLabel) if w.isVisibleTo(window.status.detail_card))
    try:
        assert "subsystem_state" in card("gw") and "subsystem_state" in detail()
        # 隐藏子系统和心跳：卡片、已展开的详情立即变，不等新消息；入库数和新鲜度不变
        window.status._toggle_status_filter(window.status.hidden_types, "subsystem_state", False)
        window.status._toggle_status_filter(window.status.hidden_types, "heartbeat", False)
        assert "subsystem_state" not in card("gw") and "当前筛选无匹配状态" in card("gw")
        assert "subsystem_state" not in detail() and "当前筛选无匹配状态" in detail()
        assert "stream_status" in card("media")
        assert window.store.count() == count and window.status.status_seen == seen
        assert window.status.type_filter.text() == "状态类型：隐藏 2 类"
        # 只看 MEDIA 和 ZLM：其他卡片不占位，两张卡排在最前；正在看的 GW 详情收起
        for module in ("gw", "vfc", "ptz", "vision", "fsvr", "ros2bridge"):
            window.status.module_actions[module].setChecked(False)
        grid = window.status.status_grid
        assert grid.cards == [window.status.module_frames["media"], window.status.module_frames["zlm"]]
        assert grid.grid.getItemPosition(grid.grid.indexOf(window.status.module_frames["media"]))[:2] == (0, 0)
        assert window.status.detail_module is None and window.status.module_filter.text() == "模块：MEDIA、ZLM"
        assert window.status.live_card[0].isVisibleTo(window)
        window.status.module_actions["media"].setChecked(False)
        assert not window.status.live_card[0].isVisibleTo(window)
        # 记住选择：新开窗口还是这样
        again = Studio(":memory:")
        assert again.status.hidden_types == {"subsystem_state", "heartbeat"} and again.status.status_grid.cards == [again.status.module_frames["zlm"]]
        again.close()
    finally:
        window.status._reset_status_filter()
    assert window.status.status_grid.cards == [window.status.module_frames[m] for m in MODULES]
    assert "subsystem_state" in card("gw") and window.status.type_filter.text() == "状态类型：全部"


def test_direct_templates_match_gw_forwarded_payload(window):
    """直连模块发的和 GW 转给模块的一样：去掉顶层 target，按 GW 路由选 Topic"""
    window.sn.setText("DOG")
    select(window, "take_photo")
    assert json.loads(window.control.editor.toPlainText())["target"] == "vision"
    window.mode_box.setCurrentText("直连模块")
    for method, topic in (("take_photo", "media"), ("speaker_play_start", "audio"), ("get_media_filelist", "fsvr"), ("move", "vfc")):
        select(window, method)
        assert "target" not in json.loads(window.control.editor.toPlainText())
        assert window.control.topic.text() == f"local/robotdog/{topic}/command"


class FakeClient:
    def __init__(self):
        self.sent, self.closed = [], False

    def publish(self, topic, text, qos, retain):
        self.sent.append((topic, json.loads(text)))
        return type("Info", (), {"rc": 0})()

    def disconnect(self):
        self.closed = True

    def loop_stop(self):
        pass


@pytest.mark.parametrize("mode", ["离线日志分析", "沙盒 · 全链路模拟"])
def test_offline_and_sandbox_stop_and_drop_live_link(window, monkeypatch, mode):
    import dog_debug.app as app_module
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("离线/沙盒不应查 ZLM"))
    window.mode_box.setCurrentText("直连模块")
    client = window.mqtt.client = FakeClient()
    window.mqtt.connected = True
    window.teleop.remote.press("motion", "fwd")
    window.mode_box.setCurrentText(mode)
    # 停止报文按切换前的直连模式真的发出去了，然后才断开
    assert client.sent[-1] == ("local/robotdog/vfc/command", {**client.sent[-1][1], "data": {"vx": 0, "vy": 0, "yaw_rate": 0}})
    assert client.closed and window.mqtt.client is None and not window.teleop.remote.motion_timer.isActive()
    window.status.zlm_host.setText("10.0.0.9:80")
    window.status.zlm_secret.setText("s")
    window._go(window.STATUS)
    assert not window.status.zlm_timer.isActive()
    if mode == "离线日志分析":
        window._go(window.CONTROL)
        window.mode_box.setCurrentText("直连模块")
        window.mode_box.setCurrentText(mode)
        assert window.stack.currentIndex() == window.LOGS


def test_trace_click_keeps_scroll_within_request():
    from dog_debug.trace_view import TraceView
    app = QApplication.instance() or QApplication([])
    events = [Event("local/robotdog/media/command", {"method": "take_photo", "tid": "t"}, "发送", sn="D", session="s")]
    events += [Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "t", "data": {"result": 0, "msg": str(i)}},
                     sn="D", session="s") for i in range(8)]
    view = TraceView("")
    view.resize(500, 300)
    view.show()
    view.show_trace(events[0], events)
    for _ in range(3):
        app.processEvents()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    kept = bar.value()
    assert kept > 0
    steps = [w for w in view.widget().findChildren(QWidget) if w.objectName() == "step"]
    QTest.mouseClick(steps[4], Qt.LeftButton)
    for _ in range(3):
        app.processEvents()
    assert view.selected is events[5] and bar.value() == kept
    # 换一个请求回到顶部
    other = [Event("local/robotdog/vfc/command", {"method": "move", "tid": "u"}, "发送", sn="D", session="s")]
    view.show_trace(other[0], other)
    assert bar.value() == 0
    view.close()


def test_control_result_keeps_chosen_message_when_new_one_arrives(window):
    select(window, "take_photo")
    window.control.send_btn.click()
    window._refresh_tail()
    ack = next(e for e in window.control.result.events if e.kind == "ACK")
    window.control.result.show_trace(ack, window.control.result.events)
    late = Event("tta/robotdog/TEST_DOG_001/services_reply", {"method": "take_photo", "tid": ack.tid, "data": {"result": 0}},
                 sn=ack.sn, session=ack.session, source="sandbox")
    window.store.add(late)
    window.store.flush()
    window._refresh_tail()
    assert len(window.control.result.events) == window.control.watch_size and window.control.result.selected.event_id == ack.event_id


def _import_and_wait(window, path):
    window.logs.start_import([str(path)])
    window.logs.import_thread.join(10)
    for _ in range(20):
        window._drain()


def test_logs_page_shows_imported_batch_in_place(window, tmp_path):
    """导入后日志排障页直接列出这一批：普通文本和 JSON 混在一起、原文时间、搜索、只看错误、详情和批次切换"""
    first = tmp_path / "相机.log"
    first.write_text("2026-11-02 10:00:01 [I] media 启动完成\n"
                     "2026-11-02 10:00:02 local/robotdog/media/command {\"method\":\"camera_livestream\",\"tid\":\"L1\",\"data\":{}}\n"
                     "2026-11-02 10:00:03 local/robotdog/media/ack {\"method\":\"camera_livestream\",\"tid\":\"L1\",\"data\":{\"result\":1,\"msg\":\"直播推流失败\"}}\n"
                     "2026-11-02 10:00:04 [E] media 推流 error: rtmp connect refused\n", encoding="utf-8")
    second = tmp_path / "云台.log"
    second.write_text("10:05:00 [I] ptz 上电\n", encoding="utf-8")
    window.store.add(Event("local/robotdog/vfc/osd", {"x": 1}, sn="LIVE", session="live"))
    window._go(window.LOGS)
    assert window.logs.log_views.currentIndex() == 0
    _import_and_wait(window, first)
    assert window.logs.log_views.currentIndex() == 1 and window.stack.currentIndex() == window.LOGS
    rows = window.logs.log_model.rows
    # 实时消息不混进来；普通文本行有内容，时间是原文里的
    assert len(rows) == 4 and all(e.session.startswith("import-") for _, e, _ in rows)
    model = window.logs.log_model
    assert model.data(model.index(0, 0)) == "10:00:01" and model.data(model.index(0, 4)) == "[I] media 启动完成"
    assert model.data(model.index(0, 1)) == 1 and "相机.log 第 1 行　原文时间 2026-11-02 10:00:01" in model.data(model.index(0, 0), Qt.ToolTipRole)

    window.logs.log_search.setText("直播")
    window.logs._log_refresh()
    window._drain()
    assert [e.line for _, e, _ in window.logs.log_model.rows] == [3]
    window.logs.log_search.setText("")
    window.logs.log_errors.setChecked(True)
    window._drain()
    assert [e.line for _, e, _ in window.logs.log_model.rows] == [3, 4]
    # 点 ACK 行：详情里是整条请求，含发出的指令
    window.logs._log_inspect(window.logs.log_model.index(0, 0))
    assert "证据链 · 2 条" in window.logs.log_detail.text() and "直播推流失败" in window.logs.log_detail.text()
    # 关掉“只看错误”，选中的那行还选着
    window.logs.log_errors.setChecked(False)
    window._drain()
    assert window.logs.log_table.currentIndex().row() == 2

    _import_and_wait(window, second)
    # 第一项是这台设备的全部日志，刚导入的那批紧跟在后面并被选中
    assert window.logs.log_batch.count() == 3 and "云台.log · 1 条" in window.logs.log_batch.currentText()
    assert window.logs.log_batch.itemText(0).startswith("TEST_DOG_001 全部日志 · 5 条")
    assert [e.line for _, e, _ in window.logs.log_model.rows] == [1]
    window.logs.log_batch.setCurrentIndex(2)
    window._drain()
    assert len(window.logs.log_model.rows) == 4
    # 离开再回来，批次和筛选都还在
    window._go(window.STATUS)
    window._go(window.LOGS)
    window._drain()
    assert window.logs.log_batch.currentIndex() == 2 and len(window.logs.log_model.rows) == 4


def test_detail_tabs_switch_without_redrawing_unchanged_content(window):
    """来回切“模块上报 / 指令与回复”、重复点已选的分段，没有新消息就不重画；本模块来了新消息，切回时才更新"""
    window.sn.setText("A")
    window._ingest(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1, "result": 1}}, sn="A", session="s"))
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "t1", "data": {}}, sn="A", session="s"))
    window._go(window.STATUS)
    window.status._open_module("media")
    first = window.status.detail_box.itemAt(0).widget()
    window.status.detail_view.group.button(1).click()
    window.status.detail_table.selectRow(0)
    window.status.detail_table.verticalScrollBar().setValue(0)
    rows = window.status.detail_model.rows
    window.status.detail_view.group.button(1).click()
    assert window.status.detail_model.rows is rows
    window.status.detail_view.group.button(0).click()
    assert window.status.detail_box.itemAt(0).widget() is first
    window.status.detail_view.group.button(0).click()
    assert window.status.detail_box.itemAt(0).widget() is first
    window.status.detail_view.group.button(1).click()
    assert window.status.detail_model.rows is rows and window.status.detail_table.currentIndex().row() == 0
    # 在“指令与回复”时来了迟到的 ACK：当前列表更新；切回上报因为模块有新消息，会重画一次
    window._ingest(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "t1", "data": {"result": 0}}, sn="A", session="s"))
    window.status._refresh_status()
    assert window.status.detail_model.rows is not rows and window.status.detail_model.rows[0][2] == "操作成功"
    window.status.detail_view.group.button(0).click()
    assert window.status.detail_box.itemAt(0).widget() is not first


def test_live_status_does_not_reread_status_page(window):
    """状态总览没有新状态时不重读，详情里的指令与回复照样更新"""
    window.sn.setText("A")
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "t1", "data": {}}, sn="A", session="s"))

    window._go(window.STATUS)
    window.status._open_module("media")
    window.status.detail_view.set_index(1)
    reads = []
    original = window.store.query
    window.store.query = lambda *a, **k: reads.append(k.get("kind")) or original(*a, **k)
    window._refresh()
    assert "状态" not in reads
    window._ingest(Event("local/robotdog/media/command", {"method": "camera_recording", "tid": "t2", "data": {}}, sn="A", session="s"))
    window._refresh()
    assert "状态" not in reads and window.status.detail_model.rows[0][0].tid == "t2"
    window._ingest(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1, "result": 1}}, sn="A", session="s"))
    window._refresh()
    assert "状态" in reads


def test_module_requests_not_pushed_out_by_many_statuses(window):
    """同一只狗之前连接里的拍照请求，之后来了 600 条状态也还在“指令与回复”里，按方法筛选也能找到"""
    window.sn.setText("A")
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "old", "data": {}}, sn="A", session="before"))
    window._ingest(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "old", "data": {"result": 0}}, sn="A", session="before"))
    for i in range(600):
        window.store.add(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": i % 2, "result": 1}},
                               sn="A", session="now"))
    window._ingest(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1, "result": 1}}, sn="A", session="now"))
    window._go(window.STATUS)
    window.status._open_module("media")
    window.status.detail_view.set_index(1)
    assert [(r[0].tid, r[2]) for r in window.status.detail_model.rows] == [("old", "操作成功")]
    window.status.detail_filter.setText("拍照")
    assert [r[0].tid for r in window.status.detail_model.rows] == ["old"]
    window.status.detail_filter.setText("录像")
    assert window.status.detail_model.rows == [] and "按方法筛选后" in window.status.detail_empty.text()


def test_request_view_keeps_old_requests_after_status_burst(window):
    """消息追踪的请求视图只按指令和 ACK 找请求，3000 多条状态之后旧请求还在；状态详情的链接直接带模块过去"""
    window.sn.setText("A")
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "old", "data": {}}, sn="A", session="s"))
    window._ingest(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "old", "data": {"result": 1, "msg": "超时"}},
                         sn="A", session="s"))
    window.store.add_rows([window.store.row(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": 1}},
                                                  sn="A", session="s")) for _ in range(3500)])
    window.store.flush()
    # 状态刷得再多，状态总览详情的“指令与回复”里也还有那次旧请求
    window._go(window.STATUS)
    window.status._open_module("media")
    window.status.detail_view.set_index(1)
    assert [(e.tid, status) for e, _, status, _ in window.status.detail_model.rows] == [("old", "失败")]


def test_trace_copies_topic_and_message_and_names_log_lines():
    """所选消息能一键复制 Topic 和完整报文；日志文件里没写 Topic 的一行显示为“日志记录”和文件行号，不像一条空消息"""
    from dog_debug.trace_view import TraceView
    from PySide6.QtWidgets import QPushButton
    app = QApplication.instance() or QApplication([])
    logged = Event("", {"method": "camera_livestream", "tid": "t", "data": {"stream_url": "124.126.230.115:60001"}},
                   sn="D", session="s", module="media", file="C:/logs/tta_media.log", line=12, raw="received payload: {...}")
    ack = Event("local/robotdog/media/ack", {"method": "camera_livestream", "tid": "t",
                                            "data": {"result": 1, "msg": "data.stream_url has no scheme or host"}}, sn="D", session="s")
    view = TraceView("")
    view.show_trace(ack, [logged, ack])
    labels = " ".join(w.text() for w in view.widget().findChildren(QLabel))
    assert "MEDIA</b>　日志记录" in labels and "原文未记录 Topic · tta_media.log 第 12 行" in labels
    buttons = {b.text(): b for b in view.widget().findChildren(QPushButton)}
    QTest.mouseClick(buttons["复制"], Qt.LeftButton)
    assert QApplication.clipboard().text() == "local/robotdog/media/ack"
    QTest.mouseClick(buttons["复制消息"], Qt.LeftButton)
    assert json.loads(QApplication.clipboard().text())["data"]["msg"] == "data.stream_url has no scheme or host"
    view.show_trace(logged, [logged, ack])
    buttons = {b.text(): b for b in view.widget().findChildren(QPushButton)}
    assert "复制" not in buttons and "原文附近" in buttons
    view.close()


def test_logs_filter_by_time_written_in_the_log(window, tmp_path):
    """按日志里写的发生时间筛，不按导入时间；跨天不混；只有时刻没日期的行如实说明没算进去；点开仍能看完整请求"""
    first = tmp_path / "a.log"
    first.write_text("2026-11-01 09:30:00 local/robotdog/media/command {\"method\":\"take_photo\",\"tid\":\"T1\",\"data\":{}}\n"
                     "2026-11-01 10:30:00 local/robotdog/media/ack {\"method\":\"take_photo\",\"tid\":\"T1\",\"data\":{\"result\":1,\"msg\":\"超时\"}}\n"
                     "2026-11-02 09:30:00.500 [I] media 第二天同一时刻\n"
                     "12:00:00 [W] media 只有时刻\n", encoding="utf-8")
    second = tmp_path / "b.log"
    second.write_text("2026-11-01T16:45:00+0800 media 下午\n", encoding="utf-8")
    _import_and_wait(window, first)
    _import_and_wait(window, second)
    window.logs.log_batch.setCurrentIndex(0)
    window._drain()
    assert len(window.logs.log_model.rows) == 5
    window.logs.log_period.setCurrentText("选时间段…")
    window.logs.log_start.setDateTime(QDateTime.fromString("2026-11-01 09:00:00", "yyyy-MM-dd HH:mm:ss"))
    window.logs.log_end.setDateTime(QDateTime.fromString("2026-11-01 10:00:00", "yyyy-MM-dd HH:mm:ss"))
    window.logs._log_refresh()
    window._drain()
    assert [e.source_time for _, e, _ in window.logs.log_model.rows] == ["2026-11-01 09:30:00"]
    assert "另有 1 条原文没有日期" in window.logs.log_count.text()
    # 窗口里只有指令那一行，点开的证据链仍是整条请求（含窗口外 10:30 的失败 ACK）
    window.logs._log_inspect(window.logs.log_model.index(0, 0))
    assert "证据链 · 2 条" in window.logs.log_detail.text() and "超时" in window.logs.log_detail.text()
    window.logs.log_end.setDateTime(QDateTime.fromString("2026-11-01 23:59:59", "yyyy-MM-dd HH:mm:ss"))
    window.logs._log_refresh()
    window._drain()
    assert [e.source_time[11:19] for _, e, _ in window.logs.log_model.rows] == ["09:30:00", "10:30:00", "16:45:00"]
    window.logs.log_period.setCurrentText("全部时间")
    window._drain()
    assert len(window.logs.log_model.rows) == 5 and "没有日期" not in window.logs.log_count.text()


def test_log_table_copy_copies_selected_log_line(window, tmp_path):
    """日志表里按 Ctrl+C 复制的是选中的日志原文"""
    window._ingest(Event("local/robotdog/media/command", {"method": "take_photo", "tid": "UNRELATED", "data": {}}, sn="TEST_DOG_001", session="s"))
    path = tmp_path / "x.log"
    path.write_text("2026-11-02 12:00:00 [E] media EXPECTED_LOG_COPY\n", encoding="utf-8")
    _import_and_wait(window, path)
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window)
    window.logs.log_table.selectRow(0)
    window.logs.log_table.setFocus()
    QTest.keySequence(window.logs.log_table, QKeySequence.Copy)
    assert QApplication.clipboard().text() == "2026-11-02 12:00:00 [E] media EXPECTED_LOG_COPY"


def test_trace_shows_time_written_in_log():
    from dog_debug.trace_view import TraceView
    app = QApplication.instance() or QApplication([])
    e = Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "t", "data": {"result": 1}}, sn="D", session="s",
              file="a.log", line=2, source_time="2026-11-02 12:00:01.500", timestamp=1e9)
    view = TraceView("")
    view.show_trace(e, [e])
    assert "12:00:01.500" in " ".join(w.text() for w in view.widget().findChildren(QLabel))
    view.close()


def _press(view, text):
    """点证据链里文字为 text 的按钮"""
    from PySide6.QtWidgets import QPushButton
    next(b for b in view.widget().findChildren(QPushButton) if b.text() == text).click()


def test_trace_lists_same_tid_from_other_sources(window, tmp_path):
    """同设备同 tid 的其他会话分组列出、点了原地切换不拼链、能回到原组；别的设备同 tid 不列"""
    window.sn.setText("D")
    log = tmp_path / "GW_1.log"
    log.write_text("2026-11-02 15:43:08 tta/robotdog/D/services {\"method\":\"camera_livestream\",\"tid\":\"L1\",\"data\":{}}\n"
                   "2026-11-02 15:43:09 local/robotdog/media/ack {\"method\":\"camera_livestream\",\"tid\":\"L1\",\"data\":{\"result\":1,\"msg\":\"推流失败\"}}\n",
                   encoding="utf-8")
    _import_and_wait(window, log)
    window._ingest(Event("local/robotdog/media/command", {"method": "camera_livestream", "tid": "L1", "data": {}}, sn="D", session="live"))
    window._ingest(Event("local/robotdog/media/ack", {"method": "camera_livestream", "tid": "L1", "data": {"result": 0}}, sn="D", session="live"))
    window._ingest(Event("local/robotdog/media/ack", {"method": "camera_livestream", "tid": "L1", "data": {"result": 0}}, sn="X", session="x"))
    from dog_debug.trace_view import TraceView
    command = next(e for _, e in window.store.query(sn="D", session="live") if e.tid == "L1" and e.kind == "指令")
    view = window.open_request(command).findChild(TraceView)
    text = view.text()
    assert "证据链 · 2 条" in text and "其他来源中相同 tid 的记录" in text
    assert "设备日志 · GW_1.log · 2 条 · 11-02 15:43:08 – 15:43:09" in text and "实时 MQTT" not in text
    _press(view, "查看")
    text = view.text()
    assert "证据链 · 2 条" in text and "推流失败" in text and "实时 MQTT · 2 条" in text
    assert all(e.session.startswith("import-") for e in view.events) and view.selected.session.startswith("import-")
    _press(view, "‹ 回到原来那组")
    assert view.origin is None and {e.session for e in view.events} == {"live"}


def test_nearby_logs_uses_time_written_in_log(window, tmp_path):
    """从请求跳到附近日志：按原文时间、同模块圈出前后 30 秒，提示不一定相关；回到请求时原页面不变；没有日志时提示去取"""
    window.sn.setText("D")
    log = tmp_path / "media.log"
    log.write_text("2026-11-02 15:43:09 local/robotdog/media/ack {\"method\":\"camera_livestream\",\"tid\":\"N1\",\"data\":{\"result\":1,\"msg\":\"推流失败\"}}\n"
                   "2026-11-02 15:43:30 [E] media WebRtcEndpoint 地址不对\n"
                   "2026-11-02 15:43:31 [I] ptz 别的模块\n"
                   "2026-11-02 15:50:00 [E] media 很久以后\n", encoding="utf-8")
    _import_and_wait(window, log)
    from dog_debug.trace_view import TraceView
    window._go(window.STATUS)
    ack = next(e for _, e in window.store.query(method="camera_livestream") if e.tid == "N1")
    view = window.open_request(ack).findChild(TraceView)
    _press(view, "这段时间的设备日志")
    for _ in range(20):
        window._drain()
    assert window.stack.currentIndex() == window.LOGS and not window.logs.nearby_bar.isHidden()
    assert "不一定和这次请求有关" in window.logs.nearby_text.text()
    assert [e.source_time[11:19] for _, e, _ in window.logs.log_model.rows] == ["15:43:09", "15:43:30"]
    window.logs._nearby_back()
    assert window.stack.currentIndex() == window.STATUS and "推流失败" in view.text()
    # 实时收到的请求按收到时刻圈；这台设备没有日志时不拿别的设备凑
    window._ingest(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "N2", "data": {"result": 0}}, sn="E", session="live"))
    live = next(e for _, e in window.store.query(sn="E", session="live") if e.tid == "N2")
    view = window.open_request(live).findChild(TraceView)
    _press(view, "这段时间的设备日志")
    for _ in range(20):
        window._drain()
    assert window.logs.log_model.rows == [] and "还没有这台设备的日志" in window.logs.log_count.text()


def test_zlm_streams_tagged_and_play_urls(window):
    """ZLM 流按来源标相机原始 / 原画转发 / AI 处理后；有 FLV 走 HTTP 端口，只有 RTSP 用 RTSP 端口，远程时开一次隧道复用"""
    streams = {"live/__input_video0": {"schemas": ["rtsp"], "readers": 1, "speed": 0, "alive": 5, "origin": "pull"},
               "live/video0": {"schemas": ["rtsp", "rtmp"], "readers": 0, "speed": 0, "alive": 5, "origin": "pull"},
               "live/video1": {"schemas": ["rtsp", "ts"], "readers": 0, "speed": 0, "alive": 5, "origin": "rtp_push"}}
    window.status.zlm_state = {"at": 1, "error": "", "streams": streams, "address": "10.0.0.1:80", "rtsp": 0}
    window._go(window.STATUS)
    window.status._open_module("zlm")
    text = " ".join(w.text() for w in window.status.detail_box.parentWidget().findChildren(QLabel))
    assert "相机原始" in text and "原画转发" in text and "AI 处理后" in text
    assert window.status._play_url("live/video0", streams["live/video0"]) == "http://10.0.0.1:80/live/video0.live.flv"
    assert window.status._play_url("live/video1", streams["live/video1"]) == "http://10.0.0.1:80/live/video1.live.ts"
    with pytest.raises(RuntimeError, match="RTSP 端口"):
        window.status._play_url("live/__input_video0", streams["live/__input_video0"])
    window.status.zlm_ports["10.0.0.1:80"] = {"rtsp": 8555}
    assert window.status._play_url("live/__input_video0", streams["live/__input_video0"]) == "rtsp://10.0.0.1:8555/live/__input_video0"

    class FakeSsh:
        opened = []

        def forward(self, port, host="127.0.0.1"):
            self.opened.append((host, port))
            return 40000 + len(self.opened)

    window.ssh = FakeSsh()
    for _ in range(2):
        assert window.status._play_url("live/__input_video0", streams["live/__input_video0"]) == "rtsp://127.0.0.1:40001/live/__input_video0"
    assert FakeSsh.opened == [("127.0.0.1", 8555)]
    # 直接拉相机：远程时经狗连相机，账号密码保留；同一相机只开一条隧道
    for _ in range(2):
        assert window.status._reachable("rtsp://admin:p@ss@192.168.234.201:8554/main.264") == "rtsp://admin:p@ss@127.0.0.1:40002/main.264"
    assert window.status._reachable("rtsp://192.168.234.1/test") == "rtsp://127.0.0.1:40003/test"
    assert FakeSsh.opened[1:] == [("192.168.234.201", 8554), ("192.168.234.1", 554)]
    window.ssh = None
    assert window.status._reachable("rtsp://192.168.234.1:8554/test") == "rtsp://192.168.234.1:8554/test"


def test_pull_presets_from_device_config(window, monkeypatch):
    """拉流候选按设备配置和 ZLM 流列表自动列出：相机直连、ZLM 原始、公开流各协议、云端 RTMP、别的 ZLM 流；
    ZLM secret 没填时顺手填上；自己填的地址记进下拉，标题和提示里密码打码"""
    import dog_debug.app as app_module
    monkeypatch.setattr(window.status, "_zlm_poll", lambda: None)
    opened = []
    monkeypatch.setattr(status_module, "StreamWindow", lambda title, resolve, parent: opened.append((title, resolve())) or type("W", (), {"show": lambda self: None})())
    window.sn.setText("DOG")
    window.status.zlm_host.setText("192.168.234.234:80")
    window.status.zlm_secret.setText("")
    window.status.zlm_ports["192.168.234.234:80"] = {"rtsp": 8555, "rtmp": 1936}
    window.status.zlm_state = {"at": 1, "error": "", "address": "192.168.234.234:80", "ports": {}, "streams": {
        "live/__input_video0": {"schemas": ["rtsp"], "origin": "pull"},
        "live/video1": {"schemas": ["rtsp", "rtmp", "ts"], "origin": "rtp_push"},
        "live/extra": {"schemas": ["rtsp"], "origin": "pull"}}}
    cfg = {"zlm": {"app": "live", "rtsp_port": 8555, "api_secret": "S", "push_protocol": {"video1": "rtmp"},
                   "rtmp_push_url_template": "rtmp://media.example/live/%s_%s"},
           "streams": {"video0": {"enabled": True, "livestream_mode": "direct", "camera_sn": "001",
                                  "zlm": {"source_url": "rtsp://user:pw@192.168.234.1:8554/test"}},
                       "video1": {"enabled": True, "livestream_mode": "processed", "ai_enabled": True, "camera_sn": "002",
                                  "zlm": {"source_url": "rtsp://192.168.234.201:8554/main.264"}},
                       "video2": {"enabled": False}}}
    window.status._media_cfg_done(cfg, "/oem/ttaviation/gateway/db/vision.cfg")
    items = [(window.status.pull_url.itemText(i), window.status.pull_url.itemData(i)) for i in range(window.status.pull_url.count())]
    assert items[:4] == [("机体相机 001 · 相机直连 · RTSP", "rtsp://user:pw@192.168.234.1:8554/test"),
                         ("机体相机 001 · ZLM 相机原始 · RTSP", "rtsp://192.168.234.234:8555/live/__input_video0"),
                         ("机体相机 001 · ZLM 原画转发 · RTSP", "rtsp://192.168.234.234:8555/live/video0"),
                         ("机体相机 001 · ZLM 原画转发 · RTMP", "rtmp://192.168.234.234:1936/live/video0")]
    assert ("云台相机 002 · ZLM 处理后（AI） · HTTP-TS", "http://192.168.234.234:80/live/video1.live.ts") in items
    assert ("云台相机 002 · 云端 RTMP（按配置里的模板）", "rtmp://media.example/live/DOG_002") in items
    assert ("ZLM live/extra · 原画转发 · RTSP", "rtsp://192.168.234.234:8555/live/extra") in items
    assert not any("video2" in url for _, url in items)
    assert window.status.zlm_secret.text() == "S" and "vision.cfg：3 路相机" in window.status.pull_note.text()
    window.status.pull_url.setCurrentIndex(0)
    assert window.status.pull_url.toolTip() == "rtsp://user:***@192.168.234.1:8554/test"
    window.status._play_selected()
    assert opened[-1] == ("拉流 · 机体相机 001 · 相机直连 · RTSP", "rtsp://user:pw@192.168.234.1:8554/test")
    # ZLM 刷新后重列，选中的那项不变
    window.status._refresh_presets()
    assert window.status._selected_url() == "rtsp://user:pw@192.168.234.1:8554/test"
    # 自己填的地址记进下拉，最新的在前
    for url in ("rtsp://a/1", "rtsp://admin:secret@b:8554/2"):
        window.status.pull_url.setEditText(url)
        window.status._play_selected()
    assert opened[-1][0] == "拉流 · rtsp://admin:***@b:8554/2"
    assert json.loads(app_module.settings().value("direct_urls"))[:2] == ["rtsp://admin:secret@b:8554/2", "rtsp://a/1"]
    assert window.status.pull_url.findData("rtsp://a/1") >= 0


def _wait(condition, seconds=10):
    import time as _time
    app = QApplication.instance()
    deadline = _time.monotonic() + seconds
    while not condition() and _time.monotonic() < deadline:
        app.processEvents()
        _time.sleep(0.02)
    return condition()


def test_stream_window_plays_and_reports_errors(tmp_path):
    """拉流窗口：能解码出画面并显示编码和分辨率；地址出错时原样显示原因"""
    import av
    import numpy
    from dog_debug.player import StreamWindow
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "clip.mp4"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("mpeg4", rate=25)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(10):
            frame = av.VideoFrame.from_ndarray(numpy.full((48, 64, 3), i * 20, numpy.uint8), format="rgb24")
            out.mux(stream.encode(frame))
        out.mux(stream.encode())
    window = StreamWindow("测试", lambda: str(path))
    assert _wait(lambda: "流已结束" in window.info.text())
    assert window.pixmap.size().toTuple() == (64, 48) and "mpeg4 64×48" in window.info.text()
    window.close()

    def broken():
        raise RuntimeError("这路流只有 RTSP")
    window = StreamWindow("测试", broken)
    assert _wait(lambda: "拉流失败：这路流只有 RTSP" in window.info.text())
    window.close()


def test_copy_mosquitto_pub_command(window):
    """复制终端命令：Broker、端口、QoS、Topic 按当前填写，报文压成一行并转义单引号；密码只留占位"""
    from PySide6.QtWidgets import QApplication
    select(window, "take_photo")
    window.host.setText("10.0.0.5")
    window.port.setValue(1884)
    window.user.setText("tta")
    window.password.setText("secret")
    window.control.editor.setPlainText('{"method": "take_photo", "data": {"note": "it\'s"}}')
    window.control._copy_pub_command()
    text = QApplication.clipboard().text()
    assert text.startswith("mosquitto_pub -h 10.0.0.5 -p 1884 -q 0 -u 'tta' -P '<密码>' -t ")
    assert text.endswith("""-m '{"method":"take_photo","data":{"note":"it'\\''s"}}'""") and "secret" not in text


def test_connection_sheet_refits_after_mode_change(window):
    """连接面板开着时切接入方式：各行显示与否定了再量高度，面板和内容一样高，不被压扁（GPT 1e92309 复现：狗 云端→直连）"""
    window.resize(1180, 900)
    window.show()
    window.device_box.setCurrentText("机器狗")
    window.mode_box.setCurrentText("云端 → 真实 GW")
    sheet = window.connection_sheet
    sheet.open()
    window.mode_box.setCurrentText("直连模块")
    assert not window.ssh_row.isHidden()
    assert sheet.area.height() == sheet.card.sizeHint().height()
    window.device_box.setCurrentText("无人机")
    window.mode_box.setCurrentText("云端 → 真实 GW")
    assert sheet.area.height() == sheet.card.sizeHint().height()
    sheet.close()
