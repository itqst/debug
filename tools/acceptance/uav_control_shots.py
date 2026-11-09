"""控制调试页无人机模式截图：参数 源码目录 输出目录 宽 高 dark|light；假客户端，不连真实 Broker"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
import dog_debug.ui as U
from dog_debug.core import Event
app = QApplication([]); app.setStyle("Fusion"); U.setup_fonts(app); U.apply_theme(app, sys.argv[5] == "dark")
w = A.Studio(":memory:"); w.resize(int(sys.argv[3]), int(sys.argv[4])); w.show()
class Stub:
    def publish(self, topic, text, qos, retain): return type("R", (), {"rc": 0})()
w.mqtt.connected, w.mqtt.device, w.mqtt.session, w.mqtt.host, w.mqtt.client = True, "uav", "shot", "192.168.1.230", Stub()
w.device_box.setCurrentText("无人机"); w.mode_box.setCurrentText("直连模块")
end = time.monotonic() + 0.5
while time.monotonic() < end: app.processEvents()
w._go(w.CONTROL)
w.control._open_command("start_multi_nav_by_points")
w.uav_send.setChecked(True)
w.control._send()
tid = json.loads(w.control.editor.toPlainText())["tid"]
w._ingest(Event("local/uav/vfc/ack", {"method": "start_multi_nav_by_points", "tid": tid, "bid": "", "result": 1,
                "msg": "system.start 被拒绝：rejected BAD_WAYPOINTS（waypoint_num must be 1..50）"}, sn="", session="shot"))
w.store.flush(); w._refresh_tail()
end = time.monotonic() + 1.5
while time.monotonic() < end: app.processEvents()
app.sendPostedEvents(None, QEvent.DeferredDelete); app.processEvents()
w.grab().save(os.path.join(sys.argv[2], f"uav_control_{sys.argv[3]}_{sys.argv[5]}.png"))
w.mqtt.client = None
