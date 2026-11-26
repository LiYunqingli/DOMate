# DOMate Guide

## 1. What it is

A terminal tool that drives your browser. A Python CLI talks to a Chrome/Edge extension over a local
WebSocket; the extension executes DOM reads, clicks, typing and script injection inside the page.

Design goals: one command per action, human-readable output by default, `--json` for machines,
stable error codes instead of free-form messages.

## 2. Install

Terminal: Python 3.8+, no third-party packages.

```
python main.py doctor
```

Both `daemon` and `browser` should be green. If the local service is not running, any command
auto-starts it in the background (detached; closing the terminal does not kill it). Logs: logs/domate.log.

Browser: open chrome://extensions, enable Developer mode, "Load unpacked", pick the `extension` folder.
The badge shows ON when connected.

### About reconnects

The extension talks to the local service over a WebSocket. It can only drop for two reasons: the
service process exited, or the browser reclaimed the extension's service worker. Both are handled,
so you should not notice them:

| Mechanism | What it does |
| --- | --- |
| Two-way keepalive (15s) | The service pings, the extension answers. Since Chrome 116, WebSocket traffic resets the service worker's 30-second idle timer, so the worker is never reclaimed while the heartbeat runs |
| Fast reconnect | Retries from 250ms, at most every 800ms, for 15 seconds — the socket reattaches as soon as the port reopens. Only after that does it back off to 1–5s |
| Command-side grace | A command that lands in the gap waits up to `command.connect_wait` (3.5s) instead of failing immediately. Normally never triggered |
| Event-driven reconnect | Switching tabs or windows, creating/closing tabs also triggers a reconnect attempt |

```
python main.py up            # ensure the service is up and wait for the extension
python main.py up --force    # restart the service first (after config changes)
```

`up` prints the elapsed time and returns almost instantly when everything is already connected.

## 3. Core concepts

### Selected tab

Most commands act on the selected tab; if none is selected, the active tab is used.

```
python main.py tab list
python main.py tab use 2        # by seq
python main.py tab use 120      # by tab id
python main.py tab use github   # by title/url fragment
python main.py dom text "css=h1" --tab 1   # one-off override
```

Selection is stored in ~/.domate/state.json. Tab ids change after a browser restart, so the tool
falls back to the active tab when the stored id no longer exists.

### Status title

While a command runs, the target tab title is prefixed with [Reading] / [Acting] / [Running]
(localized), then restored. Disable with `tab.status_title=false`.

### Selectors and indexes

```
#app .btn          CSS (default)
css=#app .btn      explicit CSS
text=Sign in       text match
xpath=//button[1]  XPath
3                  index from dom outline
```

Start with `dom outline`: it numbers links, buttons and inputs and tags them with `data-domate-idx`.
Then `click 3` clicks the third one — the shortest path for an AI agent.
Use `--index 2` to pick a specific match, or suffix `#btn@2`.

## 4. Workflows

Read an unfamiliar page:

```
python main.py page
python main.py dom tree --depth 3
python main.py dom outline
python main.py dom text "css=h1"
```

Fill a form:

```
python main.py type "css=#username" "admin"
python main.py type "css=#password" "@pwd.txt"
python main.py click "text=Sign in" --wait-nav
python main.py page
```

Wait for async rendering:

```
python main.py click "text=Search"
python main.py dom wait "css=.result-row" --timeout 15
python main.py dom wait "css=.loading" --gone
```

Extract data:

```
python main.py js "Array.from(document.querySelectorAll('.price')).map(e => e.innerText)"
```

Debug "click did nothing":

```
python main.py events --limit 20
python main.py console
python main.py dom info "css=#submit"
python main.py shot debug.png
```

## 5. Command map

```
service   serve / stop / status / doctor / config / version / help
autostart service install|uninstall|status|start|stop|restart
tabs      tab list|use|current|new|close|activate|title|reload|stop
read      dom tree|outline|html|text|find|attr|count|info|wait
interact  click type key hover scroll select check uncheck focus fill
script    js / eval
page      page goto back forward reload shot console events
logs      log serve|stats|tail|path|clear
```

## 6. Logs and the dashboard

Every event is written twice:

```
logs/domate.jsonl   structured, one JSON per line — the dashboard reads this
logs/domate.log     human-readable text — for tail / grep
```

Common fields: `ts / t / lv / cat / ev / src / pid`. Per category:

```
cat=net   method path status bytes dur_ms poll remote       HTTP requests
cat=cmd   action ok dur_ms code tab tab_title cid retry     command dispatch, one per attempt
cat=ws    cid kind tabs error                              connection lifecycle
cat=cli   argv exit ok dur_ms cwd                          terminal invocations
cat=sys   msg                                              start, stop, bind failures
```

A single command produces `cli/invoke` → `net/http` → `cmd/rpc` → `cmd/dispatch`,
so you can follow one action end to end with `--q <action>` or the command ranking panel.

The dashboard is read-only and does not need the service running:

```
python main.py log serve --open
```

It shows KPIs (volume, failure rate, P50/P95/P99, events per minute), a stacked
timeline by level, level and category splits, a latency histogram, command ranking
(failure rate + P95), a weekday × hour heatmap, HTTP status codes, a live log stream,
a paged event table, failures, silent gaps, clients, tabs and the slowest calls.
Filter by level / category / keyword / time range / polling / failures, and export
the current selection as CSV. New lines arrive over SSE; the page reconnects by itself.

Rotation is controlled by `server.log_max_mb` (8 MB) and `server.log_keep` (3);
`dashboard.window` caps how many events stay in memory (30000 by default).

## 7. Configuration

```
language             zh-CN / en-US
server.port          8765 by default
server.token         when set, the extension must use the same token
server.auto_start    auto-spawn the service
server.log_level     debug / info / warn / error
server.log_json      structured log path, logs/domate.jsonl
server.log_max_mb    rotate after this size
dashboard.port       dashboard port, 8899 by default
dashboard.window     events kept in memory
command.timeout      default timeout in seconds
command.retry        retries on timeout (set 0 for mutating commands)
tab.status_title     toggle the title prefix
security.allow_js    disable js/eval when false
security.allow_nav   disable goto/back/forward when false
```

```
python main.py config set server.port 8790
python main.py config get language
python main.py config reset
```

Change the port in the extension options too.

## 8. Troubleshooting

E_NO_BROWSER  extension not connected — start the service, then click Reconnect in the popup.
E_DAEMON_DOWN service down — run doctor, check the port.
E_PORT        port in use — serve --port 8790.
E_ELEMENT     element missing or invisible — check with dom find, or use --force.
E_TIMEOUT     page stuck — raise --timeout or run tab stop.
E_PERM        restricted page (chrome://) — cannot be scripted.

## 9. Limitations

- Synthetic events (isTrusted=false); strict anti-bot pages may reject them.
- Screenshots cover the visible viewport only.
- No network interception.
- Use one browser instance at a time.
