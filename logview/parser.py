# -*- coding: utf-8 -*-
"""日志读取与聚合：把 JSONL（主力）和旧版文本日志（历史）统一成事件流。

设计要点：
1. **只读，不改日志**。看板是观察者，任何解析失败都只丢那一行并计数。
2. **增量 tail**：记录每个文件的字节偏移，只读新追加的内容；文件被轮转/清空时
   自动回到 0 重读。
3. **滑动窗口**：内存里只保留最近 N 条（默认 30000），聚合始终在窗口内做，
   日志涨到几十万行页面也不会卡。
4. **去重**：新代码同时写 JSONL 和文本，文本只补 JSONL 开始之前的历史，
   否则每条事件会被数两遍。
"""

import json
import os
import re
import threading
import time

#: 直接从日志里搬过来的字段，聚合/展示都要用
SERIES_FIELDS = ("i", "ts", "t", "lv", "cat", "ev", "src", "msg",
                 "action", "ok", "dur_ms", "code", "error", "tab", "tab_title",
                 "tab_url", "cid", "kind", "status", "path", "method", "poll",
                 "slow", "exit", "argv", "bytes", "params")

_TEXT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.,]?(\d{3})?\s+\[(\w+)\]\s+(.*)$")
_HTTP_RE = re.compile(r'^(\S+) "(\w+) (\S+) HTTP/[\d.]+" (\d{3})')
_KV_RE = re.compile(r"(\w+)=([^\s]+)")

_LEVEL_ORDER = ("debug", "info", "warn", "error")


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "ok")


def _classify_text(message):
    """旧文本日志没有字段，只能按文案猜分类。够用即可，不怕不准。"""
    msg = message.strip()
    if msg.startswith("[browser]"):
        return "browser", "console", {"text": msg[len("[browser]"):].strip()}
    if msg.startswith("ws connected"):
        fields = {"cid": _after(msg, "ws connected: ") or "", "ev_detail": msg}
        m = re.search(r"\((\w+), (\d+) tabs", msg)
        if m:
            fields["kind"] = m.group(1)
            fields["tabs"] = int(m.group(2))
        return "ws", "connect", fields
    if msg.startswith("ws disconnected"):
        return "ws", "disconnect", {"cid": _after(msg, "ws disconnected: ")}
    if msg.startswith("ws closed"):
        return "ws", "close", {"error": msg}
    if msg.startswith("ws silent"):
        return "ws", "silent", {"error": msg}
    if msg.startswith("ws handshake"):
        return "ws", "handshake", {"error": msg}
    if msg.startswith("client registered"):
        return "ws", "register", {"cid": _after(msg, "client registered: ")}
    if "listening on" in msg:
        return "sys", "listen", {}
    if msg.startswith("shutdown requested"):
        return "sys", "shutdown", {}
    if msg.startswith("server stopped"):
        return "sys", "stop", {}
    if msg.startswith("idle for"):
        return "sys", "idle", {}
    if msg.startswith("daemon spawned"):
        return "sys", "spawn", {}
    if msg.startswith("unknown ws message"):
        return "ws", "unknown", {}
    if "HTTP/1.1" in msg:
        m = _HTTP_RE.match(msg)
        if m:
            path = m.group(3)
            return "net", "http", {
                "method": m.group(2), "path": path, "status": int(m.group(4)),
                "poll": path in ("/health", "/poll", "/status"),
            }
    return "sys", "note", {"text": msg}


def _after(text, needle):
    pos = text.find(needle)
    return text[pos + len(needle):].strip() if pos >= 0 else ""


def parse_jsonl_line(line):
    """一行 JSON -> 事件 dict；不认识就返回 None（由调用方计数）。"""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    ts = _to_float(raw.get("ts"), 0.0)
    if not ts:
        return None
    rec = {
        "ts": ts,
        "t": raw.get("t") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "lv": str(raw.get("lv") or "info").lower(),
        "cat": str(raw.get("cat") or "sys"),
        "ev": str(raw.get("ev") or "note"),
        "src": str(raw.get("src") or ""),
        "msg": raw.get("msg") or "",
    }
    for key, value in raw.items():
        if key in ("ts", "t", "lv", "cat", "ev", "src", "msg"):
            continue
        rec[key] = value
    if "ok" in rec:
        rec["ok"] = _truthy(rec["ok"])
    return rec


def parse_text_line(line):
    """旧格式文本行 -> 事件 dict。"""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    match = _TEXT_RE.match(line)
    if not match:
        return None
    stamp = match.group(1)
    millis = match.group(2) or "000"
    try:
        ts = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) + int(millis) / 1000.0
    except ValueError:
        return None
    level = match.group(3).lower()
    body = match.group(4)
    if level not in _LEVEL_ORDER:
        level = "info"

    cat, ev, fields = _classify_text(body)

    # 本仓库新版文本行会在末尾带 "  (cat/ev)" 与 k=v，能还原更多字段
    tail = re.search(r"\s+\(([a-z]+)/([a-z_]+)\)$", body)
    if tail:
        cat, ev = tail.group(1), tail.group(2)
        body = body[:tail.start()]
        for key, value in _KV_RE.findall(body):
            if key in ("dur_ms",):
                fields[key] = _to_float(value)
            elif key in ("ok", "slow", "poll"):
                fields[key] = _truthy(value)
            elif key in ("exit", "status", "bytes"):
                try:
                    fields[key] = int(value)
                except ValueError:
                    pass
            else:
                fields[key] = value
        body = _KV_RE.sub("", body).strip()

    rec = {
        "ts": ts,
        "t": stamp + "." + millis,
        "lv": level,
        "cat": cat,
        "ev": ev,
        "src": "",
        "msg": body.strip(),
    }
    rec.update({k: v for k, v in fields.items() if v not in ("", None)})
    return rec


class LogStore(object):
    """盯着若干日志文件，增量解析成事件流。"""

    def __init__(self, json_path="", text_path="", window=30000):
        self.window = max(1000, int(window or 30000))
        self.records = []
        self.seq = 0
        self.parsed = 0
        self.bad_lines = 0
        self.lock = threading.RLock()
        self._files = []
        if text_path:
            self._files.append({"path": text_path, "kind": "text", "offset": 0,
                                "buf": b"", "size": 0})
        if json_path:
            self._files.append({"path": json_path, "kind": "json", "offset": 0,
                                "buf": b"", "size": 0})
        self._json_floor = None
        self._loaded = False

    # ------------------------------------------------------------------ 读取
    def _read_chunk(self, fh, path, offset):
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < offset:
            # 被轮转或清空，从头再来
            offset = 0
        if size == offset:
            return []
        try:
            with open(path, "rb") as fp:
                fp.seek(offset)
                raw = fp.read(size - offset)
        except OSError:
            return []
        fh["offset"] = offset + len(raw)
        fh["size"] = size
        data = fh["buf"] + raw
        lines = data.split(b"\n")
        fh["buf"] = lines.pop()  # 最后一段可能是半行，留到下一轮
        return [ln.decode("utf-8", "replace") for ln in lines]

    def _append(self, rec):
        if not rec:
            return None
        self.seq += 1
        rec["i"] = self.seq
        self.records.append(rec)
        if len(self.records) > self.window:
            del self.records[:len(self.records) - self.window]
        return rec

    def load(self):
        """首次全量加载：文本补历史，JSONL 覆盖新事件，重叠部分只留一份。"""
        with self.lock:
            return self._load_locked()

    def _load_locked(self):
        text_recs, json_recs = [], []
        for fh in self._files:
            if not fh["path"] or not os.path.isfile(fh["path"]):
                continue
            lines = self._read_chunk(fh, fh["path"], 0)
            for line in lines:
                rec = (parse_text_line if fh["kind"] == "text" else parse_jsonl_line)(line)
                if rec is None:
                    if line.strip():
                        self.bad_lines += 1
                    continue
                if fh["kind"] == "text":
                    text_recs.append(rec)
                else:
                    json_recs.append(rec)
                    if self._json_floor is None:
                        self._json_floor = rec["ts"]
        # 新旧双写窗口里同一条事件会在两个文件各出现一次，按时间戳去重
        if self._json_floor is not None:
            text_recs = [r for r in text_recs if r["ts"] < self._json_floor - 0.002]
        fresh = text_recs + json_recs
        fresh.sort(key=lambda r: r["ts"])
        for rec in fresh:
            self._append(rec)
        self.parsed = len(fresh)
        self._loaded = True
        return len(fresh)

    def poll(self):
        """增量读：返回新事件列表（已加入窗口）。"""
        with self.lock:
            return self._poll_locked()

    def snapshot(self):
        """给看板用的记录副本：复制在锁内，聚合在锁外，不挡写入。"""
        with self.lock:
            return list(self.records)

    def _poll_locked(self):
        if not self._loaded:
            self.load()
            return []
        # 文本日志的「新代码段」和 JSONL 内容重复，且文本只精确到毫秒混秒级时间戳，
        # 所以文本一律只作为历史补充：JSONL 有数据后就不再从文本增量读。
        json_active = self._json_floor is not None
        fresh = []
        for fh in self._files:
            if not fh["path"] or not os.path.isfile(fh["path"]):
                continue
            if fh["kind"] == "text" and json_active:
                # 只推进偏移，不解析，避免重复计数
                try:
                    fh["offset"] = max(fh["offset"], os.path.getsize(fh["path"]))
                except OSError:
                    pass
                continue
            for line in self._read_chunk(fh, fh["path"], fh["offset"]):
                rec = (parse_text_line if fh["kind"] == "text" else parse_jsonl_line)(line)
                if rec is None:
                    if line.strip():
                        self.bad_lines += 1
                    continue
                if fh["kind"] == "json" and self._json_floor is None:
                    self._json_floor = rec["ts"]
                fresh.append(rec)
        if not fresh:
            return []
        fresh.sort(key=lambda r: r["ts"])
        out = []
        for rec in fresh:
            self._append(rec)
            out.append(rec)
        self.parsed += len(fresh)
        return out

    # ------------------------------------------------------------------ 元信息
    def files(self):
        out = []
        for fh in self._files:
            info = {"path": fh["path"], "kind": fh["kind"], "exists": False,
                    "size": 0, "mtime": None}
            try:
                st = os.stat(fh["path"])
                info.update({"exists": True, "size": st.st_size,
                             "mtime": int(st.st_mtime)})
            except OSError:
                pass
            out.append(info)
        return out

    def span(self):
        with self.lock:
            if not self.records:
                return None
            return {"from": self.records[0]["ts"], "to": self.records[-1]["ts"]}




# ---------------------------------------------------------------- 过滤与聚合
def build_filters(params):
    """把查询串转成过滤条件。空值代表「不限」。"""
    def _set(name):
        raw = params.get(name, [""])
        raw = raw[0] if isinstance(raw, (list, tuple)) else raw
        return {p.strip() for p in str(raw or "").split(",") if p.strip()}

    def _num(name):
        raw = params.get(name, [None])
        raw = raw[0] if isinstance(raw, (list, tuple)) else raw
        return _to_float(raw, 0.0) if raw not in (None, "") else None

    return {
        "lv": _set("lv"),
        "cat": _set("cat"),
        "src": _set("src"),
        "q": str((params.get("q", [""])[0] if isinstance(params.get("q"), list)
                  else params.get("q", "")) or "").strip().lower(),
        "from": _num("from"),
        "to": _num("to"),
        "poll": str((params.get("poll", ["0"])[0] if isinstance(params.get("poll"), list)
                     else params.get("poll", "0")) or "0") == "1",
        "only_fail": str((params.get("fail", ["0"])[0] if isinstance(params.get("fail"), list)
                          else params.get("fail", "0")) or "0") == "1",
    }


def match(rec, f):
    if f["lv"] and rec["lv"] not in f["lv"]:
        return False
    if f["cat"] and rec["cat"] not in f["cat"]:
        return False
    if f["src"] and rec.get("src") not in f["src"]:
        return False
    if not f["poll"] and rec.get("poll"):
        return False
    if f["only_fail"] and rec.get("ok") is not False:
        return False
    if f["from"] and rec["ts"] < f["from"]:
        return False
    if f["to"] and rec["ts"] > f["to"]:
        return False
    if f["q"]:
        haystack = " ".join(str(rec.get(k, "")) for k in
                            ("msg", "action", "code", "tab_title", "tab_url", "path",
                             "argv", "cid", "error", "cat", "ev"))
        if f["q"] not in haystack.lower():
            return False
    return True


def _percentile(sorted_values, ratio):
    if not sorted_values:
        return 0.0
    index = int(round((len(sorted_values) - 1) * ratio))
    return float(sorted_values[max(0, min(index, len(sorted_values) - 1))])


def _bucket_size(span_seconds):
    """时间轴粒度：窗口越短越细，避免只有两三根柱子。"""
    for limit, size in ((600, 5), (1800, 15), (3600, 30), (3 * 3600, 60),
                        (6 * 3600, 120), (12 * 3600, 300), (24 * 3600, 600),
                        (72 * 3600, 1800)):
        if span_seconds <= limit:
            return size
    return 3600


def _slim(rec):
    out = {}
    for key in SERIES_FIELDS:
        if key in rec:
            value = rec[key]
            if isinstance(value, str) and len(value) > 300:
                value = value[:300] + "…"
            elif isinstance(value, dict):
                value = {k: (v if isinstance(v, (int, float, bool)) else str(v)[:80])
                         for k, v in list(value.items())[:8]}
            out[key] = value
    return out


def _ms_label(value):
    if value < 1000:
        return "%dms" % value
    return "%gs" % (value / 1000.0)


def _empty_bucket(t):
    return {"t": t, "debug": 0, "info": 0, "warn": 0, "error": 0,
            "fail": 0, "total": 0, "cmd": 0, "net": 0}


def aggregate(records, filters, limit_recent=250, limit_errors=120):
    """一遍扫描算出看板需要的全部聚合数据。"""
    now = time.time()
    matched = []
    by_lv = dict((lv, 0) for lv in _LEVEL_ORDER)
    by_cat, cat_err, by_src, by_status = {}, {}, {}, {}
    latencies = []
    actions, clients, tabs = {}, {}, {}
    errors, slowest, gaps = [], [], []
    heat = [[0] * 24 for _ in range(7)]
    fails = 0
    last_60s = 0
    first_ts = last_ts = None

    for rec in records:
        if not match(rec, filters):
            continue
        matched.append(rec)
        lv = rec["lv"]
        by_lv[lv] = by_lv.get(lv, 0) + 1
        cat = rec["cat"]
        by_cat[cat] = by_cat.get(cat, 0) + 1
        src = rec.get("src") or "?"
        by_src[src] = by_src.get(src, 0) + 1

        ok = rec.get("ok")
        failed = ok is False or lv == "error"
        if failed:
            fails += 1
            cat_err[cat] = cat_err.get(cat, 0) + 1
            if len(errors) < 5000:
                errors.append(rec)

        if rec.get("status") is not None:
            key = str(rec["status"])
            by_status[key] = by_status.get(key, 0) + 1

        # 只有 dispatch 是「一次真实的浏览器往返」：rpc 是它的外层包装（含重试），
        # net 是 HTTP 本身。混在一起算耗时会重复计数，排行也会被放大。
        cmd_event = cat == "cmd" and rec["ev"] == "dispatch"
        dur = rec.get("dur_ms")
        if cmd_event and isinstance(dur, (int, float)) and dur >= 0:
            latencies.append(float(dur))
            if dur >= 200 and len(slowest) < 400:
                slowest.append(rec)

        ts = rec["ts"]
        if first_ts is None:
            first_ts = ts
        if last_ts is not None and ts - last_ts > 300 and len(gaps) < 500:
            gaps.append({"from": last_ts, "to": ts, "seconds": round(ts - last_ts, 1)})
        last_ts = ts
        if now - ts <= 60:
            last_60s += 1

        try:
            lt = time.localtime(ts)
            heat[lt.tm_wday][lt.tm_hour] += 1
        except (ValueError, OSError):
            pass

        action = rec.get("action")
        if action and cmd_event:
            slot = actions.get(action)
            if slot is None:
                slot = actions[action] = {"action": action, "n": 0, "fail": 0,
                                          "sum": 0.0, "max": 0.0, "durs": []}
            slot["n"] += 1
            if failed:
                slot["fail"] += 1
            if isinstance(dur, (int, float)):
                slot["sum"] += float(dur)
                slot["max"] = max(slot["max"], float(dur))
                if len(slot["durs"]) < 4000:
                    slot["durs"].append(float(dur))

        cid = rec.get("cid")
        if cid:
            slot = clients.get(cid)
            if slot is None:
                slot = clients[cid] = {"cid": cid, "kind": rec.get("kind") or "",
                                       "n": 0, "fail": 0, "first": ts, "last": ts}
            slot["n"] += 1
            slot["last"] = max(slot["last"], ts)
            slot["first"] = min(slot["first"], ts)
            if failed:
                slot["fail"] += 1

        tab = rec.get("tab")
        if tab is not None:
            key = str(tab)
            slot = tabs.get(key)
            if slot is None:
                slot = tabs[key] = {"tab": key, "n": 0, "fail": 0,
                                    "title": rec.get("tab_title") or "",
                                    "url": rec.get("tab_url") or ""}
            slot["n"] += 1
            if failed:
                slot["fail"] += 1
            if rec.get("tab_title"):
                slot["title"] = rec["tab_title"]
            if rec.get("tab_url"):
                slot["url"] = rec["tab_url"]

    total = len(matched)
    span_seconds = max(1.0, (last_ts - first_ts)) if (first_ts and last_ts) else 1.0
    size = _bucket_size(span_seconds) if total else 60

    # 固定步长覆盖整段：空桶必须保留，否则「静默期」会被图表抹掉
    series = []
    if total and first_ts and last_ts:
        start = int(first_ts // size) * size
        end = int(last_ts // size) * size
        if (end - start) / size > 1500:
            size = int(((end - start) / 1500) // 60 + 1) * 60
            start = int(first_ts // size) * size
        count = int((end - start) // size) + 1
        series = [_empty_bucket(start + i * size) for i in range(count)]
        index = dict((b["t"], b) for b in series)
        for rec in matched:
            bucket = index.get(int(rec["ts"] // size) * size)
            if bucket is None:
                continue
            bucket["total"] += 1
            bucket[rec["lv"]] = bucket.get(rec["lv"], 0) + 1
            bucket["cmd" if rec["cat"] == "cmd" else "net"] += 1
            if rec.get("ok") is False or rec["lv"] == "error":
                bucket["fail"] += 1

    edges = [(0, 5), (5, 20), (20, 50), (50, 100), (100, 300), (300, 1000),
             (1000, 3000), (3000, 10000), (10000, None)]
    hist = []
    for low, high in edges:
        count = 0
        for value in latencies:
            if value >= low and (high is None or value < high):
                count += 1
        if high is None:
            label = "≥%s" % _ms_label(low)
        elif low == 0:
            label = "<%s" % _ms_label(high)
        elif high < 1000:
            label = "%d-%dms" % (low, high)
        else:
            label = "%s-%s" % (_ms_label(low), _ms_label(high))
        hist.append({"label": label, "low": low, "high": high, "n": count})

    latencies.sort()
    action_rows = []
    for slot in actions.values():
        durs = sorted(slot["durs"])
        action_rows.append({
            "action": slot["action"], "n": slot["n"], "fail": slot["fail"],
            "fail_rate": round(slot["fail"] / slot["n"] * 100, 1) if slot["n"] else 0,
            "avg": round(slot["sum"] / slot["n"], 1) if slot["n"] else 0,
            "p50": round(_percentile(durs, 0.5), 1),
            "p95": round(_percentile(durs, 0.95), 1),
            "max": round(slot["max"], 1),
        })
    action_rows.sort(key=lambda r: (-r["n"], r["action"]))

    recent_out = [_slim(r) for r in matched[-limit_recent:]]
    recent_out.reverse()
    errors_out = [_slim(r) for r in errors[-limit_errors:]]
    errors_out.reverse()
    slowest.sort(key=lambda r: -(r.get("dur_ms") or 0))

    return {
        "generated": round(now, 3),
        "window": {"total": total,
                   "from": first_ts, "to": last_ts,
                   "span_s": round(span_seconds, 1), "bucket_s": size,
                   "buckets": len(series)},
        "kpi": {
            "total": total,
            "info": by_lv.get("info", 0),
            "warn": by_lv.get("warn", 0),
            "error": by_lv.get("error", 0),
            "debug": by_lv.get("debug", 0),
            "fail": fails,
            "fail_rate": round(fails / total * 100, 2) if total else 0,
            "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "p50_ms": round(_percentile(latencies, 0.5), 1),
            "p95_ms": round(_percentile(latencies, 0.95), 1),
            "p99_ms": round(_percentile(latencies, 0.99), 1),
            "max_ms": round(latencies[-1], 1) if latencies else 0,
            "actions": len(actions),
            "clients": len(clients),
            "tabs": len(tabs),
            "per_min": last_60s,
        },
        "series": series,
        "levels": [{"lv": lv, "n": by_lv.get(lv, 0)} for lv in _LEVEL_ORDER],
        "cats": sorted([{"cat": k, "n": v, "fail": cat_err.get(k, 0)}
                        for k, v in by_cat.items()], key=lambda r: -r["n"]),
        "srcs": sorted([{"src": k, "n": v} for k, v in by_src.items()],
                       key=lambda r: -r["n"]),
        "status": sorted([{"status": k, "n": v} for k, v in by_status.items()],
                         key=lambda r: -r["n"]),
        "actions": action_rows[:40],
        "hist": hist,
        "heat": heat,
        "clients": sorted(clients.values(), key=lambda r: -r["n"])[:40],
        "tabs": sorted(tabs.values(), key=lambda r: -r["n"])[:40],
        "recent": recent_out,
        "errors": errors_out,
        "slowest": [_slim(r) for r in slowest[:20]],
        "gaps": sorted(gaps, key=lambda g: -g["seconds"])[:5],
    }
