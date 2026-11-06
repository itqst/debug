"""完整故障闭环验收：原生 Windows 平台上用 QTest 点击真实按钮/列表/分段；用法 python journey.py 源码目录 输出目录

只替换 Windows 原生文件对话框（无法自动操作）为固定的中文路径，其余入口都走界面控件。任一检查失败时以退出码 1 结束
"""

import json
import os
import shutil
import sys
import time
import zipfile

os.environ["QT_QPA_PLATFORM"] = "windows"
# 重定向到文件时 Windows 默认按 GBK 输出，写“✕”会抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtGui import QInputMethodEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QPlainTextEdit, QPushButton  # noqa: E402

import dog_debug.app as A  # noqa: E402
import dog_debug.ui as U  # noqa: E402

out = sys.argv[2]
base = os.path.join(out, "验收 数据")
shutil.rmtree(base, ignore_errors=True)
os.makedirs(base)
db = os.path.join(base, "会话 库.sqlite3")
log = os.path.join(base, "相机 日志.log")
with open(log, "w", encoding="gbk") as handle:
    handle.write("12:00:00 [任务] 西4楼宇 local/robotdog/media/command {\"method\":\"take_photo\",\"tid\":\"日志tid\",\"data\":{}}\n"
                 "12:00:01 local/robotdog/media/ack {\"method\":\"take_photo\",\"tid\":\"日志tid\",\"data\":{\"result\":1,\"msg\":\"相机连接超时\"}}\n")
report, failures = [], []


def check(ok, text):
    line = ("通过 " if ok else "失败 ") + text
    beat["step"] = text
    report.append(line)
    print(line, flush=True)
    if not ok:
        failures.append(text)


app = QApplication([])
app.setStyle("Fusion")
app.setPalette(U.light_palette())
U.setup_fonts(app)
app.setStyleSheet(U.STYLE)
# step 记最近一次 check 的检查项，最长停顿发生时它就是停顿前刚完成的那一步，用来定位卡在哪
beat = {"last": time.monotonic(), "gap": 0.0, "step": "启动", "where": ""}


def tick():
    now = time.monotonic()
    if now - beat["last"] > beat["gap"]:
        beat["gap"], beat["where"] = now - beat["last"], beat["step"]
    beat["last"] = now


heart = QTimer()
heart.timeout.connect(tick)
heart.start(20)


def wait(cond, seconds=20):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return bool(cond())


def button(parent, text):
    found = [b for b in parent.findChildren(QPushButton) if b.text() == text and b.isVisible()]
    if not found:
        raise AssertionError(f"找不到按钮：{text}")
    return found[0]


def click(widget):
    QTest.mouseClick(widget, Qt.LeftButton)
    app.processEvents()


def click_row(table, row, double=False):
    rect = table.visualRect(table.model().index(row, 1))
    (QTest.mouseDClick if double else QTest.mouseClick)(table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    app.processEvents()


def click_nav(w, index):
    rect = w.nav.visualItemRect(w.nav.item(index))
    QTest.mouseClick(w.nav.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    app.processEvents()


def ime_type(widget, text):
    """中文按输入法提交的方式送进控件（QTest.keyClicks 遇到中文会让进程直接退出）"""
    event = QInputMethodEvent()
    event.setCommitString(text)
    QApplication.sendEvent(widget, event)
    app.processEvents()


def shot(w, name):
    app.processEvents()
    w.grab().save(os.path.join(out, f"journey_{name}.png"))


def on_dialog(match, action):
    """模态对话框弹出后执行 action(对话框)；用定时器轮询，因为 exec() 会阻塞调用方"""
    def poll():
        for d in app.topLevelWidgets():
            if isinstance(d, QDialog) and d.isVisible() and match(d):
                action(d)
                return
        QTimer.singleShot(100, poll)
    QTimer.singleShot(100, poll)


# 原生文件对话框无法自动操作，替换为固定的中文路径
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([log], ""))
target = os.path.join(base, "狗一号 诊断包.zip")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (target, ""))

try:
    w = A.Studio(db)
    w.resize(1480, 940)
    w.show()
    wait(w.isVisible)
    # 1 控制页：点“全部指令”，在弹出目录里点 take_photo，点“执行失败”分段，点“模拟发送”
    click_nav(w, w.CONTROL)
    click(w.control.picker_btn)
    check(wait(w.control.picker.isVisible), "1 点“全部指令”弹出指令目录")
    row = next(i for i in range(w.control.commands.count())
               if (w.control.commands.item(i).data(Qt.UserRole) or {}).get("method") == "take_photo")
    QTest.mouseClick(w.control.commands.viewport(), Qt.LeftButton, Qt.NoModifier, w.control.commands.visualItemRect(w.control.commands.item(row)).center())
    check(wait(lambda: not w.control.picker.isVisible() and w.control.selected["method"] == "take_photo"), "1 点选“拍照”后目录收起，当前指令为 take_photo")
    check(w.observer.isVisible(), "1 控制页同屏显示 3D 观察区")
    click(w.scenario.group.button(1))
    click(w.send_btn)
    check(wait(lambda: "相机连接超时" in w.result.text()), "1 控制页点“模拟发送”(执行失败)，本次结果显示“相机连接超时”")
    shot(w, "1_send")
    # 2 日志排障页点“选择文件…”导入 GBK 中文日志
    click_nav(w, w.LOGS)
    click(button(w.stack.currentWidget(), "选择文件…"))
    imported = wait(lambda: "导入完成" in w.import_info.toPlainText() and not w.logs.import_thread.is_alive() and w.inbox.empty(), 30)
    w._drain()
    record = next((l for l in w.import_info.toPlainText().splitlines() if "编码" in l), "")
    check(imported and "gb18030" in record, "2 日志排障页点“选择文件…”导入 GBK 日志，识别为 " + record.split("编码 ")[-1][:8])
    # 导入后就在日志排障页看：列表有这批的两行，点失败那行右边出详情
    check(wait(lambda: len(w.logs.log_model.rows) == 2) and w.stack.currentIndex() == w.LOGS, "2 导入后本页直接列出这一批 2 行")
    click_row(w.logs.log_table, 1)
    check(wait(lambda: "相机连接超时" in w.logs.log_detail.text()), "2 点日志表里的 ACK 行，右侧详情显示“相机连接超时”")
    shot(w, "2_logs")
    # 3 消息页按请求，失败置顶
    click_nav(w, w.MESSAGES)
    click(w.view_mode.group.button(0))
    wait(lambda: w.request_model.rows)
    rows = w.request_model.rows
    check(len(rows) >= 2 and all(r.status == "失败" for r in rows[:2]), f"3 按请求视图：{len(rows)} 个请求，失败排在前面")
    log_row = next(i for i, r in enumerate(rows) if r.events[0].tid == "日志tid")
    click_row(w.request_table, log_row)
    check(wait(lambda: "MEDIA 返回失败" in w.messages.details.text()), "4 单击失败请求，证据链结论卡为“MEDIA 返回失败”")
    shot(w, "2_request")
    # 5 点证据链里的 ACK 步骤，再点“原文附近”
    dialog_text = {}

    def grab_original(d):
        dialog_text.update(title=d.windowTitle(), text=d.findChild(QPlainTextEdit).toPlainText())
        d.grab().save(os.path.join(out, "journey_3_original.png"))
        d.reject()

    ack_step = [s for s in w.messages.details.findChildren(A.Step) if s.isVisible()][-1]
    QTest.mouseClick(ack_step, Qt.LeftButton)
    app.processEvents()
    on_dialog(lambda d: "原文附近" in d.windowTitle(), grab_original)
    click(button(w.messages.details, "原文附近"))
    check("第 2 行" in dialog_text.get("title", "") and "西4楼宇" in dialog_text.get("text", ""),
          "5 点“原文附近”：定位第 2 行，GBK 中文原文可读")
    # 6 双击请求进逐条，点筛选条回全部
    click_row(w.request_table, log_row, double=True)
    scoped = wait(lambda: w.views.currentIndex() == 1) and {(e.sn, e.session, e.tid) for _, e, _ in w.model.rows}
    check(scoped and len(scoped) == 1 and w.messages.scope_chip.isVisible(), "6 双击请求：逐条视图只剩这次请求，出现“仅此请求”筛选条")
    shot(w, "4_scoped")
    click(w.messages.scope_chip)
    check(w.scope is None and len(w.model.rows) > 2, f"7 点“仅此请求 ✕”回到全部：{len(w.model.rows)} 条")
    # 8 点“备注这次请求”，在对话框里逐字输入，点“保存”
    click(w.view_mode.group.button(0))
    wait(lambda: w.request_model.rows)
    log_row = next(i for i, r in enumerate(w.request_model.rows) if r.events[0].tid == "日志tid")
    click_row(w.request_table, log_row)

    def fill_note(d):
        edits = d.findChildren(QPlainTextEdit)
        for edit, text in zip(edits, ("拍照成功", "相机连接超时", "复现于西4楼宇，换网线后正常")):
            edit.setFocus()
            ime_type(edit, text)
        d.grab().save(os.path.join(out, "journey_5_note.png"))
        click(button(d, "保存"))

    on_dialog(lambda d: d.windowTitle() == "问题备注", fill_note)
    click(w.messages.note_btn)
    check(wait(lambda: "复现于西4楼宇" in w.messages.details.text())
          and w.request_model.data(w.request_model.index(log_row, 5)).startswith("【备注】"),
          "8 点“备注这次请求”，按输入法提交中文并点“保存”：证据链顶部出现备注卡，列表标【备注】")
    shot(w, "6_noted")
    # 9 点“导出诊断包”：等后台概览统计完，点“选择位置并导出”
    def accept_preview(d):
        if not d.confirm.isEnabled():
            QTimer.singleShot(100, lambda: accept_preview(d))
            return
        d.grab().save(os.path.join(out, "journey_7_preview.png"))
        click(d.confirm)

    on_dialog(lambda d: d.windowTitle() == "导出诊断包", accept_preview)
    click(button(w.stack.currentWidget(), "导出诊断包"))
    exported = wait(lambda: not w.export_running and os.path.exists(target), 60)
    notes, summary = [], ""
    if exported:
        with zipfile.ZipFile(target) as archive:
            notes = json.loads(archive.read("notes.json").decode("utf-8"))
            summary = archive.read("summary.md").decode("utf-8")
    check(exported and len(notes) == 1 and "【有备注】" in summary,
          f"9 点“导出诊断包”→ 概览 → “选择位置并导出”：{os.path.basename(target)} 含备注 {len(notes)} 条")
    # 10 点“检测链路”分段和“沙盒演示检测链路”，点第一行
    click(w.view_mode.group.button(2))
    click(button(w.stack.currentWidget(), "沙盒演示检测链路"))
    wait(lambda: w.messages.chain_model.rows)
    click_row(w.messages.chain_table, 0)
    check(wait(lambda: "失败在 上传终态" in w.messages.details.text()), f"10 检测链路：{len(w.messages.chain_model.rows)} 条，失败链排第一并指出“上传终态”")
    shot(w, "8_chain")
    check(beat["gap"] < 2.0, f"界面事件循环最长停顿 {beat['gap'] * 1000:.0f} ms，发生在“{beat['where'][:30]}”之后（含导入、导出、模态对话框）")
    w.close()
    app.processEvents()
    # 11 重开同一个库，点消息页和那条请求
    w2 = A.Studio(db)
    w2.resize(1480, 940)
    w2.show()
    wait(w2.isVisible)
    click_nav(w2, w2.MESSAGES)
    wait(lambda: w2.request_model.rows)
    row = next(i for i, r in enumerate(w2.request_model.rows) if r.events[0].tid == "日志tid")
    click_row(w2.request_table, row)
    check(wait(lambda: "复现于西4楼宇" in w2.messages.details.text()), "11 关窗后重开同一库：请求和备注都在")
    shot(w2, "9_reopen")
    w2.close()
except Exception as error:
    check(False, f"流程中断：{type(error).__name__}: {error}")

report.append(f"\n范围：入口均为 QTest 点击界面控件；中文输入用输入法提交事件（QInputMethodEvent）；"
              f"仅 Windows 原生文件对话框替换为固定路径 {base}")
with open(os.path.join(out, "journey_report.txt"), "w", encoding="utf-8") as handle:
    handle.write("\n".join(report) + "\n")
sys.exit(1 if failures else 0)
