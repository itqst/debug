"""回归测试页：保存、加载和运行沙盒用例"""

import html
import json
import uuid
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QTextBrowser, QVBoxLayout, QWidget

from ..core import diagnose, trace_report
from ..registry import find, make_command
from ..transport import simulate
from ..ui import button, card, GRAY, GREEN, RED, SCENARIOS


class SuitePage:
    """回归测试页：内置沙盒用例和 YAML 用例"""

    def __init__(self, app):
        self.app = app

    def _suite_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.app._page_header(layout, "回归测试", "内置九组沙盒用例覆盖六个模块；也可以加载控制页保存的 YAML 用例",
                          button("加载 YAML 用例", self._load_case), button("运行内置回归", self._run_suite, "primary"))
        result, rv = card(8, 18)
        self.suite_info = QTextBrowser()
        self.suite_info.setPlaceholderText("点右上角“运行内置回归”")
        rv.addWidget(self.suite_info)
        layout.addWidget(result, 1)
        return page

    def _save_case(self):
        import yaml
        try:
            payload = json.loads(self.app.control.editor.toPlainText())
        except ValueError as error:
            self.app.notify("JSON 格式错误：" + str(error))
            return
        path, _ = QFileDialog.getSaveFileName(self.app, "保存用例", str(payload.get("method", "case")) + ".yaml", "YAML (*.yaml)")
        if not path:
            return
        scenario = SCENARIOS[self.app.control.scenario.index()][1]
        preview = simulate(payload, "TEST", "preview", scenario)
        expected = [diagnose(e).status for e in preview if e.kind == "ACK"]
        Path(path).write_text(yaml.safe_dump({"name": payload.get("method"), "payload": payload, "scenario": scenario,
                                              "expect": {"ack_status": expected[-1] if expected else None}},
                                             allow_unicode=True), encoding="utf-8")
        self.app.notify("用例已保存")

    def _suite_html(self, rows, title=""):
        body = f"<p style='margin:0 0 14px 0;font-weight:700'>{html.escape(title)}</p>" if title else ""
        for passed, name, detail in rows:
            # passed 为 None 表示用例没写断言，只执行不判定
            color, verdict = (GRAY, "仅执行") if passed is None else (GREEN, "PASS") if passed else (RED, "FAIL")
            body += (f"<p style='margin:0 0 12px 0'><span style='color:{color};font-weight:700'>"
                     f"{verdict}</span>　<b>{html.escape(name)}</b><br>"
                     f"<span style='color:{GRAY}'>{html.escape(detail)}</span></p>")
        self.suite_info.setHtml(body)

    def _run_suite(self):
        cases = [("GW 配置查询", "oss_config_get", "success"), ("VFC 导航受理", "start_nav", "success"),
                 ("PTZ 复位", "gimbal_reset", "success"), ("Media 拍照失败", "take_photo", "failure"),
                 ("Vision AI", "start_ai", "success"), ("FSVR 上传终态", "sync_media_file", "success"),
                 ("直播活跃语义", "camera_livestream", "success"), ("嵌套失败", "camera_livestream", "nested_failure"),
                 ("缺 ACK 保持未知", "take_photo", "no_ack")]
        rows = []
        for name, method, scenario in cases:
            events = simulate(make_command(find(method)), "TEST_DOG_001", "suite-" + str(uuid.uuid4()), scenario)
            statuses = [diagnose(e).status for e in events]
            passed = ("失败" in statuses if scenario in {"failure", "nested_failure"} else
                      not any(e.kind == "ACK" for e in events) if scenario == "no_ack" else
                      "失败" not in statuses)
            rows.append((passed, name, " → ".join(dict.fromkeys(statuses))))
            for e in events:
                self.app.store.add(e)
        self.app.store.flush()
        self._suite_html(rows)
        self.app.messages._refresh()

    def _load_case(self):
        paths, _ = QFileDialog.getOpenFileNames(self.app, "读取沙盒用例（可多选，按文件名顺序执行）", "", "YAML (*.yaml *.yml)")
        if paths:
            self.run_cases(sorted(paths))

    def run_cases(self, paths):
        """按顺序在沙盒执行用例，每条记 PASS/FAIL/仅执行和证据；某条读不出来只记失败，不影响后面的"""
        import yaml
        rows = []
        for path in paths:
            name = Path(path).stem
            # 用例文件由用户编写，读取、准备、模拟任一步出错都只记这一条失败，接着跑后面的
            try:
                # utf-8-sig 兼容记事本等工具保存的带 BOM 的 UTF-8
                case = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
                payload, name = case["payload"], str(case.get("name") or name)
                payload["tid"] = str(uuid.uuid4())
                events = simulate(payload, "TEST_DOG_001", "case-" + str(uuid.uuid4()), case.get("scenario", "success"))
                expect = case.get("expect") or {}
            except Exception as error:
                rows.append((False, name, f"用例无法执行：{type(error).__name__} {error}"))
                continue
            for e in events:
                self.app.store.add(e)
            actual = [diagnose(e).status for e in events if e.kind == "ACK"]
            got = actual[-1] if actual else None
            passed = got == expect["ack_status"] if "ack_status" in expect else None
            evidence = trace_report(events).split("\n\n")[0]
            if passed is False:
                evidence = f"期望 {expect['ack_status']}，实际 {got}（tid {payload['tid'][:8]}…）；" + evidence
            rows.append((passed, name, evidence))
        self.app.store.flush()
        counts = {"PASS": sum(r[0] is True for r in rows), "FAIL": sum(r[0] is False for r in rows),
                  "仅执行": sum(r[0] is None for r in rows)}
        self._suite_html(rows, f"{len(rows)} 条用例：" + "，".join(f"{k} {v}" for k, v in counts.items() if v))
        self.app.messages._refresh()
