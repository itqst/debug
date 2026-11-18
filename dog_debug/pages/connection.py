"""左侧连接卡：连接方式、连接配置、经 FRP 的 SSH 隧道和连接状态"""

import json
import threading

from PySide6.QtWidgets import QInputDialog

from ..remote import Remote
from ..ui import broker_target, DEVICES, GRAY, GREEN, MODES, ORANGE, RED, settings


class ConnectionPanel:
    """左侧连接卡：连接方式、配置、FRP/SSH 登录、连接状态文字"""

    def __init__(self, app):
        self.app = app
        # 配置下拉上次生效的那项，选了不能用的配置时退回去
        self.profile_index = 0

    def restore_connection(self):
        """填回上次点“连接”时的连接参数；只填不连，没记过就保持默认的沙盒"""
        saved = settings().value("connection", "")
        if not saved:
            return
        config = json.loads(saved)
        self._profile_chosen_config(config)
        self.app.password.setText(config.get("password", ""))

    def _load_profiles(self, select=None):
        self.app.profile_box.clear()
        self.app.profile_box.addItem("手动填写", None)
        for name, config in self.app.store.connections().items():
            self.app.profile_box.addItem(name, config)
        if select:
            self.app.profile_box.setCurrentText(select)
        self.profile_index = self.app.profile_box.currentIndex()

    def _profile_chosen(self, index):
        config = self.app.profile_box.itemData(index)
        device = next(k for k, v in DEVICES.items() if v == (config or {}).get("device", "dog"))
        # 连着或正在登录时换设备，侧栏会换成另一种设备而连接还是原来的，先断开再换
        if config and (self.app.mqtt.client or self.ssh_connecting) and device != self.app.device_box.currentText():
            self.app.profile_box.setCurrentIndex(self.profile_index)
            self.app.notify(f"这个配置是{device}，先断开当前连接再选")
            return
        self.profile_index = index
        if config:
            self._profile_chosen_config(config)

    def _profile_chosen_config(self, config):
        # 旧版的“模拟 GW → 真实模块”发出的内容和直连一样，已合并到“直连模块”
        mode = "direct" if config["mode"] == "mock_gw" else config["mode"]
        self.app.mode_box.setCurrentText(next(k for k, v in MODES.items() if v == mode))
        self.app.host.setText(config["host"])
        self.app.port.setValue(config["port"])
        self.app.sn.setText(config["sn"])
        self.app.user.setText(config["user"])
        self.app.tls.setChecked(config["tls"])
        self.app.frp.setChecked(config.get("frp", False))
        self.app.device_box.setCurrentText(next(k for k, v in DEVICES.items() if v == config.get("device", "dog")))
        self.app.password.clear()

    def _save_profile(self):
        default = self.app.profile_box.currentText() if self.app.profile_box.currentData() else f"{self.app.host.text().strip()} · {self.app.sn.text().strip()}"
        name, ok = QInputDialog.getText(self.app, "保存连接配置", "名称（密码不保存）", text=default)
        if not ok or not name.strip():
            return
        self.app.store.save_connection(name.strip(), {"mode": self.app.mode, "host": self.app.host.text().strip(), "port": self.app.port.value(),
                                                  "sn": self.app.sn.text().strip(), "user": self.app.user.text().strip(),
                                                  "tls": self.app.tls.isChecked(),
                                                  "device": DEVICES[self.app.device_box.currentText()]})
        self._load_profiles(name.strip())
        self.app.notify(f"已保存连接配置：{name.strip()}")

    def _delete_profile(self):
        if self.app.profile_box.currentData():
            self.app.store.delete_connection(self.app.profile_box.currentText())
            self._load_profiles()

    def _set_link(self, text, color):
        self.app.link.setText(text)
        self.app.link_dot.setStyleSheet(f"background:{color};border-radius:4px")
        self.app.connect_btn.setText("断开" if self.app.mqtt.client else "连接")
        self.app.connect_btn.setObjectName("danger" if self.app.mqtt.client else "primary")
        self.app.connect_btn.style().polish(self.app.connect_btn)
        self.app.scene3d._device_card()
        self.app._update_pill()
        # 断开、换连接后保留消息的清除按钮跟着藏起来
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
            # 无人机按连上的 Broker 认设备，板上的 sn 现在是空串
            active = f"已连接无人机 {self.app.mqtt.host}"
            edited = self.app.host.text().strip() != self.app.mqtt.host
        else:
            active = f"已连接 {self.app.mqtt.host} · {self.app.mqtt.sn}"
            edited = (self.app.host.text().strip(), self.app.sn.text().strip()) != (self.app.mqtt.host, self.app.mqtt.sn)
        self._set_link(active + ("\n左侧修改在重连后生效" if edited else ""), GREEN)

    def _mode_changed(self):
        mode = MODES[self.app.mode_box.currentText()]
        # 沙盒和离线不接实时数据：断开前还按原模式发停止，否则停止会被离线拦下或被沙盒吞掉
        if mode in {"sandbox", "offline"}:
            if self.app.mqtt.client:
                self._connect()
            self.app.status.zlm_timer.stop()
        self._swap_uav_broker(self.app.mode, mode)
        self.app.mode = mode
        self.app.cloud_box.setVisible(self.app.uav_cloud())
        # 无人机只直连板上 Broker，不走 FRP
        self.app.ssh_row.setVisible(mode == "direct" and self.app.device_box.currentText() == "机器狗")
        # 面板开着时切方式，多出或少了几行：各行显示与否都定了再量高度
        self.app.connection_sheet.refit()
        sandbox = self.app.mode == "sandbox"
        self.app.control.scenario_row.setVisible(sandbox)
        self.app.control.send_btn.setEnabled(self.app.mode != "offline")
        self.app.control.send_btn.setText("模拟发送" if sandbox else "发送")
        if not self.app.mqtt.client:
            self._set_link({"sandbox": "沙盒模式，不联网", "offline": "离线分析，不发送"}.get(self.app.mode, "未连接"), GRAY)
        self.app.drafts.clear()
        # 无人机两种方式的目录不一样（模拟云端不列 Bridge 命令）
        if self.app.control._uav():
            self.app.control.mode_changed()
        else:
            self.app.control._template()
        if self.app.mode == "offline":
            self.app._go(self.app.LOGS)
            self.app.notify("离线分析只看本地历史：在这里导入日志文件")

    def _swap_uav_broker(self, old, new):
        """无人机的模拟 GW 和模拟云端各记一套 Broker（地址、端口、账号、密码，本机设置里）：切方式时存下旧的、填上新的；
        云端 Broker 可能和板上本地 Broker 不是同一个；没记过的保持当前填写"""
        if self.app.device_box.currentText() != "无人机" or old == new:
            return
        fields = (self.app.host, self.app.user, self.app.password)
        if old in ("direct", "gw"):
            settings().setValue(f"uav_broker_{old}", json.dumps([self.app.host.text().strip(), self.app.port.value(),
                                                                self.app.user.text(), self.app.password.text()]))
        saved = settings().value(f"uav_broker_{new}", "") if new in ("direct", "gw") else ""
        if saved:
            host, port, user, password = json.loads(saved)
            for field, value in zip(fields, (host, user, password)):
                field.setText(value)
            self.app.port.setValue(port)

    def _frp_toggled(self, on):
        # 经远程时 Broker 就是狗自己，地址由整机 SN 定，不用填
        self.app.host.setEnabled(not on)
        self.app.port.setEnabled(not on)

    def _connect(self):
        if self.ssh_connecting:
            self.app.notify("正在登录设备，稍等")
            return
        if self.app.mqtt.client:
            # 断开前先发停止，断开后就发不出了
            self.app.teleop.remote.halt()
            self.app.mqtt.close()
            self._close_ssh()
            self.app._lock_device(False)
            self._set_link("已断开", GRAY)
            return
        if self.app.mode in {"sandbox", "offline"}:
            self.app.notify("先在上面选择“云端 → 真实 GW”或“直连模块”")
            return
        device = DEVICES[self.app.device_box.currentText()]
        if device == "uav" and self.app.frp.isChecked():
            self.app.notify("无人机不走 FRP：关掉“经远程（FRP）”")
            return
        cloud_sn = self.app.uav_sn.text().strip() if self.app.uav_cloud() else ""
        if self.app.uav_cloud() and not cloud_sn:
            self.app.notify("模拟云端要填无人机 SN")
            return
        # 记住这次的连接（含密码，本机自用），下次启动自动填上
        settings().setValue("connection", json.dumps({
            "mode": self.app.mode, "host": self.app.host.text().strip(), "port": self.app.port.value(), "sn": self.app.sn.text().strip(),
            "user": self.app.user.text(), "password": self.app.password.text(), "tls": self.app.tls.isChecked(),
            "frp": self.app.frp.isChecked(), "device": device}))
        if self.app.mode == "direct" and self.app.frp.isChecked():
            self._connect_frp()
            return
        self.app.mqtt.connect(self.app.host.text().strip(), self.app.port.value(), self.app.sn.text().strip(),
                          self.app.user.text(), self.app.password.text(), self.app.tls.isChecked(), device, cloud_sn)
        if device == "uav" and self.app.mode in ("direct", "gw"):
            settings().setValue(f"uav_broker_{self.app.mode}", json.dumps([self.app.host.text().strip(), self.app.port.value(),
                                                                           self.app.user.text(), self.app.password.text()]))
        # 新连接回到只读，开关跟着关上；连着的时候不能换设备，免得页面是无人机的、连着的却是狗
        self.app.uav_send.setChecked(False)
        self.app._lock_device(True)
        self.app.scene.reset_telemetry()
        self.app.scene.clear_cloud()
        self._set_link("连接中…", ORANGE)

    def _open_ssh(self, via_frp):
        """在后台线程里调用：按当前填写登录设备的 SSH；远程走 FRP，局域网连 Broker 地址的 22 端口"""
        sn = self.app.sn.text().strip()
        host = self.app.host.text().strip()
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        name = sn if via_frp else host
        return Remote(sn, host, self.app.ssh_user.text().strip(), self.app.ssh_password.text(), keys.get(name, ""), via_frp), name

    def _connect_frp(self):
        """经 FRP 登录狗、把 1883 和 80 转到本机，再按直连的方式连 Broker；登录在后台，结果进 inbox"""
        self.ssh_connecting = True
        self.app._lock_device(True)
        self._set_link(f"正在经 FRP 登录 {self.app.sn.text().strip()}…", ORANGE)

        def work():
            try:
                remote, name = self._open_ssh(True)
                host, port = broker_target(remote)
                self.app.inbox.put(("ssh_ready", remote, (name, remote.forward(port, host), remote.forward(80), f"{host}:{port}")))
            except Exception as error:
                # 代理、网络、认证出错的类型各不相同，原样告诉用户
                self.app.inbox.put(("ssh_error", f"远程登录失败：{error}"))

        threading.Thread(target=work, daemon=True).start()

    def _ssh_ready(self, remote, name, mqtt_port, zlm_port, broker):
        self.ssh_connecting = False
        keys = json.loads(settings().value("ssh_host_keys", "{}"))
        keys[name] = remote.host_key
        settings().setValue("ssh_host_keys", json.dumps(keys))
        self.app.ssh, self.ssh_zlm = remote, zlm_port
        # 设备身份仍是整机 SN；127.0.0.1 只是隧道的本机入口
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
        """MQTT 掉线：先收尾遥控再关连接和隧道"""
        self.app.teleop.remote.halt()
        self.app.mqtt.close()
        self._close_ssh()
        self.app._lock_device(False)
        self._set_link(str(note), RED)

    def _ssh_failed(self, note):
        self.ssh_connecting = False
        self.app._lock_device(False)
        self._set_link(note, RED)
