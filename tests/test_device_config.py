"""设备配置页：经 SSH 列 /oem/ttaviation/gateway/db 下的文件、看内容；用假的 SSH，不连设备"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dog_debug.app import Studio
from dog_debug.remote import Remote


@pytest.fixture
def window(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    win = Studio(":memory:")
    yield win
    win.close()


class FakeRemote:
    files = {"/oem/ttaviation/gateway/db/media.cfg": b'{"zlm":{"rtsp_port":554},"streams":{}}',
             "/oem/ttaviation/gateway/db/sub/broker.cfg": b"port=1883\n",
             "/oem/ttaviation/gateway/db/state.db": b"SQLite format 3\x00\x00\x01"}
    closed = False

    def list_files(self, root):
        return sorted(p[len(root) + 1:] for p in self.files)

    def read_bytes(self, path):
        if path not in self.files:
            raise OSError("cat: no such file")
        return self.files[path]

    def close(self):
        FakeRemote.closed = True


def drain(window):
    """后台线程把结果放进 inbox，等到放进来再交给界面处理"""
    import time
    deadline = time.monotonic() + 5
    while window.inbox.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    window._drain()


def test_lists_and_shows_config_files(window, monkeypatch):
    page = window.device_config
    window.ssh_user.setText("tta")
    monkeypatch.setattr(window.logs, "_ssh_for_logs", lambda: (FakeRemote(), True))
    page.refresh()
    drain(window)
    assert [page.list.item(i).text() for i in range(page.list.count())] == ["media.cfg", "state.db", "sub/broker.cfg"]
    assert "共 3 个文件" in page.note.text() and FakeRemote.closed
    page.list.setCurrentRow(0)
    drain(window)
    assert page.text.toPlainText() == '{"zlm":{"rtsp_port":554},"streams":{}}' and "38 字节" in page.info.text()
    page.pretty.setChecked(True)
    assert page.text.toPlainText().startswith('{\n  "zlm": {')
    # 二进制文件不当文字显示
    page.list.setCurrentRow(1)
    drain(window)
    assert page.text.toPlainText() == "" and "二进制文件" in page.info.text()
    # 不是 JSON 的按原文
    page.list.setCurrentRow(2)
    drain(window)
    assert page.text.toPlainText() == "port=1883\n" and "不是 JSON" in page.info.text()
    page.filter.setText("cfg")
    assert page.list.count() == 2


def test_needs_ssh_account_and_reports_errors(window, monkeypatch):
    page = window.device_config
    notes = []
    monkeypatch.setattr(window, "notify", notes.append)
    page.refresh()
    assert "SSH 账号" in notes[-1] and not page.busy
    window.ssh_user.setText("tta")

    def fail():
        raise OSError("认证失败")
    monkeypatch.setattr(window.logs, "_ssh_for_logs", fail)
    page.refresh()
    drain(window)
    assert "列不出文件：认证失败" in page.note.text() and not page.busy


def test_remote_falls_back_to_sudo_with_password():
    """普通权限和 sudo -n 都读不了时，用登录密码经 stdin 交给 sudo -S"""
    remote = Remote.__new__(Remote)
    remote.password = "pw"
    tried = []

    def run(command):
        tried.append(command)
        return (0, b"ok", "") if command.startswith("sudo -S") else (1, b"", "Permission denied")
    remote._run = run
    assert remote.read_bytes("/oem/ttaviation/gateway/db/media.cfg") == b"ok"
    path = "/oem/ttaviation/gateway/db/media.cfg"
    assert tried == [f"cat {path}", f"sudo -n cat {path}", f"sudo -S -p '' cat {path}"]
    remote._run = lambda command: (1, b"", "Permission denied")
    with pytest.raises(OSError, match="Permission denied"):
        remote.read_bytes("/x")
