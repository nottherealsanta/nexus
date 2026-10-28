from lib import *
from p1 import turn, CTX, READ_G, EDIT_G, THOUGHT
import html as H

# ================= d03 sessions =================
def d03():
    def col(cap, inner, w=264):
        return f'<div><span class="cap">{cap}</span><div class="stage side" style="width:{w}px">{inner}</div></div>'
    full = left_sessions(open_i=0, extra_states={1: ""}).replace('class="sb sb-l"', 'class="sb sb-l" style="height:600px;background:transparent;padding:0"')
    states = ""
    labels = [("Default", 4, ""), ("Open (selection band, bold title)", 0, "open"), ("Open + focus (2 px bar)", 0, "open fcs"), ("Hover (⋯ replaces the time)", 3, "hover"),
              ("Waiting", 1, ""), ("Running (title shimmers)", 2, ""), ("Error", 3, "")]
    for cap, i, st in labels:
        states += f'<div class="t-meta" style="padding:10px 8px 2px;color:var(--text-muted)">{cap}</div>' + srow(i, state=st)
    menu = '''<div class="pop" style="width:216px;position:absolute;left:236px;top:98px;z-index:5;padding:4px">
<div class="pop-row">@ic(pencil,s14)<span>Rename</span><span class="kbd">r</span></div>
<div class="pop-row">@ic(archive,s14)<span>Archive</span><span class="kbd">a</span></div>
<div class="pop-row">@ic(copy,s14)<span>Copy id</span><span class="kbd">y</span></div>
<div class="pop-row">@ic(search,s14)<span>Open inspector</span><span class="kbd">i</span></div>
<div class="pop-sep"></div>
<div class="pop-row danger">@ic(trash,s14)<span>Delete</span><span class="kbd">d</span></div></div>'''
    wt = f'''<div class="grp" style="padding-top:12px"><span class="lbl">nexus-demo</span></div>
{srow(0, state="open")}{srow(3)}
<div class="subgrp">@ic(branch,s12)<b style="font-weight:600">feat/cache</b><span class="m trunc">~/…/nexus-demo-cache</span></div>
{srow(2, state="hover")}{srow(4)}
<div class="newrow">@ic(plus,s14)<span>New session</span></div>
<div class="lbl" style="padding:20px 8px 6px;display:block">Row menu</div>{menu.replace("position:absolute;left:236px;top:98px;z-index:5;","position:relative;left:44px;")}'''
    rail = ""
    for i in range(5):
        mk = SESS[i][1]
        rail += f'<div style="height:48px;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;{"background:var(--interactive-selection);" if i==0 else ""}"><span class="ag-{SESS[i][2]}" style="font-size:12px;font-weight:600;width:28px;height:28px;border-radius:14px;background:var(--surface-panel);display:flex;align-items:center;justify-content:center">{SESS[i][2][0].upper()}</span>{mark(mk) if mk!="none" else "<span style=height:16px></span>"}</div>'
    rail = f'<div style="width:56px;height:600px;display:flex;flex-direction:column;gap:4px">{rail}<span class="sp"></span><span class="icon-btn" style="align-self:center">@ic(settings)</span></div>'
    body = f'''<div class="sheet-pad"><h1>D03 · Sessions sidebar</h1><div style="height:24px"></div>
<div style="display:flex;gap:32px;align-items:flex-start">
{col("Sidebar (264 px, on surface.sidebar)", full)}
{col("Row states", states)}
{col("Worktree sub-groups and row menu", wt, 264)}
{col("Rail mode", rail, 72)}
</div></div>'''
    page("d03-sessions", "D03 sessions", "1440,900", body, sheet=True)

# ================= d06 tools =================
def d06():
    summ = '<span>Read 2 files, ran 1 command</span>'
    shell_detail = '''<div class="detail"><div class="dh">@ic(terminal,s14)<b>Shell</b><span class="mono" style="font-size:12px">pytest tests/test_loader.py -q</span><span class="raw"><span>raw</span><span class="switch"></span></span></div>
<pre class="mono" style="font-size:12px;line-height:18px"><span class="muted">$ </span>pytest tests/test_loader.py -q
<span class="muted">cwd ~/repos/nexus-demo · exit 0 · 1.2s</span>
...................                  [ 78%]
.....                                [100%]
<span class="add">24 passed</span> in 0.84s</pre>
<div class="df"><span>↑ 214 earlier lines</span><span class="sp"></span><span class="accent" style="font-weight:500">Show all</span></div></div>'''
    expanded = (group('<span>Read 2 files, ran 1 command</span>', '1.4s', chev="chevron-down") +
        '<div class="tree">' + member("read", "Read", "src/nexus/loader.py", "1-120", "0.1s") + member("read", "Read", "tests/test_loader.py", "1-60", "0.1s") +
        member("terminal", "Shell", "pytest tests/test_loader.py -q", "24 passed", "1.2s") + '</div>' + shell_detail)
    thinking = ('<div><div class="group" style="color:var(--text-muted)">@ic(chevron-down,chev)<b class="shim" style="font-weight:600">Thinking…</b><span class="m tnum">2.4s</span></div>'
        '<div class="rs live"><div class="more">… 14 earlier lines</div>I need to decide between memoising on the path alone and on (path, mtime). The path alone is stale after edits, so key it on both. The test should count parses using a monkeypatched parser and assert it ran once across three loads.</div></div>')
    live = ('<div><div class="group live">@ic(chevron-down,chev)<b class="shim">Working</b><span class="time">12s</span></div><div class="tree">'
        '<div class="member earlier">… 3 earlier</div>'
        + member("read", "Read", "src/nexus/loader.py", "1-120", "0.1s") + member("search", "Search", "“parse_manifest” in src", "4 matches", "0.2s")
        + member("", "Shell", "pytest tests/test_loader.py -q", "", "3.4s", cls="run", status="run") + '</div></div>')
    rs_open = ('<div><div class="thought">@ic(chevron-down,chev)<b>Thought</b><span class="d">for 2.1s</span></div>'
        '<div class="rs">The manifest is parsed on each call to Loader.load. A memo keyed by path and mtime keeps edits visible while removing the repeat reads.</div></div>')
    rs_enc = '<div class="thought">@ic(chevron-right,chev)<b>Thought</b><span class="d">for 2.1s</span><span class="d">·</span><span class="pv" style="font-style:normal">encrypted, replayed to the provider</span></div>'
    c = scroll(
        user(2, "3:44 PM", "Go ahead, but keep the change small.", folded=True, summary="") +
        '<div style="display:flex;flex-direction:column;gap:2px">' + THOUGHT + rs_enc + rs_open + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:2px">' + READ_G + EDIT_G + '</div>' +
        expanded + thinking + live) + composer(state="busy")
    page("d06-tools", "D06 tools", "1440,1100", win(c, hdr=header(run=("run", "working 12s")), right=right_info(status=("run", "working"))))

# ================= d10 new session =================
def d10():
    keys = '<span class="row gap4"><span class="kbd">Tab</span> agent</span><span class="row gap4"><span class="kbd">ctrl+m</span> model</span><span class="row gap4"><span class="kbd">ctrl+e</span> effort</span><span class="row gap4"><span class="kbd">ctrl+d</span> directory</span>'
    tools = ["read", "bash", "edit", "write", "patch", "search", "ask", "session", "code"]
    tg = "".join(f'<div class="tchip {"off" if t=="code" else ""}"><span>{t}</span><span class="switch {"" if t=="code" else "on"}"></span></div>' for t in tools)
    diamond = '<span class="mark mut" style="color:var(--text-muted)"><svg viewBox="0 0 12 12"><path d="M6 .8 11.2 6 6 11.2.8 6z" fill="currentColor"/></svg></span>'
    c = f'''<div class="page-scroll"><div class="col">
<div class="ns-h"><h1>New session in nexus-demo</h1>
<div class="l"><b class="ag-build" style="font-weight:600">Build</b> · gpt-6.1-terra <span class="muted">codex</span> · medium · ~/repos/nexus-demo @ic(branch,s12) main</div>
<div class="k">{keys}</div></div>
<div class="sec-card"><div class="ch">{diamond}<b>System prompt</b><span class="tk">2,140 tok</span></div><div class="meta" style="padding-left:24px">build · 2 sections</div>
<div class="pv" style="padding-left:24px">You are Nexus, a provider-agnostic coding agent working in the user’s workspace. You read, search and edit files…</div></div>
<div class="sec-card"><div class="ch">{diamond}<b>AGENTS.md</b><span class="tk">3,880 tok</span></div><div class="meta" style="padding-left:24px">~/repos/nexus · 1 file</div>
<div class="pv" style="padding-left:24px">This branch (nexus10, Nexus 0.10) rebuilds Nexus from the provider wire up. Read PLAN.md first…</div></div>
<div class="sec-card"><div class="ch">{diamond}<b>Tools</b><span class="tk">4,780 tok</span></div><div class="meta" style="padding-left:24px">9 tools · 8 on</div><div class="tgrid">{tg}</div></div>
<div class="total"><b>Context total</b><span>17,580 tokens</span><span class="meter"><i style="width:8.8%"></i></span><span>8.8% of the window</span></div>
</div></div>''' + composer(focus=True, pct=9, used="18k")
    page("d10-new-session", "D10 new session", "1440,900",
         win(c, hdr=header(crumbs=("nexus-demo", "New session"), run=None, attn=0), left=left_sessions(new_open=True),
             right=right_info(status=("none", "draft"), turns="0", started="—", pct=9, tools="—", used="17.6k", toks="—", cost="—")))

# ================= d11 composer =================
def d11():
    def st(cap, inner, w=None):
        return f'<div><span class="cap">{cap}</span><div class="stage" style="{"width:%dpx;"%w if w else ""}">{inner}</div></div>'
    comp_rows = [("/model", "Switch model", "ctrl+m"), ("/agent", "Switch agent", "tab"), ("/effort", "Set reasoning effort", "ctrl+e"), ("/new", "New session", "ctrl+n"), ("/compact", "Hand off to a fresh context", ""), ("/continue", "Continue after interruption", "enter")]
    pr = "".join(f'<div class="pop-row {"sel" if i==0 else ""}"><b style="font-family:Monaspace Argon,monospace;font-weight:500;font-size:12.5px;color:var(--text-primary)">{a}</b><span class="m" style="margin-left:12px;color:var(--text-muted);font-size:12px">{b}</span>{"<span class=kbd>%s</span>"%c if c else ""}</div>' for i, (a, b, c) in enumerate(comp_rows))
    pop = f'<div class="pop" style="margin-bottom:8px">{pr}<div class="pop-foot"><span>↑↓ move</span><span>tab accept</span><span>esc close</span><span class="r">6 of 12</span></div></div>'
    body = f'''<div class="sheet-pad"><h1>D11 · Composer</h1><div style="height:24px"></div>
<div class="grid" style="grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:32px 32px">
{st("Idle", composer())}
{st("Focused, with a draft", composer(text="Can you also add a test that covers the cache invalidation when the file’s mtime changes?", focus=True))}
{st("Busy (live row above the card, esc stop)", composer(state="busy"))}
{st("Completion (anchored to the card’s top edge)", '<div class="col" style="margin:0 auto">' + pop + '</div>' + composer(text="/", focus=True, strip=False))}
{st("Narrow: sheds effort, provider, meter", '<div style="width:420px;margin:0 auto">' + composer(narrow=True, text="Run the tests again", focus=False) + '</div>')}
{st("Jump to latest (never overlaps content)", composer(jump="3 new"))}
</div></div>'''
    page("d11-composer", "D11 composer", "1440,1160", body, sheet=True)

# ================= d13 overlays =================
def d13():
    base = scroll(turn(False)) + composer()
    def drow(ic, label, key="", cls="", extra=""):
        return f'<div class="drow {cls}"><span class="ico">@ic({ic},s14)</span><span>{label}</span>{extra}<span class="v">{"<span class=kbd>%s</span>" % key if key else ""}</span></div>'
    pal = f'''<div class="dialog" style="width:640px;top:88px">
<div class="dlg-h"><span class="t-title">Commands</span><span class="kbd">esc</span></div>
<div class="dlg-search">@ic(search,s14)<span>Type a command…</span></div>
<div class="dlg-body" style="height:440px">
<div class="dlg-sec"><span class="lbl">Session</span></div>
{drow("plus","New session","ctrl+n")}{drow("pencil","Rename session","f2")}{drow("archive","Archive session","")}{drow("branch","Open in worktree","")}
<div class="dlg-sec"><span class="lbl">Model</span></div>
{drow("command","Switch model…","ctrl+m","sel")}{drow("command","Change reasoning effort…","ctrl+e")}{drow("command","Switch agent…","tab")}
<div class="dlg-sec"><span class="lbl">View</span></div>
{drow("panel-left","Toggle left sidebar","ctrl+b")}{drow("panel-right","Toggle right sidebar","ctrl+shift+b")}{drow("stop","Stop turn","esc","dis",'<span class="t-meta">needs a running turn</span>')}
</div><div class="dlg-f"><span>↑↓ move</span><span>enter run</span><span>esc close</span><span class="r">12 of 99</span></div></div>'''
    page("d13-palette", "D13 palette", "1440,900", win(base, overlay='<div class="scrim"></div>' + pal))
    def setrow(label, desc, ctrl, sel=False):
        return f'<div class="drow {"sel" if sel else ""}" style="height:48px;align-items:center"><div style="display:flex;flex-direction:column;gap:0"><span style="font-size:13px;line-height:20px">{label}</span><span class="t-meta" style="line-height:16px">{desc}</span></div><span class="v" style="margin-left:auto">{ctrl}</span></div>'
    sel = lambda v: f'<span class="select">{v}@ic(chevron-down)</span>'
    sw = lambda on: f'<span class="switch {"on" if on else ""}"></span>'
    st = f'''<div class="dialog" style="width:704px;top:44px">
<div class="dlg-h"><span class="t-title">Settings</span><span class="kbd">esc</span></div>
<div class="dlg-body" style="height:700px;border-top:1px solid var(--border-default);padding:8px 16px">
<div class="dlg-sec"><span class="lbl">Appearance</span></div>
{setrow("Theme","Follows the system by default",sel("Dark"))}{setrow("Text size","Scales all type levels together",sel("100%"))}{setrow("Reduce motion","Static spinner, no shimmer or finish flash",sw(False))}
<div class="dlg-sec"><span class="lbl">Agents</span></div>
{setrow("Default agent","Used for new sessions",sel('<b class="ag-build" style="font-weight:600">Build</b>'))}{setrow("Default model","provider/model reference",sel("codex/gpt-6.1-terra"),True)}{setrow("Reasoning effort","Applies to models that support it",sel("medium"))}
<div class="dlg-sec"><span class="lbl">Approvals</span></div>
{setrow("Policy","Ask before running commands that change the workspace",sel("Ask"))}{setrow("Remember for session","Allow-for-session applies to this tool only",sw(True))}
<div class="dlg-sec"><span class="lbl">Notifications</span></div>
{setrow("Desktop notifications","When a session is waiting for you",sw(True))}{setrow("Sound","Play a chime on turn end",sw(False))}
</div><div class="dlg-f"><span>↑↓ move</span><span>enter change</span><span>esc close</span><span class="r">Core 0.10.2</span></div></div>'''
    page("d13-settings", "D13 settings", "1440,900", win(base, overlay='<div class="scrim"></div>' + st))

# ================= d14 waiting =================
def d14():
    live = ('<div><div class="group live">@ic(chevron-down,chev)<b>Working</b><span class="time">8s</span></div><div class="tree">'
        + member("read", "Read", "src/nexus/loader.py", "1-120", "0.1s") + member("", "Shell", "pytest tests/test_loader.py -q", "waiting for approval", "", cls="run", status="wait") + '</div></div>')
    wcard = '''<div class="waiting col" style="width:760px">
<div class="wh"><span class="mark wait">''' + mark("wait").split(">", 1)[1].rsplit("</span>", 1)[0] + '''</span><b>Allow bash?</b><span class="r">1 of 2 · <span class="kbd">tab</span> next</span></div>
<div class="sub">waiting on you · 0:42</div>
<dl class="params"><dt>command</dt><dd>pytest tests/test_loader.py -q</dd><dt>cwd</dt><dd>~/repos/nexus-demo</dd><dt>policy</dt><dd>ask · workspace-write</dd></dl>
<div class="choices"><span class="btn primary">Allow once<span class="kbd">y</span></span><span class="btn">Allow for session<span class="kbd">a</span></span><span class="btn">Deny<span class="kbd">n</span></span><span class="btn">Deny with a note<span class="kbd">m</span></span></div></div>'''
    c = scroll(user(1, "3:42 PM", "The loader re-reads the manifest on every call. Can you cache it and add a test?", folded=True) + THOUGHT + '<div class="prose"><p>Caching on the file’s mtime is enough. Let me run the existing tests first.</p></div>' + live) + \
        '<div class="dock col" style="padding-bottom:0">' + wcard + '</div>' + composer(strip=True).replace('class="dock col"', 'class="dock col" style="padding-top:0"', 1)
    page("d14-waiting", "D14 waiting", "1440,900", win(c, hdr=header(run=("wait", "waiting for you")), left=left_sessions(marks={0: "wait"}), right=right_info(status=("wait", "waiting for you"))))

# ================= d15 alerts =================
def d15():
    banner = '<div class="banner warn"><span class="lv">@ic(alert,s14)Rate limited</span><span>codex is throttling requests · retrying in 8s</span><span class="acts"><span class="accent" style="font-weight:500">Retry</span><span class="muted" style="display:inline-flex">@ic(x,s14)</span></span></div>'
    toasts = '''<div class="toasts">
<div class="toast ok" style="--w:70%"><span class="gl">@ic(check,s14)</span><div><div class="ti">Session archived</div><div class="tx">Spike: terminal harness</div><div class="acts"><span class="accent">Undo</span></div></div><span class="icon-btn" style="width:24px;height:24px;color:var(--text-muted)">@ic(x,s14)</span><span class="bar"></span></div>
<div class="toast err" style="--w:45%"><span class="gl">@ic(alert,s14)</span><div><div class="ti">Tool failed</div><div class="tx">bash exited with code 1 in tests/test_sync.py</div></div><span class="icon-btn" style="width:24px;height:24px;color:var(--text-muted)">@ic(x,s14)</span><span class="bar"></span></div>
<div class="toast info" style="--w:80%"><span class="gl">@ic(info,s14)</span><div><div class="ti">Needs your approval</div><div class="tx">Review the CJK width handling · bash</div><div class="acts"><span class="accent">Open</span></div></div><span class="icon-btn" style="width:24px;height:24px;color:var(--text-muted)">@ic(x,s14)</span><span class="bar"></span></div>
<span class="t-meta" style="padding:0 10px;height:24px;line-height:24px;border-radius:12px;background:var(--surface-raised);box-shadow:0 0 0 1px var(--border-default)">+2 more</span></div>'''
    c = banner + scroll(turn(False)) + composer(jump="3 new")
    c = c.replace('<div class="banner', toasts.replace('class="toasts"', 'class="toasts" style="top:48px"') + '<div class="banner', 1)
    page("d15-alerts", "D15 alerts", "1440,900", win(c, hdr=header(run=("ok", "idle"), attn=3)))

# ================= d16 files =================
def d16():
    def tr(label, depth=0, kind="file", ltr="", sel=False, ign=False, open_=False):
        ind = '<span class="indent"></span>' * depth
        if kind == "dir":
            ic = f'@ic({"chevron-down" if open_ else "chevron-right"},chev)'
        else:
            ic = '<span style="width:16px;flex:none"></span>'
        cls = "treerow " + ("dir " if kind == "dir" else "") + ("sel " if sel else "") + ("ign" if ign else "")
        return f'<div class="{cls}">{ind}{ic}<span class="trunc">{label}</span>{gl(ltr)}</div>'
    def gl(l):
        if not l: return ""
        col = {"M": "warning", "A": "success", "?": "success", "D": "error"}[l]
        return f'<span class="ltr" style="color:var(--status-{col})">{l}</span>'
    tree = (tr("src", 0, "dir", open_=True) + tr("nexus", 1, "dir", open_=True) + tr("__init__.py", 2) + tr("cache.py", 2, ltr="A") + tr("loader.py", 2, ltr="M", sel=True) + tr("manifest.py", 2) +
            tr("tests", 0, "dir", open_=True) + tr("test_cache.py", 1, ltr="?") + tr("test_loader.py", 1, ltr="M") +
            tr("docs", 0, "dir") + tr(".venv", 0, "dir", ign=True) + tr("README.md", 0, ltr="M") + tr("pyproject.toml", 0) + tr(".gitignore", 0, ign=True))
    left = f'''<aside class="sb sb-l"><div class="grp" style="padding-top:12px"><span class="lbl">nexus-demo</span><span class="sp"></span><span class="t-meta" style="display:flex;gap:4px;align-items:center">@ic(branch,s12) main</span></div>
<div class="scrollarea">{tree}</div><div class="sb-foot"><div class="menu-row">@ic(settings,s14)<span>Settings</span><span class="kbd">ctrl+,</span></div></div></aside>'''
    src = '''from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from nexus.errors import ManifestError

MANIFEST_NAME = "nexus.json"


@lru_cache(maxsize=8)
def _parse_manifest(path: str, mtime_ns: int) -> dict:
    """Parse once per (path, mtime); an edit changes mtime and busts the cache."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


class Loader:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self) -> dict:
        path = self.root / MANIFEST_NAME
        return _parse_manifest(str(path), path.stat().st_mtime_ns)

    def names(self) -> list[str]:
        manifest = self.load()
        return sorted(manifest.get("plugins", {}))

    def reload(self) -> dict:
        _parse_manifest.cache_clear()
        return self.load()'''
    lines = src.split("\n"); out = ""; n = 0
    for i, l in enumerate(lines, 1):
        e = H.escape(l)
        import re as _re
        def sub(m):
            global_n[0] += 1
            return f'<span class="hl">{m.group(0)}</span>'
        global_n = [0]
        cur = ""
        if "manifest" in l.lower():
            e = _re.sub(r"(?i)manifest", sub, e)
            n += 1
            if n == 3: cur = " cur"
        out += f'<div class="ln{cur}"><i>{i}</i><span>{e or " "}</span></div>'
    c = f'''<div class="fhead"><span>~/repos/nexus-demo/src/nexus/<b>loader.py</b></span><span class="sp"></span><span class="muted tnum" style="font-size:12px">lines 1-{len(lines)} of 300</span></div>
<div class="page-scroll code" style="padding:4px 0 0">{out}</div>
<div class="searchbar"><span class="accent b">/</span><span class="q">manifest</span><span class="muted tnum">3 of 12</span><span class="sp"></span><span class="muted" style="font-size:12px">n next · N prev · esc end</span></div>'''
    page("d16-files", "D16 files", "1440,900", win(c, hdr=header(tab="files", crumbs=("nexus-demo", "src/nexus/loader.py"), run=("ok", "idle")), left=left))

d03(); d06(); d10(); d11(); d13(); d14(); d15(); d16()
