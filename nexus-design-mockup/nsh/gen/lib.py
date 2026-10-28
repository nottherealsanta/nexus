import re, os, glob
ROOT = "/Users/santosh.rajan/repos/nexus"
OUT = ROOT + "/nexus-design-mockup/nsh"
ICON_DIR = ROOT + "/nexus-nsh/src/icons"

ICONS = {}
for f in glob.glob(ICON_DIR + "/*.svg"):
    s = open(f).read()
    inner = re.search(r"<svg[^>]*>(.*)</svg>", s, re.S).group(1)
    ICONS[os.path.basename(f)[:-4]] = inner
ICONS.update({
    "read": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4M10 9H8M16 13H8M16 17H8"/>',
    "terminal": '<path d="m4 17 6-6-6-6M12 19h8"/>',
    "code": '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    "wifi-off": '<path d="M12 20h.01M8.5 16.4a5 5 0 0 1 7 0M2 8.8a15 15 0 0 1 4.2-2.6M22 8.8a15 15 0 0 0-8-3.7M5 12.9a10 10 0 0 1 5.2-2.7M19 12.9a10 10 0 0 0-2.6-1.9M2 2l20 20"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "arrow-down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "corner": '<path d="m15 10 5 5-5 5"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
})
SPRITE = '<svg width="0" height="0" style="position:absolute" aria-hidden="true">' + "".join(
    f'<symbol id="i-{k}" viewBox="0 0 24 24">{v}</symbol>' for k, v in ICONS.items()) + "</svg>"

def render(h):
    def rep(m):
        name, cls = m.group(1), m.group(2) or ""
        return f'<svg class="ic {cls}"><use href="#i-{name}"/></svg>'
    return re.sub(r"@ic\(([\w-]+)(?:,\s*([\w ]+))?\)", rep, h)

def page(name, title, size, body, sheet=False, extra_head=""):
    html = f'''<!doctype html>
<html lang="en" data-size="{size}">
<head><meta charset="utf-8"><title>NSH {title}</title>
<script src="app.js"></script>
<link rel="stylesheet" href="nsh.css">{extra_head}</head>
<body data-size="{size}" class="{'sheet' if sheet else ''}">
{SPRITE}
{render(body)}
</body></html>
'''
    open(f"{OUT}/{name}.html", "w").write(html)

# ---------- marks ----------
def mark(kind):
    sv = {
        "wait": '<svg viewBox="0 0 12 12"><path d="M6 .8 11.2 6 6 11.2.8 6z" fill="currentColor"/></svg>',
        "run": '<svg class="spin" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="6" cy="6" r="4.4" opacity=".3"/><path d="M6 1.6a4.4 4.4 0 0 1 4.4 4.4" stroke-linecap="round"/></svg>',
        "err": '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"/></svg>',
        "unread": '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="3.2" fill="currentColor"/></svg>',
        "ok": '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.3 4.8 9 10 3.2"/></svg>',
        "warn": '<svg viewBox="0 0 12 12"><path d="M6 1 11.5 10.5h-11z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M6 4.6v2.6M6 8.6v.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
        "stopped": '<svg viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" rx="1.5" fill="currentColor"/></svg>',
        "none": '',
        "mut": '<svg viewBox="0 0 12 12"><circle cx="6" cy="6" r="3.2" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
    }[kind]
    return f'<span class="mark {kind}">{sv}</span>'

# ---------- sessions list ----------
SESS = [
    ("Refactor the loader", "none", "build", "main", "now", ""),
    ("Review the CJK width handling", "wait", "orchestrate", "main", "3m", "2"),
    ("Release notes for 0.10", "run", "plan", "docs/0.10", "1h", ""),
    ("Fix flaky test in sync", "err", "build", "main", "2h", ""),
    ("Spike: terminal harness", "unread", "scout", "main", "1d", ""),
]

def srow(i, t=None, state="", overrides=None):
    title, mk, ag, br, time, kids = SESS[i]
    o = overrides or {}
    mk = o.get("mk", mk)
    cls = "srow " + state + (" unread" if mk == "unread" else "")
    shim = " shim" if mk == "run" else ""
    return (f'<div class="{cls}"><span class="mk">{mark(mk) if mk != "none" else ""}</span>'
            f'<div class="l1"><span class="t{shim}">{title}</span><span class="time">{time}</span><span class="more">@ic(more,s14)</span></div>'
            f'<div class="l2"><span class="ag-{ag}">{ag}</span><span>·</span><span>@ic(branch,s12)</span><span class="trunc">{br}</span>'
            f'<span class="kids">{kids}{"@ic(corner,s12)" if kids else ""}</span></div></div>')

def left_sessions(open_i=0, extra_states=None, new_open=False, search="Search sessions", marks=None):
    st = extra_states or {}
    rows = ""
    for i in range(5):
        s = "open" if (i == open_i and not new_open) else ""
        s += " " + st.get(i, "")
        rows += srow(i, state=s, overrides=({'mk': marks[i]} if marks and i in marks else None))
    return f'''<aside class="sb sb-l">
<div class="sb-search">@ic(search,s14)<span>{search}</span><span class="kbd">ctrl+k</span></div>
<div class="grp"><span class="lbl">nexus-demo</span></div>
<div class="scrollarea">{rows}
<div class="newrow {'open' if new_open else ''}">@ic(plus,s14)<span>New session</span></div></div>
<div class="sb-foot"><div class="menu-row">@ic(settings,s14)<span>Settings</span><span class="kbd">ctrl+,</span></div></div>
</aside>'''

# ---------- header ----------
def header(tab="sessions", crumbs=("nexus-demo", "Refactor the loader"), run=("ok", "idle"), attn=1, left=True, right=True, mid_extra="", child=None):
    tabs = [("sessions", "sessions", "Sessions"), ("files", "files", "Files"), ("projects", "projects", "Projects")]
    t = ""
    for key, ic, lab in tabs:
        t += f'<span class="icon-btn hi {"on" if key == tab else ""}" title="{lab}">@ic({ic},s14)</span>'
    title = crumbs[-1] if crumbs else ""
    titles = [s[0] for s in SESS]
    if not crumbs or title == "Nexus":
        stab = []
    elif title in titles:
        stab = [(s[0], s[1], s[0] == title) for s in SESS[:4]]
    else:
        stab = [(s[0], s[1], False) for s in SESS[:3]] + [(title, "none", True)]
    cr = ""
    for ttl, mk, viewed in stab:
        g = mark(mk) if mk != "none" else '<span class="mark idle">·</span>'
        if viewed and child:
            label = f'<span class="trunc muted">{ttl}</span><span class="sep">›</span><b class="trunc">{child}</b>'
        else:
            label = f'<span class="trunc">{ttl}</span>'
        cr += f'<span class="stab {"on" if viewed else ""}">{g}{label}<span class="x">@ic(x,s12)</span></span>'
    if stab:
        cr += '<span class="stab-new" title="New session">@ic(plus,s14)</span>'
    rs = ""
    if run:
        k, txt = run
        cls = {"wait": "wait", "off": "off"}.get(k, "")
        rs = f'<span class="runstat {cls}">{mark(k) if k != "off" else mark("err")}<span>{txt}</span></span>'
    att = f'<span class="attn"><i></i>{attn}</span>' if attn else ""
    return f'''<header class="hdr"><div class="hdr-l"><span class="icon-btn">@ic(panel-left)</span><nav class="hicons">{t}</nav></div>
<div class="hdr-c"><div class="stabs">{cr}</div>{rs}</div>
<div class="hdr-r">{att}<span class="icon-btn">@ic(panel-right)</span></div></header>'''

# ---------- right sidebar ----------
def right_info(status=("ok", "idle"), turns="2 · 3 tools", started="3:40 PM · 2m", pct=41, git=True, branch_extra="↑1", agent="Build", agcls="ag-build", tools=None, used="82k", toks="12k in <span class=\"m\">·</span> 2k out", cost="$0.04"):
    sm, st = status
    g = ""
    if git:
        g = f'''<div class="sec"><span class="lbl">Git</span>
<div class="row gap8" style="font-size:13px;color:var(--text-primary);height:24px">@ic(branch,s14)<span>main</span><span class="muted tnum">{branch_extra}</span></div>
<div class="gitgrp"><span>Unstaged</span><span class="n">2</span></div>
<div class="gitrow"><span class="ltr M">M</span><span class="p">README.md</span><span class="n add">+2</span><span class="n del">−0</span></div>
<div class="gitrow"><span class="ltr M">M</span><span class="p">src/loader.py</span><span class="n add">+12</span><span class="n del">−3</span></div>
<div class="gitgrp"><span>Untracked</span><span class="n">1</span></div>
<div class="gitrow"><span class="ltr Q">?</span><span class="p">tests/test_cache.py</span></div></div>'''
    return f'''<aside class="sb sb-r">
<div class="sec"><span class="lbl">Session</span><dl class="kv">
<dt>Agent</dt><dd><b class="{agcls}" style="font-weight:600">{agent}</b></dd>
<dt>Model</dt><dd>gpt-6.1-terra</dd><dt>Effort</dt><dd>medium</dd>
<dt>Status</dt><dd class="row gap4" style="display:flex">{mark(sm)}<span>{st}</span></dd>
<dt>Turns</dt><dd>{turns}</dd><dt>Tools</dt><dd>{tools or "read 2 <span class=\"m\">·</span> bash 1"}</dd>
<dt>Started</dt><dd>{started}</dd><dt>Id</dt><dd>ses_1a1…5a19</dd></dl></div>
<div class="sec"><span class="lbl">Context</span>
<div class="meter" style="margin:4px 0 10px"><i style="width:{pct}%"></i></div>
<dl class="kv"><dt>Used</dt><dd>{used} <span class="m">of 200k · {pct}%</span></dd><dt>Tokens</dt><dd>{toks}</dd><dt>Cost</dt><dd>{cost}</dd></dl></div>
{g}</aside>'''

# ---------- composer ----------
def composer(state="idle", text="", agent=("Build", "ag-build"), effort="medium", pct=41, used="82k", focus=False, narrow=False, live=False, strip=True, jump=None, hint="ctrl+p commands"):
    if text:
        body = f'<div class="in val">{text}<span class="caret"></span></div>'
    else:
        body = '<div class="in"><span class="caret"></span>Type a message…</div>'
    liverow = ""
    if state == "busy":
        liverow = '<div class="live-row">' + mark("run") + '<span class="shim" style="font-weight:600">Working</span><span class="muted">·</span><span>12s</span><span class="muted">·</span><span>↓4.1k</span><span class="sp"></span><span class="muted" style="font-size:12px">steering needs Core (Step 12a)</span></div>'
    btn = '<span class="send stop" title="Stop">@ic(stop,s14)</span>' if state == "busy" else ('<span class="send">@ic(arrow-up)</span>' if text else '<span class="send off">@ic(arrow-up)</span>')
    mt = '' if narrow else f'<span class="mtr"><span class="meter sm"><i style="width:{pct}%"></i></span><span>{used} · {pct}%</span></span>'
    prov = '' if narrow else '<span class="pill"><span class="m">gpt-6.1-terra</span><span class="m" style="opacity:.7">codex</span>@ic(chevron-down)</span>'
    if narrow:
        prov = '<span class="pill"><span class="m">gpt-6.1-terra</span>@ic(chevron-down)</span>'
    eff = '' if narrow else f'<span class="pill">{effort}@ic(chevron-down)</span>'
    stop_hint = '<span class="mtr" style="margin-right:8px"><span class="kbd">esc</span><span>stop</span></span>' if state == "busy" else mt
    j = f'<span class="jump">@ic(arrow-down,s14)<span>{jump}</span><span class="m">· end</span></span>' if jump else ""
    stp = ""
    if strip:
        stp = f'''<div class="strip"><b>nexus-demo</b><span>~/repos/nexus-demo</span><span class="s">@ic(branch,s12) main</span><span>checkout</span><span class="sp"></span><span>{hint if state != "busy" else ""}</span></div>'''
    return f'''<div class="dock col">{j}{liverow}<div class="composer {'fcs' if focus else ''}">{body}
<div class="ctrl"><span class="pill"><span class="dot {agent[1]}"></span><b class="{agent[1]}">{agent[0]}</b>@ic(chevron-down)</span>{prov}{eff}<span class="sp"></span>{stop_hint}{btn}</div></div>{stp}</div>'''

# ---------- window ----------
def win(centre, left=True, right=True, overlay="", hdr=None, cls="", style=""):
    c = "win " + ("" if right else "no-right ") + ("" if left else "no-left ") + cls
    return f'''<div class="{c}" style="{style}">{hdr or header(left=left, right=right)}
{left if isinstance(left, str) else (left_sessions() if left else '')}
<main class="page">{centre}</main>
{right if isinstance(right, str) else (right_info() if right else '')}
{overlay}</div>'''

# ---------- timeline pieces ----------
def user(n, time, text, focus=False, tags=(), folded=False, summary=""):
    tg = "".join(f'<span class="tag">{t}</span>' for t in tags)
    ch = "@ic(chevron-down,chev)" if not folded else "@ic(chevron-right,chev)"
    acts = '<span class="acts"><span class="icon-btn">@ic(copy,s14)</span><span class="icon-btn">@ic(chevron-down,s14)</span></span>'
    if folded:
        return f'<div class="user folded {"fcs" if focus else ""}">{ch}<div class="txt">{text}</div><div class="meta">{summary}{tg}<span>#{n} · {time}</span></div></div>'
    return f'<div class="user {"fcs" if focus else ""}">{ch}<div class="txt">{text}</div><div class="meta">{tg}<span>#{n} · {time}</span>{acts}</div></div>'

def group(summary, time, extra="", cls="", chev="chevron-right", live=False):
    return f'<div class="group {cls}">@ic({chev},chev){summary}{extra}<span class="time">{time}</span></div>'

def member(icon, verb, arg, sum_="", time="", cls="", status=None):
    ico = f'<span class="vi mark {status}" style="width:16px">{mark(status)}</span>' if status else f'@ic({icon},vi)'
    return f'<div class="member {cls}">{ico}<b>{verb}</b><span class="arg">{arg}</span><span class="sum">{sum_}</span><span class="time">{time}</span></div>'

def footer(extra_status='', model="gpt-6.1-terra", tools="3 tools", dur="4.2s", toks="12k in · 2k out · $0.04"):
    return f'<div class="foot"><b class="ag-build">▪ Build</b><span class="dot">·</span><span>{model}</span><span class="dot">·</span><span>medium</span><span class="dot">·</span><span>{tools}</span><span class="dot">·</span><span>{dur}</span><span class="dot">·</span><span>{toks}</span>{extra_status}</div>'

def scroll(inner, bottom=False):
    return f'<div class="page-scroll" style="display:flex;flex-direction:column;justify-content:{"flex-end" if bottom else "flex-start"}"><div class="col tl">{inner}</div></div>'
