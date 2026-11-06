"""日志排障页截图：参数 源码目录 输出目录 宽 高 dark|light；导入一份普通文本和 JSON 混合的日志后点开一行"""
import os
import sys
import tempfile
import time
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
import dog_debug.app as A
import dog_debug.ui as U

app = QApplication([])
app.setStyle("Fusion")
U.setup_fonts(app)
dark = sys.argv[5] == "dark"
U.apply_theme(app, dark)
w = A.Studio(":memory:")
w.resize(int(sys.argv[3]), int(sys.argv[4]))
w.show()
log = os.path.join(tempfile.mkdtemp(), "tta_media 现场.log")
# 用今天的日期：截图里选的时间段是“今天 0 点到现在”
day = time.strftime("%Y-%m-%d")
with open(log, "w", encoding="utf-8") as out:
    for i in range(40):
        out.write(f"{day} 10:{i // 60:02d}:{i % 60:02d}.120 [I] media 推流心跳 index=1 bitrate={1800 + i}kbps\n")
        if i == 12:
            out.write(day + ' 10:00:12.300 local/robotdog/media/command {"method":"camera_livestream","tid":"L1","data":{"index":1,"state":1}}\n')
            out.write(day + ' 10:00:12.410 local/robotdog/media/ack {"method":"camera_livestream","tid":"L1","data":{"result":1,"msg":"直播推流失败"}}\n')
            out.write(f"{day} 10:00:12.500 [E] media rtmp connect error: connection refused\n")
w.logs.start_import([log])
w.logs.import_thread.join(10)
for _ in range(30):
    w._drain()
    app.processEvents()
# 再选“选时间段…”，看起止两格在窄窗里挤不挤
w.logs.log_period.setCurrentText("选时间段…")
for _ in range(10):
    w._drain()
    app.processEvents()
row = next(i for i, (_, e, _) in enumerate(w.logs.log_model.rows) if e.tid == "L1" and e.kind == "ACK")
w.logs.log_table.selectRow(row)
w.logs._log_inspect(w.logs.log_model.index(row, 0))
deadline = time.monotonic() + 0.6
while time.monotonic() < deadline:
    app.processEvents()
w.grab().save(os.path.join(sys.argv[2], f"logs_{sys.argv[3]}_{sys.argv[5]}.png"))
