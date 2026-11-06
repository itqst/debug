"""雷达点云：按协议解码、入库瘦身、沙盒 pcd_switch 链路、3D 场景与界面，不访问机器狗"""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from dog_debug import pointcloud
from dog_debug.app import Studio
from dog_debug.core import Event, Store, diagnose, load_event
from dog_debug.registry import find, make_command
from dog_debug.scene import Scene
from dog_debug.transport import simulate

# 《MQTT通信协议-点云数据pcd.md》第 4 节的校验样例
SAMPLE = {"ts": 1793611403947, "method": "pcd", "data": {
    "type": "pointcloud", "source": "/front_lidar", "frame_id": "livox_frame", "stamp": 1792403359509, "seq": 7,
    "encoding": "xyz_i16_i8", "scale": 0.001, "count": 3, "src_count": 20160, "leaf": 0.081,
    "points": "0gQ89iwBCiji4BUiC5owdf//MPj/"}}

# 3.1.1 起的新格式样例（协议第 4 节）：点 = 偏移 + o
SAMPLE_MAP = {"ts": 1793611403947, "method": "pcd", "data": {
    "type": "pointcloud", "source": "/front_lidar", "frame_id": "map", "stamp": 1792403359509, "seq": 7,
    "o_x": 12.5, "o_y": -3.2, "o_z": 0.45, "o_yaw": 1.5708,
    "encoding": "xyz_i16_i8", "scale": 0.001, "count": 3, "src_count": 20160, "leaf": 0.081,
    "points": "0gQ89iwBCiji4BUiC5owdf//MPj/"}}


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    win = Studio(":memory:")
    yield win
    win.close()
    app.processEvents()


def test_decode_matches_protocol_sample():
    cloud = pointcloud.decode(SAMPLE["data"], received=1.0)
    assert np.allclose(cloud.xyz, [(1.234, -2.5, 0.3), (-7.64, 5.6, 2.85), (30.0, -0.001, -2.0)], atol=1e-6)
    assert cloud.intensity.tolist() == [10, 154, 255]
    assert "points" not in cloud.meta and cloud.meta["seq"] == 7


def test_decode_map_sample_adds_origin():
    cloud = pointcloud.decode(SAMPLE_MAP["data"])
    assert cloud.in_map and cloud.origin == (12.5, -3.2, 0.45, 1.5708)
    assert np.allclose(cloud.xyz, [(13.734, -5.7, 0.75), (4.86, 2.4, 3.3), (42.5, -3.201, -1.55)], atol=1e-5)
    # 旧格式没有 o_x：雷达坐标系
    assert not pointcloud.decode(SAMPLE["data"]).in_map


@pytest.mark.parametrize("change", [{"o_y": None}, {"o_z": "1"}, {"o_yaw": "x"}])
def test_decode_rejects_partial_or_bad_origin(change):
    data = {k: v for k, v in {**SAMPLE_MAP["data"], **change}.items() if v is not None}
    with pytest.raises(pointcloud.CloudError, match="o_"):
        pointcloud.decode(data)


def test_demo_frame_map_format_keeps_room_fixed():
    # 新格式里房间固定在原点坐标系，雷达在走：两帧的 o 不同，但点（偏移 + o）的范围一样
    a, b = pointcloud.decode(pointcloud.demo_frame(1)), pointcloud.decode(pointcloud.demo_frame(40))
    assert a.in_map and b.in_map and a.origin[:2] != b.origin[:2]
    assert np.allclose(a.xyz.min(axis=0)[:2], b.xyz.min(axis=0)[:2], atol=0.1)
    assert not pointcloud.decode(pointcloud.demo_frame(1, in_map=False)).in_map


def test_map_accumulates_map_frames_and_dedupes_voxels():
    acc = pointcloud.MapAccumulator(voxel=0.1)
    a, b = pointcloud.decode(pointcloud.demo_frame(1)), pointcloud.decode(pointcloud.demo_frame(40))
    assert acc.add(a) and len(acc.xyz) > 0
    n1 = len(acc.xyz)
    assert acc.add(a) and len(acc.xyz) == n1          # 同一帧再来一次，体素都已有，不增加
    acc.add(b)
    assert len(acc.xyz) >= n1 and len(acc.trail) == 2 and acc.distance > 0.5
    keys = np.floor(acc.xyz / 0.1).astype(np.int64)
    assert len(np.unique(keys, axis=0)) == len(acc.xyz)   # 每个体素只留一个点
    assert not acc.add(pointcloud.decode(pointcloud.demo_frame(1, in_map=False)))   # 旧格式不累加
    acc.clear()
    assert len(acc.xyz) == 0 and acc.trail == []


def test_map_stops_growing_when_full():
    acc = pointcloud.MapAccumulator(voxel=0.05, max_points=1000)
    acc.add(pointcloud.decode(pointcloud.demo_frame(1)))
    assert acc.full
    n = len(acc.xyz)
    acc.add(pointcloud.decode(pointcloud.demo_frame(80)))
    assert len(acc.xyz) == n and len(acc.trail) == 2   # 满了不再加点，轨迹照记


def test_scene_builds_map_and_clear_resets_it():
    scene = Scene()
    scene.set_cloud(pointcloud.decode(pointcloud.demo_frame(3), received=1.0))
    scene.set_cloud(pointcloud.decode(pointcloud.demo_frame(30), received=1.5))
    assert len(scene.cloud_map.xyz) > 0 and len(scene.cloud_map.trail) == 2
    scene.clear_cloud()
    assert len(scene.cloud_map.xyz) == 0


def test_encode_roundtrip_and_scale_follows_farthest_point():
    xyz = np.array([(0.5, -1.25, 0.0), (40.0, 0.0, 1.0)])
    data = pointcloud.encode(xyz, [0, 300])
    # 40 m 超出 1 mm 的 int16 范围（32.767 m），换 2 mm；强度超过 255 截到 255
    assert data["scale"] == 0.002 and data["count"] == 2
    cloud = pointcloud.decode(data)
    assert np.allclose(cloud.xyz, xyz, atol=0.001) and cloud.intensity.tolist() == [0, 255]
    assert pointcloud.encode([(32.0, 2.0, 3.0)])["scale"] == 0.001


@pytest.mark.parametrize("change, reason", [({"encoding": "xyz_f32"}, "encoding"), ({"count": 4}, "count=4"),
                                            ({"points": "不是base64"}, "base64"), ({"scale": 0}, "scale")])
def test_decode_rejects_frames_it_cannot_trust(change, reason):
    with pytest.raises(pointcloud.CloudError, match=reason):
        pointcloud.decode({**SAMPLE["data"], **change})


def test_cloud_topics_are_recognised_and_others_are_not():
    data = SAMPLE["data"]
    assert pointcloud.cloud_data(Event("tta/robotdog/A/state", SAMPLE)) is data
    assert pointcloud.cloud_data(Event("local/robotdog/ros2bridge/data", {"sn": "A", "data": data})) is data
    assert pointcloud.cloud_data(Event("tta/robotdog/A/state", {"method": "mapping_status", "data": {}})) is None
    assert pointcloud.cloud_data(Event("local/robotdog/ros2bridge/heartbeat", {"data": {"enabled": True}})) is None


def test_store_keeps_metadata_but_not_raw_points():
    frame = pointcloud.demo_frame(1)
    store = Store()
    event = Event("local/robotdog/ros2bridge/data", {"sn": "A", "data": frame}, sn="A", session="s",
                  raw=json.dumps({"sn": "A", "data": frame}))
    store.add(event)
    body = store.db.execute("SELECT body FROM events").fetchone()[0]
    assert len(body) < 2000 and frame["points"][:40] not in body
    saved = load_event(body)
    assert saved.payload["data"]["count"] == frame["count"] and "未入库" in saved.payload["data"]["points"]
    assert saved.module == "ros2bridge" and saved.kind == "状态" and saved.method == "pointcloud"
    # 原事件不被改动，3D 仍能用原始点
    assert event.payload["data"]["points"] == frame["points"]
    cloud = Event("tta/robotdog/A/state", {"method": "pcd", "data": frame}, sn="A", session="s")
    assert Store.row(cloud)[3] == "ros2bridge"


def test_sandbox_pcd_switch_is_local_and_ack_result_is_on_root():
    events = simulate(make_command(find("pcd_switch")), "A", "s")
    topics = [e.topic for e in events]
    assert topics[:3] == ["local/robotdog/ros2bridge/command", "local/robotdog/ros2bridge/ack",
                          "local/robotdog/ros2bridge/heartbeat"]
    assert not any(t.endswith("/services") for t in topics)
    ack = events[1].payload
    assert ack["result"] == 0 and ack["data"] == {"enable": True} and diagnose(events[1]).status == "操作成功"
    assert events[2].payload["data"]["enabled"] is True
    clouds = [e for e in events if pointcloud.cloud_data(e) is not None]
    assert {e.topic for e in clouds} == {"local/robotdog/ros2bridge/data", "tta/robotdog/A/state"}
    off = make_command(find("pcd_switch"))
    off["data"]["enable"] = False
    events = simulate(off, "A", "s")
    assert events[2].payload["data"]["enabled"] is False and not any(pointcloud.cloud_data(e) for e in events)


def test_sandbox_pcd_switch_bad_field_fails_like_device():
    bad = make_command(find("pcd_switch"))
    bad["data"]["enable"] = 1
    ack = simulate(bad, "A", "s")[1]
    assert diagnose(ack).status == "失败" and "enable" in diagnose(ack).summary


def test_scene_counts_one_frame_once_and_reports_rate():
    app = QApplication.instance() or QApplication([])
    scene = Scene()
    frame = pointcloud.demo_frame(5)
    assert scene.set_cloud(pointcloud.decode(frame, received=10.0))
    # 本地 data 和 GW 转发的云端 pcd 是同一帧
    assert not scene.set_cloud(pointcloud.decode(frame, received=10.1))
    assert scene.set_cloud(pointcloud.decode(pointcloud.demo_frame(6), received=10.5))
    # 跨度不到 1 s 不报频率
    assert scene.cloud_rate == 0.0
    assert scene.set_cloud(pointcloud.decode(pointcloud.demo_frame(7), received=11.0))
    assert scene.cloud_rate == pytest.approx(2.0)
    scene.clear_cloud()
    assert scene.cloud is None and not scene.cloud_live
    app.processEvents()


def test_cloud_colors_by_height_and_intensity():
    from dog_debug.viewer import cloud_colors
    cloud = pointcloud.decode(pointcloud.demo_frame(2))
    for mode in ("高度", "强度"):
        colors = cloud_colors(cloud, mode, 0.5)
        assert colors.shape == (len(cloud.xyz), 4) and colors.dtype == np.float32
        assert np.all((colors >= 0) & (colors <= 1)) and np.allclose(colors[:, 3], 0.5)
    low, high = np.argmin(cloud.xyz[:, 2]), np.argmax(cloud.xyz[:, 2])
    by_height = cloud_colors(cloud, "高度")
    assert by_height[low, 2] > by_height[low, 0] and by_height[high, 0] > by_height[high, 2]


def test_sandbox_switch_shows_cloud_and_card(window):
    window.missions.cloud_state.setText("")
    assert window._quick_send("pcd_switch", {"enable": True})
    assert window.scene.cloud is not None and window.scene.cloud_live
    assert window.cloud_pill.text() == "点云 · 实时" and "点" in window.cloud_info.text()
    assert "采集中" in window.missions.cloud_state.text() and "操作成功" in window.missions.cloud_ack.text()
    assert window.quick.topic == "local/robotdog/ros2bridge/command"
    window._quick_send("pcd_switch", {"enable": False})
    assert "未采集" in window.missions.cloud_state.text()


def test_pcd_switch_goes_to_local_topic_in_every_mode(window):
    item = find("pcd_switch")
    for text in ("云端 → 真实 GW", "直连模块", "沙盒 · 全链路模拟"):
        window.mode_box.setCurrentText(text)
        assert window._topic_for(item) == "local/robotdog/ros2bridge/command"
    window.mode_box.setCurrentText("直连模块")
    # 没连 Broker 时照常报错、不排队
    assert not window._quick_send("pcd_switch", {"enable": True})


def test_only_current_device_cloud_drives_3d(window):
    window.mode_box.setCurrentText("直连模块")
    frame = pointcloud.demo_frame(1)
    window._ingest(Event("local/robotdog/ros2bridge/data", {"sn": "A", "data": frame}, sn="A", session="old", source="mqtt"))
    window._ingest(Event("local/robotdog/ros2bridge/data", {"sn": "A", "data": frame}, sn="A", session="s", source="sandbox"))
    assert window.scene.cloud is None
    # 入库照常（瘦身后）
    assert window.store.count() == 2


def test_bad_frame_is_reported_once_and_still_stored(window):
    bad = {**SAMPLE["data"], "count": 99}
    notes = []
    window.notify = notes.append
    for _ in range(3):
        window._ingest(Event("tta/robotdog/A/state", {"method": "pcd", "data": bad}, sn="A", session=window.session, source="sandbox"))
    assert len(notes) == 1 and "count=99" in notes[0] and window.scene.cloud is None
    assert window.store.count() == 3
    window._ingest(Event("tta/robotdog/A/state", SAMPLE, sn="A", session=window.session, source="sandbox"))
    window._ingest(Event("tta/robotdog/A/state", {"method": "pcd", "data": bad}, sn="A", session=window.session, source="sandbox"))
    assert len(notes) == 2 and window.scene.cloud is not None
