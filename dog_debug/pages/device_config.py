"""设备配置页：经 SSH 列出设备 /oem/ttaviation/gateway/db 下的配置文件，点一个看内容；只读"""

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit

from ..remote import CONFIG_ROOT
from ..ui import button, card, label, Switch

# 再大的文件不在界面里整段显示，免得卡住；另存到本机仍是完整的
SHOW_LIMIT = 2 * 1024 * 1024


class DeviceConfigPage:
    """设备配置：左边文件列表，右边内容；读取都在后台线程，结果经 inbox 回来"""

    def __init__(self, app):
        self.app = app
        self.files, self.current, self.content, self.busy, self.listed_root = [], "", b"", False, CONFIG_ROOT

    def _config_card(self):
        """日志与配置页右边的设备配置卡：目录和筛选、文件列表、内容；经 SSH 只读"""
        page, layout = card(8, 16)
        head = QHBoxLayout()
        head.addWidget(label("设备配置（SSH 只读）", "sectionTitle"))
        head.addStretch()
        head.addWidget(button("刷新列表", self.refresh, "primary"))
        layout.addLayout(head)
        self.root = QLineEdit(CONFIG_ROOT)
        self.root.setToolTip("要看的目录，含子目录；回车刷新")
        self.root.returnPressed.connect(self.refresh)
        layout.addWidget(self.root)
        self.filter = QLineEdit()
        self.filter.setObjectName("search")
        self.filter.setPlaceholderText("按文件名筛选，如 media、cfg")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._fill_list)
        layout.addWidget(self.filter)
        self.note = label("还没读取；在连接设置里填好 SSH 账号后点“刷新列表”", "caption", True)
        layout.addWidget(self.note)
        self.list = QListWidget()
        # 和指令目录一样的圆角选中行、无边框
        self.list.setObjectName("commands")
        self.list.setMaximumHeight(190)
        self.list.currentItemChanged.connect(lambda item, _: item and self._open(item.data(Qt.UserRole)))
        layout.addWidget(self.list)
        head = QHBoxLayout()
        self.title = label("", "sectionTitle")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.title, 1)
        self.pretty = Switch("格式化 JSON")
        self.pretty.toggled.connect(lambda _: self._show())
        head.addWidget(self.pretty)
        layout.addLayout(head)
        actions = QHBoxLayout()
        actions.addWidget(button("重新读取", lambda: self.current and self._open(self.current)))
        actions.addWidget(button("复制", lambda: QApplication.clipboard().setText(self.text.toPlainText())))
        actions.addWidget(button("另存到本机…", self._save))
        actions.addStretch()
        layout.addLayout(actions)
        self.info = label("", "caption", True)
        layout.addWidget(self.info)
        self.text = QPlainTextEdit()
        self.text.setObjectName("code")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.text, 1)
        return page

    def _background(self, kind, work):
        """开一条 SSH 跑 work(remote)，结果放进 inbox；已经远程连着就用那条，否则按当前填写临时连一次"""
        if not self.app.ssh_user.text().strip():
            self.app.notify("要先在连接设置里填 SSH 账号")
            return False

        def run():
            remote, own = None, False
            try:
                remote, own = self.app.logs._ssh_for_logs()
                self.app.inbox.put((kind, work(remote), None))
            except Exception as error:
                # 连不上、认证失败、sudo 不让读等，原样说明
                self.app.inbox.put((kind, None, str(error)))
            finally:
                if own and remote:
                    remote.close()

        threading.Thread(target=run, daemon=True).start()
        return True

    def refresh(self):
        if self.busy:
            return
        root = self.root.text().strip() or CONFIG_ROOT
        if self._background("config_list", lambda remote: (root, remote.list_files(root))):
            self.busy = True
            self.note.setText(f"正在列 {root} …")

    def list_done(self, value, error):
        self.busy = False
        if error:
            self.note.setText(f"列不出文件：{error}")
            return
        root, self.files = value
        self.listed_root = root
        self.note.setText(f"{root} 下共 {len(self.files)} 个文件 · {time.strftime('%H:%M:%S')} 读取")
        self._fill_list()

    def _fill_list(self):
        needle = self.filter.text().strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        for name in self.files:
            if needle in name.lower():
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, name)
                self.list.addItem(item)
                if name == self.current:
                    self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _open(self, name):
        path = f"{self.listed_root.rstrip('/')}/{name}"
        if self._background("config_file", lambda remote: (name, remote.read_bytes(path))):
            self.current = name
            self.title.setText(name)
            self.info.setText(f"正在读 {path} …")

    def file_done(self, value, error):
        if error:
            self.info.setText(f"读不了：{error}")
            self.text.clear()
            return
        name, self.content = value
        if name != self.current:
            # 读的过程中又点了别的文件
            return
        self.read_at = time.strftime("%H:%M:%S")
        self._show()

    def _show(self):
        if not self.current or not hasattr(self, "read_at"):
            return
        data, size = self.content, len(self.content)
        where = f"{self.listed_root.rstrip('/')}/{self.current} · {size} 字节 · {self.read_at} 读取"
        if b"\x00" in data[:8192]:
            # 数据库这类二进制文件当文字显示是一堆乱码
            self.info.setText(where + " · 二进制文件，不显示内容，可以另存到本机")
            self.text.clear()
            return
        text = data[:SHOW_LIMIT].decode("utf-8", "replace")
        note = ""
        if self.pretty.isChecked():
            try:
                text = json.dumps(json.loads(data.decode("utf-8")), ensure_ascii=False, indent=2)
            except ValueError:
                note = " · 不是 JSON，按原文显示"
        if size > SHOW_LIMIT:
            note += f" · 只显示前 {SHOW_LIMIT // 1024 // 1024} MB，完整内容另存到本机看"
        self.info.setText(where + note)
        self.text.setPlainText(text)

    def _save(self):
        if not self.content:
            self.app.notify("先选一个文件")
            return
        path, _ = QFileDialog.getSaveFileName(self.app, "另存配置文件", str(Path.home() / Path(self.current).name))
        if path:
            Path(path).write_bytes(self.content)
            self.app.notify(f"已保存到 {path}")
