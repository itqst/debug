"""连接与通道设置：机器狗/无人机独立配置、历史连接自动记录、预设快速选择与删除、FRP/SSH 隧道和连接状态"""

import json
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QPushButton, QVBoxLayout

from ..remote import Remote
from ..trace_view import Step
from ..ui import (
    BLUE, broker_target, DEVICES, GRAY, GREEN, label, MODES, ORANGE, pill, RED, settings, tint,
)

# 机器狗与无人机的独立默认配置（新设备首次切换且无历史记录时使用）
DEVICE_DEFAULTS = {
    "dog": {
        "mode": "sandbox", "host": "127.0.0.1", "port": 1883, "sn": "TEST_DOG_001",
        "user": "", "password": "", "tls": False, "frp": False,
    },
    "uav": {
        "mode": "direct", "host": "192.168.50.3", "port": 1883, "uav_sn": "1209039909594066",
        "user": "orangepi", "password": "", "tls": False, "cloud_answer": True,
    },
}

MODE_SHORT = {"sandbox": "沙盒", "gw": "云端", "direct": "直连", "offline": "离线"}
MODE_COLORS = {"sandbox": GRAY, "gw": "#5856D6", "direct": BLUE, "offline": GRAY}


class ConnectionPanel:
    """连接与通道设置：双设备配置分离、自动记录与快速切换预设、FRP/SSH 登录、连接状态文字"""

    def __init__(self, app):
        self.app = app
        self.profile_index = 0
        self.active_profile = ""
        self._restoring = False
        self._active_device = "dog"

    def current_device(self):
        return DEVICES[self.app.device_box.currentText()]

    def _snapshot(self, device=None):
        """抓取当前表单配置快照（按设备只保存该设备关心的字段）"""
        device = device or self.current_device()
        data = {
            "device": device,
            "mode": MODES[self.app.mode_box.currentText()],
            "host": self.app.host.text().strip(),
            "port": self.app.port.value(),
            "user": self.app.user.text(),
            "password": self.app.password.text(),
            "tls": self.app.tls.isChecked(),
        }
        if device == "uav":
            data["uav_sn"] = self.app.uav_sn.text().strip()
            data["cloud_answer"] = self.app.cloud_answer.isChecked()
        else:
            data["sn"] = self.app.sn.text().strip()
            data["frp"] = self.app.frp.isChecked()
        return data

    def save_device_state(self, device=None):
        """把当前表单状态写入该设备专属的本机设置（机器狗与无人机互不覆盖）"""
        if self._restoring:
            return
        device = device or self.current_device()
        snap = self._snapshot(device)
        stored = settings()
        stored.setValue(f"connection_{device}", json.dumps(snap, ensure_ascii=False))
        stored.setValue("last_device", device)
        if device == "uav" and snap["mode"] in ("direct", "gw"):
            stored.setValue(
                f"uav_broker_{snap['mode']}",
                json.dumps([snap["host"], snap["port"], snap["user"], snap["password"], snap["tls"]], ensure_ascii=False),
            )

    def load_device_state(self, device):
        """从本机设置恢复该设备专属配置；没有则回落到设备默认值"""
        stored = settings()
        raw = stored.value(f"connection_{device}", "")
        config = None
        if raw:
            try:
                config = json.loads(raw)
            except ValueError:
                config = None
        if not isinstance(config, dict):
            config = dict(DEVICE_DEFAULTS[device])
        config["device"] = device
        self._apply_config(config, include_password=True, switch_device=False)

    def on_device_changed(self, old_device, new_device):
        """切换设备时：先保存旧设备表单，再载入新设备独立配置并刷新该设备的预设列表"""
        if old_device and old_device != new_device and not self._restoring:
            self.save_device_state(old_device)
        self._active_device = new_device
        if not self._restoring:
            self.load_device_state(new_device)
            settings().setValue("last_device", new_device)
        self.active_profile = ""
        self._load_profiles()

    def restore_connection(self):
        """启动时恢复上次使用的设备及其专属连接参数；只填不连，兼容旧版单份 connection 配置"""
        stored = settings()
        last_device = stored.value("last_device", "")
        if last_device not in ("dog", "uav"):
            legacy = stored.value("connection", "")
            if legacy:
                try:
                    cfg = json.loads(legacy)
                    last_device = cfg.get("device", "dog")
                    if last_device not in ("dog", "uav"):
                        last_device = "dog"
                    stored.setValue(f"connection_{last_device}", legacy)
                    stored.setValue("last_device", last_device)
                except ValueError:
                    last_device = ""
        if last_device not in ("dog", "uav"):
            self._load_profiles()
            return
        self._restoring = True
        try:
            self._active_device = last_device
            device_label = next(k for k, v in DEVICES.items() if v == last_device)
            if self.app.device_box.currentText() != device_label:
                self.app.device_box.setCurrentText(device_label)
            self.load_device_state(last_device)
        finally:
            self._restoring = False
        self._load_profiles()

    def _load_profiles(self, select=None):
        """仅加载当前设备的预设与历史记录，同时同步隐藏的 profile_box 和可视快速选择列表"""
        device = self.current_device()
        profiles = self.app.store.connections(device=device)
        self.app.profile_box.blockSignals(True)
        self.app.profile_box.clear()
        self.app.profile_box.addItem("手动填写", None)
        for name, config in profiles.items():
            self.app.profile_box.addItem(name, config)
        if select and select in profiles:
            self.app.profile_box.setCurrentText(select)
            self.active_profile = select
        elif self.active_profile in profiles:
            self.app.profile_box.setCurrentText(self.active_profile)
        else:
            self.active_profile = ""
            self.app.profile_box.setCurrentIndex(0)
        self.profile_index = self.app.profile_box.currentIndex()
        self.app.profile_box.blockSignals(False)
        self._render_profile_list(profiles)

    def _render_profile_list(self, profiles=None):
        """在连接设置弹窗中渲染当前设备的快速选择配置列表（点击即填，右侧一键删除）"""
        if not hasattr(self.app, "profile_list_layout"):
            return
        if profiles is None:
            profiles = self.app.store.connections(device=self.current_device())
        layout = self.app.profile_list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        if not profiles:
            empty = label("暂无已保存配置：连接后会自动记录到这里，也可点右上角「＋ 保存当前」", "caption", True)
            empty.setContentsMargins(6, 10, 6, 10)
            layout.addWidget(empty)
        else:
            for name, config in profiles.items():
                layout.addWidget(self._make_profile_row(name, config))
        if hasattr(self.app, "profile_scroll"):
            rows = max(1, min(len(profiles), 4))
            self.app.profile_scroll.setFixedHeight(44 if not profiles else rows * 52 + 8)
        if hasattr(self.app, "connection_sheet"):
            self.app.connection_sheet.refit()

    def _make_profile_row(self, name, config):
        """单条配置卡片：左侧名称+模式标签+地址/SN 摘要，点击即秒切；右侧 × 一键删除"""
        row_card = Step()
        active = name == self.active_profile
        row_card.setObjectName("presetRowActive" if active else "presetRow")
        row_card.setCursor(Qt.PointingHandCursor)
        row_card.setFixedHeight(46)
        row_card.clicked.connect(lambda n=name: self._select_profile_by_name(n))
        hl = QHBoxLayout(row_card)
        hl.setContentsMargins(10, 4, 8, 4)
        hl.setSpacing(8)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(1)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        title = label(name, "presetTitle")
        top.addWidget(title)
        mode = "direct" if config.get("mode") == "mock_gw" else config.get("mode", "direct")
        tag = pill(MODE_SHORT.get(mode, mode), MODE_COLORS.get(mode, GRAY))
        top.addWidget(tag)
        if config.get("auto_history"):
            hist = pill("最近", GRAY)
            top.addWidget(hist)
        top.addStretch()
        left.addLayout(top)
        host = config.get("host") or "127.0.0.1"
        port = config.get("port", 1883)
        sn_val = config.get("uav_sn") if config.get("device") == "uav" else config.get("sn")
        parts = [f"{host}:{port}"]
        if sn_val and (config.get("device") != "uav" or mode == "gw"):
            parts.append(f"SN {sn_val}")
        if config.get("user"):
            parts.append(str(config["user"]))
        if config.get("tls"):
            parts.append("TLS")
        if config.get("frp"):
            parts.append("FRP")
        sub = label(" · ".join(parts), "caption")
        left.addWidget(sub)
        hl.addLayout(left, 1)
        delete_btn = QPushButton("×")
        delete_btn.setObjectName("presetDel")
        delete_btn.setFixedSize(24, 24)
        delete_btn.setCursor(Qt.PointingHandCursor)
        delete_btn.setToolTip(f"删除配置「{name}」")
        delete_btn.clicked.connect(lambda _=False, n=name: self._delete_profile_by_name(n))
        hl.addWidget(delete_btn, 0, Qt.AlignVCenter)
        return row_card

    def _select_profile_by_name(self, name):
        index = self.app.profile_box.findText(name)
        if index > 0:
            self.app.profile_box.setCurrentIndex(index)
            self._profile_chosen(index)

    def _delete_profile_by_name(self, name):
        self.app.store.delete_connection(name)
        if self.active_profile == name:
            self.active_profile = ""
        self._load_profiles()
        self.app.notify(f"已删除配置：{name}")

    def _profile_chosen(self, index):
        config = self.app.profile_box.itemData(index)
        device = next(k for k, v in DEVICES.items() if v == (config or {}).get("device", "dog"))
        if config and (self.app.mqtt.client or self.ssh_connecting) and device != self.app.device_box.currentText():
            self.app.profile_box.setCurrentIndex(self.profile_index)
            self.app.notify(f"这个配置是{device}，先断开当前连接再选")
            return
        self.profile_index = index
        if config:
            self.active_profile = self.app.profile_box.itemText(index)
            self._profile_chosen_config(config)
            self._render_profile_list()
            self.app.notify(f"已切换配置：{self.active_profile}")
        else:
            self.active_profile = ""
            self._render_profile_list()

    def _profile_chosen_config(self, config):
        self._apply_config(config, include_password=False, switch_device=True)

    def _apply_config(self, config, include_password=False, switch_device=True):
        """把配置字典填到连接表单；支持机器狗/无人机各自字段与旧版 mock_gw 迁移"""
        mode = "direct" if config.get("mode") == "mock_gw" else config.get("mode", "direct")
        mode_text = next((k for k, v in MODES.items() if v == mode), next(iter(MODES)))
        device = config.get("device", self.current_device())
        was_restoring = self._restoring
        self._restoring = True
        try:
            if switch_device:
                dev_text = next((k for k, v in DEVICES.items() if v == device), "机器狗")
                if self.app.device_box.currentText() != dev_text:
                    self.app.device_box.setCurrentText(dev_text)
            if self.app.mode_box.currentText() != mode_text:
                self.app.mode_box.setCurrentText(mode_text)
            self.app.host.setText(str(config.get("host", "127.0.0.1")))
            self.app.port.setValue(int(config.get("port", 1883)))
            if device == "uav":
                if config.get("uav_sn") is not None:
                    self.app.uav_sn.setText(str(config.get("uav_sn", "")))
                if "cloud_answer" in config:
                    self.app.cloud_answer.setChecked(bool(config.get("cloud_answer", True)))
                self.app.frp.setChecked(False)
            else:
                if config.get("sn") is not None:
                    self.app.sn.setText(str(config.get("sn", "")))
                self.app.frp.setChecked(bool(config.get("frp", False)))
            self.app.user.setText(str(config.get("user", "")))
            self.app.tls.setChecked(bool(config.get("tls", False)))
            if include_password:
                self.app.password.setText(str(config.get("password", "")))
            else:
                self.app.password.clear()
        finally:
            self._restoring = was_restoring
        self._update_network_visibility()
        self.app._update_pill()

    def _build_profile_payload(self, auto_history=False):
        """构造写入 SQLite connections 表的配置字典（不含密码）"""
        device = self.current_device()
        payload = {
            "device": device,
            "mode": self.app.mode,
            "host": self.app.host.text().strip(),
            "port": self.app.port.value(),
            "sn": self.app.sn.text().strip() if device == "dog" else "",
            "uav_sn": self.app.uav_sn.text().strip() if device == "uav" else "",
            "user": self.app.user.text().strip(),
            "tls": self.app.tls.isChecked(),
            "frp": self.app.frp.isChecked() if device == "dog" else False,
            "cloud_answer": self.app.cloud_answer.isChecked() if device == "uav" else True,
            "auto_history": auto_history,
            "updated_at": time.time(),
        }
        return payload

    def _record_history(self):
        """每次连接时自动记录到当前设备的历史配置；同设备同目标自动去重，不覆盖已手动命名的预设"""
        device = self.current_device()
        payload = self._build_profile_payload(auto_history=True)
        sig = (device, payload["mode"], payload["host"], payload["port"],
               payload["uav_sn"] if device == "uav" else payload["sn"], payload["user"], payload["tls"], payload["frp"])
        existing = self.app.store.connections(device=device)
        for name, cfg in existing.items():
            cfg_sig = (cfg.get("device", "dog"), cfg.get("mode"), cfg.get("host", ""), cfg.get("port", 1883),
                       cfg.get("uav_sn", "") if device == "uav" else cfg.get("sn", ""),
                       cfg.get("user", ""), bool(cfg.get("tls", False)), bool(cfg.get("frp", False)))
            if cfg_sig == sig:
                updated = {**cfg, **payload, "auto_history": bool(cfg.get("auto_history", False))}
                self.app.store.save_connection(name, updated)
                self._load_profiles(name)
                return
        mode_tag = MODE_SHORT.get(payload["mode"], payload["mode"])
        target = payload["uav_sn"] if (device == "uav" and payload["mode"] == "gw" and payload["uav_sn"]) else payload["host"]
        if device == "dog" and payload["sn"]:
            target = f"{payload['host']} · {payload['sn']}"
        base_name = f"{mode_tag} · {target or '127.0.0.1'}"
        name = base_name
        suffix = 2
        all_names = set(self.app.store.connections())
        while name in all_names:
            name = f"{base_name} ({suffix})"
            suffix += 1
        self.app.store.save_connection(name, payload)
        self._load_profiles(name)

    def _save_profile(self):
        device = self.current_device()
        ident = self.app.uav_sn.text().strip() if (device == "uav" and self.app.mode == "gw") else (
            self.app.sn.text().strip() if device == "dog" else "")
        fallback = f"{self.app.host.text().strip()}" + (f" · {ident}" if ident else "")
        default = self.active_profile or (self.app.profile_box.currentText() if self.app.profile_box.currentData() else fallback)
        dev_name = "无人机" if device == "uav" else "机器狗"
        name, ok = QInputDialog.getText(self.app, f"保存{dev_name}连接配置", "配置名称（仅保存在当前设备下，密码不保存）", text=default)
        if not ok or not name.strip():
            return
        clean_name = name.strip()
        self.app.store.save_connection(clean_name, self._build_profile_payload(auto_history=False))
        self._load_profiles(clean_name)
        self.app.notify(f"已保存{dev_name}配置：{clean_name}")

    def _delete_profile(self):
        name = self.active_profile or (self.app.profile_box.currentText() if self.app.profile_box.currentData() else "")
        if name:
            self._delete_profile_by_name(name)

    def _set_link(self, text, color):
        self.app.link.setText(text)
        self.app.link_dot.setStyleSheet(f"background:{color};border-radius:4px")
        self.app.connect_btn.setText("断开" if self.app.mqtt.client else "连接")
        self.app.connect_btn.setObjectName("danger" if self.app.mqtt.client else "primary")
        self.app.connect_btn.style().polish(self.app.connect_btn)
        self.app.scene3d._device_card()
        self.app._update_pill()
        self.app.uav._render_alerts()

    def _connection_hint(self):
        """连接中修改了 Broker/SN 时提示仍按旧连接收发，重连后才切换"""
        if not self.app.mqtt.connected:
            return
        if self.app.ssh:
            active = f"已经远程连接 {self.app.ssh.host} · {self.app.mqtt.sn}"
            edited = self.app.sn.text().strip() != self.app.mqtt.sn
        elif self.app.mqtt.device == "uav" and self.app.mqtt.cloud_sn:
            active = f"已连接 {self.app.mqtt.host}，模拟云端 · {self.app.mqtt.cloud_sn}"
            edited = (self.app.host.text().strip(), self.app.uav_sn.text().strip()) != (self.app.mqtt.host, self.app.mqtt.cloud_sn)
        elif self.app.mqtt.device == "uav":
            active = f"已连接无人机 {self.app.mqtt.host}"
            edited = self.app.host.text().strip() != self.app.mqtt.host
        else:
            active = f"已连接 {self.app.mqtt.host} · {self.app.mqtt.sn}"
            edited = (self.app.host.text().strip(), self.app.sn.text().strip()) != (self.app.mqtt.host, self.app.mqtt.sn)
        self._set_link(active + ("\n修改将在重连后生效" if edited else ""), GREEN)

    def _update_network_visibility(self):
        """沙盒和离线模式收起 Broker/账号/TLS 等联网输入区，界面更清爽"""
        network = self.app.mode in ("gw", "direct")
        if hasattr(self.app, "network_box"):
            self.app.network_box.setVisible(network)
        if hasattr(self.app, "offline_hint"):
            self.app.offline_hint.setVisible(not network)
            if not network:
                self.app.offline_hint.setText(
                    "沙盒模式在本地模拟全链路交互，无需配置 Broker；切到「云端 → GW」或「局域网直连」后填写连接信息。"
                    if self.app.mode == "sandbox"
                    else "离线分析仅查看导入的本地日志文件，不会联网或发送任何指令。"
                )
        if hasattr(self.app, "cloud_box"):
            self.app.cloud_box.setVisible(self.app.uav_cloud())
        if hasattr(self.app, "ssh_row"):
            self.app.ssh_row.setVisible(self.app.mode == "direct" and self.current_device() == "dog")

    def _mode_changed(self):
        mode = MODES[self.app.mode_box.currentText()]
        if mode in {"sandbox", "offline"}:
            if self.app.mqtt.client:
                self._connect()
            self.app.status.zlm_timer.stop()
        if not self._restoring:
            self._swap_uav_broker(self.app.mode, mode)
        self.app.mode = mode
        self._update_network_visibility()
        if hasattr(self.app, "connection_sheet"):
            self.app.connection_sheet.refit()
        sandbox = self.app.mode == "sandbox"
        self.app.control.scenario_row.setVisible(sandbox)
        self.app.control.send_btn.setEnabled(self.app.mode != "offline")
        self.app.control.send_btn.setText("模拟发送" if sandbox else "发送")
        if not self.app.mqtt.client:
            self._set_link({"sandbox": "沙盒模式，不联网", "offline": "离线分析，不发送"}.get(self.app.mode, "未连接"), GRAY)
        self.app.drafts.clear()
        if self.app.control._uav():
            self.app.control.mode_changed()
        else:
            self.app.control._template()
        if not self._restoring:
            self.save_device_state()
        if self.app.mode == "offline" and not self._restoring:
            self.app._go(self.app.LOGS)
            self.app.notify("离线分析只看本地历史：在这里导入日志文件")

    def _swap_uav_broker(self, old, new):
        """无人机的局域网直连和模拟云端各记一套 Broker（地址、端口、账号、密码、TLS），切方式时自动切换"""
        if self.current_device() != "uav" or old == new:
            return
        fields = (self.app.host, self.app.user, self.app.password)
        if old in ("direct", "gw"):
            settings().setValue(
                f"uav_broker_{old}",
                json.dumps([self.app.host.text().strip(), self.app.port.value(),
                            self.app.user.text(), self.app.password.text(), self.app.tls.isChecked()]),
            )
        saved = settings().value(f"uav_broker_{new}", "") if new in ("direct", "gw") else ""
        if saved:
            data = json.loads(saved)
            host, port, user, password = data[:4]
            for field, value in zip(fields, (host, user, password)):
                field.setText(value)
            self.app.port.setValue(port)
            if len(data) >= 5:
                self.app.tls.setChecked(bool(data[4]))

    def _frp_toggled(self, on):
        self.app.host.setEnabled(not on)
        self.app.port.setEnabled(not on)

    def _connect(self):
        if self.ssh_connecting:
            self.app.notify("正在登录设备，稍等")
            return
        if self.app.mqtt.client:
            self.app.teleop.remote.halt()
            self.app.mqtt.close()
            self._close_ssh()
            self.app._lock_device(False)
            self._set_link("已断开", GRAY)
            return
        if self.app.mode in {"sandbox", "offline"}:
            self.app.notify("先在上面选择“云端 → GW”或“局域网直连”")
            return
        device = self.current_device()
        if device == "uav" and self.app.frp.isChecked():
            self.app.notify("无人机不走 FRP：关掉“经远程（FRP）”")
            return
        cloud_sn = self.app.uav_sn.text().strip() if self.app.uav_cloud() else ""
        if self.app.uav_cloud() and not cloud_sn:
            self.app.notify("模拟云端要填无人机 SN")
            return
        # 独立保存该设备配置，并自动记录到当前设备的历史配置列表
        self.save_device_state(device)
        self._record_history()
        if self.app.mode == "direct" and self.app.frp.isChecked():
            self._connect_frp()
            return
        self.app.mqtt.connect(
            self.app.host.text().strip(), self.app.port.value(), self.app.sn.text().strip(),
            self.app.user.text(), self.app.password.text(), self.app.tls.isChecked(), device, cloud_sn,
        )
        self.app.uav_send.setChecked(False)
        self.app._lock_device(True)
        self.app.scene.reset_telemetry()
        self.app.scene.clear_cloud()
        self._set_link("连接中…", ORANGE)

    def _open_ssh(self, via_frp):
        sn = self.app.sn.text().strip()
        host = self.app.host.text().strip()
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        name = sn if via_frp else host
        return Remote(sn, host, self.app.ssh_user.text().strip(), self.app.ssh_password.text(), keys.get(name, ""), via_frp), name

    def _connect_frp(self):
        self.ssh_connecting = True
        self.app._lock_device(True)
        self._set_link(f"正在经 FRP 登录 {self.app.sn.text().strip()}…", ORANGE)

        def work():
            try:
                remote, name = self._open_ssh(True)
                host, port = broker_target(remote)
                self.app.inbox.put(("ssh_ready", remote, (name, remote.forward(port, host), remote.forward(80), f"{host}:{port}")))
            except Exception as error:
                self.app.inbox.put(("ssh_error", f"远程登录失败：{error}"))

        threading.Thread(target=work, daemon=True).start()

    def _ssh_ready(self, remote, name, mqtt_port, zlm_port, broker):
        self.ssh_connecting = False
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        keys[name] = remote.host_key
        settings().setValue("ssh_host_keys", json.dumps(keys))
        self.app.ssh, self.ssh_zlm = remote, zlm_port
        self.app.mqtt.connect("127.0.0.1", mqtt_port, self.app.sn.text().strip(), self.app.user.text(), self.app.password.text(), False)
        self.app.scene.reset_telemetry()
        self._set_link(f"已登录设备，正在连 Broker {broker}…", ORANGE)

    def _close_ssh(self):
        self.app.logs._stop_follow()
        if self.app.ssh:
            self.app.ssh.close()
        self.app.ssh, self.ssh_zlm, self.ssh_rtsp = None, 0, {}

    def _connected(self, note):
        if self.app.mqtt.connected:
            self._connection_hint()
            if self.app.mqtt.device == "uav":
                self.app._go(self.app.UAV)
            else:
                self.app.status._zlm_follow()
        else:
            self._set_link(str(note), RED)

    def _lost(self, note):
        self.app.teleop.remote.halt()
        self.app.mqtt.close()
        self._close_ssh()
        self.app._lock_device(False)
        self._set_link(str(note), RED)

    def _ssh_failed(self, note):
        self.ssh_connecting = False
        self.app._lock_device(False)
        self._set_link(note, RED)
