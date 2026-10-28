from lib import *
from p1 import CTX, READ_G, EDIT_G, THOUGHT
import html as H

# ================= d01 tokens =================
def d01():
    def sw(var, label=None, fg="var(--text-primary)"):
        return f'<div class="sw" data-var="{var}" style="background:var({var});color:{fg}"><span>{label or var}</span></div>'
    surf = "".join(sw(v, n) for v, n in [("--surface-canvas", "canvas"), ("--surface-sidebar", "sidebar"), ("--surface-panel", "panel"), ("--surface-raised", "raised")])
    txt = f'''<div class="spec"><span class="k">text.primary</span><span class="t-body">The loader re-reads the manifest on every call</span></div>
<div class="spec"><span class="k">text.secondary</span><span class="t-body sec">Read 2 files, ran 1 command</span></div>
<div class="spec"><span class="k">text.muted</span><span class="t-body muted">#1 · 3:42 PM · key hints and meta</span></div>'''
    bor = f'''<div class="grid" style="grid-template-columns:repeat(3,1fr);gap:12px">
<div class="sw" style="background:var(--surface-panel);box-shadow:inset 0 0 0 1px var(--border-default)">border.default</div>
<div class="sw" style="background:var(--surface-panel);box-shadow:inset 0 0 0 1px var(--border-strong)">border.strong</div>
<div class="sw" style="background:var(--surface-panel);box-shadow:0 0 0 1px var(--surface-panel),0 0 0 3px var(--border-focus)">border.focus</div></div>'''
    inter = "".join(sw(v, n, c) for v, n, c in [("--interactive-accent", "accent", "var(--interactive-on-accent)"), ("--interactive-selection", "selection", "var(--text-primary)"), ("--interactive-hover", "hover", "var(--text-primary)"), ("--interactive-focus", "focus", "var(--interactive-on-accent)")])
    stat = "".join(f'<div class="sw" data-var="{v}" style="background:var(--surface-panel);color:var(--{v[2:]})"><span>{ic} {n}</span></div>' for v, n, ic in [("--status-success", "success", "✓"), ("--status-warning", "warning", "▲"), ("--status-error", "error", "✕"), ("--status-info", "info", "●")])
    diff = '''<div class="sw" style="background:var(--diff-add-bg);color:var(--diff-add)">diff.add / add_bg</div><div class="sw" style="background:var(--diff-del-bg);color:var(--diff-del)">diff.del / del_bg</div>'''
    ag = "".join(f'<div class="sw" style="background:var(--surface-panel);color:var(--agent-{n})"><span>▪ {n}</span></div>' for n in ["blue", "amber", "violet", "green", "teal", "sky", "rose", "gold"])
    typ = '''<div class="spec"><span class="k">title 15/20 · 600</span><span class="t-title">Refactor the loader</span></div>
<div class="spec"><span class="k">body 14/22</span><span class="t-body">Ask the agent to cache the manifest and add a test.</span></div>
<div class="spec"><span class="k">prose 15/24</span><span class="t-prose">parse_manifest is called from Loader.load on every request; I’ll memoize it on the file’s mtime.</span></div>
<div class="spec"><span class="k">secondary 13/20</span><span class="t-sec">Read 2 files, ran 1 command · Edited 1 file</span></div>
<div class="spec"><span class="k">meta 12/16 tnum</span><span class="t-meta">#2 · 3:44 PM · 12k in · 2k out · $0.04 · 4.2s</span></div>
<div class="spec"><span class="k">code 13/20 Monaspace</span><span class="mono">def load(self) -&gt; dict:  # 0123456789</span></div>
<div class="spec"><span class="k">label 11 caps .06em</span><span class="lbl">Session · Context · Git</span></div>'''
    sp = "".join(f'<div class="row gap12" style="margin:6px 0"><span class="t-meta" style="width:32px">{n}</span><span class="bar4" style="width:{n}px"></span></div>' for n in [4, 8, 12, 16, 24, 32])
    rad = '''<div class="row gap12"><div style="width:96px;height:56px;background:var(--surface-panel);border-radius:6px;box-shadow:0 0 0 1px var(--border-default);padding:8px" class="t-meta">6 · controls, rows</div>
<div style="width:128px;height:56px;background:var(--surface-raised);border-radius:10px;box-shadow:0 0 0 1px var(--border-default);padding:8px" class="t-meta">10 · cards, dialogs</div>
<div style="width:128px;height:56px;background:var(--surface-panel);border-radius:12px;box-shadow:0 0 0 1px var(--border-default);padding:8px" class="t-meta">12 · composer</div></div>'''
    scrim = '''<div style="position:relative;height:88px;border-radius:10px;overflow:hidden;box-shadow:0 0 0 1px var(--border-default)"><div style="padding:12px 16px" class="t-body">Session text under a scrim<br><span class="muted t-sec">rgb(0 0 0 / .45) dark · .18 light</span></div><div class="scrim" style="z-index:1"></div>
<div class="pop" style="position:absolute;z-index:2;left:260px;top:20px;width:200px;height:48px;display:flex;align-items:center;padding:0 16px;font-size:13px">Raised dialog</div></div>'''
    def blk(t, inner): return f'<div><div class="lbl" style="margin-bottom:12px">{t}</div>{inner}</div>'
    body = f'''<div class="sheet-pad"><h1>D01 · Tokens and surfaces</h1><p class="t-sec" style="margin:4px 0 24px">Chrome (header band, both sidebars) on surface.sidebar. Page on canvas. Panel holds the composer and code. Raised holds the user band, dialogs and toasts.</p>
<div class="grid" style="grid-template-columns:1fr 1fr;gap:32px 56px;align-items:start">
{blk("Surfaces", '<div class="grid" style="grid-template-columns:repeat(4,1fr);gap:12px">' + surf + '</div>')}
{blk("Text", txt)}
{blk("Borders", bor)}
{blk("Interactive", '<div class="grid" style="grid-template-columns:repeat(4,1fr);gap:12px">' + inter + '</div>')}
{blk("Status", '<div class="grid" style="grid-template-columns:repeat(4,1fr);gap:12px">' + stat + '</div>')}
{blk("Diff", '<div class="grid" style="grid-template-columns:1fr 1fr;gap:12px">' + diff + '</div>')}
{blk("Agent identity", '<div class="grid" style="grid-template-columns:repeat(4,1fr);gap:12px">' + ag + '</div>')}
{blk("Scrim and elevation", scrim)}
{blk("Type scale", typ)}
<div style="display:flex;flex-direction:column;gap:32px">{blk("Spacing (4 px base)", sp)}{blk("Radius", rad)}
{blk("Focus ring and accent", '<div class="row gap12"><span class="btn focus">Focused control</span><span class="btn primary">Primary</span><span class="accent b">Waiting mark ◆</span></div>')}</div>
</div></div>
<script>document.querySelectorAll('.sw[data-var]').forEach(function(e){{var v=getComputedStyle(document.documentElement).getPropertyValue(e.dataset.var).trim();var s=document.createElement('span');s.style.cssText='margin-left:auto;opacity:.8';s.textContent=v;e.appendChild(s);e.style.justifyContent='space-between'}});</script>'''
    page("d01-tokens", "D01 tokens", "1440,1160", body, sheet=True)

# ================= d08 markdown =================
def d08():
    code = '''@lru_cache(maxsize=8)
def _parse_manifest(path: str, mtime_ns: int) -> dict:
    return json.loads(Path(path).read_text())'''
    fence = lambda cls="": f'<div class="fence {cls}"><div class="fh"><span>python</span><span class="btn sm ghost">@ic(copy,s12)Copy</span></div><pre>{H.escape(code)}</pre></div>'
    md = f'''<div class="md">
<h1>Caching the manifest</h1>
<p>The loader re-reads <code>nexus.json</code> on every call. We memoize <code>_parse_manifest</code> on the file’s <strong>mtime</strong>, so edits still show up; see <a href="#">src/nexus/loader.py:12</a> and the <a href="#">design notes</a>.</p>
<h2>What changed</h2>
<ul><li>Added <code>@lru_cache</code> keyed by path and mtime<ul><li>cache size is 8 entries</li><li>cleared by <code>Loader.reload()</code></li></ul></li><li>Moved the parse into a module-level function</li></ul>
<h3>Checklist</h3>
<ul class="task"><li><span class="cb on">@ic(check)</span>Regression test counts parses</li><li><span class="cb on">@ic(check)</span>Both tests pass</li><li><span class="cb"></span>Update the changelog</li></ul>
{fence()}
<table><tr><th>File</th><th>Change</th><th style="text-align:right">Lines</th></tr>
<tr><td><code>src/nexus/loader.py</code></td><td>memoize parse</td><td style="text-align:right" class="tnum">+12 −3</td></tr>
<tr><td><code>tests/test_loader.py</code></td><td>count parses across loads</td><td style="text-align:right" class="tnum">+18</td></tr></table>
<blockquote>Keying only on the path would serve stale data after an edit, so mtime is part of the key.</blockquote>
</div>'''
    side = f'''<div style="display:flex;flex-direction:column;gap:32px;width:520px">
<div><span class="cap">Inline code: primary on panel, never accent</span><div class="md"><p><code>parse_manifest</code> is called from <code>Loader.load</code> on every request.</p></div></div>
<div><span class="cap">Fence states: default, hover (Copy appears), focused (bar in the gutter)</span><div class="md">{fence()}{fence("hov")}{fence("foc hov")}</div></div>
<div><span class="cap">Mermaid: the diagram, a note, then the source fence</span>
<div class="stage" style="background:var(--surface-panel);display:flex;justify-content:center;padding:20px"><svg width="300" height="72" viewBox="0 0 300 72" fill="none" stroke="var(--text-secondary)" stroke-width="1.5"><rect x="4" y="22" width="76" height="28" rx="6"/><rect x="112" y="22" width="76" height="28" rx="6"/><rect x="220" y="22" width="76" height="28" rx="6"/><path d="M80 36h32M188 36h32"/><g stroke="none" fill="var(--text-primary)" font-size="12" font-family="system-ui" text-anchor="middle"><text x="42" y="40">load</text><text x="150" y="40">cache</text><text x="258" y="40">parse</text></g></svg></div>
<div class="mermaid">@ic(info,s12)mermaid · source below</div></div></div>'''
    body = f'<div class="sheet-pad"><h1>D08 · Markdown and code fences</h1><div style="height:8px"></div><div style="display:flex;gap:56px;align-items:flex-start"><div style="width:760px"><span class="cap">Prose 15/24 in the 760 px reading column</span>{md}</div>{side}</div></div>'
    page("d08-markdown", "D08 markdown", "1440,900", body, sheet=True)

# ================= d09 diff =================
def d09():
    rows = [
        ("c", 10, 10, "from pathlib import Path"), ("c", 11, 11, ""), ("c", 12, 12, "from nexus.errors import ManifestError"),
        ("a", None, 13, ""), ("a", None, 14, "MANIFEST_NAME = \"nexus.json\""), ("a", None, 15, ""), ("a", None, 16, ""),
        ("a", None, 17, "@lru_cache(maxsize=8)"),
        ("m", 13, 18, "def _parse_manifest(path):", "def _parse_manifest(path: str, mtime_ns: int) -> dict:"),
        ("c", 14, 19, "    return json.loads(Path(path).read_text())"),
        ("c", 15, 20, ""), ("c", 16, 21, "class Loader:"),
    ]
    rows2 = [
        ("c", 40, 46, "    def load(self) -> dict:"),
        ("d", 41, 47, '        text = (self.root / "nexus.json").read_text()'),
        ("m", 42, 47, "        return json.loads(text)", "        path = self.root / MANIFEST_NAME"),
        ("a", None, 48, "        return _parse_manifest(str(path), path.stat().st_mtime_ns)"),
        ("c", 43, 49, ""),
    ]
    def split(rs):
        o = ""
        for r in rs:
            k = r[0]
            if k == "c":
                o += f'<div class="dl"><span class="n">{r[1]}</span><span class="c">{H.escape(r[3]) or " "}</span><span class="rule"></span><span class="n">{r[2]}</span><span class="c">{H.escape(r[3]) or " "}</span></div>'
            elif k == "m":
                o += f'<div class="dl"><span class="n nr">{r[1]}</span><span class="r">{H.escape(r[3])}</span><span class="rule"></span><span class="n na">{r[2]}</span><span class="a">{H.escape(r[4])}</span></div>'
            elif k == "d":
                o += f'<div class="dl"><span class="n nr">{r[1]}</span><span class="r">{H.escape(r[3])}</span><span class="rule"></span><span class="n hatch"></span><span class="hatch"></span></div>'
            else:
                o += f'<div class="dl"><span class="n hatch"></span><span class="hatch"></span><span class="rule"></span><span class="n na">{r[2]}</span><span class="a">{H.escape(r[3]) or " "}</span></div>'
        return o
    def uni(rs):
        o = ""
        for r in rs:
            k = r[0]
            if k == "c": o += f'<div class="dl"><span class="n">{r[1]}</span><span class="n">{r[2]}</span><span class="sign c"> </span><span class="c">{H.escape(r[3]) or " "}</span></div>'
            elif k == "m":
                o += f'<div class="dl"><span class="n nr">{r[1]}</span><span class="n nr"></span><span class="sign r">−</span><span class="r">{H.escape(r[3])}</span></div>'
                o += f'<div class="dl"><span class="n na"></span><span class="n na">{r[2]}</span><span class="sign a">+</span><span class="a">{H.escape(r[4])}</span></div>'
            elif k == "d": o += f'<div class="dl"><span class="n nr">{r[1]}</span><span class="n nr"></span><span class="sign r">−</span><span class="r">{H.escape(r[3])}</span></div>'
            else: o += f'<div class="dl"><span class="n na"></span><span class="n na">{r[2]}</span><span class="sign a">+</span><span class="a">{H.escape(r[3]) or " "}</span></div>'
        return o
    hdr = '<div class="dfh"><span class="t-meta" style="color:var(--text-muted)">@ic(file,s14)</span><span style="color:var(--text-primary)">src/nexus/loader.py</span><span class="sp"></span><span class="cnt add">+12</span><span class="cnt del">−3</span></div>'
    hunk = lambda t: f'<div class="hunk"><span>⋯</span><span class="mono" style="font-size:12px">{t}</span></div>'
    sp = f'<div class="diff split">{hdr}{hunk("@@ -10,7 +10,12 @@")}{split(rows)}{hunk("@@ -40,6 +46,6 @@ class Loader")}{split(rows2)}</div>'
    un = f'<div class="diff uni">{hdr}{hunk("@@ -10,7 +10,12 @@")}{uni(rows)}{hunk("@@ -40,6 +46,6 @@ class Loader")}{uni(rows2)}</div>'
    body = f'''<div class="sheet-pad"><h1>D09 · Diffs</h1><div style="height:16px"></div>
<span class="cap">Split view at 1100 px window width and above (Monaspace 13/20, 48 px number gutter, hatch fills the empty side)</span>{sp}
<div style="height:32px"></div><span class="cap">Unified view below 1100 px (new line numbers, sign, tinted text)</span><div style="width:760px">{un}</div></div>'''
    page("d09-diff", "D09 diff", "1440,1100", body, sheet=True)

# ================= d17 phone =================
def d17():
    body = f'''<div class="phone">
<div class="hdr2"><span class="icon-btn">@ic(menu)</span><div class="crumb grow" style="justify-content:flex-start"><span class="trunc">…</span><span class="sep">›</span><b class="trunc">Refactor the loader</b></div><span class="attn" style="margin-right:4px"><i></i>1</span><span class="icon-btn">@ic(panel-right)</span></div>
<div class="pad">
{CTX.replace('class="ctxfold"','class="ctxfold" style="margin:0 -4px"')}
<div class="user"><span class="chev" style="margin-top:4px">@ic(chevron-down,chev)</span><div class="txt" style="font-size:15px;line-height:24px">The loader re-reads the manifest on every call. Can you cache it and add a test?</div><div class="meta"><span>#1 · 3:42 PM</span></div></div>
{THOUGHT.replace('<span class="d">·</span><span class="pv">Looking for where the manifest is parsed</span>','')}
<div class="prose"><p><code>parse_manifest</code> is called from <code>Loader.load</code> on every request. I’ll memoize it on the file’s mtime so edits still show up.</p></div>
{READ_G}{EDIT_G}
<div class="prose"><p>Both tests pass now.</p></div>
<div class="foot"><b class="ag-build">▪ Build</b><span class="dot">·</span><span>medium</span><span class="dot">·</span><span>3 tools</span><span class="dot">·</span><span>4.2s</span><span class="dot">·</span><span>$0.04</span><span class="st ok">@ic(check)done</span></div>
</div>
<div style="position:relative;flex:none"><span class="jump" style="right:16px">@ic(arrow-down,s14)<span>3 new</span></span></div>
<div class="composer"><div class="in"><span class="caret"></span>Type a message…</div>
<div class="ctrl" style="margin:0 -4px -4px"><span class="pill"><span class="dot ag-build"></span><b class="ag-build">Build</b>@ic(chevron-down)</span><span class="pill"><span class="m">gpt-6.1-terra</span>@ic(chevron-down)</span><span class="sp"></span><span class="send off">@ic(arrow-up)</span></div></div>
<div class="home"><i></i></div></div>'''
    page("d17-phone", "D17 phone", "390,844", body)

# ================= d18 states =================
def d18():
    wm = '''<div class="wordmark"><svg width="168" height="48" viewBox="0 0 168 48" fill="none"><g stroke="var(--text-muted)" stroke-width="1.5"><path d="M10 34 24 12 38 34M10 34h28"/></g><circle cx="24" cy="12" r="4" fill="var(--interactive-accent)"/><circle cx="10" cy="34" r="3" fill="var(--text-secondary)"/><circle cx="38" cy="34" r="3" fill="var(--text-secondary)"/>
<text x="56" y="34" font-family="system-ui, sans-serif" font-size="30" font-weight="700" letter-spacing="3" fill="var(--text-primary)">nexus</text></svg></div>'''
    empty_c = f'''<div class="page-scroll" style="display:flex;align-items:center;justify-content:center"><div class="wordmark" style="gap:20px">{wm}<div style="text-align:center"><div class="t-title">No projects yet</div><div class="t-sec" style="margin-top:4px">Add a project to start a session.</div>
<div class="row gap8" style="justify-content:center;margin-top:16px"><span class="btn primary">@ic(plus,s14)Add a project<span class="kbd">ctrl+o</span></span><span class="btn">Commands<span class="kbd">ctrl+p</span></span></div></div></div></div>'''
    left_empty = '''<aside class="sb sb-l"><div class="sb-search" style="opacity:.6">@ic(search,s14)<span>Search sessions</span><span class="kbd">ctrl+k</span></div><div class="grp"><span class="lbl">Projects</span></div><div class="t-sec muted" style="padding:4px 10px">Nothing here yet.</div><div class="sb-foot"><div class="menu-row">@ic(settings,s14)<span>Settings</span><span class="kbd">ctrl+,</span></div></div></aside>'''
    A = win(empty_c, left=left_empty, right=False, hdr=header(crumbs=("Nexus",), run=None, attn=0, right=False), style="--win-h:400px")
    sk = "".join(f'<div style="height:52px;padding:10px 8px;display:flex;gap:8px"><span style="width:16px"></span><div style="flex:1;display:flex;flex-direction:column;gap:10px"><div class="skel" style="width:{w}%"></div><div class="skel" style="width:{v}%;height:8px"></div></div></div>' for w, v in [(70, 40), (82, 52), (58, 36), (76, 44)])
    left_load = f'''<aside class="sb sb-l"><div class="sb-search">@ic(search,s14)<span>Search sessions</span><span class="kbd">ctrl+k</span></div><div class="grp"><span class="lbl">nexus-demo</span></div>
<div class="row gap8" style="height:32px;padding:0 8px;font-size:13px;color:var(--text-muted)">{mark("run")}<span>loading sessions…</span></div>{sk}</aside>'''
    load_c = '<div class="page-scroll"><div class="col tl" style="padding-top:32px"><div class="skel" style="width:70%;height:14px"></div><div class="skel" style="width:92%"></div><div class="skel" style="width:84%"></div><div class="skel" style="width:60%"></div></div></div>'
    B = win(load_c, left=left_load, right=False, hdr=header(crumbs=("nexus-demo", "Loading…"), run=("run", "loading"), attn=0, right=False), style="--win-h:340px")
    off_banner = '<div class="banner err"><span class="lv">@ic(wifi-off,s14)Disconnected from Core</span><span>retrying in 4s</span><span class="acts"><span class="accent" style="font-weight:500">Retry</span></span></div>'
    comp = composer(text="Also cover the case where nexus.json is missing", focus=True, strip=False).replace('<span class="send">', '<span class="send off">')
    comp = comp.replace('<div class="in val">', '<div class="in val">', 1)
    off_c = off_banner + scroll(CTX + user(1, "3:42 PM", "The loader re-reads the manifest on every call. Can you cache it and add a test?")) + \
        '<div class="col t-meta" style="padding:0 0 8px;color:var(--status-error);display:flex;align-items:center;gap:6px">@ic(wifi-off,s12)Reconnecting to Core… your draft is kept</div>' + comp
    C = win(off_c, right=False, hdr=header(run=("off", "offline"), attn=0, right=False), left=left_sessions(), style="--win-h:520px")
    body = f'''<div class="sheet-pad"><h1>D18 · Empty, loading and offline</h1><div style="height:24px"></div>
<span class="cap">Empty workspace</span><div class="frame">{A}</div><div style="height:32px"></div>
<span class="cap">Loading (skeletons keep the list’s footprint, so nothing shifts)</span><div class="frame">{B}</div><div style="height:32px"></div>
<span class="cap">Offline (composer stays editable)</span><div class="frame">{C}</div></div>'''
    page("d18-states", "D18 states", "1440,1560", body, sheet=True)

d01(); d08(); d09(); d17(); d18()
