"""远程（FRP）连接和设备日志：代理握手、挑文件、隧道连接、取日志自动导入、实时跟随；不连真实设备"""

import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import socket
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

import dog_debug.app as app_module
import dog_debug.remote as remote
from dog_debug.app import Studio
from dog_debug.core import Event
from dog_debug.remote import pick_files


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    win = Studio(":memory:")
    yield win
    win.close()
    app.processEvents()


def fake_proxy(reply):
    """本机起一个只应答一次的假代理，返回它的地址和收到的请求"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    got = []

    def serve():
        client, _ = server.accept()
        got.append(client.recv(4096))
        # 响应头和 SSH 版本串挤在同一个包里，握手不能把版本串吞掉
        client.sendall(reply)
        time.sleep(0.3)
        client.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname(), got


def test_proxy_handshake_keeps_ssh_banner(monkeypatch):
    address, got = fake_proxy(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\nSSH-2.0-OpenSSH_8.9p1\r\n")
    monkeypatch.setattr(remote, "FRP_PROXY", address)
    sock = remote._proxy_socket("DOG.tta_edge.com", 22)
    assert got[0].startswith(b"CONNECT DOG.tta_edge.com:22 HTTP/1.1")
    assert sock.recv(64).startswith(b"SSH-2.0-")
    sock.close()


def test_proxy_refusal_is_reported(monkeypatch):
    address, _ = fake_proxy(b"HTTP/1.1 404 Not Found\r\n\r\n")
    monkeypatch.setattr(remote, "FRP_PROXY", address)
    with pytest.raises(OSError, match="404"):
        remote._proxy_socket("DOG.tta_edge.com", 22)


def test_pick_files_skips_current_link_and_old_files():
    now = time.time()
    files = [("tta.log", 10, now), ("GW_0000011_20261102-154308.log", 10, now),
             ("GW_0000010_20261102-120227.log", 20, now - 7200), ("notes.txt", 1, now)]
    assert [f[0] for f in pick_files(files, now - 1800)] == ["GW_0000011_20261102-154308.log"]
    assert [f[0] for f in pick_files(files, None)] == ["GW_0000010_20261102-120227.log", "GW_0000011_20261102-154308.log"]


class FakeRemote:
    """代替 SSH：forward 给固定端口并记下目标，设备文件和日志从内存里给，follow 在线程里逐行回调"""
    made, files = [], {}

    def __init__(self, sn, host, user, password, host_key="", via_frp=True):
        self.host = f"{sn}.tta_edge.com" if via_frp else host
        self.args = (sn, host, user, password, host_key, via_frp)
        self.host_key, self.closed, self.alive = "KEY", False, True
        FakeRemote.made.append(self)

    def forward(self, port, host="127.0.0.1"):
        self.forwards = getattr(self, "forwards", []) + [(host, port)]
        return {1883: 41883, 80: 40080}.get(port, 40000 + port % 1000)

    def read_file(self, path):
        if path not in FakeRemote.files:
            raise OSError(f"cat: {path}: No such file or directory")
        return FakeRemote.files[path]

    def list_logs(self, folder):
        return [("tta.log", 80, time.time()), (f"{folder.upper()}_0000001_20261102-150000.log", 80, time.time())]

    def download(self, folder, name, path, progress, cancelled):
        with open(path, "w", encoding="utf-8") as out:
            out.write(f"2026-11-02 15:00:01.000 E [tta_{folder}_1.0] x.cpp:1 run | {folder} 出错 error\n"
                      f"2026-11-02 15:00:02.000 I [tta_{folder}_1.0] x.cpp:2 run | {folder} 正常\n")
        progress(80)

    def follow(self, folder, on_line):
        if folder == "gateway":
            threading.Thread(target=lambda: [on_line(f"2026-11-02 16:00:0{i}.000 I [tta_gateway_3.0.1] a.cpp:1 x | 第 {i} 行")
                                             for i in range(3)], daemon=True).start()
        return lambda: None

    def close(self):
        self.closed = True


def drain_until(window, cond, seconds=5):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        window._drain()
        time.sleep(0.02)
    return cond()


def test_frp_connect_tunnels_mqtt_and_zlm_and_closes(window, monkeypatch):
    FakeRemote.made.clear()
    FakeRemote.files = {}
    monkeypatch.setattr(app_module, "Remote", FakeRemote)
    connected = []
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: connected.append(a) or setattr(window.mqtt, "client", object()))
    monkeypatch.setattr(window.mqtt, "close", lambda: setattr(window.mqtt, "client", None))
    window.mode_box.setCurrentText("直连模块")
    window.sn.setText("10300262998249")
    window.frp.setChecked(True)
    window.ssh_user.setText("u")
    window.ssh_password.setText("p")
    window._connect()
    assert drain_until(window, lambda: connected)
    # 设备身份还是整机 SN，127.0.0.1 只是隧道入口
    assert connected[0][:3] == ("127.0.0.1", 41883, "10300262998249")
    assert FakeRemote.made[0].args[2:4] == ("u", "p") and FakeRemote.made[0].args[5]
    window.mqtt.connected, window.mqtt.host, window.mqtt.sn = True, "127.0.0.1", "10300262998249"
    window.inbox.put(("connection", "ok"))
    window._drain()
    assert window.zlm_host.text() == "127.0.0.1:40080" and "10300262998249.tta_edge.com" in window.link.text()
    window._connect()
    assert FakeRemote.made[0].closed and window.ssh is None


def test_device_logs_download_and_show_on_logs_page(window, monkeypatch, tmp_path):
    from PySide6.QtCore import QStandardPaths
    monkeypatch.setattr(app_module, "Remote", FakeRemote)
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda kind: str(tmp_path))
    window.mode_box.setCurrentText("直连模块")
    window.frp.setChecked(True)
    window.sn.setText("DOG")
    window.fetch_device_logs(["gateway", "media"], time.time() - 1800)
    assert drain_until(window, lambda: not window.fetching and window.import_thread and not window.import_thread.is_alive()
                       and window.log_model.rows, 10)
    # 当前文件 tta.log 不重复取；文件留在本机
    assert sorted(p.name for p in (tmp_path / "设备日志" / "DOG").rglob("*.log")) == ["GATEWAY_0000001_20261102-150000.log",
                                                                                     "MEDIA_0000001_20261102-150000.log"]
    rows = window.log_model.rows
    assert len(rows) == 4 and {e.module for _, e, _ in rows} == {"gw", "media"}
    window.log_errors.setChecked(True)
    window._drain()
    assert [e.module for _, e, _ in window.log_model.rows] == ["gw", "media"]


def test_live_follow_lines_appear_in_their_own_batch(window, monkeypatch):
    monkeypatch.setattr(app_module, "Remote", FakeRemote)
    window.mode_box.setCurrentText("直连模块")
    window.frp.setChecked(True)
    window.sn.setText("DOG")
    window._go(window.LOGS)
    window.live_follow.setChecked(True)
    assert drain_until(window, lambda: len(window.log_model.rows) == 3)
    assert window.log_batch.currentText().startswith("实时跟随中") and window.log_model.rows[2][1].module == "gw"
    window.live_follow.setChecked(False)
    assert window.follow_stops == []
    assert drain_until(window, lambda: window.log_batch.findText("3 条", app_module.Qt.MatchEndsWith) >= 0)


def test_old_live_follow_batches_are_dropped(window):
    """实时跟随的批次最后一行超过 48 小时就删掉，连同备注；导入的批次再旧也留着"""
    old = time.time() - app_module.LIVE_KEEP - 60
    for session in ("import-live-old", "import-live-new", "import-file"):
        stamp = old if session != "import-live-new" else time.time()
        window.store.add(Event("", {}, sn="DOG", session=session, raw="x", file="a.log", timestamp=stamp))
    window.store.set_note(("DOG", "import-live-old", "t"), {"remark": "旧"})
    window.store.flush()
    window.sn.setText("DOG")
    window._go(window.LOGS)
    assert drain_until(window, lambda: window.log_batch.count() == 3)
    sessions = {key[1] for key in (window.log_batch.itemData(i) for i in range(window.log_batch.count()))}
    assert sessions == {"", "import-live-new", "import-file"}
    assert window.store.note(("DOG", "import-live-old", "t")) is None


def test_frp_tunnels_to_broker_from_device_config(window, monkeypatch):
    """FRP 登进去的板子不是 Broker 时（NX 登录、Broker 在 192.168.234.1），隧道按 mqttBroker.cfg 转到 GW 实际连的 Broker"""
    FakeRemote.made.clear()
    FakeRemote.files = {app_module.BROKER_CFG: json.dumps({"local_host": "192.168.234.1", "local_port": 1883})}
    monkeypatch.setattr(app_module, "Remote", FakeRemote)
    connected = []
    monkeypatch.setattr(window.mqtt, "connect", lambda *a: connected.append(a) or setattr(window.mqtt, "client", object()))
    monkeypatch.setattr(window.mqtt, "close", lambda: setattr(window.mqtt, "client", None))
    window.mode_box.setCurrentText("直连模块")
    window.sn.setText("10300262026905")
    window.frp.setChecked(True)
    window.ssh_user.setText("robot")
    window._connect()
    assert drain_until(window, lambda: connected)
    assert FakeRemote.made[0].forwards == [("192.168.234.1", 1883), ("127.0.0.1", 80)]
    assert connected[0][:2] == ("127.0.0.1", 41883) and "192.168.234.1:1883" in window.link.text()
    window._connect()
    FakeRemote.files = {}
