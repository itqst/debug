"""保存脱敏消息并按明确证据诊断请求，未知不推断成功"""

import functools
import itertools
import json
import re
import sqlite3
import time
import uuid
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

from .pointcloud import cloud_data, slim
from .registry import NAV, UPLOAD, find

SENSITIVE = re.compile(r"(?i)^(?:.*password|passwd|pwd|.*secret|.*token|authorization|credentials|accesskeyid|accesskeysecret|api[_-]?key|signature|auth_key.*)$")
TEXT_SECRET = re.compile(r'''(?ix)(["']?(?:password|passwd|pwd|secret|accessKeyId|accessKeySecret|securityToken|token|authorization|auth_key\w*)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)''')
# 只抹掉 URL 中的账号密码和签名类参数，streamid、房间等定位信息保留
URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s\"']+:[^/@\s\"']+@")
URL_SECRET = re.compile(r"(?i)([?&](?:auth_key\w*|token|security_?token|signature|sign|secret|access_?key\w*|x-oss-[\w-]+)=)[^&\s\"'#]+")

# 带同请求 tid 的文件业务终态，可据此更新请求结论
UPLOAD_STATES = {"media_file_upload_status", "sync_map_file_status", "detection_upload_status"}
# 所有脱敏规则（字段名和文本）都至少需要其中一个关键字；先做一次便宜的检查，大日志里绝大多数内容可以直接跳过
# 用小写后的子串查找而不是忽略大小写的正则：后者在长字符串上慢一个数量级
SECRET_HINTS = ("pass", "pwd", "secret", "token", "auth", "credential", "api_key", "api-key", "apikey",
                "access_key", "accesskey", "sign", "x-oss-", "@")


def secret_hint(text):
    low = text.lower()
    return any(hint in low for hint in SECRET_HINTS)


# 同一批字段名在日志里反复出现，缓存判定结果
@functools.lru_cache(maxsize=4096)
def _sensitive(key):
    return bool(SENSITIVE.match(key))


def redact(value):
    """进入数据库前脱敏，关联字段保留，不回写用户原文件"""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _sensitive(key) else redact(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and secret_hint(value):
        value = re.sub(r"(?im)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s\"']+", r"\1[REDACTED]", value)
        value = TEXT_SECRET.sub(r"\1[REDACTED]", value)
        value = URL_USERINFO.sub(r"\1[REDACTED]@", value)
        value = URL_SECRET.sub(r"\1[REDACTED]", value)
    return value


# 事件 ID 只需在本进程内唯一（界面靠它恢复选中行）；uuid4 在大日志导入里占两成耗时
_EVENT_PREFIX = uuid.uuid4().hex[:10] + "-"
# 无人机本地 MQTT 和 ROS 之间的 Bridge，报文外壳和狗、VFC 都不一样（`nav/MQTT接口字段说明_20261105_源码版.md`）
BRIDGE = "local/uav/bridge/"
_EVENT_COUNTER = itertools.count()


@dataclass
class Event:
    topic: str
    payload: dict = field(default_factory=dict)
    direction: str = "接收"
    source: str = "mqtt"
    sn: str = ""
    session: str = ""
    module: str = ""
    timestamp: float = field(default_factory=time.time)
    retained: bool = False
    duplicate: bool = False
    qos: int | None = None
    file: str = ""
    line: int = 0
    offset: int = 0
    raw: str = ""
    source_time: str = ""
    event_id: str = field(default_factory=lambda: _EVENT_PREFIX + format(next(_EVENT_COUNTER), "x"))
    # 不加注解，不是 dataclass 字段、不入库；日志导入时为 True 表示 payload 可从 raw 重新解析
    payload_from_raw = False

    @property
    def method(self):
        # 无人机 Bridge 的指令和回执用 action 指明动作，状态和事件用 type
        if self.topic.startswith(BRIDGE):
            return str(self.payload.get("action") or self.payload.get("type") or "未解析")
        # 无人机 GW 往云端报的上线没有 method，type 是设备类型（uav），不是消息类型
        if self.topic == "tta/uav/status/online":
            return "online"
        value = self.payload.get("method") or self.payload.get("type")
        # 内部抓拍回复的契约不要求带 method，按所在 Topic 归到它回复的请求
        if not value and self.topic.endswith("/detect/ack"):
            return "capture_detection"
        # OSD 和心跳报文本身不带 method，按 Topic 认出来，不显示成“未解析”
        if not value and self.topic.endswith("/osd"):
            return "osd"
        if not value and self.topic.endswith("/heartbeat"):
            return "heartbeat"
        # ros2bridge 的点云只有 data.type=pointcloud，根上没有 method
        if not value and self.topic.endswith("/ros2bridge/data"):
            return "pointcloud"
        return str(value or "未解析")

    @property
    def tid(self):
        # Bridge 按 id 配对指令和回执（同一个 id 先回 accepted 再回 final），当作请求号；
        # 加前缀和 VFC 的 tid 分开：tid 由上游随意给，可能和某个 Bridge id 是同一个字符串
        if self.topic.startswith(BRIDGE):
            return "bridge:" + str(self.payload["id"]) if self.payload.get("id") else ""
        return str(self.payload.get("tid", ""))

    @property
    def kind(self):
        if self.topic.endswith(("/ack", "/reply", "_reply")):
            return "ACK"
        if self.topic.endswith(("/command", "/services")):
            return "指令"
        if self.topic.endswith("/events"):
            return "事件"
        # 无人机 GW 往云端报的上线（tta/uav/status/online）当 GW 的状态看，GW 卡片和详情里能看到；狗的不动
        if self.topic == "tta/uav/status/online":
            return "状态"
        # Bridge 的状态分领域发在 state/<领域> 下
        if self.topic.endswith(("/state", "/heartbeat", "/data", "/osd", "/fcu")) or self.topic.startswith(BRIDGE + "state/"):
            return "状态"
        return "日志"

    def clean(self):
        # 逐字段浅拷贝即可，redact 会为 payload 生成新对象，不改动原事件
        record = {name: getattr(self, name) for name in self.__dataclass_fields__}
        record["payload"] = redact(self.payload)
        record["raw"] = redact(self.raw)
        return record


RAW_PAYLOAD = "payload_in_raw"
DECODER = json.JSONDecoder()


def load_event(body):
    """库里的 body 还原成 Event；payload 省略存储的行从 raw 重新解析"""
    record = json.loads(body)
    if record.pop(RAW_PAYLOAD, None):
        raw = record.get("raw", "")
        record["payload"] = DECODER.raw_decode(raw, raw.find("{"))[0]
    return Event(**record)


def full_body(body):
    """导出用：把省略 payload 的行补全，导出的 JSONL 可以原样再导入"""
    if RAW_PAYLOAD not in body:
        return body
    record = json.loads(body)
    record.pop(RAW_PAYLOAD)
    raw = record.get("raw", "")
    record["payload"] = DECODER.raw_decode(raw, raw.find("{"))[0]
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


# 有固定默认值的字段（timestamp、event_id 每次生成，不在其中）
STATIC_DEFAULTS = {f.name: f.default for f in fields(Event) if f.default is not MISSING}


def infer_module(topic, payload):
    match = re.search(r"local/(?:robotdog|uav)/(\w+)/", topic)
    if match:
        return match[1]
    # 无人机云端主题（GW 转过的）：指令和回执看 target，状态和 OSD 是 VFC 的，上线和心跳是 GW 自己的；
    # 上线 tta/uav/status/online 先认，不然 status 会被当成 SN、上线记到 VFC 名下
    if topic.startswith("tta/uav/status/"):
        return "gw"
    match = re.match(r"tta/uav/[^/]+/(\w+)$", topic)
    if match:
        if match[1] in ("services", "services_reply"):
            return str(payload.get("target") or "vfc")
        # storage_status 是 GW 自己报的（磁盘容量、待传文件数），其余状态和 OSD 是 VFC 经 GW 转的
        return "gw" if match[1].startswith("heartbeat") or payload.get("method") == "storage_status" else "vfc"
    data = payload.get("data", {})
    name = payload.get("name") or (data.get("name") if isinstance(data, dict) else "")
    return str(name or payload.get("target") or (find(payload.get("method")) or {}).get("module") or "unknown")


class Store:
    """SQLite 连接只供所属线程使用；采集线程用队列交给拥有者"""

    def __init__(self, path=":memory:", reader=False):
        """reader 为 True 时以只读方式打开同一个库文件，给后台检索/导出线程用，可与写入并发"""
        self.path = str(path)
        if reader:
            self.db = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
            self.db.execute("PRAGMA cache_size=-65536")
            return
        self.db = sqlite3.connect(self.path)
        # 写入次数：状态行和其他行（指令、ACK、事件、日志）分开计，界面据此判断请求列表、状态总览要不要重算
        self.status_version, self.request_version = 0, 0
        # 请求结论按 (sn, session, tid) 缓存，这个 tid 进了新消息才重算；无人机详情等反复列请求时不用每次重新配对
        self.request_cache = {}
        # WAL + NORMAL：崩溃最多丢最后一批未提交的消息，换来批量写入快很多
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # 默认 2 MB 缓存在几 GB 的库上写索引会频繁读盘，导入时主线程卡顿可达数秒
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, stamp REAL, sn TEXT, session TEXT, module TEXT, method TEXT, tid TEXT, kind TEXT, body TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS scope ON events(sn,session,tid)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_tid ON events(tid)")
        # 状态详情按方法筛选、按 method 取最新消息；已有的大库首次打开会建一次索引
        self.db.execute("CREATE INDEX IF NOT EXISTS by_method ON events(method, id)")
        # previous() 找同设备同方法时间上的前一条（状态详情的字段对比），没有它要按 stamp 临时排序，一次几毫秒到几十毫秒
        self.db.execute("CREATE INDEX IF NOT EXISTS by_device_method ON events(sn, method, stamp)")
        # 状态详情的“指令与回复”按模块取指令和 ACK；不走索引时状态多、指令少的模块要扫全表
        self.db.execute("CREATE INDEX IF NOT EXISTS by_module_kind ON events(module, kind, id)")
        # 不筛模块、只按类型取最新消息时用；没有它要倒着扫全表，状态多的库一次两三百毫秒
        self.db.execute("CREATE INDEX IF NOT EXISTS by_kind ON events(kind, id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notes (sn TEXT, session TEXT, tid TEXT, body TEXT, updated REAL, "
                        "PRIMARY KEY (sn, session, tid))")
        self.db.execute("CREATE TABLE IF NOT EXISTS connections (name TEXT PRIMARY KEY, body TEXT)")
        self._prefix_bridge_ids()

    def _prefix_bridge_ids(self):
        """旧版存的 Bridge 请求号没有“bridge:”前缀，打开时补上，和现在 Event.tid 一致；再打开时已补过的不动

        按 module 列找 Bridge 的行（走 by_module_kind 索引），大库也快。备注只在这个编号只属于 Bridge 时跟着改；
        同一会话里 VFC 也用了同一个字符串时，旧备注分不清是谁的，留在原编号上
        """
        old = "module='bridge' AND tid!='' AND tid NOT LIKE 'bridge:%'"
        keys = self.db.execute(f"SELECT DISTINCT sn, session, tid FROM events WHERE {old}").fetchall()
        if not keys:
            return
        for sn, session, tid in keys:
            shared = self.db.execute("SELECT 1 FROM events WHERE sn=? AND session=? AND tid=? AND module!='bridge' LIMIT 1",
                                     (sn, session, tid)).fetchone()
            if not shared:
                self.db.execute("UPDATE OR IGNORE notes SET tid='bridge:'||tid WHERE sn=? AND session=? AND tid=?", (sn, session, tid))
        self.db.execute(f"UPDATE events SET tid='bridge:'||tid WHERE {old}")
        self.db.commit()

    @staticmethod
    def row(event):
        """事件转成待插入的行并完成脱敏；不碰数据库，可在采集线程里调用

        等于静态默认值的字段不写入 body，读回时由 Event 默认值补齐，库体积小很多
        """
        if not event.module:
            event.module = infer_module(event.topic, event.payload)
        record = {k: v for k in event.__dataclass_fields__
                  if (v := getattr(event, k)) != STATIC_DEFAULTS.get(k, MISSING)}
        # 点云一条约 50 KB，原始点只在内存里给 3D 用，库里只存元数据；原文也换成瘦身后的报文
        if cloud_data(event) is not None:
            record["payload"] = slim(event.payload)
            record["raw"] = json.dumps(record["payload"], ensure_ascii=False)
        body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        # 整条序列化后没有任何敏感关键字就不必逐层脱敏
        if secret_hint(body):
            record["payload"], record["raw"] = redact(event.payload), redact(event.raw)
            body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        elif event.payload_from_raw:
            # 日志行的 payload 就是原文里的第一个 JSON，读回时重新解析，库体积约减半；含敏感词的行仍按字段脱敏后存全量
            del record["payload"]
            record[RAW_PAYLOAD] = 1
            body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        return event.timestamp, event.sn, event.session, event.module, event.method, event.tid, event.kind, body

    def add(self, event):
        self.add_rows([self.row(event)])

    def add_rows(self, rows):
        statuses = sum(row[6] == "状态" for row in rows)
        # 上传进度是状态，但会改变请求结论（上传中 → 完成/失败），两边都算
        uploads = sum(row[6] == "状态" and row[4] in UPLOAD_STATES for row in rows)
        self.status_version += statuses
        self.request_version += len(rows) - statuses + uploads
        for row in rows:
            if row[5]:
                self.request_cache.pop((row[1], row[2], row[5]), None)
        self.db.executemany("INSERT INTO events(stamp,sn,session,module,method,tid,kind,body) VALUES(?,?,?,?,?,?,?,?)", rows)

    def flush(self):
        self.db.commit()

    def query(self, module="", method="", kind="", text="", limit=1000, before=None, sn="", live=False, methods=None, session="",
              prefixes=(), device=""):
        """按条件取最新的 limit 条，返回 [(行号, 事件)]；before 为行号时只取更早的，用于翻页

        kind 可以是一组类型；methods 为列表时只要这些方法，prefixes 只要以这些前缀开头的方法（如 Bridge 的 "navigation."）。条件都在 SQL 里，先筛再取 limit 条，不会被别的消息挤掉
        sn 只取这台设备的；live 为真时不要导入的日志（会话以 import- 开头），实时总览不被历史日志顶掉；
        device 为 "dog" 时不要无人机的消息（local/uav/，旧版把狗的 SN 记在过无人机消息上），为 "uav" 时只要无人机的
        """
        clauses, params = [], []
        if isinstance(kind, tuple):
            clauses.append("kind IN (" + ",".join("?" * len(kind)) + ")")
            params.extend(kind)
            kind = ""
        if methods is not None:
            clauses.append("method IN (" + ",".join("?" * len(methods)) + ")")
            params.extend(methods)
        if prefixes:
            # 前缀只有字母、点和下划线，GLOB 里没有要转义的
            clauses.append("(" + " OR ".join("method GLOB ?" for _ in prefixes) + ")")
            params.extend(prefix + "*" for prefix in prefixes)
        if sn:
            # 一元 + 让 SQLite 不走 (sn, session, tid) 索引：按主键倒着扫、取够就停，不必把这台设备的消息全取出来再排序
            clauses.append("+sn=?")
            params.append(sn)
        if live:
            clauses.append("session NOT LIKE 'import-%'")
        if device:
            # 本地主题和模拟云端时的云端主题都算无人机的
            uav = "(body LIKE '%\"topic\":\"local/uav/%' OR body LIKE '%\"topic\":\"tta/uav/%')"
            clauses.append(uav if device == "uav" else "NOT " + uav)
        if session:
            clauses.append("session=?")
            params.append(session)
        if before is not None:
            clauses.append("id<?")
            params.append(before)
        for column, value in (("module", module), ("method", method), ("kind", kind)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if text:
            clauses.append("instr(lower(body),lower(?))>0")
            params.append(text)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # 先只按行号排序取够条数，再取这些行的报文：直接排序会把每行报文都带进临时排序，
        # 点云这类单条几十 KB 的模块点一下筛选要好几秒
        rows = self.db.execute("SELECT id,body FROM events WHERE id IN (SELECT id FROM events" + where + " ORDER BY id DESC LIMIT ?) "
                               "ORDER BY id DESC", params + [limit])
        return [(row_id, load_event(body)) for row_id, body in rows]

    def import_batches(self, limit=50):
        """导入过的批次，最近导入的在前：[(sn, session, 条数, 首行 id, 末行 id)]；要扫全表，大库放后台线程调用"""
        return self.db.execute("SELECT sn, session, COUNT(*), MIN(id), MAX(id) FROM events WHERE session LIKE 'import-%' "
                               "GROUP BY sn, session ORDER BY MAX(id) DESC LIMIT ?", (limit,)).fetchall()

    # 原文时间统一成可比较的 YYYY-MM-DD HH:MM:SS；只有时刻、没有日期的行算不出，不按时间筛时照常显示
    SOURCE_DAY = "replace(replace(substr(json_extract(body,'$.source_time'),1,19),'T',' '),'/','-')"
    DATED = "coalesce(length(json_extract(body,'$.source_time')), 0) >= 19"

    def log_rows(self, sn, session, module="", text="", start="", end=""):
        """导入的日志按原文顺序逐条产出 (行号, 事件)；session 为空时是这台设备导入过的全部批次

        start、end 为 "YYYY-MM-DD HH:MM:SS"，按日志里写的发生时间筛（不是导入时间），没有日期的行不会命中
        """
        where, params = self._log_scope(sn, session, module, text)
        if start or end:
            where += f" AND {self.DATED} AND {self.SOURCE_DAY} BETWEEN ? AND ?"
            params += [start or "0000", end or "9999"]
        for row_id, body in self.db.execute(f"SELECT id,body FROM events WHERE {where} ORDER BY id", params):
            yield row_id, load_event(body)

    def undated_logs(self, sn, session, module="", text=""):
        """同样范围里原文没有日期的行数，按时间筛时告诉用户这些没算进去"""
        where, params = self._log_scope(sn, session, module, text)
        return self.db.execute(f"SELECT COUNT(*) FROM events WHERE {where} AND NOT ({self.DATED})", params).fetchone()[0]

    @staticmethod
    def _log_scope(sn, session, module, text):
        clauses, params = ["sn=?"], [sn]
        if session:
            clauses.append("session=?")
            params.append(session)
        else:
            clauses.append("session LIKE 'import-%'")
        if module:
            clauses.append("module=?")
            params.append(module)
        if text:
            clauses.append("instr(lower(body),lower(?))>0")
            params.append(text)
        return " AND ".join(clauses), params

    def session_events(self, session):
        """一次连接（会话）里的全部消息，按入库顺序逐条产出"""
        for (body,) in self.db.execute("SELECT body FROM events WHERE session=? ORDER BY id", (session,)):
            yield load_event(body)

    def event_at(self, row_id):
        return load_event(self.db.execute("SELECT body FROM events WHERE id=?", (row_id,)).fetchone()[0])

    def query_methods(self, methods, limit=5000):
        """取指定 method 的最新 limit 条消息（行号倒序）"""
        methods = sorted(methods)
        rows = self.db.execute("SELECT id,body FROM events WHERE method IN (" + ",".join("?" * len(methods)) +
                               ") ORDER BY id DESC LIMIT ?", methods + [limit])
        return [(row_id, load_event(body)) for row_id, body in rows]

    def trace(self, event):
        if not event.tid:
            return [event]
        rows = self.db.execute("SELECT body FROM events WHERE sn=? AND session=? AND tid=? ORDER BY id",
                               (event.sn, event.session, event.tid))
        return [load_event(row[0]) for row in rows]

    def other_sources(self, event):
        """同设备、同 tid、其他会话里的记录，每个会话一组，各组按入库顺序；event 没有 tid 时为空

        不同会话可能是导入的日志、实时跟随或另一次 MQTT 连接，tid 也可能被复用，所以只分组列出，不拼进当前请求
        """
        if not event.tid:
            return []
        groups = {}
        for session, body in self.db.execute("SELECT session, body FROM events WHERE tid=? AND sn=? AND session!=? ORDER BY id",
                                             (event.tid, event.sn, event.session)):
            groups.setdefault(session, []).append(load_event(body))
        return list(groups.values())

    # 回放用：OSD 消息只按 Topic 认（云端 .../osd，本地 vfc/data），Topic 在 body 里
    OSD_ROWS = ("kind='状态' AND (body LIKE '%/osd\"%' OR body LIKE '%local/robotdog/vfc/data%') AND body NOT LIKE '%\"topic\":\"local/uav/%'"
                " AND body NOT LIKE '%\"topic\":\"tta/uav/%'")

    def osd_sessions(self, limit=20):
        """带 OSD 消息的 (设备, 会话)，最近导入或收到的在前：[(sn, session, OSD 条数)]；要扫全表，大库放后台线程调用"""
        return self.db.execute(f"SELECT sn, session, COUNT(*) FROM events WHERE {self.OSD_ROWS} "
                               "GROUP BY sn, session ORDER BY MAX(id) DESC LIMIT ?", (limit,)).fetchall()

    def osd_frames(self, sn, session):
        """某会话的 OSD 消息，按入库顺序（导入日志即原文行序）：[(行 id, 事件)]"""
        rows = self.db.execute(f"SELECT id, body FROM events WHERE sn=? AND session=? AND {self.OSD_ROWS} ORDER BY id",
                               (sn, session))
        return [(i, load_event(body)) for i, body in rows]

    def recent_before(self, sn, session, row_id, limit=8):
        """同会话里截至某行（含）最近的非 OSD 消息，新的在前：[(行 id, 事件)]"""
        # 按主键往回扫，不走 (sn, session, tid) 索引，否则大会话要扫完整个会话
        rows = self.db.execute(f"SELECT id, body FROM events NOT INDEXED WHERE id<=? AND sn=? AND session=? "
                               f"AND NOT ({self.OSD_ROWS}) ORDER BY id DESC LIMIT ?", (row_id, sn, session, limit))
        return [(i, load_event(body)) for i, body in rows]

    def previous(self, event):
        """同设备、同 Topic、同方法里时间上紧挨着的前一条消息；没有返回 None"""
        rows = self.db.execute("SELECT body FROM events WHERE method=? AND sn=? AND stamp<? ORDER BY stamp DESC LIMIT 50",
                               (event.method, event.sn, event.timestamp))
        return next((e for e in (load_event(body) for (body,) in rows) if e.topic == event.topic), None)

    def groups(self, keys):
        """按 (sn, session, tid) 取完整请求事件，各组按入库顺序；tid 分批查询避开 SQLite 参数上限"""
        wanted = set(keys)
        result = {k: [] for k in keys}
        tids = sorted({k[2] for k in keys})
        for start in range(0, len(tids), 500):
            chunk = tids[start:start + 500]
            rows = self.db.execute("SELECT body FROM events WHERE tid IN (" + ",".join("?" * len(chunk)) + ") ORDER BY id", chunk)
            for (body,) in rows:
                e = load_event(body)
                if (e.sn, e.session, e.tid) in wanted:
                    result[(e.sn, e.session, e.tid)].append(e)
        return result

    @property
    def on_disk(self):
        return self.path not in ("", ":memory:")

    def reader(self):
        """同一库文件的只读连接；内存库没有文件，返回自身"""
        return Store(self.path, reader=True) if self.on_disk else self

    def size(self):
        """库文件占用字节数（含 WAL），内存库为 0"""
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        return sum(Path(path + suffix).stat().st_size for suffix in ("", "-wal") if path and Path(path + suffix).exists())

    def clear(self):
        """删除全部消息并回收空间；不可恢复，导入的原始日志文件不受影响"""
        self.db.execute("DELETE FROM events")
        self.db.execute("DELETE FROM notes")
        self.db.commit()
        self.request_cache.clear()
        self.status_version += 1
        self.request_version += 1
        self.db.execute("VACUUM")

    def drop_session(self, sn, session):
        """删除一个会话的全部消息和备注，不回收文件空间（VACUUM 在大库上要几十秒）"""
        self.flush()
        self.db.execute("DELETE FROM events WHERE sn=? AND session=?", (sn, session))
        self.db.execute("DELETE FROM notes WHERE sn=? AND session=?", (sn, session))
        self.db.commit()
        self.request_cache.clear()
        self.status_version += 1
        self.request_version += 1

    def note(self, key):
        """读取请求 (sn, session, tid) 的问题备注，没有返回 None"""
        row = self.db.execute("SELECT body FROM notes WHERE sn=? AND session=? AND tid=?", key).fetchone()
        return json.loads(row[0]) if row else None

    def set_note(self, key, note):
        """保存备注（预期 expected、实际 actual、备注 remark、已复现 reproduced）；文字入库前脱敏，全空则删除"""
        # 请求列表里有【备注】标记，备注变了也要重画
        self.request_version += 1
        if not any(note.get(k) for k in ("expected", "actual", "remark", "reproduced")):
            self.db.execute("DELETE FROM notes WHERE sn=? AND session=? AND tid=?", key)
        else:
            self.db.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?)",
                            (*key, json.dumps(redact(note), ensure_ascii=False), time.time()))
        self.db.commit()

    def notes(self):
        """全部备注 {(sn, session, tid): 备注}"""
        return {(sn, session, tid): json.loads(body)
                for sn, session, tid, body in self.db.execute("SELECT sn, session, tid, body FROM notes ORDER BY updated")}

    def connections(self, device=None):
        """保存的连接配置 {名称: config}；传入 device（"dog"/"uav"）时只返回该设备的配置，手动预设置顶、最近使用在前"""
        items = []
        for name, body in self.db.execute("SELECT name, body FROM connections"):
            cfg = json.loads(body)
            if device and cfg.get("device", "dog") != device:
                continue
            items.append((name, cfg))
        items.sort(key=lambda pair: (bool(pair[1].get("auto_history")), -(pair[1].get("updated_at") or 0), pair[0]))
        return dict(items)

    def save_connection(self, name, config):
        self.db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (name, json.dumps(config, ensure_ascii=False)))
        self.db.commit()

    def delete_connection(self, name):
        self.db.execute("DELETE FROM connections WHERE name = ?", (name,))
        self.db.commit()

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def last_id(self):
        """最新一条消息的行号，空库为 None；行号只增不减，可用来判断库里有没有新消息"""
        return self.db.execute("SELECT max(id) FROM events").fetchone()[0]

    def recent_requests(self, limit):
        """最近有消息的请求键 [(sn, session, tid)]，按最后一条消息倒序，以及请求总数"""
        where = "tid!='' AND kind IN ('指令','ACK')"
        total = self.db.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM events WHERE {where} GROUP BY sn,session,tid)").fetchone()[0]
        keys = self.db.execute(f"SELECT sn,session,tid FROM events WHERE {where} GROUP BY sn,session,tid "
                               "ORDER BY MAX(id) DESC LIMIT ?", (limit,)).fetchall()
        return [tuple(k) for k in keys], total

    def export(self, path):
        """仅导出已脱敏的数据，不包含原文文件"""
        self.flush()
        with Path(path).open("w", encoding="utf-8") as output:
            for (body,) in self.db.execute("SELECT body FROM events ORDER BY id"):
                output.write(full_body(body) + "\n")

    def close(self):
        self.flush()
        self.db.close()


@dataclass
class Diagnosis:
    status: str
    summary: str
    suggestion: str
    confidence: str = "明确报文"


def diagnose(event):
    p = event.payload
    if event.kind not in ("ACK", "状态", "事件"):
        if event.kind == "指令":
            return Diagnosis("待判", "指令已发出，等待 ACK", "一直没有 ACK 就查目标模块日志")
        if re.search(r"(?i)\b(error|failed|exception|timeout)\b|失败|超时", event.raw):
            return Diagnosis("错误线索", event.raw.strip()[:400], "打开原文附近，看前后请求", "日志文本")
        return Diagnosis("未判定", "", "按 tid、方法或错误关键字筛选", "原文")
    if event.retained:
        return Diagnosis("历史快照", "Broker 保留的旧消息", "等新的状态上报")
    if event.method == "room_state":
        return Diagnosis("寻址信息", "拉流房间信息", "推流是否在跑看 stream_status")
    if event.method == "stream_status":
        view = p.get("info", p.get("data", {}))
        active = view.get("result") if isinstance(view, dict) else None
        return Diagnosis("活跃" if active == 1 else "未活跃" if active == 0 else "待核",
                         "外推流状态：" + str(view.get("msg", "")) if isinstance(view, dict) else "状态格式不匹配",
                         "stream_status 的 result=1 表示正在推流")
    if event.kind == "状态" and event.method not in UPLOAD_STATES:
        return Diagnosis("状态观测", "收到 " + event.method, "")
    if event.kind == "ACK" and event.topic.startswith(BRIDGE):
        return _bridge_ack(p)
    view = p.get("data", {})
    if isinstance(view, dict) and isinstance(view.get("data"), dict) and "result" in view["data"]:
        view = view["data"]
    waypoint = "/WaypointAction/" in event.topic
    internal = event.topic.endswith("/detect/ack")
    if waypoint or internal:
        result = p.get("result")
        msg = str(p.get("msg", ""))
    elif isinstance(view, dict) and "result" in view:
        result, msg = view["result"], str(view.get("msg", ""))
    elif "result" in p:
        # 实机 VFC 的本地 ACK 把 result/msg 放在根上、data 为 null；Media、FSVR 放在 data 里，两种都认
        result, msg = p["result"], str(p.get("msg", ""))
    else:
        return Diagnosis("契约待核", "ACK 里找不到 data.result", "看原始 JSON，核对该模块的 ACK 格式")
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return Diagnosis("契约待核", "result 不是数字", "看原始 JSON 的 result 类型")
    if result != 0:
        reason = msg or str(view.get("reason", "")) if isinstance(view, dict) else msg
        suggestion = "看该模块前后日志和请求参数"
        if "task_name" in reason:
            suggestion = "核目标版本的任务名规则，中文本身合法"
        elif any(word in reason.lower() for word in ("camera", "timeout", "connect")):
            suggestion = "查相机/网络连接和目标服务日志"
        elif any(word in reason.lower() for word in ("alsa", "audio device", "recording")):
            suggestion = "查声卡枚举、设备名和占用"
        return Diagnosis("失败", reason or "返回失败码但没有错误说明", suggestion)
    if internal:
        phase = p.get("data", {}).get("phase")
        return Diagnosis("JPEG 已生成" if phase == "completed" else "阶段待判",
                         "抓拍阶段：" + str(phase), "上传结果另看 FSVR")
    if event.method in UPLOAD or event.method in UPLOAD_STATES:
        state = view.get("state") if isinstance(view, dict) else None
        return Diagnosis("完成" if state == "0" else "已受理/上传中", msg or "文件业务结果",
                         "等 state=0 终态")
    if event.method == "camera_livestream":
        return Diagnosis("请求已处理", msg or "开关流 ACK", "推流是否起来看 stream_status")
    if event.method == "speaker_play_start":
        return Diagnosis("已开始", msg or "开始播放", "协议没有播完通知")
    if event.method in NAV:
        return Diagnosis("导航已受理", msg or "导航请求已受理", "到达情况看 nav_status")
    return Diagnosis("操作成功", msg or "ACK 成功", "")


def trace_report(events):
    lines = []
    failed = [e for e in events if e.kind == "ACK" and diagnose(e).status == "失败"]
    if failed:
        first = next((e for e in failed if e.module != "gw"), failed[0])
        d = diagnose(first)
        lines.append(f"定位：{first.module.upper()} 返回明确失败\n原因：{d.summary}\n下一步：{d.suggestion}")
    methods = {e.method for e in events if e.kind == "指令"}
    acks = [e for e in events if e.kind == "ACK" and (not methods or e.method in methods)]
    if not acks:
        lines.append("ACK 未观测：不等于设备失败，也不确定是丢包")
    counts = {}
    for ack in acks:
        counts[ack.topic] = counts.get(ack.topic, 0) + 1
    if any(count > 1 for count in counts.values()):
        lines.append("同一回复通道有多份 ACK：可能是重投或补发，不能据此断言重复执行")
    for e in events:
        d = diagnose(e)
        lines.append(f"[{e.module} · {e.kind} · {d.status}] {e.topic}\n{d.summary}\n建议：{d.suggestion}")
    if any(e.kind == "指令" and e.method == "camera_livestream" for e in events):
        lines.append("周期 stream_status 通常无 tid，请到状态页核同设备/流的新状态；不强行归入本请求")
    return "\n\n".join(lines)


# 请求列表的排序：失败最前，其次没收到回复的，再按最近时间
REQUEST_ORDER = {"失败": 0, "重复请求未执行": 0, "无 ACK": 1, "结果不确定": 1, "契约待核": 2}
# 这些来源的时间戳都是本机接收时间，可以相减算耗时；导入日志跨主机时钟不可比
LOCAL_CLOCK = {"mqtt", "sandbox", "mock-gw"}


@dataclass
class Request:
    events: list
    status: str
    summary: str
    method: str
    module: str
    chain: str
    latency_ms: float | None
    note: str = ""

    @property
    def key(self):
        e = self.events[0]
        return e.sn, e.session, e.tid


def occurred(event):
    """事件实际发生的时间戳：日志行用原文里带日期的时间，实时收到的用收到时刻；原文只有时刻、没有日期的返回 None

    导入的行 timestamp 是导入时刻，不能拿来找现场的日志
    """
    text = event.source_time.replace("T", " ").replace("/", "-")[:19]
    if len(text) == 19:
        try:
            return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    return None if event.session.startswith("import-") else event.timestamp


def _bridge_ack(p):
    """Bridge 回执：结果在 status/final 和 info 里，不在 data.result；同一 id 先回 accepted，再回 final"""
    info = p.get("info") if isinstance(p.get("info"), dict) else {}
    status, code, msg = str(p.get("status", "")), str(info.get("data", "")), str(info.get("msg", ""))
    text = f"{status} {code}：{msg}" if code else f"{status}：{msg}"
    if p.get("duplicate"):
        # Bridge 按 id 去重：收到用过的 id 不执行，原样回上次那条命令的缓存结果
        return Diagnosis("重复请求未执行", "Bridge 认为这个 id 用过，回的是上次的缓存结果：" + text, "这次没有真正执行；换新 id 和当前 ts 再发")
    if not p.get("final"):
        return Diagnosis("受理中", "Bridge " + text, "等同一 id 的最终回执")
    if status == "succeeded":
        return Diagnosis("完成", text, "")
    if status == "confirmed":
        # 比如 navigation.next 的 GOAL_RECEIPT 只说明航点指令被接收，到没到点看状态和事件
        return Diagnosis("请求已处理", text, "confirmed 只说明 Bridge 确认了这一步，结果看状态和事件")
    if status in ("rejected", "failed", "cancelled"):
        return Diagnosis("失败", text, "看原因码 info.data 和 Bridge 日志")
    if status == "uncertain":
        return Diagnosis("结果不确定", text, "先查现场和状态，不要盲目重发")
    return Diagnosis("契约待核", "Bridge 回执的 status 不认识：" + text, "看原始 JSON")


def summarize(events):
    """把同一请求（同设备、同会话、同 tid）的事件归成一行结论

    结论优先级：任一明确失败 → 模块自己的 ACK → GW 回复 → 只有指令记为“无 ACK”；
    同一回复通道多份 ACK 只提示可能重投，不算重复执行
    """
    commands = [e for e in events if e.kind == "指令"]
    acks = [e for e in events if e.kind == "ACK"]
    module_acks = [e for e in acks if e.module != "gw"]
    diagnosed = [(e, diagnose(e)) for e in events]
    failed = [(e, d) for e, d in diagnosed if d.status in ("失败", "错误线索")]
    if failed:
        focus, d = next(((e, d) for e, d in failed if e.module != "gw"), failed[0])
        status, summary = "失败", f"{focus.module.upper()}：{d.summary}"
    elif acks:
        # Bridge 同一 id 会先回 accepted 再回 final，重复回执或迟到的 accepted 不能把终态退回受理中
        finals = [e for e in (module_acks or acks) if e.topic.startswith(BRIDGE) and e.payload.get("final")]
        d = diagnose((finals or module_acks or acks)[-1])
        status, summary = d.status, d.summary
        # ACK 只代表受理；同请求后续的上传状态是业务终态，以最后一条为准
        progress = [e for e in events if e.kind == "状态" and e.method in UPLOAD_STATES]
        if progress:
            d = diagnose(progress[-1])
            status, summary = d.status, d.summary
    elif commands and all(e.retained for e in commands):
        # 连上 Broker 时收到的保留指令：是以前有人带 Retain 发的，不是这次发的，也不该等它的回复
        status, summary = "保留消息", "Broker 上保留的旧指令，连接时收到的，不是这次发的；模块每次重新订阅都会再执行一遍，建议清掉"
    elif commands:
        status, summary = "无 ACK", "指令已发出，没收到回复"
    else:
        d = diagnose(events[-1])
        status, summary = d.status, d.summary
    method = (commands or events)[0].method
    reply = (module_acks or acks)[:1]
    # 按实际回复方标模块：文件类指令发给 GW，GW 自己处理就标 GW，转给 FSVR 由 FSVR 回就标 FSVR
    module = reply[0].module if reply else next((e.module for e in events if e.module not in ("gw", "unknown", "")), events[0].module)
    steps = []
    for e in events:
        step = f"{e.module.upper()} {e.kind}"
        if not steps or steps[-1] != step:
            steps.append(step)
    latency = None
    if commands and reply and {e.source.split(":")[0] for e in (commands[0], reply[0])} <= LOCAL_CLOCK:
        latency = (reply[0].timestamp - commands[0].timestamp) * 1000
    counts = {}
    for ack in acks:
        counts[ack.topic] = counts.get(ack.topic, 0) + 1
    repeated = max(counts.values(), default=0)
    notes = [f"同一通道收到 {repeated} 份回复，可能是重投或补发"] if repeated > 1 else []
    # 例如区域测温的应答 method 写成 point_set：标出冲突，不静默当成功
    other = sorted({a.method for a in acks} - {method, "未解析"}) if commands else []
    if other:
        notes.append(f"回复 method 为 {'、'.join(other)}，与请求 {method} 不一致")
        # 没有已证实的别名规则，不按回复结果码判成功，也不判设备失败；原结果写进结论供核对
        if status != "失败":
            status, summary = "契约待核", f"回复 method 不一致，原结果：{status}" + (f"（{summary}）" if summary else "")
    note = "；".join(notes)
    return Request(events, status, summary, method, module, " → ".join(steps), latency, note)


# 每条消息都会变的标识字段，对比时不算变化
VOLATILE = {"tid", "bid", "ts", "timestamp", "seq"}


def payload_diff(old, new):
    """两份报文逐字段对比，返回 [(路径, 旧值, 新值)]；嵌套对象按“a.b”展开，列表整体比较，缺失记为 None"""
    def flat(value, prefix=""):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if not prefix and key in VOLATILE:
                    continue
                out.update(flat(item, f"{prefix}{key}."))
            return out
        return {prefix[:-1]: value}

    a, b = flat(old), flat(new)
    return [(path, a.get(path), b.get(path)) for path in sorted(a.keys() | b.keys()) if a.get(path) != b.get(path)]


def requests(store, rows):
    """按筛选命中的事件找出所属请求，返回完整请求列表（失败置顶、再按时间倒序）"""
    # 只有指令和 ACK 的 tid 代表请求；实机 VFC 心跳也带 tid（如 <SN>-hb-347），不能每次心跳都算一个请求
    keys = list(dict.fromkeys((e.sn, e.session, e.tid) for _, e in rows if e.tid and e.kind in ("指令", "ACK")))
    cache = store.request_cache
    missing = [k for k in keys if k not in cache]
    if len(cache) + len(missing) > 20000:
        # 每个请求连着它的全部报文，长时间开着别让缓存无限长
        cache.clear()
        missing = keys
    for k, events in store.groups(missing).items():
        cache[k] = summarize(events)
    result = [cache[k] for k in keys]
    result.sort(key=lambda r: (REQUEST_ORDER.get(r.status, 9), -r.events[-1].timestamp))
    return result


def diagnostic_zip(store, path, environment, focus=None, request_limit=20000):
    """导出诊断包：脱敏会话、请求结论（失败在前）、所选请求、运行环境；不含原始日志文件

    session.jsonl 逐行流式写入，不整体读进内存；summary.md 列最近 request_limit 个请求，
    条数不全时在摘要首部写明“共 N 个，列出 M 个”。environment 由调用方给出，原样写入
    """
    import zipfile
    store.flush()
    keys, total = store.recent_requests(request_limit)
    groups = store.groups(keys)
    found = [summarize(groups[k]) for k in keys]
    found.sort(key=lambda r: (REQUEST_ORDER.get(r.status, 9), -r.events[-1].timestamp))
    events = store.count()
    scope = f"共 {total} 个请求，全部列出" if len(found) >= total else f"共 {total} 个请求，按最近消息列出 {len(found)} 个"
    lines = ["# 诊断摘要", "", f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "",
             f"消息 {events} 条（全部写入 session.jsonl）；{scope}",
             f"所列请求中失败 {sum(r.status == '失败' for r in found)} 个，无 ACK {sum(r.status == '无 ACK' for r in found)} 个", "",
             "| 时间 | 设备 | 模块 | 方法 | 结果 | 耗时 | 结论 | tid |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    notes = store.notes()
    for r in found:
        e = r.events[0]
        latency = "—" if r.latency_ms is None else f"{r.latency_ms:.0f} ms"
        summary = (("【有备注】" if r.key in notes else "") + r.summary + ("；" + r.note if r.note else "")).replace("|", "/").replace("\n", " ")
        lines.append(f"| {time.strftime('%H:%M:%S', time.localtime(e.timestamp))} | {e.sn} | {r.module} | {r.method} "
                     f"| {r.status} | {latency} | {summary} | {e.tid} |")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", "summary.md     请求结论，失败和无 ACK 排在前面；首部写明请求总数和列出数\n"
                                       "session.jsonl  全部消息（入库前已脱敏凭据）\n"
                                       "focus.json     导出时选中的请求（如有）\n"
                                       "notes.json     问题备注：预期、实际、备注、是否已复现（如有）\n"
                                       "environment.json  工具版本、连接模式和设备\n"
                                       "不包含导入的原始日志文件\n")
        archive.writestr("summary.md", "\n".join(lines) + "\n")
        # 流式写入时 zipfile 事先不知道大小，超过 2 GB 必须提前声明 ZIP64
        with archive.open("session.jsonl", "w", force_zip64=True) as output:
            for (body,) in store.db.execute("SELECT body FROM events ORDER BY id"):
                output.write(full_body(body).encode("utf-8") + b"\n")
        archive.writestr("environment.json", json.dumps(environment, ensure_ascii=False, indent=2))
        if notes:
            archive.writestr("notes.json", json.dumps([{"sn": k[0], "session": k[1], "tid": k[2], **v} for k, v in notes.items()],
                                                      ensure_ascii=False, indent=2))
        if focus:
            focused = store.groups([focus]).get(focus, [])
            if focused:
                r = summarize(focused)
                archive.writestr("focus.json", json.dumps({"status": r.status, "summary": r.summary, "note": r.note,
                                                           "remark": notes.get(focus),
                                                           "chain": r.chain, "events": [e.clean() for e in focused]},
                                                          ensure_ascii=False, indent=2))
    return {"events": events, "requests": total, "listed": len(found)}


def export_preview(store):
    """诊断包导出前的概览：消息数、请求数、设备和会话、来源分布、被脱敏的消息数和几条样例"""
    db = store.db
    store.flush()
    sources = {}
    for (body,) in db.execute("SELECT body FROM events WHERE id IN (SELECT MIN(id) FROM events GROUP BY session)"):
        source = json.loads(body).get("source", "").split(":")[0]
        sources[source] = sources.get(source, 0) + 1
    samples = [json.loads(b).get("topic", "") for (b,) in
               db.execute("SELECT body FROM events WHERE instr(body,'[REDACTED]')>0 ORDER BY id DESC LIMIT 5")]
    return {"events": store.count(),
            "requests": store.recent_requests(0)[1],
            "devices": sorted(sn for (sn,) in db.execute("SELECT DISTINCT sn FROM events") if sn),
            "sessions": sources,
            "redacted": db.execute("SELECT COUNT(*) FROM events WHERE instr(body,'[REDACTED]')>0").fetchone()[0],
            "redacted_topics": samples,
            "notes": db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]}
