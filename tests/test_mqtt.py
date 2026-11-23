"""用本地 MQTT 协议接收端核客户端收发，不连接产品 Broker"""

import json
import queue
import socket
import struct
import threading
import time

from dog_debug.transport import MqttTransport


def receive(sock):
    first = sock.recv(1)
    if not first:
        return None, b""
    size, multiplier = 0, 1
    while True:
        byte = sock.recv(1)[0]
        size += (byte & 127) * multiplier
        if byte < 128:
            break
        multiplier *= 128
    body = b""
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        if not chunk:
            raise RuntimeError("packet truncated")
        body += chunk
    return first[0] >> 4, body


def packet(kind, body):
    length = len(body)
    header = bytearray([kind])
    while True:
        value, length = length % 128, length // 128
        header.append(value | (128 if length else 0))
        if not length:
            break
    return bytes(header) + body


def test_real_paho_socket_publish_and_subscription():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(5)
    published = []
    errors = []

    def broker():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(5)
                assert receive(conn)[0] == 1
                conn.sendall(b"\x20\x02\x00\x00")
                while True:
                    kind, body = receive(conn)
                    if kind in (None, 14):
                        break
                    if kind == 8:
                        conn.sendall(packet(0x90, body[:2] + b"\x00\x00"))
                        topic = b"local/robotdog/media/ack"
                        payload = json.dumps({"method": "take_photo", "tid": "wire", "data": {"result": 0}}).encode()
                        conn.sendall(packet(0x30, struct.pack("!H", len(topic)) + topic + payload))
                    elif kind == 3:
                        size = struct.unpack("!H", body[:2])[0]
                        published.append((body[2:2+size].decode(), json.loads(body[2+size:])))
                    elif kind == 12:
                        conn.sendall(b"\xd0\x00")
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=broker, daemon=True)
    thread.start()
    inbox = queue.Queue()
    transport = MqttTransport(inbox)
    try:
        transport.connect("127.0.0.1", server.getsockname()[1], "TEST")
        until = time.monotonic() + 5
        event = None
        while time.monotonic() < until:
            kind, value = inbox.get(timeout=5)
            if kind == "event":
                event = value
                break
        assert event and event.payload["tid"] == "wire"
        transport.publish("tta/robotdog/TEST/services", {"method": "take_photo", "tid": "wire", "data": {}})
        while not published and time.monotonic() < until:
            time.sleep(0.01)
        assert published[0][0] == "tta/robotdog/TEST/services"
    finally:
        transport.close()
        thread.join(timeout=2)
        server.close()
    assert not errors
