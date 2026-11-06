"""验证 URDF 加载、正运动学和 3D 场景状态，不创建 OpenGL"""

import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dog_debug import robot
from dog_debug.core import Event
from dog_debug.scene import Scene, osd_fields


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("variant,movable", [("L2 点足运动版", 12), ("L2W 轮足运动版", 16)])
def test_models_load_with_all_meshes(variant, movable):
    r = robot.load(variant)
    assert len(r.links) == 17 and len(r.joints) == 16 and len(r.movable) == movable
    assert all(link.mesh is not None for link in r.links.values())
    assert sum(link.mesh.shape[0] for link in r.links.values()) > 500000


def test_stand_pose_puts_feet_under_hips():
    r = robot.load("L2 点足运动版")
    world = r.poses(robot.pose_angles(r, "站立"))
    for leg in ("FBL", "FAR", "RBL", "RAR"):
        hip, foot = world[leg + "_HIP_LINK"][:3, 3], world[leg + "_FOOT_LINK"][:3, 3]
        assert abs(foot[0] - hip[0]) < 0.05 and foot[2] < hip[2] - 0.25
    assert robot.ground_height(r, robot.pose_angles(r, "趴下")) < robot.ground_height(r, robot.pose_angles(r, "站立"))


def test_joint_angles_clamped_to_limits():
    r = robot.load("L2 点足运动版")
    knee = next(j for j in r.movable if j.name == "FBL_KNEE_JOINT")
    assert robot.pose_angles(r, "零位")["FBL_KNEE_JOINT"] == knee.upper


def test_osd_fields_from_cloud_and_local():
    cloud = Event("tta/robotdog/A/osd", {"x": 1.0, "heading": 30, "batteryRemain": 50, "sn": "A"})
    local = Event("local/robotdog/vfc/data", {"data": {"y": -1.2, "speed": 0.3}})
    assert osd_fields(cloud) == {"x": 1.0, "heading": 30, "batteryRemain": 50}
    assert osd_fields(local) == {"y": -1.2, "speed": 0.3}
    assert osd_fields(Event("local/robotdog/media/state", {"x": 1})) == {}


def test_commands_drive_demo_and_osd_overrides(app):
    scene = Scene()
    scene.set_variant("L2 点足运动版")
    scene.command("passive", {})
    for _ in range(30):
        scene.last -= 0.05
        scene.tick()
    assert scene.pose_name == "趴下" and scene.animation is None
    scene.command("move", {"vx": 0.5, "vy": 0, "yaw_rate": 0})
    scene.last -= 0.1
    scene.tick()
    assert scene.x > 0 and scene.phase > 0
    scene.telemetry(Event("tta/robotdog/A/osd", {"x": 3.0, "y": 4.0, "heading": 90}))
    assert scene.live and (scene.x, scene.y) == (3.0, 4.0) and math.isclose(scene.heading, math.pi / 2)
    scene.last -= 0.1
    scene.tick()
    assert (scene.x, scene.y) == (3.0, 4.0)
    scene.timer.stop()


def live_studio():
    from dog_debug.app import Studio
    window = Studio(":memory:")
    window.mqtt.connected, window.mqtt.sn, window.mqtt.session = True, "TEST_DOG_001", "live"
    return window


def osd(sn="TEST_DOG_001", session="live", source="mqtt", **fields):
    return Event(f"tta/robotdog/{sn}/osd", fields, source=source, sn=sn, session=session)


def test_studio_forwards_sent_commands_to_scene(app):
    from dog_debug.app import Studio
    window = Studio(":memory:")
    window._quick_send("stand_up", {})
    assert window.scene.pose_name == "站立"
    window.close()


def test_current_device_osd_drives_card(app):
    window = live_studio()
    window._ingest(osd(batteryRemain=61))
    assert "电量 61%" in window.device_meta.text() and "OSD 实时" in window.device_meta.text()
    window.mqtt.connected = False
    window.close()


@pytest.mark.parametrize("event", [
    osd("OTHER_DOG", "live", x=88, y=99, batteryRemain=4),
    osd(session="foreign", x=88, y=99, batteryRemain=4),
    osd(source="log:abc", session="old-log", x=88, y=99, batteryRemain=4),
    osd(source="sandbox", x=88, y=99, batteryRemain=4),
])
def test_foreign_or_replayed_osd_is_stored_not_live(app, event):
    window = live_studio()
    window._ingest(event)
    window.store.flush()
    assert window.store.query(text="88")
    assert not window.scene.osd and (window.scene.x, window.scene.y) == (0.0, 0.0)
    assert "实时" not in window.device_meta.text()
    window.mqtt.connected = False
    window.close()


def test_live_mark_expires_without_new_messages(app, monkeypatch):
    import dog_debug.scene as scene_module
    window = live_studio()
    monkeypatch.setattr(scene_module.time, "monotonic", lambda: 1000.0)
    window._ingest(osd(batteryRemain=50))
    assert "OSD 实时" in window.scene3d.position_pill.text()
    monkeypatch.setattr(scene_module.time, "monotonic", lambda: 1004.0)
    window._drain()
    assert "演示" in window.scene3d.position_pill.text() and "OSD 已停" in window.device_meta.text()
    window.mqtt.connected = False
    window.close()


def test_new_connection_drops_old_live_osd(app):
    window = live_studio()
    window._ingest(osd(batteryRemain=50))
    window.scene.reset_telemetry()
    assert not window.scene.live
    window.mqtt.connected = False
    window.close()


def test_bad_move_fields_still_send_and_demo_ignores(app):
    from dog_debug.app import Studio
    window = Studio(":memory:")
    payload = {"method": "move", "target": "vfc", "tid": "raw-move-bad", "data": {"vx": "bad", "vy": 0, "yaw_rate": 0}}
    sent = window._emit(payload, "{}", "tta/robotdog/TEST_DOG_001/services", "vfc")
    assert sent is not None and window.scene.velocity == (0.0, 0.0, 0.0)
    assert "不是数字" in window.demo_note
    assert window.scene.command("move", ["not", "dict"]) and window.scene.command("move", {"vx": True})
    window.close()


def test_vfc_osd_with_nested_data_drives_scene():
    from dog_debug.core import Event
    from dog_debug.scene import Scene, osd_fields
    # 与 VFC 源码一致：根上 sn/ts，遥测在 data 里
    e = Event("local/robotdog/vfc/osd", {"sn": "DOG", "ts": 1, "data": {"sn": "DOG", "x": 1.25, "y": -0.5, "heading": 40,
                                                                          "batteryRemain": 50, "speed": 0.3}})
    assert osd_fields(e) == {"x": 1.25, "y": -0.5, "heading": 40, "speed": 0.3, "batteryRemain": 50}
    assert e.method == "osd"
    scene = Scene()
    scene.telemetry(e)
    assert (scene.x, scene.y, scene.osd["batteryRemain"]) == (1.25, -0.5, 50) and scene.live
    flat = Event("tta/robotdog/DOG/osd", {"x": 2.0, "y": 1.0})
    assert osd_fields(flat) == {"x": 2.0, "y": 1.0}
