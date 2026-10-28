from lib import *

CTX = '<div class="ctxfold">@ic(chevron-right,chev)<b>Context</b><span class="m">· 4 items · 1.1k tokens</span></div>'
READ_G = group('<span>Read 2 files, ran 1 command</span>', '1.4s')
EDIT_G = group('<span>Edited 1 file</span><span class="add tnum" style="font-size:12px;margin-left:4px">+12</span><span class="del tnum" style="font-size:12px">−3</span><span class="m">·</span><span class="fail">@ic(x,s12)1 failed</span>', '0.9s')
THOUGHT = '<div class="thought">@ic(chevron-right,chev)<b>Thought</b><span class="d">for 2.1s</span><span class="d">·</span><span class="pv">Looking for where the manifest is parsed</span></div>'

def turn(focus2=True):
    return (
        CTX +
        user(1, "3:42 PM", "The loader re-reads the manifest on every call. Can you cache it and add a test?") +
        '<div style="display:flex;flex-direction:column;gap:8px">' + THOUGHT +
        '<div class="prose"><p>I’ll start by finding where the manifest is parsed.</p></div>' + READ_G + '</div>' +
        '<div class="prose"><p><code>parse_manifest</code> is called from <code>Loader.load</code> on every request. I’ll memoize it on the file’s mtime so edits still show up, and add a regression test that counts parses.</p></div>' +
        EDIT_G +
        '<div class="prose"><p>The first edit did not apply because the line had changed; I re-read the file and applied it. Both tests pass.</p></div>' +
        footer() +
        user(2, "3:44 PM", "Go ahead, but keep the change small.<br>日本語のコメントも残してください。", focus=focus2)
    )

def d02():
    c = scroll(turn(False)) + composer()
    page("d02-shell", "D02 shell", "1440,900", win(c))

def d04():
    c = scroll(turn(True)) + composer()
    page("d04-turn", "D04 turn", "1440,900", win(c))

d02(); d04()
