"""经 SSH 连到狗：远程走 FRP 的 HTTP CONNECT 代理，局域网直接连 22 端口

连上后可以把狗上的端口转到本机（MQTT 1883、ZLM 80），读 /userdata/gw_logs 下的日志文件、实时跟随当前日志，
列出和读取 /oem/ttaviation/gateway/db 下的配置文件。只做读：列目录、读文件、tail；不在狗上写、不重启服务
"""

import re
import shlex
import socket
import threading
from datetime import datetime

# FRP 接入（用户 2026-11-02 截图）：整机 SN 拼域名，经这个 HTTP 代理 CONNECT 到 SSH
FRP_PROXY = ("124.126.230.115", 22222)
LOG_ROOT = "/userdata/gw_logs"
# 各模块的配置文件（media.cfg、mqttBroker.cfg 等），普通账号读不了，要 sudo
CONFIG_ROOT = "/oem/ttaviation/gateway/db"
# 日志目录名 → 上位机里的模块名
LOG_DIRS = {"gateway": "gw", "vfc": "vfc", "ptz": "ptz", "media": "media", "vision": "vision", "fsvr": "fsvr"}
# 当前文件，和最新的编号文件是硬链接，列文件时跳过以免取两遍
CURRENT = "tta.log"
# 文件名里的开始时间：GW_0000011_20261102-154308.log 或 LOG_0000070_11-02_15-20.log
STARTED = (re.compile(r"_(\d{8})-(\d{6})\.log$"), re.compile(r"_(\d{2})-(\d{2})_(\d{2})-(\d{2})\.log$"))


def frp_host(sn):
    return f"{sn}.tta_edge.com"


def _proxy_socket(host, port):
    """向 FRP 代理发 HTTP CONNECT，返回已经接到对端 SSH 的套接字"""
    sock = socket.create_connection(FRP_PROXY, timeout=15)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    # 逐字节读到响应头结束：SSH 服务端会紧跟着发版本串，多读一点就会把它吞掉
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        byte = sock.recv(1)
        if not byte:
            raise OSError("FRP 代理断开了连接")
        head += byte
    status = head.split(b"\r\n")[0].decode("latin-1")
    if " 200 " not in status + " ":
        sock.close()
        raise OSError(f"FRP 代理拒绝：{status}（检查整机 SN 是否正确、设备是否在线）")
    return sock


def file_start(name, year):
    """日志文件名里的开始时间（本地时间戳）；认不出返回 None"""
    match = STARTED[0].search(name)
    if match:
        return datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").timestamp()
    match = STARTED[1].search(name)
    if match:
        return datetime(year, int(match[1]), int(match[2]), int(match[3]), int(match[4])).timestamp()
    return None


def pick_files(files, since):
    """按时间段挑文件：files 为 [(名字, 大小, 修改时间)]，since 为起点时间戳（None 表示全部）

    文件从“文件名里的开始时间”写到“修改时间”，和 [since, 现在] 有重叠就要；当前文件 tta.log 和最新编号文件是同一个，跳过
    """
    year = datetime.now().year
    chosen = []
    for name, size, mtime in files:
        if name == CURRENT or not name.endswith(".log"):
            continue
        if since is None or mtime >= since:
            chosen.append((name, size, mtime))
    return sorted(chosen, key=lambda f: file_start(f[0], year) or f[2])


class Remote:
    """一条到狗的 SSH 连接；host_key 为上次记下的主机密钥（base64），变了就拒绝"""

    def __init__(self, sn, host, user, password, host_key="", via_frp=True):
        import paramiko
        self.host = frp_host(sn) if via_frp else host
        sock = _proxy_socket(self.host, 22) if via_frp else socket.create_connection((host, 22), timeout=15)
        self.transport = paramiko.Transport(sock)
        self.transport.start_client(timeout=15)
        key = self.transport.get_remote_server_key()
        self.host_key = key.get_base64()
        # 第一次连接时记下主机密钥，之后对不上就不登录，免得把密码交给别的机器
        if host_key and host_key != self.host_key:
            self.transport.close()
            raise OSError(f"{self.host} 的主机密钥和上次不一样，已停止登录；确认设备确实换过系统后，清掉记下的密钥再连")
        self.transport.auth_password(user, password)
        # sudo 要密码时用同一个账号的密码（经 stdin 交给 sudo -S，不出现在命令行里）
        self.password = password
        self.transport.set_keepalive(15)
        self.servers, self.follows = [], []

    @property
    def alive(self):
        return self.transport.is_active()

    def forward(self, port, host="127.0.0.1"):
        """把狗能连到的 host:port（默认狗自己）转到本机一个空闲端口，返回本机端口；狗内网里的相机也经这条转"""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        self.servers.append(server)

        def accept():
            while True:
                try:
                    client, peer = server.accept()
                except OSError:
                    return
                try:
                    channel = self.transport.open_channel("direct-tcpip", (host, port), peer)
                except Exception:
                    client.close()
                    continue
                for source, target in ((client, channel), (channel, client)):
                    threading.Thread(target=_pipe, args=(source, target), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        return server.getsockname()[1]

    def _run(self, command):
        """在设备上执行一条只读命令，返回 (退出码, 标准输出字节, 标准错误文字)"""
        channel = self.transport.open_session()
        channel.exec_command(command)
        if command.startswith("sudo -S"):
            channel.sendall((self.password + "\n").encode())
        out, err = channel.makefile("rb").read(), channel.makefile_stderr("rb").read()
        return channel.recv_exit_status(), out, err.decode("utf-8", "replace").strip()

    def _privileged(self, command, what):
        """先用普通权限，不行再 sudo -n（免密的设备）、sudo -S（要密码的设备，用登录密码）；都不行抛 OSError 带设备给的原因"""
        errors = []
        for prefix in ("", "sudo -n ", "sudo -S -p '' "):
            code, out, err = self._run(prefix + command)
            if code == 0:
                return out
            errors.append(err)
        raise OSError(next((e for e in reversed(errors) if e), "") or f"读不了 {what}")

    def read_bytes(self, path):
        """读设备上一个文件的原始内容"""
        return self._privileged(f"cat {shlex.quote(path)}", path)

    def read_file(self, path):
        """读设备上一个文本文件"""
        return self.read_bytes(path).decode("utf-8", "replace")

    def list_files(self, root):
        """列 root 下（含子目录）的全部文件，返回相对 root 的路径，按名字排"""
        out = self._privileged(f"find {shlex.quote(root)} -type f", root).decode("utf-8", "replace")
        prefix = root.rstrip("/") + "/"
        return sorted(line[len(prefix):] if line.startswith(prefix) else line for line in out.splitlines() if line.strip())

    def list_logs(self, folder):
        """列 /userdata/gw_logs/<folder>/ 下的文件：[(名字, 大小, 修改时间)]"""
        sftp = self.transport.open_sftp_client()
        try:
            return [(a.filename, a.st_size, a.st_mtime) for a in sftp.listdir_attr(f"{LOG_ROOT}/{folder}")
                    if not str(a.longname).startswith("d")]
        finally:
            sftp.close()

    def download(self, folder, name, path, progress=lambda done: None, cancelled=lambda: False):
        """把一个日志文件读到本机 path；取消时停下，已写的部分留着"""
        sftp = self.transport.open_sftp_client()
        try:
            with sftp.open(f"{LOG_ROOT}/{folder}/{name}", "rb") as source, open(path, "wb") as out:
                source.prefetch()
                done = 0
                while not cancelled():
                    chunk = source.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done)
        finally:
            sftp.close()

    def follow(self, folder, on_line):
        """实时跟随 <folder>/tta.log 的新行，每行调 on_line(文字)；轮转后 tail -F 会接着跟新文件。返回停止用的函数"""
        channel = self.transport.open_session()
        channel.exec_command("tail -n 0 -F " + shlex.quote(f"{LOG_ROOT}/{folder}/{CURRENT}") + " 2>/dev/null")
        self.follows.append(channel)

        def read():
            pending = b""
            while True:
                try:
                    data = channel.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                pending += data
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    on_line(line.decode("utf-8", "replace"))

        threading.Thread(target=read, daemon=True).start()
        return channel.close

    def close(self):
        for server in self.servers:
            server.close()
        for channel in self.follows:
            channel.close()
        self.transport.close()


def _pipe(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except (OSError, EOFError):
        pass
    finally:
        for end in (source, target):
            # 隧道关闭时另一头可能已经由 SSH 传输层关掉，关已断开的通道会抛各种异常，这里只是收尾
            try:
                end.close()
            except Exception:
                pass
