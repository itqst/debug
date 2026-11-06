"""深色外观逐页截图：参数 源码目录 输出目录 [宽 高]；只在本进程切深色，不改本机保存的外观设置"""
import math
import sys
import time
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
import dog_debug.ui as U
from dog_debug.core import Event

app = QApplication([])
app.setStyle("Fusion")
U.setup_fonts(app)
U.apply_theme(app, True)
w = A.Studio(":memory:")
w.resize(int(sys.argv[3]) if len(sys.argv) > 3 else 1480, int(sys.argv[4]) if len(sys.argv) > 4 else 940)
w.show()

def prepare():
    w.control._demo()
    w._go(w.CONTROL); w.control.scenario.set_index(1); w.control._send(); w.control.scenario.set_index(0)
    w.mode_box.setCurrentText("云端 → 真实 GW")
    w.mqtt.connected, w.mqtt.sn, w.mqtt.session = True, "TEST_DOG_001", "s"
    for i in range(12):
        w._ingest(Event("tta/robotdog/TEST_DOG_001/osd", {"x": 0.1 * i, "y": 0.0, "heading": 30 + 5 * i, "speed": 0.3 + 0.1 * math.sin(i),
                                                          "batteryRemain": 80 - i * 0.2, "temperature": 40 + math.sin(i)},
                        sn="TEST_DOG_001", session="s"))
    w._go(w.REMOTE)

def capture():
    for i in range(len(w.PAGES)):
        w._go(i)
        # 等切页动画（260 ms）放完再截，否则新页还是半透明
        settle = time.monotonic() + 0.4
        while time.monotonic() < settle:
            app.processEvents()
        w.grab().save(f"{sys.argv[2]}/dark{i}.png")
    w.close(); app.quit()

QTimer.singleShot(300, prepare)
QTimer.singleShot(3000, capture)
app.exec()
