"""状态页模块详情截图：参数 源码目录 输出目录 宽 高 dark|light；沙盒里发几条 MEDIA 指令、再来一条只有回复的，截“指令与回复”列表和点开后的整条请求"""
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
import dog_debug.ui as U
from dog_debug.core import Event

app = QApplication([])
app.setStyle("Fusion")
U.setup_fonts(app)
U.apply_theme(app, sys.argv[5] == "dark")
w = A.Studio(":memory:")
w.resize(int(sys.argv[3]), int(sys.argv[4]))
w.show()


def settle(seconds=0.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()


for index, sn in ((0, "001"), (1, "002")):
    w._ingest(Event("local/robotdog/media/state", {"type": "stream_status", "info": {"index": index, "sn": sn, "type": "video", "result": 1}},
                    sn="TEST_DOG_001", session=w.session))
w._quick_send("take_photo", {})
w.scenario.set_index(1)
w._quick_send("camera_recording", {"state": 1})
w.scenario.set_index(2)
w._quick_send("camera_livestream", {"state": 1})
w.scenario.set_index(0)
w._ingest(Event("local/robotdog/media/ack", {"method": "camera_focal_length_set", "tid": "only-ack", "data": {"result": 0}},
                sn="TEST_DOG_001", session=w.session))
w._go(w.STATUS)
settle(1.0)
w.status._open_module("media")
w.status.detail_view.set_index(1)
settle()
w.grab().save(os.path.join(sys.argv[2], f"status_cmd_{sys.argv[3]}_{sys.argv[5]}.png"))
row = next(i for i, (e, *_) in enumerate(w.status.detail_model.rows) if e.method == "camera_recording")
w.status.detail_table.selectRow(row)
w.status._detail_open(w.status.detail_model.index(row, 0))
settle()
w.grab().save(os.path.join(sys.argv[2], f"status_trace_{sys.argv[3]}_{sys.argv[5]}.png"))
