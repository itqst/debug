"""一次请求的证据链视图：结论卡、时间线和所选消息"""

import html
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget,
)

from .core import diagnose, occurred, payload_diff, summarize
from .ui import button, dot, FAILED, GRAY, label, ORANGE, pill, RED, tint, tone


class Step(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()


class TraceView(QScrollArea):
    """一次请求的证据链：顶部结论卡、时间线、所选消息 JSON；点时间线切换所选消息

    设了 store 后列出其他来源中相同 tid 的记录，点了原地换成那一组，可以回到原来那组；切换后发 switched(请求键)。
    nearby_logs 为真时多一个“这段时间的设备日志”，点了发 nearby(当前这组事件)
    """

    original = Signal(object)
    switched = Signal(object)
    nearby = Signal(object)

    def __init__(self, placeholder):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.placeholder = placeholder
        self.events, self.selected, self.lines, self.note, self.headline = [], None, [], None, None
        # previous(event) 返回上一条同类消息时，所选消息下方列出和它相比变了哪些字段
        self.previous = None
        self.store, self.nearby_logs, self.origin = None, False, None
        self.show_empty()

    def text(self):
        """当前展示的全部文字，供测试和复制使用"""
        return "\n".join(self.lines)

    def _reset(self):
        """新建一棵空控件树，填完后由 _attach 换上"""
        self.lines = []
        self.body = QWidget()
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(2, 2, 6, 2)
        self.box.setSpacing(8)

    def _attach(self, position=0):
        """换上填好的控件树并滚到 position

        整棵树填好再挂上去，滚动范围当场就能算出来；先挂空树再往里加，子控件要等之后的事件才显示，设回的位置会被截成 0
        """
        # 点证据链里的某一步会在它自己的鼠标事件里重建整棵控件；旧控件必须延迟释放，立即删除会访问已释放内存而崩溃
        old = self.takeWidget()
        if old:
            old.deleteLater()
        self.setWidget(self.body)
        self.verticalScrollBar().setValue(position)

    def _add(self, widget):
        if isinstance(widget, QLabel):
            self.lines.append(widget.text())
        self.box.addWidget(widget)
        return widget

    def show_empty(self, text=None):
        self.events, self.selected = [], None
        self._reset()
        self.box.addStretch()
        hint = self._add(label(text or self.placeholder, "caption", True))
        hint.setAlignment(Qt.AlignCenter)
        self.box.addStretch()
        self._attach()

    def show_trace(self, selected, events, note=None, headline=None, origin=None):
        """headline 为 (标题, 颜色, 摘要, 建议) 时用它做结论卡，否则按消息诊断生成；origin 是切到别的来源前所选的消息"""
        # 同一请求里切换所选消息或来了新消息时保留滚动位置；换了请求才回到顶部
        same = self.selected is not None and any(e.event_id == self.selected.event_id for e in events)
        position = self.verticalScrollBar().value()
        # 备注绑定完整请求键，切到别的来源时跟着换
        if note is None and self.store and selected.tid and not headline:
            note = self.store.note((selected.sn, selected.session, selected.tid))
        self.events, self.selected, self.note, self.headline, self.origin = events, selected, note, headline, origin
        self._reset()
        if origin:
            back = QHBoxLayout()
            back.addWidget(button("‹ 回到原来那组", lambda: self._switch(origin, None), "link"))
            back.addStretch()
            self.box.addLayout(back)
        diagnosed = [(e, diagnose(e)) for e in events]
        failed = [(e, d) for e, d in diagnosed if d.status in FAILED]
        waiting = any(e.kind == "指令" for e in events) and not any(e.kind == "ACK" for e in events)
        if headline:
            title, color, summary, suggestion = headline
        elif failed:
            focus, d = next(((e, d) for e, d in failed if e.module != "gw"), failed[0])
            title, color = focus.module.upper() + " 返回失败", RED
            summary, suggestion = d.summary, d.suggestion
        elif waiting:
            title, color = "等待 ACK", ORANGE
            summary, suggestion = "指令已发出，还没收到回复", "一直没有 ACK 就查 GW 转发和目标模块日志"
        elif any(e.kind == "ACK" for e in events):
            # 有回复时结论是整个请求的，和按请求汇总（summarize）同一套判定；不看当前选中的是哪一条（选中指令本身只会是“待判”）
            r = summarize(events)
            title, color, summary, suggestion = r.status, tone(r.status), "；".join(filter(None, (r.summary, r.note))), ""
        else:
            d = diagnose(selected)
            title, color, summary, suggestion = d.status, tone(d.status), d.summary, d.suggestion
        self._hero(title, color, summary, suggestion, selected)
        if note:
            self._note(note)

        head = QHBoxLayout()
        title = label(f"证据链 · {len(events)} 条", "sectionTitle")
        head.addWidget(title)
        self.lines.append(title.text())
        head.addStretch()
        if self.nearby_logs and any(occurred(e) is not None for e in events):
            head.addWidget(button("这段时间的设备日志", lambda: self.nearby.emit(self.events), "link"))
        self.box.addLayout(head)
        for e, d in diagnosed:
            self._step(e, d)
        if self.store and not headline:
            self._others(selected)
        self._detail(selected)
        self.box.addStretch()
        self._attach(position if same else 0)

    def _note(self, note):
        box = QFrame()
        box.setObjectName("noteCard")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)
        header = label("问题备注" + ("　· 已复现" if note.get("reproduced") else ""), "groupHeader")
        layout.addWidget(header)
        self.lines.append(header.text())
        for caption, key in (("预期", "expected"), ("实际", "actual"), ("备注", "remark")):
            if note.get(key):
                line = label(f"{caption}　{note[key]}", wrap=True)
                layout.addWidget(line)
                self.lines.append(line.text())
        self.box.addWidget(box)

    def _hero(self, title, color, summary, suggestion, selected):
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setStyleSheet(f"QFrame#hero{{background:{tint(color, 26)};border-radius:14px}}")
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)
        meta = label(f"{selected.sn or '未知设备'}  ·  {selected.source.split(':')[0]}", "caption")
        heading = label(title)
        heading.setStyleSheet(f"font-size:20px;font-weight:700;color:{color}")
        layout.addWidget(meta)
        layout.addWidget(heading)
        self.lines += [meta.text(), title]
        if summary:
            reason = label(summary, wrap=True)
            reason.setStyleSheet("font-size:14px")
            reason.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(reason)
            self.lines.append(summary)
        if suggestion:
            advice = label("下一步　" + suggestion, wrap=True)
            advice.setStyleSheet("color:palette(window-text)")
            advice.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(advice)
            self.lines.append(advice.text())
        self.box.addWidget(hero)

    def _step(self, e, d):
        step = Step()
        step.setObjectName("stepSelected" if e is self.selected else "step")
        step.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(step)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        marker = dot(tone(d.status), 10)
        row.addWidget(marker, 0, Qt.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(8)
        # 日志文件里的一行（原文没写 Topic）和收到的 MQTT 消息分开说，免得像多了一条空消息
        logged = e.kind == "日志" and e.file
        kind = "日志记录" if logged else f"{e.kind} · {e.direction}"
        name = label(f"<b>{html.escape(e.module.upper())}</b>　{html.escape(kind)}")
        head.addWidget(name)
        if not (logged and d.status == "未判定"):
            head.addWidget(pill(d.status, tone(d.status)))
        head.addStretch()
        # 日志来的消息显示原文里写的时间；实时收到的显示收到时刻
        when = e.source_time.split()[-1].split("T")[-1] if e.source_time else time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        head.addWidget(label(when, "caption"))
        column.addLayout(head)
        topic = label(e.topic or (f"原文未记录 Topic · {Path(e.file).name} 第 {e.line} 行" if logged else "无 Topic"), "caption")
        topic.setStyleSheet("font-family:'Cascadia Mono','Consolas',monospace;font-size:11px")
        column.addWidget(topic)
        self.lines += [f"[{e.module} · {e.kind} · {d.status}] {e.topic}"]
        if d.summary and e.kind != "指令":
            summary = label(d.summary, wrap=True)
            column.addWidget(summary)
            self.lines.append(d.summary)
        row.addLayout(column, 1)
        step.clicked.connect(lambda e=e: self.show_trace(e, self.events, self.note, self.headline, self.origin))
        self.box.addWidget(step)

    def refresh(self):
        """按库里现在的消息重画当前这组，所选消息和“回到原来那组”不变"""
        if self.selected:
            self.show_trace(self.selected, self.store.trace(self.selected), headline=self.headline, origin=self.origin)

    def _switch(self, e, origin):
        """原地换成 e 所在的那组 (设备, 会话, tid)；结论卡落在失败那条，否则落在 e"""
        events = self.store.trace(e)
        focus = next((x for x in events if diagnose(x).status in FAILED), e)
        self.show_trace(focus, events, origin=origin)
        self.switched.emit((e.sn, e.session, e.tid))

    def _others(self, selected):
        groups = self.store.other_sources(selected)
        if not groups:
            return
        self._add(label("其他来源中相同 tid 的记录", "sectionTitle"))
        self._add(label("同一台设备、同一个 tid，不一定是同一次操作；点“查看”单独看那一组，不和上面拼在一起", "caption", True))
        for events in groups:
            row = QHBoxLayout()
            line = label(f"{self._source_name(events)} · {len(events)} 条 · {self._span(events)}", wrap=True)
            self.lines.append(line.text())
            row.addWidget(line, 1)
            row.addWidget(button("查看", lambda _=False, e=events[0]: self._switch(e, self.origin or self.selected), "link"), 0, Qt.AlignTop)
            self.box.addLayout(row)

    @staticmethod
    def _source_name(events):
        session = events[0].session
        if session.startswith("import-live-"):
            return "实时跟随"
        if session.startswith("import-"):
            names = list(dict.fromkeys(Path(e.file).name for e in events if e.file))
            return "设备日志 · " + (names[0] + (" 等" if len(names) > 1 else "") if names else "导入")
        return "沙盒" if events[0].source == "sandbox" else "实时 MQTT"

    @staticmethod
    def _span(events):
        """各条实际发生时间的起止；原文没写日期的照原文写时刻"""
        def when(e):
            stamp = occurred(e)
            return time.strftime("%m-%d %H:%M:%S", time.localtime(stamp)) if stamp is not None else e.source_time or "时间未知"
        first, last = when(events[0]), when(events[-1])
        return first if first == last else f"{first} – {last.split()[-1]}"

    def _detail(self, e):
        text = json.dumps(e.payload, ensure_ascii=False, indent=2) if e.payload else e.raw.strip()
        head = QHBoxLayout()
        head.addWidget(label("所选消息", "sectionTitle"))
        head.addStretch()
        head.addWidget(self._copy_button("复制消息", text))
        if e.file:
            head.addWidget(button("原文附近", lambda: self.original.emit(e), "link"))
        self.box.addLayout(head)
        where = e.topic or (f"日志记录，原文未记录 Topic · {e.file} 第 {e.line} 行" if e.file else "")
        if where:
            row = QHBoxLayout()
            line = label(where, "caption", True)
            self.lines.append(where)
            line.setStyleSheet("font-family:'Cascadia Mono','Consolas',monospace;font-size:11px")
            line.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(line, 1)
            if e.topic:
                row.addWidget(self._copy_button("复制", e.topic), 0, Qt.AlignTop)
            self.box.addLayout(row)
        code = QPlainTextEdit(text)
        code.setObjectName("code")
        code.setReadOnly(True)
        code.setFixedHeight(min(26 + 18 * (text.count("\n") + 1), 380))
        self.lines.append(text)
        self.box.addWidget(code)
        before = self.previous(e) if self.previous and e.payload else None
        if before:
            self._diff(before, e)

    @staticmethod
    def _copy_button(text, content):
        """点了把 content 放进剪贴板，按钮短暂显示“已复制”"""
        def copy():
            QApplication.clipboard().setText(content)
            widget.setText("已复制 ✓")
            QTimer.singleShot(1200, lambda: widget.setText(text))

        widget = button(text, copy, "link")
        return widget

    def _diff(self, before, e):
        changes = payload_diff(before.payload, e.payload)
        when = time.strftime("%H:%M:%S", time.localtime(before.timestamp))
        self._add(label(f"和上一条同类消息相比（{when}）" + ("" if changes else "：内容相同"), "groupHeader"))
        shown = lambda v: "（无）" if v is None else json.dumps(v, ensure_ascii=False)[:60]
        for path, old, new in changes[:12]:
            line = label(f"<span style='font-family:Consolas,monospace'>{html.escape(path)}</span>　"
                         f"<span style='color:{GRAY}'>{html.escape(shown(old))}</span> → "
                         f"<b style='color:{ORANGE}'>{html.escape(shown(new))}</b>", wrap=True)
            self._add(line)
        if len(changes) > 12:
            self._add(label(f"另有 {len(changes) - 12} 个字段变化，看上方完整报文", "caption"))
