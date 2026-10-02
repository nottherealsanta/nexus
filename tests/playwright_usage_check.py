"""Exercise the production web usage dialog with delayed and failed host replies.

This isolated browser check avoids the broad harness's CSP evaluation issue.
Run with PYTHONPATH=. .venv/bin/python tests/playwright_usage_check.py.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    app = (ROOT / "nexus/ui/web/js/app.js").read_text()
    start = app.index("async function openUsage(){")
    end = app.index("\n}", start) + 2
    usage = (ROOT / "nexus/ui/web/js/usage.js").read_text().replace("export ", "")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('<div id="text-overlay" hidden><h2 id="text-title"></h2><div id="text-body"></div></div>')
            page.add_script_tag(content=usage + """
                const state={workspace:'test',session:'s'}, pending=[];
                const USAGE_TITLE='Provider usage and limits';
                const $=id=>document.getElementById(id);
                function el(tag,cls='',text=''){const node=document.createElement(tag);node.className=cls;node.textContent=text;return node;}
                const api={command:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))};
                function showText(title,text,{node}){$('text-title').textContent=title;$('text-body').replaceChildren(node);$('text-overlay').hidden=false;}
            """ + app[start:end])
            page.evaluate("void openUsage()")
            assert page.locator(".usage-spinner").is_visible()
            assert page.locator("#text-body").inner_text().startswith("Reading usage")
            page.evaluate("pending.shift().resolve({providers:[{label:'Codex',plan:'Plus'}],fetched_at:1})")
            page.locator(".usage-provider").wait_for()
            page.get_by_role("button", name="Refresh").click()
            assert page.locator(".usage-spinner").is_visible()
            assert page.locator(".usage-provider-name").inner_text() == "Codex · Plus"
            page.evaluate("pending.shift().reject(new Error('offline'))")
            page.locator(".usage-error").wait_for()
            assert "offline" in page.locator(".usage-error").inner_text()
            assert page.locator(".usage-provider-name").inner_text() == "Codex · Plus"
            page.get_by_role("button", name="Refresh").click()
            page.evaluate("state.session='new';pending.shift().resolve({providers:[{label:'Stale'}]})")
            assert page.locator(".usage-provider-name").inner_text() == "Codex · Plus"
            page.evaluate("void openUsage()")
            page.evaluate("void openUsage()")
            page.evaluate("pending.shift().resolve({providers:[{label:'Older'}]})")
            assert page.locator(".usage-provider-name").inner_text() == "Codex · Plus"
            page.evaluate("pending.shift().resolve({providers:[{label:'Newest'}]})")
            page.get_by_text("Newest", exact=True).wait_for()
            page.evaluate("void openUsage();$('text-body').replaceChildren()")
            page.evaluate("pending.shift().resolve({providers:[{label:'Closed'}]})")
            assert page.locator(".usage-provider").count() == 0
            print("Usage: cached refresh, loading, errors, session changes, stale replies and dismissal passed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
