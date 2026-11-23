"""喂一段模拟 OSD 后截遥控页：看遥测走势线和 3D 罗盘；参数 源码目录 输出目录 [宽 高]"""
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
# 沙盒不驱动实时状态，切到真实 GW 模式并伪装连接，让 OSD 按当前设备处理
w.mode_box.setCurrentText("云端 → 真实 GW")
w.mqtt.connected, w.mqtt.sn, w.mqtt.session = True, "TEST_DOG_001", "s"
state = {"i": 0}

def feed():
    i = state["i"]
    state["i"] += 1
    t = i * 0.6
    w._ingest(Event("tta/robotdog/TEST_DOG_001/osd", {"x": 0.8 * math.sin(t / 4), "y": 0.4 * t / 6, "heading": (35 + 8 * t) % 360,
              "speed": 0.35 + 0.25 * math.sin(t), "batteryRemain": 86 - t * 0.4, "temperature": 41 + 1.5 * math.sin(t / 2),
              "functionMode": 1, "controller": 1, "flyingTime": 300 + int(t)}, sn="TEST_DOG_001", session="s"))
    if state["i"] < 26:
        QTimer.singleShot(520, feed)
    else:
        QTimer.singleShot(600, shot)

def shot():
    w.grab().save(f"{sys.argv[2]}/osd.png")
    w.close(); app.quit()

QTimer.singleShot(2500, feed)
app.exec()
