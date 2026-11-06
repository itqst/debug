"""无人机页截图：参数 源码目录 输出目录 宽 高 dark|light；把板上抓包（真飞和指令测试）当成刚收到的实时消息灌进去，再加一条残留的保留指令"""
import json
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from PySide6.QtCore import QEvent
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
w.mqtt.connected, w.mqtt.device, w.mqtt.session, w.mqtt.host = True, "uav", "shot", "192.168.1.230"
w.mqtt.client = object()
data = Path(sys.argv[1]) / "tests" / "data" / "uav"
for name in ("flight.jsonl", "commands.jsonl"):
    rows = [json.loads(l) for l in (data / name).read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if "topic" in r]
    first = rows[0].get("t", rows[0].get("mono"))
    last = rows[-1].get("t", rows[-1].get("mono"))
    for r in rows:
        # 抓包里的相对时间平移到“刚刚”，最后一条就是现在
        at = time.time() - (last - r.get("t", r.get("mono")))
        w._ingest(Event(r["topic"], r["body"], sn="", session="shot", retained=r["retain"], timestamp=at))
w._ingest(Event("local/uav/vfc/command", {"method": "start_multi_nav_by_points", "tid": "t-old", "bid": "task-0"},
                session="shot", retained=True))
w.store.flush()
# 窗口刚显示还没排版时切页，切页动画拿到的旧页截图和位置都不对，先让它排好
end = time.monotonic() + 0.5
while time.monotonic() < end:
    app.processEvents()
w._go(w.UAV)
end = time.monotonic() + 1.5
while time.monotonic() < end:
    app.processEvents()
# 切页动画用 deleteLater 收掉旧页截图，processEvents 不处理延迟删除，这里补上
app.sendPostedEvents(None, QEvent.DeferredDelete)
w.uav.render()
app.processEvents()
w.grab().save(os.path.join(sys.argv[2], f"uav_{sys.argv[3]}_{sys.argv[5]}.png"))
w.mqtt.client = None
