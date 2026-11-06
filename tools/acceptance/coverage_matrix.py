"""从代码生成协议覆盖矩阵 docs/acceptance/协议覆盖矩阵.md：专用控件、结果判定、沙盒数据、测试引用逐项列出；实机一律记“未验”"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dog_debug import app, core, transport  # noqa: E402
from dog_debug.registry import MODULES, entries  # noqa: E402


def literals(source):
    return set(re.findall(r'"([a-z_]+)"', source))


def main():
    panels = literals(inspect.getsource(app.RemotePanel)) | literals(inspect.getsource(app.MissionPanel))
    rules = literals(inspect.getsource(core.diagnose)) | core.UPLOAD_STATES | core.DETECTION_METHODS
    mocks = (set(transport.MOCK_DATA) | literals(inspect.getsource(transport.simulate)) | literals(inspect.getsource(transport.simulate_detection))
             | literals(inspect.getsource(transport.simulate_pcd_switch)) | literals(inspect.getsource(transport.simulate_clouds)))
    tests = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py"))
    rows, counts = [], {m: [0, 0, 0] for m in MODULES}
    for item in sorted(entries(), key=lambda e: (MODULES.index(e["module"]), e["method"])):
        method = item["method"]
        form = "是" if method in panels else "—"
        rule = "专门规则" if method in rules else "通用 ACK"
        mock = "示例数据" if method in mocks else ("链路模拟" if item["category"] == "command" else "—")
        # 全部 command 条目由 test_every_command_sandbox_roundtrip 遍历；其余看测试里是否直接出现方法名
        sweep = item["category"] == "command" and "test_every_command_sandbox_roundtrip" in tests
        tested = "是" if f'"{method}"' in tests or f"'{method}'" in tests else "遍历" if sweep else "—"
        note = item["unverified"] or ""
        counts[item["module"]][0] += 1
        counts[item["module"]][1] += form == "是"
        counts[item["module"]][2] += tested != "—"
        rows.append(f"| {item['module'].upper()} | `{method}` | {item['category']} | {item['source']} | {form} | {rule} | {mock} | {tested} | 未验 | {note} |")
    summary = ["| 模块 | 条目 | 有专用控件 | 测试引用 |", "| --- | --- | --- | --- |"]
    summary += [f"| {m.upper()} | {c[0]} | {c[1]} | {c[2]} |" for m, c in counts.items()]
    text = "\n".join([
        "# 协议覆盖矩阵", "",
        "由 `tools/acceptance/coverage_matrix.py` 从代码生成。“实机”一列全部未验：本轮没有连接真实 GW、Broker 或机器狗", "",
        f"目录共 {len(entries())} 条，来自整机云端协议、设备端 local 协议和 FSVR 现行 mqtt_protocol.md 的方法表；"
        "只代表这几份文档登记的方法，不等于各模块源码里的全部能力",
        "", "列含义：专用控件＝遥控页或导航与文件页有按钮/表单（其余在控制页 JSON 编辑发送）；结果判定＝`diagnose` 里有该方法的专门解释，"
        "否则按通用 ACK 结果层级；沙盒＝链路模拟（GW→模块→ACK）或带示例业务数据；测试引用＝测试代码里直接出现该方法名（是），或由全部指令遍历测试覆盖（遍历）", "",
        *summary, "",
        "| 模块 | 方法 | 类型 | 文档来源 | 专用控件 | 结果判定 | 沙盒 | 测试引用 | 实机 | 待核 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |", *rows, ""])
    out = ROOT / "docs" / "acceptance" / "协议覆盖矩阵.md"
    out.write_text(text, encoding="utf-8")
    print(out, len(rows))


if __name__ == "__main__":
    main()
