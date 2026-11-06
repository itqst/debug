"""增量解析日志和会话 JSONL，保留来源偏移和截断记录"""

import hashlib
import json
import re
from pathlib import Path

from .core import DECODER, Event, infer_module

TOPIC = re.compile(r"(?:local/robotdog/[\w/]+|tta/robotdog/[\w/]+)")
MAX_RECORD = 1024 * 1024
SOURCE_TIME = re.compile(r"(?:\d{4}[-/]\d{2}[-/]\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?")
MODULE_PRIORITY = ("media", "vision", "fsvr", "ptz", "vfc", "gw")
# 用于小写后的文本；忽略大小写的正则在长行上明显更慢
MODULE_WORD = re.compile(r"(?:tta_|\b)(media|vision|fsvr|ptz|vfc|gw|gateway)(?:\b|_)")


def first_object(text):
    """text 里第一个 { 起的完整 JSON 对象；外层尚未完整时返回 None，不把内层 data 当成完整业务消息"""
    try:
        return DECODER.raw_decode(text, text.find("{"))[0]
    except ValueError:
        return None


def detect_encoding(path):
    """判断日志编码：UTF-8 BOM → utf-8-sig；UTF-16 BOM 不支持（抛 ValueError）；
    否则取开头 4 MB 严格按 UTF-8 解，解不了再试 GB18030（兼容 GBK/GB2312），都不行按 UTF-8 并逐行标记未解码
    """
    with open(path, "rb") as source:
        head = source.read(4 * 1024 * 1024)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("UTF-16 编码的日志暂不支持，请转成 UTF-8 后再导入")
    # 采样可能在多字节字符中间截断，去掉末尾不完整的一行再判断
    sample = head[:head.rfind(b"\n") + 1] if len(head) == 4 * 1024 * 1024 and b"\n" in head else head
    for encoding in ("utf-8", "gb18030"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def import_log(path, sn, session, cancelled=lambda: False, stats=None):
    """按有界记录流式读取；调用方迭代驱动，取消不修改原始日志

    stats 为字典时持续写入已读字节 bytes、编码 encoding、解码失败行数 undecoded 和前 10 个行号 undecoded_lines，
    读完后写入整文件 sha256；取消时没有 sha256。解码失败的行用替换字符保留，原文行号和偏移不变，可回原文核对
    """
    path = Path(path)
    digest = hashlib.sha256()
    stats = {} if stats is None else stats
    stats["bytes"], stats["undecoded"], stats["undecoded_lines"] = 0, 0, []
    encoding = stats["encoding"] = detect_encoding(path)
    line_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
    source_name = "log:" + path.name
    pending, start_line, start_offset = "", 0, 0
    with path.open("rb") as source:
        number, position = 0, 0
        while True:
            if cancelled():
                return
            offset = position
            data = source.readline(MAX_RECORD)
            if not data:
                break
            position += len(data)
            digest.update(data)
            stats["bytes"] = position
            number += 1
            if number == 1 and encoding == "utf-8-sig":
                data = data[3:]
            try:
                text = data.decode(line_encoding)
            except UnicodeDecodeError:
                # 混合编码或损坏的行：保留可读部分和原文位置，不静默当成正常文本
                text = data.decode(line_encoding, errors="replace")
                stats["undecoded"] += 1
                if len(stats["undecoded_lines"]) < 10:
                    stats["undecoded_lines"].append(number)
            if not pending:
                start_line, start_offset = number, offset
            pending += text
            value = first_object(pending) if "{" in pending else None
            if value is not None or "{" not in pending or len(pending) >= MAX_RECORD:
                yield log_event(pending, value, path, source_name, sn, session, start_line, start_offset)
                pending = ""
        if pending:
            yield log_event(pending, None, path, source_name, sn, session, start_line, start_offset)
    stats["sha256"] = digest.hexdigest()


def log_event(text, value, path, source_name, sn, session, line, offset):
    value = value or {}
    # 订阅记录工具抓的 jsonl（nav 的抓包）：一行 {t/wall, topic, retain, body}
    if "topic" in value and "body" in value and "payload" not in value:
        body = value["body"]
        event = Event(topic=value["topic"], payload=body if isinstance(body, dict) else {}, source=source_name, sn=sn, session=session,
                      file=str(path), line=line, offset=offset, raw=text[:MAX_RECORD], retained=bool(value.get("retain")))
        event.source_time = str(value.get("wall") or "")
        event.module = infer_module(event.topic, event.payload)
        return event
    if isinstance(value.get("payload"), dict) and "topic" in value:
        allowed = Event.__dataclass_fields__
        record = {k: v for k, v in value.items() if k in allowed}
        # 用户选择的导入会话隔离同 tid，原连接信息保留在原文
        record.update(session=session, sn=str(value.get("sn") or sn), file=str(path), line=line, offset=offset)
        return Event(**record)
    topic = TOPIC.search(text) if "robotdog/" in text else None
    topic = topic[0] if topic else ""
    event = Event(topic=topic, payload=value, source=source_name, sn=sn,
                  session=session, file=str(path), line=line, offset=offset, raw=text[:MAX_RECORD])
    source_time = SOURCE_TIME.search(text, 0, 160)
    event.source_time = source_time[0] if source_time else ""
    # payload 取自原文的第一个 JSON 且原文未截断时，入库可只存原文
    event.payload_from_raw = bool(value) and len(text) <= MAX_RECORD
    event.module = infer_module(topic, value)
    if event.module == "unknown":
        # 文本里出现多个模块名时按固定优先级取，不按出现位置
        # 设备日志里 GW 自称 tta_gateway
        found = {"gw" if m == "gateway" else m for m in MODULE_WORD.findall(text.lower())}
        event.module = next((m for m in MODULE_PRIORITY if m in found), "unknown")
    return event
