#!/usr/bin/env python3
"""End-to-end test of the Soundprint page in headless Chromium.
Usage: e2e.py SITE_DIR OUT_DIR [ARI_SHARE_LINK_FILE]"""
import functools, http.server, os, subprocess, sys, tempfile, threading
from playwright.sync_api import sync_playwright
site, out = sys.argv[1], sys.argv[2]
here = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp()
subprocess.run([sys.executable, os.path.join(here, "make_fake_export.py"), f"{tmp}/ext.zip", f"{tmp}/basic.zip"], check=True)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=site))
srv.RequestHandlerClass.log_message = lambda *a: None
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/"
fails = []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)
with sync_playwright() as pw:
    b = pw.chromium.launch()
    for scheme in ("light", "dark"):
        ctx = b.new_context(viewport={"width": 420, "height": 900}, color_scheme=scheme, device_scale_factor=1)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        pg = ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
        pg.goto(base)
        check(pg.locator("#drop").is_visible(), f"[{scheme}] landing shows the drop zone")
        # broken link
        pg.goto(base + "#s=notvalid"); pg.wait_for_timeout(300)
        check("damaged" in pg.inner_text("#app"), f"[{scheme}] bad share link explains itself")
        pg.goto(base); pg.fill("#nameIn", "Sam")
        pg.set_input_files("#file", f"{tmp}/ext.zip"); pg.wait_for_url("**#view=mine", timeout=15000); pg.wait_for_timeout(400)
        txt = pg.inner_text("#app")
        check("Sam's listening" in txt and "Top artists" in txt and "When you listen" in txt and "Your eras" in txt, f"[{scheme}] extended export renders all sections")
        check("Nightwish" in txt, f"[{scheme}] top artist present")
        pg.click("#artistsCard [data-term=s]"); check(pg.locator("#artistsCard .pod").count() == 3, f"[{scheme}] term switch redraws podium")
        svg = pg.locator(".chart svg").first; svg.scroll_into_view_if_needed(); bb = svg.bounding_box()
        pg.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2); pg.wait_for_timeout(100)
        check(float(pg.eval_on_selector("#tip", "e => getComputedStyle(e).opacity")) > 0, f"[{scheme}] line chart hover shows tooltip")
        if scheme == "light": pg.screenshot(path=f"{out}/mine-light.png", full_page=True)
        else: pg.screenshot(path=f"{out}/mine-dark.png", full_page=True)
        pg.evaluate("navigator.share = undefined")
        pg.click("[data-go=share]"); pg.wait_for_timeout(300)
        link = pg.evaluate("navigator.clipboard.readText()")
        check("#s=" in link, f"[{scheme}] share copies a link ({len(link)} chars)")
        # basic export, as a friend, in a fresh context
        ctx2 = b.new_context(viewport={"width": 1100, "height": 900}, color_scheme=scheme)
        p2 = ctx2.new_page(); p2.on("pageerror", lambda e: errs.append(str(e)))
        p2.goto(link); p2.wait_for_timeout(400)
        check("Sam shared" in p2.inner_text("#app"), f"[{scheme}] friend sees shared profile")
        p2.click("[data-go=home]"); p2.wait_for_timeout(200)
        p2.set_input_files("#file", f"{tmp}/basic.zip"); p2.wait_for_url("**c=1", timeout=15000); p2.wait_for_timeout(300)
        t2 = p2.inner_text("#app")
        check("friendsam & Sam" in t2 and "taste match" in t2, f"[{scheme}] friend's basic export goes straight to compare")
        if scheme == "dark": p2.screenshot(path=f"{out}/compare-dark.png", full_page=True)
        if len(sys.argv) > 3:
            ari = open(sys.argv[3]).read().strip().split("#", 1)[1]
            p2.goto(base + "#" + ari); p2.wait_for_timeout(500)
            ta = p2.inner_text("#app")
            check("Genres" in ta and "Release years" in ta and "Mainstream meter" in ta, f"[{scheme}] Ari's API profile renders")
            p2.screenshot(path=f"{out}/ari-{scheme}.png", full_page=True)
            p2.click("[data-go=compare]"); p2.wait_for_timeout(300)
            check("taste match" in p2.inner_text("#app"), f"[{scheme}] compare with Ari works")
        check(not errs, f"[{scheme}] no JS errors {errs[:3]}")
        ctx.close(); ctx2.close()
    b.close()
print("FAILED:", len(fails) if fails else "none")
sys.exit(1 if fails else 0)
