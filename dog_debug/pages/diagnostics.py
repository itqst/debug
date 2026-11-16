"""诊断包导出：先在对话框里给出概览（消息、请求、设备、脱敏情况、备注），确认后选位置，大库在后台写"""

import sqlite3
import sys
import threading
import time

from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QVBoxLayout

from .. import __version__
from ..core import diagnostic_zip, export_preview
from ..ui import button, label


class Diagnostics:
    """导出诊断包；入口在日志排障页右上角"""

    def __init__(self, app):
        self.app = app
        self.export_running = False
        self.preview, self.export_dialog = None, None

    def _fill_preview(self, dialog, info):
        dialog.loading.hide()
        devices = "、".join(info["devices"][:6]) + ("…" if len(info["devices"]) > 6 else "") or "无"
        sources = "、".join(f"{k or '未知'} {v}" for k, v in info["sessions"].items()) or "无"
        for caption, text in (("消息", f"{info['events']} 条，全部写入"), ("请求", f"{info['requests']} 个"),
                              ("设备", devices), ("会话来源", sources),
                              ("已脱敏", f"{info['redacted']} 条消息含 [REDACTED]，入库时已处理，包里不会有原值"),
                              ("问题备注", f"{info['notes']} 条"),
                              ("不包含", "导入的原始日志文件、MQTT 密码")):
            row = QHBoxLayout()
            name = label(caption, "caption")
            name.setFixedWidth(64)
            row.addWidget(name)
            row.addWidget(label(text, wrap=True), 1)
            dialog.body.addLayout(row)
        if info["redacted_topics"]:
            dialog.body.addWidget(label("脱敏样例 Topic：" + "，".join(t or "无 Topic" for t in info["redacted_topics"]), "caption", True))
        dialog.confirm.setEnabled(True)
        dialog.info = info

    def export(self):
        """先给出导出概览（内容全部来自已脱敏的库），确认后再选保存位置

        库文件上的概览统计要扫全表，在后台只读连接里做，对话框先显示“正在统计…”；取消即中断统计
        """
        dialog = QDialog(self.app)
        self.export_dialog = dialog
        dialog.setWindowTitle("导出诊断包")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        layout.addWidget(label("导出诊断包", "heading"))
        dialog.body = QVBoxLayout()
        dialog.body.setSpacing(8)
        dialog.loading = label("正在统计本地数据…", "caption")
        dialog.body.addWidget(dialog.loading)
        layout.addLayout(dialog.body)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", dialog.reject))
        dialog.confirm = button("选择位置并导出", dialog.accept, "primary")
        dialog.confirm.setEnabled(False)
        actions.addWidget(dialog.confirm)
        layout.addSpacing(6)
        layout.addLayout(actions)
        if self.app.store.on_disk:
            self.app.store.flush()
            reader, token = self.app.store.reader(), object()
            self.preview = {"reader": reader, "token": token}

            def work():
                try:
                    self.app.inbox.put(("preview", token, export_preview(reader)))
                except sqlite3.OperationalError:
                    # 用户取消，统计被中断
                    pass
                finally:
                    reader.db.close()

            threading.Thread(target=work, daemon=True).start()
        else:
            self._fill_preview(dialog, export_preview(self.app.store))
        accepted = dialog.exec() == QDialog.Accepted
        if self.preview:
            try:
                self.preview["reader"].db.interrupt()
            except sqlite3.ProgrammingError:
                pass
        self.preview, self.export_dialog = None, None
        if not accepted:
            return
        name = time.strftime("dog-debug-%Y%m%d-%H%M%S.zip")
        path, _ = QFileDialog.getSaveFileName(self.app, "导出诊断包", name, "诊断包 (*.zip)")
        if not path:
            return
        if not self.app.store.on_disk:
            result = self.export_bundle(path)
            self.app.notify(f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求")
            return
        # 大库导出要几分钟，后台只读连接里写，界面照常可用
        self.app.store.flush()
        environment = self._environment()
        self.export_running = True
        self.app.notify("正在后台导出诊断包…")

        def work():
            reader = self.app.store.reader()
            try:
                result = diagnostic_zip(reader, path, environment)
                self.app.inbox.put(("export_done", f"诊断包已导出：{result['events']} 条消息，{result['listed']}/{result['requests']} 个请求"))
            except Exception as error:
                self.app.inbox.put(("export_done", "导出失败：" + str(error)))
            finally:
                reader.db.close()

        threading.Thread(target=work, daemon=True).start()

    def export_bundle(self, path):
        return diagnostic_zip(self.app.store, path, self._environment())

    def _environment(self):
        return {"version": __version__, "mode": self.app.mode, "platform": sys.platform,
                "broker": f"{self.app.mqtt.host}:{self.app.port.value()}" if self.app.mqtt.connected else "",
                "device_sn": self.app.mqtt.sn if self.app.mqtt.connected else self.app.sn.text().strip(),
                "model": self.app.scene.variant, "osd": self.app.scene.osd}

    def preview_done(self, token, rows):
        # 导出窗口已经关了或又开了一次，旧的预览结果不再填
        if self.preview and self.preview["token"] is token and self.export_dialog:
            self._fill_preview(self.export_dialog, rows)

    def export_done(self, note):
        self.export_running = False
        self.app.notify(note)
