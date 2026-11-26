/* DOMate 日志看板前端：无框架、无 CDN，SSE 实时推送 + 手写图表 */
(function () {
  "use strict";

  var LV_COLORS = { debug: "#5b6b82", info: "#4d9dff", warn: "#f5b544", error: "#f2636b" };
  var LV_ORDER = ["debug", "info", "warn", "error"];
  var CAT_COLORS = { cmd: "cmd", net: "net", ws: "ws", cli: "cli", browser: "browser", sys: "sys" };

  var $ = function (sel) { return document.querySelector(sel); };
  var state = {
    lv: ["info", "warn", "error"],
    cat: [],
    q: "",
    range: 0,
    fromTs: 0,
    poll: false,
    fail: false,
    paused: false,
    data: null,
    filterKey: "",
    streamIds: {},
    tableRows: [],
    tableKey: "",
  };

  // ------------------------------------------------------------ 工具
  function h(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function fmt(n) {
    if (n === null || n === undefined) return "-";
    if (typeof n !== "number") return String(n);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return n.toLocaleString("zh-CN");
  }
  function ms(v) {
    if (v === null || v === undefined) return "-";
    if (v < 1) return v.toFixed(2) + "ms";
    if (v < 1000) return v.toFixed(0) + "ms";
    return (v / 1000).toFixed(2) + "s";
  }
  function clock(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function stamp(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + clock(ts);
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function niceMax(v) {
    if (v <= 5) return 5;
    var exp = Math.pow(10, Math.floor(Math.log10(v)));
    var base = v / exp;
    var mult = base <= 1 ? 1 : base <= 2 ? 2 : base <= 5 ? 5 : 10;
    return mult * exp;
  }
  function tip(node, htmlFn) {
    node.addEventListener("mousemove", function (ev) {
      var t = $("#tip");
      t.hidden = false;
      t.textContent = htmlFn();
      var x = Math.min(ev.clientX + 14, window.innerWidth - t.offsetWidth - 12);
      var y = Math.max(6, ev.clientY - t.offsetHeight - 12);
      t.style.left = x + "px";
      t.style.top = y + "px";
    });
    node.addEventListener("mouseleave", function () { $("#tip").hidden = true; });
  }

  // ------------------------------------------------------------ 连接
  function rangeStart() {
    // 固定住窗口起点：每秒都重算的话筛选 key 一直在变，表格和日志流会被反复清空
    if (!state.range) return 0;
    if (!state.fromTs) state.fromTs = Math.floor(Date.now() / 1000) - state.range;
    return state.fromTs;
  }

  function query() {
    var p = new URLSearchParams();
    if (state.lv.length) p.set("lv", state.lv.join(","));
    if (state.cat.length) p.set("cat", state.cat.join(","));
    if (state.q) p.set("q", state.q);
    var from = rangeStart();
    if (from > 0) p.set("from", String(from));
    p.set("poll", state.poll ? "1" : "0");
    p.set("fail", state.fail ? "1" : "0");
    return p.toString();
  }

  // 只反映「用户改过的筛选」，不含随时间滑动的 from：
  // 否则每 30 秒重新锚定窗口都会把日志流和表格清空一次。
  function sig() {
    return [state.lv.join(","), state.cat.join(","), state.q,
            state.poll ? 1 : 0, state.fail ? 1 : 0].join("|");
  }

  var es = null;
  var lastSnapAt = 0;
  var reconnectTimer = null;
  var liveKind = "wait";

  function setLive(kind, text) {
    var dot = $("#liveDot");
    liveKind = kind;
    dot.className = "dot " + (kind === "on" ? "on" : kind === "wait" ? "wait" : kind === "off" ? "off" : "");
    $("#liveText").textContent = text || { on: "实时", wait: "连接中", off: "已断开", paused: "已暂停" }[kind];
  }

  // 实时标签上带「N 秒前」，让人一眼看出数据是不是真的在动
  setInterval(function () {
    if (liveKind !== "on") return;
    var age = Math.round((Date.now() - lastSnapAt) / 1000);
    var el = $("#liveText");
    el.textContent = age >= 2 ? "实时 · " + age + "s" : "实时";
    el.title = "最后一次推送 " + age + " 秒前";
  }, 1000);

  function connect() {
    if (es) { es.close(); es = null; }
    clearTimeout(reconnectTimer);
    $("#csvBtn").href = "/api/export.csv?" + query();
    if (state.paused) { setLive("paused"); return; }
    setLive("wait");
    lastSnapAt = Date.now();
    es = new EventSource("/api/stream?" + query() + "&tick=5");
    es.addEventListener("snapshot", function (ev) {
      lastSnapAt = Date.now();
      setLive("on");
      try { render(JSON.parse(ev.data)); } catch (err) { console.error(err); }
    });
    es.onerror = function () {
      if (state.paused) return;
      // 浏览器对非 200/断流不一定自动重连，自己兜一层，别让页面无声冻住
      setLive("off", "重连中…");
      if (es && es.readyState === 2) { es.close(); es = null; }
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 2500);
    };
  }

  // 看门狗：12 秒没收到推送就主动重连一次（睡眠唤醒、代理掐流都能救回来）
  setInterval(function () {
    if (state.paused || !state.data) return;
    if (Date.now() - lastSnapAt > 12000) connect();
  }, 4000);

  var reconnectTimer = null;

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(function () {
      state.filterKey = "";
      state.tableKey = "";
      connect();
    }, 260);
  }

  // ------------------------------------------------------------ 渲染主入口
  function render(d) {
    state.data = d;
    renderHeader(d);
    renderKpis(d);
    renderTimeline(d.series, d.window.bucket_s);
    renderLevels(d.levels, d.kpi.total);
    renderHbars($("#catChart"), d.cats.map(function (c) {
      return { label: c.cat, n: c.n, cls: CAT_COLORS[c.cat] || "", cat: c.cat, fail: c.fail };
    }), d.kpi.total);
    renderLatency(d.hist, d.kpi);
    renderActions(d.actions);
    renderHeat(d.heat);
    renderStatus(d.status, d.kpi.total);
    renderStream(d.recent);
    renderTable(d.recent, d.meta.version);
    renderErrors(d.errors);
    renderClients(d.clients);
    renderTabs(d.tabs);
    renderSlow(d.slowest);
    renderGaps(d.gaps);
  }

  function renderHeader(d) {
    var k = d.kpi, w = d.window, m = d.meta;
    var files = (m.files || []).filter(function (f) { return f.exists; });
    var span = w.from ? stamp(w.from) + " → " + stamp(w.to) : "无数据";
    $("#subtitle").textContent = files.length + " 个日志文件 · 已载入 " + fmt(m.parsed) +
      " 条 · 内存窗口 " + fmt(m.in_memory) + "/" + fmt(m.window) + " 条 · " + span;
    $("#windowInfo").textContent = "当前筛选 " + fmt(k.total) + " 条 · 粒度 " + w.bucket_s + "s · 更新于 " + clock(d.generated);
  }

  function renderKpis(d) {
    var k = d.kpi;
    var cards = [
      { cls: "", label: "事件总数", value: fmt(k.total), foot: "窗口内匹配 " + fmt(k.total) + " 条" },
      { cls: k.fail ? "error" : "ok", label: "失败 / 错误", value: fmt(k.fail), foot: "失败率 " + k.fail_rate + "%" },
      { cls: "", label: "平均耗时", value: ms(k.avg_ms), foot: "P50 " + ms(k.p50_ms) + " · P95 " + ms(k.p95_ms) },
      { cls: "", label: "P99 / 最慢", value: ms(k.p99_ms), foot: "最慢 " + ms(k.max_ms) },
      { cls: "accent", label: "命令种类", value: fmt(k.actions), foot: "标签页 " + fmt(k.tabs) + " 个" },
      { cls: "accent", label: "浏览器客户端", value: fmt(k.clients), foot: "含历史连接" },
      { cls: "", label: "近 60 秒事件", value: fmt(k.per_min), foot: (k.per_min / 60).toFixed(2) + " 条/秒" },
      { cls: k.error ? "warn" : "ok", label: "error / warn", value: fmt(k.error) + " / " + fmt(k.warn), foot: "debug " + fmt(k.debug) },
    ];
    var box = $("#kpis");
    box.innerHTML = "";
    cards.forEach(function (c) {
      var node = h("div", "kpi " + c.cls);
      node.appendChild(h("div", "label", c.label));
      node.appendChild(h("div", "value", c.value));
      node.appendChild(h("div", "foot", c.foot));
      box.appendChild(node);
    });
  }

  // ------------------------------------------------------------ 时间线（SVG）
  function renderTimeline(series, bucket) {
    var box = $("#timelineChart");
    box.innerHTML = "";
    if (!series || !series.length) {
      box.appendChild(h("div", "empty", "这个筛选条件下没有事件"));
      return;
    }
    var W = Math.max(320, box.clientWidth - 20);
    var H = Math.max(160, box.clientHeight - 12);
    // 注意：别叫 pad，会遮住上面的 pad(n) 补零函数
    var padBox = { l: 42, r: 8, t: 10, b: 20 };
    var plotW = W - padBox.l - padBox.r;
    var plotH = H - padBox.t - padBox.b;
    var max = 0;
    series.forEach(function (b) { if (b.total > max) max = b.total; });
    max = niceMax(Math.max(1, max));

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("height", H);

    function y(v) { return padBox.t + plotH - (v / max) * plotH; }

    // 网格与 y 轴刻度
    for (var i = 0; i <= 3; i++) {
      var val = (max / 3) * i;
      var yy = y(val);
      var line = document.createElementNS(svg.namespaceURI, "line");
      line.setAttribute("x1", padBox.l); line.setAttribute("x2", W - padBox.r);
      line.setAttribute("y1", yy); line.setAttribute("y2", yy);
      line.setAttribute("stroke", "#1b2432");
      line.setAttribute("stroke-width", "1");
      svg.appendChild(line);
      var label = document.createElementNS(svg.namespaceURI, "text");
      label.setAttribute("x", padBox.l - 6); label.setAttribute("y", yy + 3.5);
      label.setAttribute("fill", "#55617a");
      label.setAttribute("font-size", "9.5");
      label.setAttribute("text-anchor", "end");
      label.textContent = Math.round(val);
      svg.appendChild(label);
    }

    var n = series.length;
    var bw = plotW / n;
    var gap = bw > 5 ? 1 : 0;
    var failTicks = [];

    series.forEach(function (b, idx) {
      var x = padBox.l + idx * bw;
      var acc = 0;
      LV_ORDER.forEach(function (lv) {
        var v = b[lv] || 0;
        if (!v) return;
        var height = (v / max) * plotH;
        var rect = document.createElementNS(svg.namespaceURI, "rect");
        rect.setAttribute("x", x + gap / 2);
        rect.setAttribute("y", y(acc + v));
        rect.setAttribute("width", Math.max(1, bw - gap));
        rect.setAttribute("height", Math.max(1, height));
        rect.setAttribute("fill", LV_COLORS[lv]);
        rect.setAttribute("opacity", lv === "debug" ? "0.5" : "0.85");
        svg.appendChild(rect);
        acc += v;
      });
      if (b.fail) {
        var ty = padBox.t + plotH - 1;
        var tick = document.createElementNS(svg.namespaceURI, "rect");
        tick.setAttribute("x", x + gap / 2);
        tick.setAttribute("y", ty - 2);
        tick.setAttribute("width", Math.max(1, bw - gap));
        tick.setAttribute("height", "2");
        tick.setAttribute("fill", "#ff5d66");
        svg.appendChild(tick);
        failTicks.push({ x: x + bw / 2, y: ty });
      }
      var hit = document.createElementNS(svg.namespaceURI, "rect");
      hit.setAttribute("x", x); hit.setAttribute("y", padBox.t);
      hit.setAttribute("width", Math.max(1, bw)); hit.setAttribute("height", plotH);
      hit.setAttribute("fill", "transparent");
      hit.style.cursor = "crosshair";
      tip(hit, function () {
        return stamp(b.t) + "  +" + bucket + "s\n总计 " + b.total +
          "  info " + (b.info || 0) + "  warn " + (b.warn || 0) + "  error " + (b.error || 0) +
          "\n失败 " + (b.fail || 0) + "  cmd " + (b.cmd || 0);
      });
      svg.appendChild(hit);
    });

    // x 轴时间标签
    var step = Math.max(1, Math.floor(n / 6));
    for (var j = 0; j < n; j += step) {
      var text = document.createElementNS(svg.namespaceURI, "text");
      text.setAttribute("x", padBox.l + j * bw + bw / 2);
      text.setAttribute("y", H - 5);
      text.setAttribute("fill", "#55617a");
      text.setAttribute("font-size", "9.5");
      text.setAttribute("text-anchor", "middle");
      var b0 = series[j];
      var d0 = new Date(b0.t * 1000);
      text.textContent = (n * bucket > 86400 ? pad(d0.getMonth() + 1) + "-" + pad(d0.getDate()) + " " : "") +
        pad(d0.getHours()) + ":" + pad(d0.getMinutes());
      svg.appendChild(text);
    }
    box.appendChild(svg);
    $("#timelineInfo").textContent = series.length + " 个时间桶 · 每桶 " + bucket + "s";
  }

  // ------------------------------------------------------------ 级别
  function renderLevels(levels, total) {
    var bar = $("#levelBar"), legend = $("#levelLegend");
    bar.innerHTML = ""; legend.innerHTML = "";
    levels.forEach(function (item) {
      var n = item.n || 0;
      var seg = h("i");
      seg.style.width = total ? (n / total * 100) + "%" : "0";
      seg.style.background = LV_COLORS[item.lv];
      bar.appendChild(seg);
      var wrap = h("span", "item");
      var sw = h("span", "swatch");
      sw.style.background = LV_COLORS[item.lv];
      wrap.appendChild(sw);
      wrap.appendChild(h("span", "", item.lv));
      var b = h("b", "", fmt(n));
      wrap.appendChild(b);
      wrap.appendChild(h("span", "", total ? " " + (n / total * 100).toFixed(1) + "%" : " 0%"));
      legend.appendChild(wrap);
    });
    $("#levelInfo").textContent = fmt(total) + " 条";
  }

  // ------------------------------------------------------------ 通用横条
  function renderHbars(box, rows, total) {
    box.innerHTML = "";
    if (!rows.length) { box.appendChild(h("div", "empty", "暂无数据")); return; }
    var max = rows[0].n || 1;
    rows.forEach(function (r) {
      var row = h("div", "hbar");
      row.appendChild(h("div", "name", r.label));
      var track = h("div", "track");
      var fill = h("div", "fill " + (r.cls || ""));
      fill.style.width = Math.max(1.5, (r.n / max) * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      var val = h("div", "val");
      val.appendChild(document.createTextNode(fmt(r.n)));
      if (total) {
        var em = h("em", "", " " + (r.n / total * 100).toFixed(0) + "%");
        val.appendChild(em);
      }
      row.appendChild(val);
      if (r.cat) {
        row.addEventListener("click", function () {
          if (state.cat.length === 1 && state.cat[0] === r.cat) state.cat = [];
          else state.cat = [r.cat];
          syncChips("#catChips", "cat", state.cat);
          scheduleReconnect();
        });
      }
      if (r.fail) {
        tip(row, function () { return r.label + "\n共 " + r.n + " 条，其中失败 " + r.fail + " 条"; });
      }
      box.appendChild(row);
    });
  }

  // ------------------------------------------------------------ 延迟直方图
  function renderLatency(hist, kpi) {
    var box = $("#latencyChart");
    box.innerHTML = "";
    var max = 0;
    hist.forEach(function (b) { if (b.n > max) max = b.n; });
    max = Math.max(1, max);
    hist.forEach(function (b) {
      var col = h("div", "col");
      var num = h("div", "num", b.n ? fmt(b.n) : "");
      var bar = h("div", "bar");
      bar.style.height = Math.max(1, (b.n / max) * 92) + "%";
      bar.style.background = b.n ? "linear-gradient(180deg,#5aa9ff,#24507e)" : "#141c28";
      col.appendChild(num);
      col.appendChild(bar);
      col.appendChild(h("div", "cap", b.label));
      tip(col, function () { return b.label + "：" + b.n + " 次"; });
      box.appendChild(col);
    });
    $("#latencyInfo").textContent = "P95 " + ms(kpi.p95_ms) + " · 最大 " + ms(kpi.max_ms);
  }

  // ------------------------------------------------------------ 命令排行
  function renderActions(rows) {
    var box = $("#actionChart");
    box.innerHTML = "";
    if (!rows.length) { box.appendChild(h("div", "empty", "还没有命令日志")); return; }
    var max = rows[0].n || 1;
    rows.slice(0, 16).forEach(function (r) {
      var row = h("div", "arow");
      row.appendChild(h("div", "a-name", r.action));
      var track = h("div", "a-track");
      var fill = h("div", "a-fill");
      fill.style.width = Math.max(1.5, (r.n / max) * 100) + "%";
      fill.style.background = r.fail_rate > 30
        ? "linear-gradient(90deg,#8c2f38,#f2636b)"
        : "linear-gradient(90deg,#2c5f9e,#4d9dff)";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(h("div", "a-n", fmt(r.n)));
      var fr = h("div", "a-fail " + (r.fail_rate > 30 ? "bad" : r.fail_rate > 0 ? "mid" : ""),
        r.fail_rate ? r.fail_rate + "%" : "0%");
      row.appendChild(fr);
      row.appendChild(h("div", "a-p95", ms(r.p95)));
      tip(row, function () {
        return r.action + "\n次数 " + r.n + "  失败 " + r.fail + " (" + r.fail_rate + "%)" +
          "\n平均 " + ms(r.avg) + "  P50 " + ms(r.p50) + "  P95 " + ms(r.p95) + "  最大 " + ms(r.max);
      });
      row.addEventListener("click", function () {
        state.q = r.action;
        $("#q").value = r.action;
        scheduleReconnect();
      });
      box.appendChild(row);
    });
  }

  // ------------------------------------------------------------ 热力图
  function renderHeat(heat) {
    var box = $("#heatChart");
    box.innerHTML = "";
    var days = ["一", "二", "三", "四", "五", "六", "日"];
    var max = 0;
    heat.forEach(function (row) { row.forEach(function (v) { if (v > max) max = v; }); });
    max = Math.max(1, max);
    var head = h("div", "hl", "");
    box.appendChild(head);
    for (var hour = 0; hour < 24; hour++) {
      box.appendChild(h("div", "hh", hour % 3 === 0 ? pad(hour) : ""));
    }
    // 用 forEach 而不是 for：闭包里要拿到「当时那一格」的星期与小时，
    // var 是函数作用域，for 循环里建闭包会全部指向最后一格。
    heat.forEach(function (row, d) {
      box.appendChild(h("div", "hl", days[d] || d));
      row.forEach(function (v, hr) {
        var cell = h("div", "cell");
        var ratio = Math.pow(v / max, 0.55);
        cell.style.background = v === 0 ? "#0c121c"
          : "rgba(77,157,255," + (0.12 + 0.78 * ratio).toFixed(3) + ")";
        if (v > 0) {
          cell.setAttribute("data-tip", "1");
          tip(cell, function () { return "周" + days[d] + " " + pad(hr) + ":00 —— " + v + " 条"; });
        }
        box.appendChild(cell);
      });
    });
  }

  // ------------------------------------------------------------ HTTP 状态
  function renderStatus(rows, total) {
    var mapped = rows.map(function (r) {
      var code = parseInt(r.status, 10);
      var cls = code >= 500 ? "cli" : code >= 400 ? "browser" : "net";
      return { label: r.status, n: r.n, cls: cls, status: code };
    });
    var sum = 0;
    mapped.forEach(function (r) { sum += r.n; });
    renderHbars($("#statusChart"), mapped, sum || total);
    $("#statusInfo").textContent = "共 " + rows.length + " 种状态";
  }

  // ------------------------------------------------------------ 实时流
  function renderStream(recent) {
    var box = $("#stream");
    if (state.filterKey !== sig()) {
      state.filterKey = sig();
      state.streamIds = {};
      box.innerHTML = "";
    }
    if (!recent || !recent.length) {
      if (!box.childElementCount) box.appendChild(h("div", "empty", "等待日志…"));
      return;
    }
    var asc = recent.slice().reverse();
    var frag = document.createDocumentFragment();
    var added = 0;
    asc.forEach(function (rec) {
      if (state.streamIds[rec.i]) return;
      state.streamIds[rec.i] = 1;
      frag.appendChild(streamRow(rec));
      added++;
    });
    if (added) {
      box.appendChild(frag);
      while (box.childElementCount > 320) box.removeChild(box.firstChild);
      if ($("#autoScroll").checked) box.scrollTop = box.scrollHeight;
    }
    $("#streamInfo").textContent = "显示 " + box.childElementCount + " 条 · 最新 " +
      (recent[0] ? recent[0].t.slice(11) : "-");
  }

  function streamRow(rec) {
    var row = h("div", "srow lv-" + rec.lv);
    row.appendChild(h("span", "ts", (rec.t || "").slice(11)));
    var lv = h("span", "lv lv-chip " + rec.lv, rec.lv);
    row.appendChild(lv);
    row.appendChild(h("span", "cat", rec.cat));
    var msg = h("span", "msg" + (rec.ok === false ? " fail" : ""), rec.msg || "");
    row.appendChild(msg);
    row.appendChild(h("span", "dur", rec.dur_ms != null ? ms(rec.dur_ms) : ""));
    row.addEventListener("click", function () { showDetail(rec); });
    return row;
  }

  // ------------------------------------------------------------ 事件表
  function renderTable(rows, version) {
    var key = sig();
    var wrap = $("#table");
    if (state.tableKey !== key) {
      state.tableKey = key;
      state.tableRows = [];
      wrap.innerHTML = "";
      var thead = document.createElement("table");
      thead.innerHTML = "<thead><tr><th>时间</th><th>级别</th><th>分类</th><th>事件</th>" +
        "<th>动作 / 文本</th><th>耗时</th><th>结果</th><th>标签页</th></tr></thead><tbody></tbody>";
      wrap.appendChild(thead);
      state.tableEl = thead;
    }
    if (!state.tableEl) return;
    if (state.tableRows.length === 0 || !state.tableRows.length) {
      state.tableRows = rows.slice();
      rebuildTableBody();
    } else {
      var known = {};
      state.tableRows.forEach(function (r) { known[r.i] = 1; });
      var fresh = rows.filter(function (r) { return !known[r.i]; });
      if (fresh.length) {
        state.tableRows = fresh.concat(state.tableRows).slice(0, 600);
        rebuildTableBody();
      }
    }
    $("#tableInfo").textContent = "已显示 " + state.tableRows.length + " / " + fmt((state.data || {}).kpi.total || 0) + " 条";
  }

  function rebuildTableBody() {
    var tbody = state.tableEl.querySelector("tbody");
    tbody.innerHTML = "";
    var frag = document.createDocumentFragment();
    state.tableRows.forEach(function (rec) {
      var tr = document.createElement("tr");
      tr.appendChild(td((rec.t || "").slice(5, 23), "mono"));
      var cell = document.createElement("td");
      cell.appendChild(h("span", "lv-chip " + rec.lv, rec.lv));
      tr.appendChild(cell);
      tr.appendChild(td(rec.cat, "mono"));
      tr.appendChild(td(rec.ev, "mono"));
      tr.appendChild(td(rec.action || rec.msg || rec.argv || "", "msg"));
      tr.appendChild(td(rec.dur_ms != null ? ms(rec.dur_ms) : "", "num"));
      var res = td(rec.ok === false ? (rec.code || "失败") : rec.ok === true ? "ok" : "", "mono");
      if (rec.ok === false) res.style.color = "#f2636b";
      tr.appendChild(res);
      tr.appendChild(td(rec.tab_title || rec.tab || "", "msg"));
      tr.addEventListener("click", function () { showDetail(rec); });
      frag.appendChild(tr);
    });
    tbody.appendChild(frag);
  }

  function td(text, cls) {
    var cell = document.createElement("td");
    if (cls) cell.className = cls;
    cell.textContent = text === undefined || text === null ? "" : String(text);
    return cell;
  }

  // ------------------------------------------------------------ 错误表
  function renderErrors(rows) {
    var wrap = $("#errorTable");
    wrap.innerHTML = "";
    if (!rows.length) { wrap.appendChild(h("div", "empty", "没有失败记录")); return; }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>时间</th><th>级别</th><th>来源</th><th>动作/文本</th><th>错误码</th></tr></thead><tbody></tbody>";
    var tbody = table.querySelector("tbody");
    rows.forEach(function (rec) {
      var tr = document.createElement("tr");
      tr.appendChild(td((rec.t || "").slice(5, 23), "mono"));
      var c = document.createElement("td");
      c.appendChild(h("span", "lv-chip " + rec.lv, rec.lv));
      tr.appendChild(c);
      tr.appendChild(td(rec.cat + "/" + rec.ev, "mono"));
      tr.appendChild(td(rec.action || rec.msg || "", "msg"));
      tr.appendChild(td(rec.code || (rec.error ? String(rec.error).slice(0, 40) : ""), "mono"));
      tr.addEventListener("click", function () { showDetail(rec); });
      tbody.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderClients(rows) {
    var wrap = $("#clientTable");
    wrap.innerHTML = "";
    if (!rows.length) { wrap.appendChild(h("div", "empty", "暂无客户端")); return; }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>客户端</th><th>类型</th><th>事件</th><th>失败</th><th>最后活动</th></tr></thead><tbody></tbody>";
    var tbody = table.querySelector("tbody");
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      tr.appendChild(td(r.cid, "mono"));
      tr.appendChild(td(r.kind || "-"));
      tr.appendChild(td(fmt(r.n), "num"));
      tr.appendChild(td(fmt(r.fail), "num"));
      tr.appendChild(td(clock(r.last), "mono"));
      tbody.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderTabs(rows) {
    var wrap = $("#tabTable");
    wrap.innerHTML = "";
    if (!rows.length) { wrap.appendChild(h("div", "empty", "暂无标签页操作")); return; }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>标签页</th><th>标题</th><th>事件</th><th>失败</th></tr></thead><tbody></tbody>";
    var tbody = table.querySelector("tbody");
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      tr.appendChild(td(r.tab, "mono"));
      tr.appendChild(td(r.title || "-", "msg"));
      tr.appendChild(td(fmt(r.n), "num"));
      tr.appendChild(td(fmt(r.fail), "num"));
      tbody.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderSlow(rows) {
    var wrap = $("#slowTable");
    wrap.innerHTML = "";
    if (!rows.length) { wrap.appendChild(h("div", "empty", "暂无耗时记录")); return; }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>耗时</th><th>动作</th><th>结果</th><th>时间</th></tr></thead><tbody></tbody>";
    var tbody = table.querySelector("tbody");
    rows.forEach(function (rec) {
      var tr = document.createElement("tr");
      tr.appendChild(td(ms(rec.dur_ms), "num"));
      tr.appendChild(td(rec.action || rec.msg || "", "msg"));
      tr.appendChild(td(rec.ok === false ? (rec.code || "失败") : "ok", "mono"));
      tr.appendChild(td((rec.t || "").slice(11), "mono"));
      tr.addEventListener("click", function () { showDetail(rec); });
      tbody.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderGaps(rows) {
    var wrap = $("#gapTable");
    wrap.innerHTML = "";
    if (!rows.length) { wrap.appendChild(h("div", "empty", "没有长时间空档")); return; }
    var table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>时长</th><th>从</th><th>到</th></tr></thead><tbody></tbody>";
    var tbody = table.querySelector("tbody");
    rows.forEach(function (g) {
      var tr = document.createElement("tr");
      var sec = g.seconds;
      var text = sec > 3600 ? (sec / 3600).toFixed(1) + "h" : sec > 60 ? (sec / 60).toFixed(0) + "m" : sec + "s";
      tr.appendChild(td(text, "num"));
      tr.appendChild(td(stamp(g.from), "mono"));
      tr.appendChild(td(stamp(g.to), "mono"));
      tbody.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  // ------------------------------------------------------------ 详情
  var drawerOpenedAt = 0;

  function showDetail(rec) {
    var drawer = $("#drawer");
    $("#drawerTitle").textContent = (rec.cat || "") + " / " + (rec.ev || "") + "  #" + (rec.i || "");
    var lines = [];
    Object.keys(rec).sort().forEach(function (key) {
      var value = rec[key];
      if (value && typeof value === "object") value = JSON.stringify(value, null, 2);
      lines.push(key + ": " + value);
    });
    $("#drawerBody").textContent = lines.join("\n");
    drawer.hidden = false;
    drawerOpenedAt = Date.now();
  }

  function closeDetail() {
    var drawer = $("#drawer");
    if (drawer.hidden) return;
    drawer.hidden = true;
    drawerOpenedAt = 0;
  }

  // ------------------------------------------------------------ 交互
  function syncChips(selector, attr, values) {
    document.querySelectorAll(selector + " .chip").forEach(function (btn) {
      btn.classList.toggle("on", values.indexOf(btn.dataset[attr]) >= 0);
    });
  }

  function initFilters() {
    $("#rangeSeg").addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-range]");
      if (!btn) return;
      document.querySelectorAll("#rangeSeg button").forEach(function (b) { b.classList.toggle("on", b === btn); });
      state.range = parseInt(btn.dataset.range, 10) || 0;
      state.fromTs = 0;
      scheduleReconnect();
    });

    $("#lvChips").addEventListener("click", function (ev) {
      var btn = ev.target.closest(".chip");
      if (!btn) return;
      var lv = btn.dataset.lv;
      var pos = state.lv.indexOf(lv);
      if (pos >= 0) { if (state.lv.length > 1) state.lv.splice(pos, 1); }
      else state.lv.push(lv);
      syncChips("#lvChips", "lv", state.lv);
      scheduleReconnect();
    });

    $("#catChips").addEventListener("click", function (ev) {
      var btn = ev.target.closest(".chip");
      if (!btn) return;
      var cat = btn.dataset.cat;
      var pos = state.cat.indexOf(cat);
      if (pos >= 0) state.cat.splice(pos, 1);
      else state.cat.push(cat);
      syncChips("#catChips", "cat", state.cat);
      scheduleReconnect();
    });

    var qTimer = null;
    $("#q").addEventListener("input", function () {
      clearTimeout(qTimer);
      qTimer = setTimeout(function () {
        state.q = $("#q").value.trim();
        scheduleReconnect();
      }, 300);
    });

    $("#pollChk").addEventListener("change", function () {
      state.poll = this.checked;
      scheduleReconnect();
    });
    $("#failChk").addEventListener("change", function () {
      state.fail = this.checked;
      scheduleReconnect();
    });

    $("#pauseBtn").addEventListener("click", function () {
      state.paused = !state.paused;
      this.textContent = state.paused ? "继续" : "暂停";
      if (state.paused) {
        if (es) { es.close(); es = null; }
        setLive("paused");
      } else {
        connect();
      }
    });

    $("#refreshBtn").addEventListener("click", function () {
      fetch("/api/snapshot?" + query()).then(function (r) { return r.json(); })
        .then(function (d) { render(d); }).catch(function (e) { console.error(e); });
    });

    $("#moreBtn").addEventListener("click", function () {
      var offset = state.tableRows.length;
      fetch("/api/events?" + query() + "&offset=" + offset + "&limit=200")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.items || !d.items.length) { $("#tableInfo").textContent = "没有更多了"; return; }
          var known = {};
          state.tableRows.forEach(function (r) { known[r.i] = 1; });
          var extra = d.items.filter(function (r) { return !known[r.i]; });
          if (!extra.length) { $("#tableInfo").textContent = "没有更多了"; return; }
          state.tableRows = state.tableRows.concat(extra);
          rebuildTableBody();
          $("#tableInfo").textContent = "已显示 " + state.tableRows.length + " / " + fmt(d.total) + " 条";
        }).catch(function (e) { console.error(e); });
    });

    $("#drawerClose").addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      closeDetail();
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeDetail();
    });

    // 点抽屉以外的地方也关；刚打开的那一下（同一次点击冒泡上来）要放行
    document.addEventListener("click", function (ev) {
      var drawer = $("#drawer");
      if (drawer.hidden) return;
      if (drawer.contains(ev.target)) return;
      if (Date.now() - drawerOpenedAt < 250) return;
      closeDetail();
    });
  }

  // 窗口尺寸变化时重画时间线（SVG 用像素宽度算柱子，不重画会错位）
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (state.data) renderTimeline(state.data.series, state.data.window.bucket_s);
    }, 150);
  });

  // 15 分钟这种滑动窗口要跟着时间走，否则越看越旧
  setInterval(function () {
    if (state.range > 0 && !state.paused) {
      state.fromTs = Math.floor(Date.now() / 1000) - state.range;
      connect();
    }
  }, 30000);

  document.addEventListener("DOMContentLoaded", function () {
    initFilters();
    connect();
  });
})();
