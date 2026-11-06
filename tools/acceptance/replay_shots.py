"""造一段带 OSD 和指令/ACK 的日志会话，打开回放拖到中间截图；参数 源码目录 输出目录 [宽 高]"""
import math
import sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
import dog_debug.ui as U
from dog_debug.core import Event

app = QApplication([])
app.setStyle("Fusion"); app.setPalette(U.light_palette())
U.setup_fonts(app)
app.setStyleSheet(U.STYLE)
w = A.Studio(":memory:")
w.resize(int(sys.argv[3]) if len(sys.argv) > 3 else 1480, int(sys.argv[4]) if len(sys.argv) > 4 else 940)
w.show()
for i in range(120):
    t = i * 0.1
    clock = f"12:00:{int(t):02d}.{int(t * 10) % 10}"
    w.store.add(Event("tta/robotdog/DOG_A/osd", {"x": 1.2 * math.sin(t / 3), "y": 0.08 * i, "heading": 90 + 20 * math.sin(t / 2),
                                                 "speed": 0.4, "batteryRemain": 80}, sn="DOG_A", session="日志-相机", source="log:相机.log",
                      source_time=clock))
    if i == 58:
        w.store.add(Event("tta/robotdog/DOG_A/services", {"method": "take_photo", "tid": "p1", "data": {"index": 1}}, sn="DOG_A",
                          session="日志-相机", source="log:相机.log", source_time=clock))
    if i == 60:
        w.store.add(Event("local/robotdog/media/ack", {"method": "take_photo", "tid": "p1", "data": {"result": 1, "msg": "相机连接超时"}},
                          sn="DOG_A", session="日志-相机", source="log:相机.log", source_time=clock))

def start():
    w.store.flush()
    w._background(lambda r: r.osd_frames("DOG_A", "日志-相机"), "replay_frames", "DOG_A", "日志-相机")
    QTimer.singleShot(600, seek)

def seek():
    w.scene3d.replay_slider.setValue(61)
    QTimer.singleShot(900, shot)

def shot():
    w.grab().save(f"{sys.argv[2]}/replay.png")
    w.close(); app.quit()

QTimer.singleShot(2500, start)
app.exec()
