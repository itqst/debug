"""逐页截图：参数 源码目录 输出目录 [宽 高 前缀]"""
import sys
import time
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
if len(sys.argv) > 4:
    w.resize(int(sys.argv[3]), int(sys.argv[4]))
w.show()
out = sys.argv[2]
prefix = sys.argv[5] if len(sys.argv) > 5 else "page"

def prepare():
    w.control._demo()
    w.suite._run_suite()
    w.store.add(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": "1", "result": 1, "msg": "pushing"}}, sn="TEST_DOG_001", session="s", module="media"))
    w.store.flush()
    w.mission.missions.send("get_map_list", {}); w.mission.missions.send("get_media_filelist", {})
    w.mission.missions.send("ir_metering_point_set", {"x": 0.5, "y": 0.5})
    w._go(w.CONTROL); w.control.scenario.set_index(1); w.control._send(); w.control.scenario.set_index(0)
    w._quick_send("stand_up", {})
    w._ingest(Event("tta/robotdog/TEST_DOG_001/osd", {"x": 0.0, "y": 0.0, "heading": 0, "batteryRemain": 76, "speed": 0.0}))
    w._go(w.REMOTE)

def capture():
    for i in range(len(w.PAGES)):
        w._go(i)
        # 等切页动画（260 ms）放完再截，否则新页还是半透明
        settle = time.monotonic() + 0.4
        while time.monotonic() < settle:
            app.processEvents()
        w.grab().save(f"{out}/{prefix}{i}.png")
    w.close(); app.quit()

QTimer.singleShot(300, prepare)
QTimer.singleShot(2500, capture)
app.exec()
