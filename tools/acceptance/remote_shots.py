"""远程连接和设备日志入口截图：参数 源码目录 输出目录 宽 高 dark|light；不连设备，只看直连模块 + 经远程（FRP）的连接区和日志页页头"""
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
import dog_debug.app as A

app = QApplication([])
app.setStyle("Fusion")
A.setup_fonts(app)
A.apply_theme(app, sys.argv[5] == "dark")
w = A.Studio(":memory:")
w.resize(int(sys.argv[3]), int(sys.argv[4]))
w.show()
w.mode_box.setCurrentText("直连模块")
w.sn.setText("10300262998249")
w.frp.setChecked(True)
w.ssh_user.setText("tta")
w._go(w.LOGS)
# 侧栏在矮窗口里会滚动，滚到底露出连接区
from PySide6.QtWidgets import QScrollArea
side = next(a for a in w.findChildren(QScrollArea) if a.widget() and a.widget().isAncestorOf(w.connect_btn))
deadline = time.monotonic() + 1.5
while time.monotonic() < deadline:
    app.processEvents()
side.verticalScrollBar().setValue(side.verticalScrollBar().maximum())
deadline = time.monotonic() + 0.3
while time.monotonic() < deadline:
    app.processEvents()
w.grab().save(os.path.join(sys.argv[2], f"remote_{sys.argv[3]}_{sys.argv[5]}.png"))
