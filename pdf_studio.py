#!/usr/bin/env python3
"""RulesForge PDF Studio: merge PDFs, capture web pages as PDFs, name and tag the result for a Game Pack.

Run:  python pdf_studio.py     (needs:  pip install pypdf)
It opens a page in your browser on this computer only (127.0.0.1); nothing is uploaded anywhere.
Web capture uses Chrome, Edge or Chromium already installed on your computer.
The merged PDF is saved to packs/incoming/<Game>/<Edition>/<Type>/<Title>.pdf with a <Title>.json beside it
holding the title, faction, version and description, which is exactly what RulesForge's pack converter reads.
"""
import base64, io, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject, TextStringObject, FloatObject
    from pypdf.generic import ContentStream
except ImportError:
    _m = "PDF Studio needs the pypdf package.\nOpen a Command Prompt and run:  pip install pypdf\nthen start PDF Studio again."
    try:
        import tkinter, tkinter.messagebox
        _r = tkinter.Tk(); _r.withdraw(); _r.attributes("-topmost", True); tkinter.messagebox.showerror("RulesForge PDF Studio", _m); _r.destroy()
    except Exception:
        try:
            import ctypes; ctypes.windll.user32.MessageBoxW(0, _m, "RulesForge PDF Studio", 0x10)
        except Exception: pass
    sys.exit(_m)

HERE = Path(__file__).resolve().parent
# Files you add are kept here between runs, so a restart (or the window being closed by accident) does not lose them.
KEEP = Path(os.environ.get("RF_PDFSTUDIO_DIR") or (Path.home() / ".rulesforge-pdf-studio"))
WORK = KEEP / "work"; WORK.mkdir(parents=True, exist_ok=True)
INDEX = KEEP / "files.json"
TYPES = {"Rules": "Core Rules, rules updates, FAQs", "Factions": "An army or faction", "Companions": "Event Companions (Doubles, Teams…)", "Collections": "Reference PDFs (monsters, spells…)", "Sections": "Your own section, named by you (Kill Team, Approved Ops…)"}
FILES = {}   # id -> {"name", "path", "pages"}


def _load_files():
    """Bring back files from earlier runs; forget old ones (older than 3 days) and any that are gone."""
    try: old = json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception: old = {}
    now = time.time()
    for fid, f in old.items():
        p = Path(f.get("path", ""))
        if p.exists() and now - p.stat().st_mtime < 3 * 86400: FILES[fid] = f
    for p in WORK.glob("*"):
        try:
            if p.is_file() and now - p.stat().st_mtime > 3 * 86400 and str(p) not in {f["path"] for f in FILES.values()}: p.unlink()
        except OSError: pass


def _save_files():
    try: INDEX.write_text(json.dumps(FILES), encoding="utf-8")
    except OSError: pass


def default_out():
    for p in [HERE, *HERE.parents]:
        if (p / "packs").is_dir():
            return p / "packs" / "incoming"
    return Path.home() / "RulesForge-PDFs"


OUT = Path(os.environ.get("RF_OUT") or default_out())
CONFIG = Path.home() / ".rulesforge-pdfstudio.json"   # remembers the GitHub details (and token, if you tick "remember")
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")


def load_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except Exception:
        return {}



def safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .") or "untitled"


def parse_range(text, total):
    """'1-3, 5, 9-' -> zero-based page indexes. Blank means every page."""
    if not text.strip():
        return list(range(total))
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            a, b = int(m.group(1) or 1), int(m.group(2) or total)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError(f"Can't read the page range '{part}'")
        if a < 1 or b > total or a > b:
            raise ValueError(f"Page range '{part}' is outside 1–{total}")
        out.extend(range(a - 1, b))
    return out


def find_browser():
    cands = [os.environ.get("CHROME_PATH")]
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge"):
        cands.append(shutil.which(n))
    cands += [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return next((c for c in cands if c and Path(c).exists()), None)


def register(path, name):
    n = len(PdfReader(str(path)).pages)
    fid = uuid.uuid4().hex[:10]
    FILES[fid] = {"name": name, "path": str(path), "pages": n}; _save_files()
    return {"id": fid, "name": name, "pages": n}


def capture(url):
    browser = find_browser()
    if not browser:
        raise RuntimeError("Couldn't find Chrome or Edge on this computer. Install one, or set CHROME_PATH to its program.")
    if not re.match(r"https?://", url):
        url = "https://" + url
    dest = WORK / f"{uuid.uuid4().hex[:8]}.pdf"
    prof = WORK / f"profile-{uuid.uuid4().hex[:6]}"
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={prof}", "--no-pdf-header-footer",
           "--hide-scrollbars", "--virtual-time-budget=10000", f"--print-to-pdf={dest}", url]
    subprocess.run(cmd, capture_output=True, timeout=120)
    shutil.rmtree(prof, ignore_errors=True)
    if not dest.exists() or dest.stat().st_size < 500:
        raise RuntimeError("The page couldn't be captured (is the address right, and are you online?).")
    host = urlparse(url).netloc or "web page"
    return register(dest, f"{host} (web page)")


# ---------- live web page capture: open the page, let the user tick boxes and open sections, then print it ----------
class WS:
    """A tiny WebSocket client (standard library only) for Chrome's DevTools protocol."""
    def __init__(self, url):
        u = urlparse(url); self.s = socket.create_connection((u.hostname, u.port), 10); self.id = 0; self.events = []
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = self.s.recv(4096)
            if not c: raise RuntimeError("The browser closed the connection.")
            buf += c
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]: raise RuntimeError("The browser refused the connection.")

    def _read(self, n):
        while len(self.buf) < n:
            c = self.s.recv(1 << 20)
            if not c: raise EOFError("The browser closed.")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, text):
        data = text.encode(); n = len(data); mask = os.urandom(4); h = bytearray([0x81])
        if n < 126: h.append(0x80 | n)
        elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
        else: h.append(0x80 | 127); h += struct.pack(">Q", n)
        self.s.sendall(bytes(h) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2); n = b2 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n); op = b1 & 0x0F
            if op == 8: raise EOFError("The browser closed.")
            if op in (0, 1, 2): msg += data
            if b1 & 0x80 and op in (0, 1, 2): return msg.decode()

    def call(self, method, params=None, timeout=60):
        self.id += 1; mid = self.id; self.s.settimeout(timeout)
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self._recv())
            if m.get("id") == mid:
                if "error" in m: raise RuntimeError(m["error"].get("message", "browser error"))
                return m.get("result", {})
            if "method" in m: self.events.append(m["method"])

    def wait_event(self, name, timeout):
        end = time.time() + timeout
        while name not in self.events and time.time() < end:
            self.s.settimeout(max(0.1, end - time.time()))
            try: m = json.loads(self._recv())
            except socket.timeout: break
            if "method" in m: self.events.append(m["method"])
        return name in self.events


SESSIONS = {}
AD_HOSTS = """doubleclick.net googlesyndication.com googleadservices.com googletagservices.com adservice.google.com pagead2.googlesyndication.com
google-analytics.com googletagmanager.com adnxs.com adsrvr.org amazon-adsystem.com criteo.com criteo.net taboola.com outbrain.com pubmatic.com
rubiconproject.com openx.net casalemedia.com moatads.com scorecardresearch.com quantserve.com quantcount.com connect.facebook.net facebook.com/tr hotjar.com
ezoic.net ezodn.com ezoiccdn.com mediavine.com adthrive.com sharethrough.com smartadserver.com 33across.com indexww.com advertising.com yieldmo.com
gumgum.com lijit.com sovrn.com media.net bidswitch.net teads.tv connatix.com adform.net adition.com contextweb.com sitescout.com imrworldwide.com
chartbeat.com chartbeat.net newrelic.com nr-data.net bugsnag.com sentry.io segment.io mixpanel.com amplitude.com fullstory.com clarity.ms
cdn.carbonads.com carbonads.net buysellads.com adroll.com liveintent.com id5-sync.com crwdcntrl.net bluekai.com demdex.net everesttech.net
rlcdn.com tapad.com agkn.com mathtag.com turn.com serving-sys.com flashtalking.com innovid.com tremorhub.com spotxchange.com springserve.com
primis.tech vidazoo.com vidoomy.com adsafeprotected.com doubleverify.com iasds01.com""".split()
AD_URLS = ["*://*." + h.split("/")[0] + "/*" if "/" not in h else "*://*." + h + "*" for h in AD_HOSTS] + ["*://" + h.split("/")[0] + "/*" for h in AD_HOSTS if "/" not in h]
AD_CSS_JS = r"""(()=>{const css='ins.adsbygoogle,[id^="google_ads_iframe"],[id^="div-gpt-ad"],iframe[src*="doubleclick"],iframe[src*="googlesyndication"],iframe[src*="adservice"],[data-ad-slot],[data-google-query-id],.ezoic-ad,[id^="ezoic-pub-ad"],[class*="mediavine-"],#mediavine-settings,[id*="adthrive"],[class*="adthrive"],.adsbygoogle-noablate,[aria-label="Advertisement"],[id^="aswift_"],.carbon-wrap,#carbonads,.grid-ad,.ad-slot,.ad-container,.advertisement,.sticky-ad,.header-ad,.footer-ad{display:none!important;visibility:hidden!important;height:0!important;min-height:0!important;margin:0!important;padding:0!important}';
const put=()=>{try{if(document.getElementById('rf-noads'))return;const host=document.head||document.documentElement;if(!host)return;const s=document.createElement('style');s.id='rf-noads';s.textContent=css;host.appendChild(s)}catch(e){}};put();document.addEventListener('DOMContentLoaded',put);new MutationObserver(put).observe(document,{childList:true,subtree:true})})()"""
SCAN_JS = r"""(()=>{const txt=t=>(t||'').replace(/\s+/g,' ').trim().slice(0,80);
document.querySelectorAll('[data-rf-c]').forEach(e=>e.removeAttribute('data-rf-c'));
const label=e=>{if(e.id){const l=document.querySelector('label[for="'+CSS.escape(e.id)+'"]');if(l&&txt(l.textContent))return txt(l.textContent)}
const p=e.closest('label');if(p&&txt(p.textContent))return txt(p.textContent);if(e.getAttribute('aria-label'))return txt(e.getAttribute('aria-label'));
const nx=e.nextSibling&&e.nextSibling.textContent;if(txt(nx))return txt(nx);const pv=e.previousSibling&&e.previousSibling.textContent;if(txt(pv))return txt(pv);
return txt(e.parentElement&&e.parentElement.textContent)||'(unlabelled)'};
const cand=[],seen=new Set();const add=(e,type,l,st)=>{if(seen.has(e))return;seen.add(e);cand.push([e,type,l,st])};
document.querySelectorAll('input[type=checkbox]').forEach(e=>add(e,'checkbox',label(e),e.checked));
document.querySelectorAll('details').forEach(e=>{const s=e.querySelector('summary');add(e,'details',txt(s&&s.textContent)||'(section)',e.open)});
document.querySelectorAll('[aria-expanded]').forEach(e=>{if(!e.closest('details'))add(e,'aria',txt(e.textContent)||'(section)',e.getAttribute('aria-expanded')==='true')});
document.querySelectorAll('button,a,span,div,label,[role=button]').forEach(e=>{if(e.children.length||seen.has(e)||e.hasAttribute('aria-expanded'))return;const t=txt(e.textContent);
if(t&&t.length<40&&/^(show|hide|expand|collapse)\b/i.test(t)&&!e.closest('summary'))add(e,'button',t,null)});
cand.sort((a,b)=>a[0]===b[0]?0:(a[0].compareDocumentPosition(b[0])&4)?-1:1);   // the order they appear on the page
return cand.slice(0,200).map(([e,type,l,st],i)=>{e.setAttribute('data-rf-c',i);return{i,type,label:l,state:st}})})()"""
STATE_JS = r"""(()=>[...document.querySelectorAll('[data-rf-c]')].map(e=>({i:+e.getAttribute('data-rf-c'),state:e.matches('input')?e.checked:e.matches('details')?e.open:e.hasAttribute('aria-expanded')?e.getAttribute('aria-expanded')==='true':null,label:(e.textContent||'').replace(/\s+/g,' ').trim().slice(0,80)})))()"""


def _set_js(ops):
    return "(()=>{const o=" + json.dumps(ops) + r""";for(const x of o){const e=document.querySelector('[data-rf-c="'+x.i+'"]');if(!e)continue;
if(x.kind==='checkbox'){if(e.checked!==x.v)e.click()}else if(x.kind==='details'){e.open=x.v}
else if(x.kind==='aria'){if((e.getAttribute('aria-expanded')==='true')!==x.v)e.click()}
else if(x.kind==='button'){const t=(e.textContent||'').trim().toLowerCase(),open=/^(show|expand)/.test(t);if(open===x.v)e.click()}}return 1})()"""


def close_session(sid):
    s = SESSIONS.pop(sid, None)
    if not s: return
    try: s["ws"].s.close()
    except Exception: pass
    try: s["proc"].kill()
    except Exception: pass
    shutil.rmtree(s["prof"], ignore_errors=True)


def open_page(d, progress):
    for k in list(SESSIONS): close_session(k)
    browser = find_browser()
    if not browser:
        raise RuntimeError("Couldn't find Chrome or Edge on this computer. Install one, or set CHROME_PATH to its program.")
    url = d.get("url", "").strip()
    if not re.match(r"https?://|file://", url): url = "https://" + url
    progress(5, 100, "Starting the browser…")
    prof = WORK / f"profile-{uuid.uuid4().hex[:6]}"; prof.mkdir(parents=True, exist_ok=True)
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}
    proc = subprocess.Popen([browser, "--headless=new", "--disable-gpu", "--no-sandbox", f"--user-data-dir={prof}", "--remote-debugging-port=0", "--hide-scrollbars", "--window-size=1280,1600", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
    sid = uuid.uuid4().hex[:8]; SESSIONS[sid] = {"proc": proc, "prof": prof, "ws": None, "t": time.time(), "url": url, "lock": threading.Lock(), "n": 0}
    try:
        port = None
        for _ in range(100):
            f = prof / "DevToolsActivePort"
            if f.exists() and f.read_text().strip():
                port = int(f.read_text().split()[0]); break
            time.sleep(0.15)
        if not port: raise RuntimeError("The browser didn't start.")
        progress(20, 100, "Connecting…")
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10))
        ws = WS(next(t for t in tabs if t.get("type") == "page")["webSocketDebuggerUrl"]); SESSIONS[sid]["ws"] = ws
        ws.call("Page.enable")
        if d.get("adblock", True):
            ws.call("Network.enable"); ws.call("Network.setBlockedURLs", {"urls": AD_URLS})
            ws.call("Page.addScriptToEvaluateOnNewDocument", {"source": AD_CSS_JS})
        ws.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1600, "deviceScaleFactor": 1, "mobile": False})
        set_theme(ws, d.get("theme", "light"))
        progress(30, 100, "Loading the page…")
        ws.call("Page.navigate", {"url": url}, timeout=60)
        end = time.time() + 45
        while not ws.wait_event("Page.loadEventFired", 1) and time.time() < end:
            progress(30 + int(45 * (1 - (end - time.time()) / 45)), 100, "Loading the page…")
        progress(80, 100, "Letting the page settle…"); time.sleep(1.5)
        ws.call("Runtime.evaluate", {"expression": "document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(a){const h=a.getAttribute('href')||'';if(!/^(#|javascript:)/i.test(h))e.preventDefault()}},true)"})
        progress(90, 100, "Finding checkboxes and sections…")
        controls = ws.call("Runtime.evaluate", {"expression": SCAN_JS, "returnByValue": True})["result"].get("value", [])
        SESSIONS[sid]["t"] = time.time()
        progress(94, 100, "Creating the preview…")
        pv = live_preview({"sid": sid})
        return {"sid": sid, "preview": pv["url"], "controls": controls, "title": ws.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})["result"].get("value", "")}
    except Exception:
        close_session(sid); raise


# Small floating buttons in the bottom-right corner (e.g. the "rulebook" and "back to top" octagons) are repeated on every
# printed page and are not part of the content: they are hidden while the PDF is made, then put back.
# The round buttons that float at the right of Wahapedia pages (book, skull, back to top) are deleted from the page for good: they scroll with the reader,
# so they would otherwise sit on top of the page's own pictures in every strip and in the saved PDF. A floating (fixed or sticky) block that is small and
# sits at the right edge, lower down the screen, is set to display:none; the page underneath is left as it is.
HIDE_FLOATERS_JS = """(()=>{const W=innerWidth,H=innerHeight;let n=0;
for(const e of document.querySelectorAll('body *')){
const cs=getComputedStyle(e);if(cs.position!=='fixed'&&cs.position!=='sticky')continue;if(cs.display==='none')continue;
const r=e.getBoundingClientRect();if(r.width<=0||r.width>170||r.height>520)continue;
if(r.left+r.width/2<W*0.7||r.top+r.height/2<H*0.3)continue;
e.style.setProperty('display','none','important');n++}
return n})()"""
# Chrome prints any element that has a CSS filter (even a drop-shadow) as one flat picture, text included. Datacards then arrive as images with nothing to read, so filters are switched off before saving.
NOFILTER_JS = """(()=>{let n=0;for(const e of document.querySelectorAll('body, body *')){const f=getComputedStyle(e).filter;if(f&&f!=='none'){e.style.setProperty('filter','none','important');n++}}return n})()"""
SIZE_JS = "({w:Math.ceil(Math.max(document.documentElement.scrollWidth,innerWidth)),h:Math.ceil(Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0))})"


def _pdf_bytes(ws, lazy=False):
    if lazy:   # scroll to the bottom and back so lazy-loaded images and sections appear
        ws.call("Runtime.evaluate", {"expression": "window.scrollTo(0,document.body.scrollHeight)"}); time.sleep(0.8)
        ws.call("Runtime.evaluate", {"expression": "window.scrollTo(0,0)"})
    ws.call("Runtime.evaluate", {"expression": HIDE_FLOATERS_JS, "returnByValue": True})
    try: ws.call("Runtime.evaluate", {"expression": NOFILTER_JS, "returnByValue": True})
    except Exception: pass
    try:
        # One long page: the paper is made exactly as large as the whole web page, so nothing is split at page breaks.
        sz = ws.call("Runtime.evaluate", {"expression": SIZE_JS, "returnByValue": True})["result"].get("value") or {}
        w, h = int(sz.get("w") or 0), int(sz.get("h") or 0)
        if w > 100 and h > 100:
            r = ws.call("Page.printToPDF", {"printBackground": True, "displayHeaderFooter": False, "preferCSSPageSize": False,
                                            "paperWidth": w / 96.0, "paperHeight": (h + 4) / 96.0,
                                            "marginTop": 0, "marginBottom": 0, "marginLeft": 0, "marginRight": 0}, timeout=300)
            data = base64.b64decode(r["data"])
            try:
                if len(PdfReader(io.BytesIO(data)).pages) == 1: return data
            except Exception: return data
            return stitch_pages(data)   # the browser still split it (a very long page): join the pages into one
        r = ws.call("Page.printToPDF", {"printBackground": True, "displayHeaderFooter": False, "preferCSSPageSize": True}, timeout=300)
        return stitch_pages(base64.b64decode(r["data"]))
    finally:
        pass


def stitch_pages(data):
    """Joins every page of a PDF into one tall page (top to bottom), clipping each page to its own box."""
    from pypdf import PageObject, Transformation
    from pypdf.generic import FloatObject
    # The pages are edited, so they must belong to a writer (pypdf 7 no longer edits pages that only belong to a reader).
    src = PdfWriter(clone_from=io.BytesIO(data)); pgs = list(src.pages)
    if len(pgs) < 2: return data
    boxes = [(float(p.mediabox.left), float(p.mediabox.bottom), float(p.mediabox.width), float(p.mediabox.height)) for p in pgs]
    W = max(b[2] for b in boxes); H = sum(b[3] for b in boxes)
    out = PdfWriter(); page = out.add_blank_page(width=W, height=H); top = 0.0
    for p, (L, B, w, h) in zip(pgs, boxes):
        cs = ContentStream(p.get_contents(), src)
        ops = [([], b"q"), ([FloatObject(L), FloatObject(B), FloatObject(w), FloatObject(h)], b"re"), ([], b"W"), ([], b"n")] + list(cs.operations) + [([], b"Q")]
        cs.operations = ops; p.replace_contents(cs)
        page.merge_transformed_page(p, Transformation().translate(-L, H - top - h - B))
        top += h
    buf = io.BytesIO(); out.write(buf); return buf.getvalue()


def live_preview(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    with s["lock"]:
        s["n"] += 1; dest = WORK / f"live-{d['sid']}-{s['n']}.pdf"; dest.write_bytes(_pdf_bytes(s["ws"], s["n"] == 1))
        if s["n"] > 3: (WORK / f"live-{d['sid']}-{s['n'] - 3}.pdf").unlink(missing_ok=True)
    return {"url": f"/api/pdf?path={dest.name}"}


CHUNK = 1200


def page_size(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    with s["lock"]:
        try: s["ws"].call("Runtime.evaluate", {"expression": HIDE_FLOATERS_JS, "returnByValue": True})   # the floating round buttons are removed before every measure and picture
        except Exception: pass
        h = int(s["ws"].call("Runtime.evaluate", {"expression": "Math.max(document.documentElement.scrollHeight,document.body?document.body.scrollHeight:0)", "returnByValue": True})["result"].get("value", 1600))
    return {"height": max(400, min(h, 60000)), "chunk": CHUNK}


def page_shot(d):
    """One horizontal strip of the live page as a picture. The viewer asks for the strips nearest the reader first."""
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    y, h = int(d.get("y", 0)), int(d.get("h", CHUNK))
    with s["lock"]:
        r = s["ws"].call("Page.captureScreenshot", {"format": "jpeg", "quality": 65, "captureBeyondViewport": True, "clip": {"x": 0, "y": y, "width": 1280, "height": h, "scale": 1}}, timeout=120)
    return {"src": "data:image/jpeg;base64," + r["data"]}


def page_controls(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    with s["lock"]:
        return {"controls": s["ws"].call("Runtime.evaluate", {"expression": SCAN_JS, "returnByValue": True})["result"].get("value", [])}


def set_theme(ws, theme):
    """Tell the hidden browser which colour scheme the page should use, so the viewer and the saved PDF match.
    Without this the browser follows the computer: a dark-mode computer gives sites their dark styles in the PDF."""
    theme = "dark" if theme == "dark" else "light"
    ws.call("Emulation.setEmulatedMedia", {"media": "screen", "features": [{"name": "prefers-color-scheme", "value": theme}]})
    return theme


def page_theme(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    with s["lock"]:
        return {"theme": set_theme(s["ws"], d.get("theme", "light"))}


def page_reveal(d):
    """Scroll the live page so the first instance of one listed item is in view; returns its height on the page."""
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    i = int(d.get("i", -1))
    with s["lock"]:
        r = s["ws"].call("Runtime.evaluate", {"expression": f"""(()=>{{const e=document.querySelector('[data-rf-c="{i}"]');if(!e)return null;
e.scrollIntoView({{block:'center'}});const b=e.getBoundingClientRect();return window.scrollY+b.top+b.height/2;}})()""", "returnByValue": True})
        y = r["result"].get("value")
    return {"y": None if y is None else float(y)}


def page_click(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    x, y = float(d.get("x", 0)), float(d.get("y", 0))
    with s["lock"]:
        ws = s["ws"]
        sy = float(ws.call("Runtime.evaluate", {"expression": f"window.scrollTo(0,{max(0, y - 300)});window.scrollY", "returnByValue": True})["result"].get("value", 0))
        vy = y - sy
        for t, extra in (("mouseMoved", {}), ("mousePressed", {"button": "left", "clickCount": 1}), ("mouseReleased", {"button": "left", "clickCount": 1})):
            ws.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": vy, **extra})
        # wait for the page to react: two animation frames and a short settle, not a fixed half second
        ws.call("Runtime.evaluate", {"expression": "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>setTimeout(r,60))))", "awaitPromise": True}, timeout=10)
    return {"ok": True}


def page_set(d):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    with s["lock"]:
        s["ws"].call("Runtime.evaluate", {"expression": _set_js(d.get("ops", [])), "returnByValue": True}); time.sleep(0.3)
        return {"states": s["ws"].call("Runtime.evaluate", {"expression": STATE_JS, "returnByValue": True})["result"].get("value", [])}


def print_page(d, progress):
    s = SESSIONS.get(d.get("sid", ""))
    if not s: raise RuntimeError("That page session ended. Open the page again.")
    try:
        progress(15, 100, "Preparing the page…")
        with s["lock"]:
            progress(35, 100, "Creating the PDF…")
            data = _pdf_bytes(s["ws"], True)
        progress(90, 100, "Saving…")
        dest = WORK / f"{uuid.uuid4().hex[:8]}.pdf"; dest.write_bytes(data)
        host = urlparse(s["url"]).netloc or "web page"
        return register(dest, f"{host} (web page)")
    finally:
        close_session(d.get("sid", ""))


def _mul(a, b):
    """Multiply two PDF matrices given as (a, b, c, d, e, f)."""
    return (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3], a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3], a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])


def text_runs(page):
    """Every text-showing step on the page as (operation index, y position in page units, plain text)."""
    cs = page.get_contents()
    ops = list(cs.operations) if cs is not None else []
    I = (1, 0, 0, 1, 0, 0)
    ctm, stack, tm, tlm, lead, runs = I, [], I, I, 0.0, []
    for n, (operands, op) in enumerate(ops):
        o = op.decode() if isinstance(op, bytes) else op
        if o == "q": stack.append(ctm)
        elif o == "Q": ctm = stack.pop() if stack else I
        elif o == "cm": ctm = _mul(tuple(float(x) for x in operands), ctm)
        elif o == "BT": tm = tlm = I
        elif o == "Tm": tm = tlm = tuple(float(x) for x in operands)
        elif o in ("Td", "TD"):
            tlm = _mul((1, 0, 0, 1, float(operands[0]), float(operands[1])), tlm); tm = tlm
            if o == "TD": lead = -float(operands[1])
        elif o == "TL": lead = float(operands[0])
        elif o in ("T*", "'", '"'):
            tlm = _mul((1, 0, 0, 1, 0, -lead), tlm); tm = tlm
        if o in ("Tj", "TJ", "'", '"'):
            val = operands[-1]
            txt = "".join(str(x) for x in val if not isinstance(x, (int, float, FloatObject))) if o == "TJ" else str(val)
            runs.append((n, _mul(tm, ctm)[5], txt))
    return ops, runs


def crop_top(page, pct):
    """Moves the top edge of the page down by pct% so the header above it is gone from view (the page starts lower)."""
    if not pct: return
    from pypdf.generic import RectangleObject
    mb = page.mediabox; l, b, r, t = float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)
    nt = t - (t - b) * pct / 100
    box = RectangleObject([l, b, r, nt])
    page.mediabox = box; page.cropbox = box


def crop_bottom(page, pct):
    """Moves the bottom edge of the page up by pct% so the footer below it is gone from view (the page ends higher)."""
    if not pct: return
    from pypdf.generic import RectangleObject
    mb = page.mediabox; l, b, r, t = float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)
    nb = b + (t - b) * pct / 100
    box = RectangleObject([l, nb, r, t])
    page.mediabox = box; page.cropbox = box


def clear_bands(page, top_pct, bottom_pct):
    """Really removes the text in the top/bottom share of the page (not just covers it)."""
    if not (top_pct or bottom_pct):
        return
    h = float(page.mediabox.height); base = float(page.mediabox.bottom)
    lo, hi = base + h * bottom_pct / 100, base + h * (1 - top_pct / 100)
    ops, runs = text_runs(page)
    kill = {n for n, y, _ in runs if (bottom_pct and y < lo) or (top_pct and y > hi)}
    if not kill:
        return
    for n in kill:
        operands, op = ops[n]
        o = op.decode() if isinstance(op, bytes) else op
        ops[n] = ([ArrayObject()] if o == "TJ" else [TextStringObject("")] if o == "Tj" else [*operands[:-1], TextStringObject("")], op)
    cs = ContentStream(None, page.pdf); cs.operations = ops
    page.replace_contents(cs)


def detect_bands(paths_pages, progress=None):
    """Finds lines that repeat at the top or bottom of many pages (site name, date, address, page numbers) and
    returns the suggested band sizes in percent plus the repeated text found."""
    # Repeated headers and footers show up on nearly every page, so reading up to 30 evenly spread pages finds
    # exactly the same lines as reading all of them, in a fraction of the time on long books.
    flat = [(p, i) for p, idxs in paths_pages for i in idxs]
    if len(flat) > 30:
        flat = [flat[round(k * (len(flat) - 1) / 29)] for k in range(30)]
    grouped = {}
    for p, i in flat: grouped.setdefault(p, []).append(i)
    paths_pages = list(grouped.items())
    seen = {}; total = 0; heights = []; every = len(flat); done = 0
    for path, idxs in paths_pages:
        r = PdfReader(path)
        for i in idxs:
            pg = r.pages[i]; total += 1; done += 1
            if progress: progress(done - 1, every, f"Reading page {done} of {every}")
            h = float(pg.mediabox.height); base = float(pg.mediabox.bottom); heights.append(h)
            lines = {}
            def visit(text, cm, tm, fd, fs):
                if text.strip():
                    m = _mul(tuple(tm), tuple(cm)); lines.setdefault(round(m[5]), []).append(text)
            pg.extract_text(visitor_text=visit)
            for y, parts in lines.items():
                txt = " ".join("".join(parts).split()); rel = (y - base) / h; key = re.sub(r"\d+", "#", txt)
                if key and (rel > 0.93 or rel < 0.07):
                    seen.setdefault(("top" if rel > 0.5 else "bottom", key), []).append((rel, txt))
    need = max(2, int(total * 0.4)); top = bot = 0.0; found = []
    for (where, key), hits in seen.items():
        if len(hits) >= need:
            found.append(f"{where}: {hits[0][1][:60]}")
            if where == "top": top = max(top, (1 - min(r for r, _ in hits)) * 100 + 1.5)
            else: bot = max(bot, max(r for r, _ in hits) * 100 + 1.5)
    return {"top": round(top, 1), "bottom": round(bot, 1), "found": sorted(set(found)), "pages": total}


def band(v, default=0.0, hi=40.0):
    try: x = float(v) if v not in (None, "") else float(default)
    except (TypeError, ValueError): x = float(default)
    return max(0.0, min(hi, x))


def build(items, title, top=0.0, bottom=0.0, progress=None):   # an item may also carry "top1": extra % cut from the first kept page only
    w = PdfWriter(); every = 0; done = 0
    for it in items:
        f = FILES.get(it["id"])
        if f: every += len(parse_range(it.get("range", ""), f["pages"]))
    for it in items:
        f = FILES.get(it["id"])
        if not f:
            raise ValueError("A file in the list is no longer available; add it again.")
        r = PdfReader(f["path"])
        idx = parse_range(it.get("range", ""), len(r.pages))
        first = len(w.pages)
        for i in idx:
            w.add_page(r.pages[i])
            tp = band(it.get("top"), top)
            t1 = band(it.get("top1"), 0, 95.0) if i == idx[0] else 0
            b1 = band(it.get("bottom1"), 0, 95.0) if i == idx[-1] else 0   # "bottom1": extra % cut from the bottom of the last kept page only
            clear_bands(w.pages[-1], max(tp, t1), max(band(it.get("bottom"), bottom), b1))
            crop_top(w.pages[-1], t1); crop_bottom(w.pages[-1], b1)
            done += 1
            if progress: progress(done, every, f"Merging page {done} of {every}")
        if len(items) > 1 and idx:
            w.add_outline_item(Path(f["name"]).stem, first)
    if not len(w.pages):
        raise ValueError("Nothing to merge: add at least one PDF.")
    w.add_metadata({"/Title": title})
    return w


def bands(d):
    return (max(0.0, min(40.0, float(d.get("top") or 0))), max(0.0, min(40.0, float(d.get("bottom") or 0))))


def detect(d, progress=None):
    pp = []
    for it in d["items"]:
        f = FILES[it["id"]]
        pp.append((f["path"], parse_range(it.get("range", ""), f["pages"])))
    return detect_bands(pp, progress)


JOBS = {}
MERGED = {}


def start_job(fn):
    """Runs fn(progress) on a thread; the page polls /api/job for the percentage."""
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"pct": 0, "label": "Starting…", "done": False}
    def prog(n, total, label):
        JOBS[jid].update(pct=int(100 * n / max(total, 1)), label=label)
    def run():
        try:
            JOBS[jid]["result"] = fn(prog)
            JOBS[jid].update(pct=100, label="Done")
        except Exception as e:
            JOBS[jid]["error"] = str(e) or e.__class__.__name__
        JOBS[jid]["done"] = True
    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


PREV = []


def preview(d, progress):
    """The selected file as it will be merged (page range and header/footer removal applied), for the viewer."""
    w = build([d["item"]], "Preview", progress=progress)
    dest = WORK / f"pv-{uuid.uuid4().hex[:8]}.pdf"; w.write(str(dest)); PREV.append(dest)
    while len(PREV) > 4: PREV.pop(0).unlink(missing_ok=True)
    return {"url": f"/api/pdf?path={dest.name}", "pages": len(w.pages)}


def merge(d, progress):
    title = d.get("title", "").strip()
    if not title:
        raise ValueError("Give the file a title first: it's what the app will call it.")
    w = build(d["items"], title, *bands(d), progress=progress)
    progress(99, 100, "Saving the merged file…")
    mid = uuid.uuid4().hex[:8]; dest = WORK / f"merged-{mid}.pdf"
    w.write(str(dest)); MERGED[mid] = dest
    return {"merged": mid, "name": f"{safe(title)}.pdf", "pages": len(w.pages), "mb": (f"{dest.stat().st_size // 1024} KB" if dest.stat().st_size < 1048576 else f"{dest.stat().st_size / 1048576:.1f} MB")}


def github_put(cfg, repo_path, data, message):
    """Create or replace one file in the repository through GitHub's contents API."""
    url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/" + "/".join(urllib.request.quote(p) for p in repo_path.split("/"))
    head = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json", "User-Agent": "rulesforge-pdfstudio"}
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{url}?ref={cfg['branch']}", headers=head)) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404: raise RuntimeError(gh_error(e))
    body = {"message": message, "content": base64.b64encode(data).decode(), "branch": cfg["branch"]}
    if sha: body["sha"] = sha
    try:
        with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), {**head, "Content-Type": "application/json"}, method="PUT")) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(gh_error(e))


def gh_get(cfg, path):
    head = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json", "User-Agent": "rulesforge-pdfstudio"}
    url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/{path}?ref={cfg['branch']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=head)) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise RuntimeError(gh_error(e))


def remote_games(d):
    """The Game / Edition folders that already exist under packs/incoming in the repository."""
    cfg = {"repo": d.get("repo", "").strip(), "branch": d.get("branch", "").strip() or "game-packs", "token": d.get("token", "").strip() or load_config().get("token", "")}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg["repo"]) or not cfg["token"]:
        raise ValueError("Fill in the repository (owner/name) and a GitHub token first.")
    games = {}
    for g in gh_get(cfg, "packs/incoming") or []:
        if g.get("type") == "dir":
            games[g["name"]] = sorted(e["name"] for e in (gh_get(cfg, f"packs/incoming/{urllib.request.quote(g['name'])}") or []) if e.get("type") == "dir")
    return {"games": games}


def gh_error(e):
    try: msg = json.load(e).get("message", "")
    except Exception: msg = ""
    hint = {401: "GitHub didn't accept the token (wrong, expired, or revoked).", 403: "The token isn't allowed to write to this repository (it needs Contents: Read and write).",
            404: "GitHub can't find that repository or branch with this token."}.get(e.code, "")
    return f"GitHub said {e.code}: {msg} {hint}".strip()


TAG_RGB = {"toc": (112, 72, 232), "h1": (201, 42, 42), "h2": (194, 65, 12), "h3": (47, 122, 58), "h4": (11, 114, 133), "para": (24, 100, 171), "img": (166, 30, 77), "sec": (134, 142, 150), "grp": (12, 166, 120)}
TAG_NAME = {"toc": "Table of contents", "h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3", "h4": "Heading 4", "para": "Paragraph", "img": "Image", "sec": "Section", "grp": "Group"}


def compute_tags(items):
    """The tags of every kept page, with page numbers in the saved PDF (after page ranges and merging)."""
    tags, offset = [], 0
    for it in items or []:
        f = FILES.get(it.get("id", ""))
        if not f: continue
        kept = parse_range(it.get("range", ""), f["pages"])
        where = {orig: n for n, orig in enumerate(kept)}
        for m in it.get("marks") or []:
            if m.get("k") in TAG_RGB and m.get("p") in where: tags.append({**m, "p": offset + where[m["p"]]})
        offset += len(kept)
    return tags


def annotate_file(path, tags):
    """Write each tag into the PDF as a standard annotation: flags become note icons, boxes become square annotations.
    Each one's /Contents starts with RF:<tag> so RulesForge can tell them apart from other annotations."""
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject
    path = Path(path); tmp = path.with_suffix(".tagging.pdf")
    w = PdfWriter(clone_from=PdfReader(str(path)))
    for t in tags:
        mb = w.pages[t["p"]].cropbox
        L, B, W, H = float(mb.left), float(mb.bottom), float(mb.width), float(mb.height)
        r, g, b = (c / 255 for c in TAG_RGB[t["k"]])
        ann = DictionaryObject()
        ann[NameObject("/Type")] = NameObject("/Annot")
        x0 = L + t["x"] * W; top = B + H - t["y"] * H
        if "w" in t:
            ann[NameObject("/Subtype")] = NameObject("/Square")
            x1 = x0 + t["w"] * W; bot = top - t["h"] * H
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (x0, bot, x1, top)])
            bs = DictionaryObject(); bs[NameObject("/W")] = NumberObject(4); bs[NameObject("/S")] = NameObject("/S"); ann[NameObject("/BS")] = bs
        else:
            ann[NameObject("/Subtype")] = NameObject("/Text")
            ann[NameObject("/Name")] = NameObject("/Note"); ann[NameObject("/Open")] = NameObject("/false")
            ann[NameObject("/Rect")] = ArrayObject([FloatObject(v) for v in (x0, top - 18, x0 + 18, top)])
        ann[NameObject("/C")] = ArrayObject([FloatObject(r), FloatObject(g), FloatObject(b)])
        ann[NameObject("/F")] = NumberObject(4)
        ann[NameObject("/Contents")] = TextStringObject(f"RF:{t['k']}|{TAG_NAME[t['k']]}" + (f"|link={t['link']}" if t.get("link") else "") + (f"|role={t['role']}" if t.get("role") else ""))
        ann[NameObject("/T")] = TextStringObject("RulesForge")
        ann[NameObject("/NM")] = TextStringObject(str(t.get("id", "")))
        w.add_annotation(page_number=t["p"], annotation=ann)
    w.write(str(tmp))
    tmp.replace(path)


def save(d):
    title, game, edition, typ = (d.get(k, "").strip() for k in ("title", "game", "edition", "type"))
    if not title:
        raise ValueError("Give the file a title: it's what the app will call it.")
    pre = MERGED.get(d.get("merged", ""))
    tags = compute_tags(d.get("items", []))
    if d.get("mode") == "download":
        if pre and pre.exists() and not tags: return {"download": f"/api/pdf?path={pre.name}", "name": f"{safe(title)}.pdf"}
        dest = WORK / f"{safe(title)}.pdf"
        if pre and pre.exists(): shutil.copyfile(pre, dest)
        else: build(d["items"], title, *bands(d)).write(str(dest))
        if tags: annotate_file(dest, tags)
        return {"download": f"/api/pdf?path={dest.name}", "name": dest.name}
    if not game or not edition or typ not in TYPES:
        raise ValueError("Pick the Game, Edition and Type so it lands in the right Game Pack.")
    folder = OUT / safe(game) / safe(edition) / typ
    writer = None if pre and pre.exists() else build(d["items"], title, *bands(d))   # build first, so a bad page range leaves no empty folders behind
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{safe(title)}.pdf"
    if writer is None: shutil.copyfile(pre, dest)
    else: writer.write(str(dest))
    if tags: annotate_file(dest, tags)
    meta = {"title": title}
    if tags: meta["tags"] = tags
    if d.get("profile") in PROFILE_IDS: meta["profile"] = d["profile"]
    # A Sections file is named by what the volunteer types (e.g. "Kill Team"); that name is its column in the Game Pack.
    if typ == "Sections":
        meta["section"] = (d.get("section", "") or title).strip()
    if typ == "Factions" and d.get("faction", "").strip():
        meta["faction"] = d["faction"].strip()
    if typ == "Rules" and d.get("rulesKind") == "updates":
        meta["kind"] = "updates"
    for k in ("version", "description"):
        if d.get(k, "").strip():
            meta[k] = d[k].strip()
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    out = {"saved": str(dest), "pages": len(PdfReader(str(dest)).pages)}
    gh = d.get("github")
    if gh:
        cfg = {"repo": gh["repo"].strip(), "branch": gh["branch"].strip() or "game-packs", "token": gh["token"].strip() or load_config().get("token", "")}
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg["repo"]) or not cfg["token"]:
            raise ValueError("To upload, fill in the repository (owner/name) and a GitHub token.")
        if gh.get("remember"):
            CONFIG.write_text(json.dumps(cfg), "utf-8")
            try: CONFIG.chmod(0o600)
            except OSError: pass
        rel = dest.relative_to(OUT.parent.parent).as_posix() if OUT.name == "incoming" else f"packs/incoming/{dest.relative_to(OUT).as_posix()}"
        msg = f"Add {title} ({typ})"
        # The details file first, so it is already there when the PDF triggers the converter.
        github_put(cfg, rel[:-4] + ".json", dest.with_suffix(".json").read_bytes(), msg)
        github_put(cfg, rel, dest.read_bytes(), msg)
        out["uploaded"] = f"{cfg['repo']}@{cfg['branch']}:{rel}"
    return out


def existing():
    games = {}
    if OUT.is_dir():
        for g in sorted(p for p in OUT.iterdir() if p.is_dir()):
            games[g.name] = sorted(e.name for e in g.iterdir() if e.is_dir())
    return games


# ---- auto-quit: the page pings while it is open; when the tab is closed the program stops itself ----
if sys.stdout is None: sys.stdout = open(os.devnull, "w")   # pythonw has no console
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
SEEN = {"last": None, "bye": None, "start": __import__("time").time()}


def watch(srv):
    import time
    while True:
        time.sleep(1)
        now = time.time(); last, bye = SEEN["last"], SEEN["bye"]
        gone = (bye and now - bye > 8) or (last and now - last > 1800) or (last is None and now - SEEN["start"] > 90)
        if gone:
            srv.shutdown(); return


APP_ID = "pdf-studio"; VERSION = "52.1"
PROFILE_IDS = ("40k", "killteam", "aos")   # game styles: add the id here and in PROFILES in the page script (see GAME-TEMPLATES.md)
SRV = [None]


def claim_port(port):
    """If an older copy of THIS tool holds the port, ask it to quit and take its place. Never touches the other tool."""
    base = f"http://127.0.0.1:{port}"
    try: info = json.load(urllib.request.urlopen(base + "/api/id", timeout=1.5))
    except Exception: return "free"
    if not isinstance(info, dict) or info.get("app") != APP_ID: return "free"
    if info.get("version") == VERSION: return "same"
    try: urllib.request.urlopen(urllib.request.Request(base + "/api/quit", b"{}", {"X-RF-Quit": "1"}, method="POST"), timeout=2)
    except Exception: pass
    import time as _t
    for _ in range(30):
        _t.sleep(0.2)
        try: urllib.request.urlopen(base + "/api/id", timeout=0.5)
        except Exception: return "free"
    return "busy"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def local(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if self.path.split("?")[0] == "/api/id":
            return self.send(200, {"app": APP_ID, "version": VERSION})
        if self.path.split("?")[0] == "/api/ping":
            import time; SEEN["last"] = time.time(); SEEN["bye"] = None; return self.send(200, {"ok": True})
        if self.path.split("?")[0] == "/api/bye":
            import time; SEEN["bye"] = time.time(); return self.send(200, {"ok": True})
        u = urlparse(self.path)
        if not self.local(): return self.send(403, {"error": "local only"})
        if u.path == "/":
            return self.send(200, PAGE.replace("__TYPES__", json.dumps(TYPES)).replace("__VERSION__", VERSION).encode(), "text/html; charset=utf-8")
        if u.path == "/api/info":
            c = load_config()
            return self.send(200, {"out": str(OUT), "games": existing(), "browser": bool(find_browser()), "repo": c.get("repo", "Taptap91/40k-gameforge"), "branch": c.get("branch", "game-packs"), "hasToken": bool(c.get("token"))})
        if u.path == "/api/job":
            j = JOBS.get(parse_qs(u.query).get("id", [""])[0])
            return self.send(200 if j else 404, j or {"error": "unknown job"})
        if u.path.startswith("/vendor/"):
            name = Path(u.path[len("/vendor/"):]).name
            f = Path(__file__).resolve().parent / "web" / "vendor" / name
            if name in ("pdf.mjs", "pdf.worker.mjs") and f.exists(): return self.send(200, f.read_bytes(), "text/javascript; charset=utf-8")
            return self.send(404, {"error": "not found"})
        if u.path == "/api/source":
            f = FILES.get(parse_qs(u.query).get("id", [""])[0])
            if f and Path(f["path"]).exists():
                t1 = band(parse_qs(u.query).get("top1", ["0"])[0], 0, 95.0); b1 = band(parse_qs(u.query).get("bottom1", ["0"])[0], 0, 95.0)
                if t1 or b1:   # the same crop the saved PDF gets, so the tags line up with what is saved
                    import io
                    w = PdfWriter(clone_from=PdfReader(f["path"])); pg0, pgN = w.pages[0], w.pages[len(w.pages) - 1]
                    if t1: clear_bands(pg0, t1, 0)
                    if b1: clear_bands(pgN, 0, b1)
                    crop_top(pg0, t1); crop_bottom(pgN, b1)
                    buf = io.BytesIO(); w.write(buf); return self.send(200, buf.getvalue(), "application/pdf")
                return self.send(200, Path(f["path"]).read_bytes(), "application/pdf")
            return self.send(404, {"error": "not found"})
        if u.path == "/api/pdf":
            p = WORK / Path(parse_qs(u.query).get("path", [""])[0]).name
            if p.exists(): return self.send(200, p.read_bytes(), "application/pdf")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.local(): return self.send(403, {"error": "local only"})
        if self.path.split("?")[0] == "/api/bye":
            # the page sends this beacon (a POST) when its tab/window closes; watch() then stops the program
            import time; SEEN["bye"] = time.time(); return self.send(200, {"ok": True})
        if self.path == "/api/quit":
            if self.headers.get("X-RF-Quit") != "1": return self.send(403, {"error": "no"})
            self.send(200, {"ok": True}); threading.Timer(0.3, lambda: SRV[0] and SRV[0].shutdown()).start(); return
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        try:
            if u.path == "/api/upload":
                name = safe(parse_qs(u.query).get("name", ["file.pdf"])[0])
                p = WORK / f"{uuid.uuid4().hex[:8]}.pdf"; p.write_bytes(raw)
                return self.send(200, register(p, name))
            d = json.loads(raw or b"{}")
            if u.path == "/api/capture": return self.send(200, capture(d.get("url", "").strip()))
            if u.path == "/api/page/open": return self.send(200, start_job(lambda p: open_page(d, p)))
            if u.path == "/api/page/set": return self.send(200, page_set(d))
            if u.path == "/api/page/preview": return self.send(200, live_preview(d))
            if u.path == "/api/page/shot": return self.send(200, page_shot(d))
            if u.path == "/api/page/size": return self.send(200, page_size(d))
            if u.path == "/api/page/controls": return self.send(200, page_controls(d))
            if u.path == "/api/page/click": return self.send(200, page_click(d))
            if u.path == "/api/page/reveal": return self.send(200, page_reveal(d))
            if u.path == "/api/page/theme": return self.send(200, page_theme(d))
            if u.path == "/api/preview": return self.send(200, start_job(lambda p: preview(d, p)))
            if u.path == "/api/page/print": return self.send(200, start_job(lambda p: print_page(d, p)))
            if u.path == "/api/page/cancel": close_session(d.get("sid", "")); return self.send(200, {"ok": True})
            if u.path == "/api/save": return self.send(200, save(d))
            if u.path == "/api/remote": return self.send(200, remote_games(d))
            if u.path == "/api/detect": return self.send(200, start_job(lambda p: detect(d, p)))
            if u.path == "/api/merge": return self.send(200, start_job(lambda p: merge(d, p)))
        except Exception as e:
            return self.send(400, {"error": str(e) or e.__class__.__name__})
        self.send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RulesForge PDF Studio v__VERSION__</title><style>
:root{color-scheme:dark;--bg:#1c1c1e;--c:#2a2a2d;--b:#3a3a3e;--bd:#6e6e76;--c2:#34343a;--t:#f2f2f4;--m:#a4a4ac;--a:#0a84ff;--ai:#0b0b0d;--err:#ff8a80;--ok:#5ddc8b;--warn:#ffb340;--main:#0e0e10;--frame:#2b2b2e}
html.light{color-scheme:light;--bg:#f5f5f7;--c:#ffffff;--b:#d2d2d7;--bd:#76767c;--c2:#e5e5ea;--t:#1c1c1e;--m:#5a5a60;--a:#0b5cc7;--ai:#ffffff;--err:#b42318;--ok:#1e7a3e;--warn:#8a4b00;--main:#e5e5ea;--frame:#d1d1d6}
.top{display:flex;align-items:center;gap:10px}.top h1{flex:1;min-width:0}
#tsw{width:92px;flex:none;margin:0;padding:2px}#tsw button{padding:3px 0;font-size:15px;line-height:1.2;color:var(--t)}#tsw i{background:var(--c2);border:1px solid var(--bd)}#tsw button.on{font-weight:700}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.4 system-ui,sans-serif;display:grid;grid-template-columns:minmax(330px,25%) 1fr;overflow:hidden}
@media(max-width:760px){body{grid-template-columns:1fr;grid-template-rows:auto 55vh;overflow:auto}aside{overflow:visible}.stepwrap{min-height:420px}}
aside{display:flex;flex-direction:column;overflow:hidden;min-height:0;border-right:1px solid var(--b);background:var(--c);padding:0 12px 12px}.stepwrap{flex:1;min-height:0;overflow:hidden}.track{display:flex;width:500%;height:100%;transition:transform .35s ease}.step{flex:0 0 20%;width:20%;min-width:0;height:100%;overflow-y:auto;padding:4px 4px 16px}.step h2{font-size:16px;font-weight:600;margin:8px 0 10px}.step h3{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--m);font-weight:600;margin:16px 0 4px}.nav{display:flex;gap:8px;padding:10px 0 0;border-top:1px solid var(--b)}.nav button{flex:1;padding:10px;font-size:15px;font-weight:600;min-height:44px}.nav button.dis{background:var(--c2);border-color:var(--bd);color:var(--m);opacity:1}.ind{display:flex;gap:6px;justify-content:center;padding:10px 0 2px}.ind span{width:34px;height:34px;border-radius:8px;border:1px solid var(--bd);display:grid;place-items:center;font-weight:600;color:var(--m);background:var(--bg);font-size:14px}.ind span.on{background:var(--a);border-color:var(--a);color:var(--ai)}#pop{position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;z-index:50;padding:16px}#pop[hidden]{display:none}#pop .popbox{background:var(--c);border:1px solid var(--b);border-radius:12px;padding:18px;max-width:360px;width:100%}#pop p{margin:0 0 14px;font-size:15px;line-height:1.5}#pop .row2{justify-content:flex-end}#pop button{min-height:40px;padding:8px 16px;font-size:14px;font-weight:600}
h1{margin:0;font-size:17px;padding:12px 0 2px}h1 small{display:block;color:var(--m);font-weight:400;font-size:11px}
#stat{position:sticky;top:0;z-index:5;background:var(--c);padding:8px 0;border-bottom:1px solid var(--b);margin-bottom:6px}
#msg{min-height:20px;font-size:13px}.err{color:var(--err)}.ok{color:var(--ok)}
details.sec{border-bottom:1px solid var(--b);padding:6px 0}details.sec>summary{cursor:pointer;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--m);padding:6px 0;font-weight:600}
.drop{border:2px dashed var(--bd);border-radius:10px;padding:14px;text-align:center;color:var(--m);cursor:pointer;font-size:13px}.drop.on{border-color:var(--a);color:var(--t)}
input,select,textarea,button{font:inherit;color:var(--t);background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:6px 8px}input,select,textarea{width:100%}button{cursor:pointer}button.p{background:var(--a);border-color:var(--a);color:var(--ai);font-weight:600}button:disabled{opacity:.5;cursor:default}
label{display:block;margin:8px 0 2px;color:var(--m);font-size:12px}.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.hint{color:var(--m);font-size:12px;margin-top:3px}
.cap{display:flex;gap:6px;margin-top:8px}.cap input{flex:1;min-width:0}
.row{display:flex;gap:4px;align-items:center;background:var(--bg);border:2px solid var(--b);border-radius:8px;padding:4px 6px;margin-top:6px;cursor:pointer}.row.on{border-color:var(--a)}
.row .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}.row small{color:var(--m)}.row input{width:64px;padding:2px 4px;font-size:12px}.row button{padding:1px 6px}
.bar{height:10px;border-radius:8px;background:var(--bg);border:1px solid var(--b);overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;width:0;background:var(--a);transition:width .2s}
.sw{position:relative;display:grid;grid-template-columns:1fr 1fr;margin-top:10px;background:var(--bg);border:1px solid var(--b);border-radius:999px;padding:3px;isolation:isolate}.sw i{position:absolute;z-index:-1;top:3px;bottom:3px;left:3px;width:calc(50% - 3px);border-radius:999px;background:var(--a);transition:transform .25s cubic-bezier(.3,1.3,.5,1)}.sw[data-v=up] i{transform:translateX(100%)}.sw button{border:0;background:transparent;border-radius:999px;font-weight:600;font-size:13px}
.pgp{border:1px solid var(--b);border-radius:10px;padding:8px;margin-top:8px}.sw2{display:inline-flex;border:1px solid var(--b);border-radius:999px;padding:2px;margin:6px 0;background:var(--bg)}.sw2 button{border:0;background:none;padding:5px 14px;border-radius:999px;font-size:12px;cursor:pointer;color:inherit}.sw2 button.on{background:var(--a);color:var(--ai)}.ci{display:flex;align-items:center;gap:6px;margin:3px 0}.ci label{flex:1;margin:0}.ci .go{padding:3px 8px;font-size:12px;white-space:nowrap}.row2{display:flex;gap:5px;flex-wrap:wrap;margin:6px 0}.row2 button{padding:4px 8px;font-size:12px}
.thmrow{margin:10px 0}.thml{font-size:12px;color:var(--m);margin-bottom:6px}.swrow{display:flex;align-items:center;gap:8px;font-size:13px}.tgl{position:relative;width:46px;height:26px;border-radius:999px;padding:0;background:var(--bg);border:1px solid var(--bd);flex:none}.tgl i{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:var(--m);transition:transform .2s}.tgl[aria-checked=true]{background:var(--a);border-color:var(--a)}.tgl[aria-checked=true] i{transform:translateX(20px);background:var(--ai)}
#tagview{flex:1;overflow:auto;background:var(--frame);padding:12px 0}
.pg{position:relative;margin:0 auto 14px;background:#fff;border:1px solid var(--bd)}.pg canvas{display:block}.pg.arm{cursor:crosshair}.ov{position:absolute;inset:0}
.pno{position:absolute;right:6px;bottom:4px;background:rgba(0,0,0,.65);color:#fff;font-size:11px;padding:2px 6px;border-radius:4px;pointer-events:none}
.tagrow{display:flex;gap:10px;align-items:center;margin:4px 0;position:relative}.tagbtn{flex:none;min-width:132px;min-height:32px;font-weight:600;font-size:14px;text-align:left;padding:4px 12px;border:2px solid var(--c);background:var(--bg);color:var(--t);border-radius:8px}.tagbtn.on{background:var(--c);color:#fff}
.tip{flex:none;width:22px;height:22px;border:1.5px solid var(--m);border-radius:50%;color:var(--m);font-size:13px;font-weight:700;line-height:19px;text-align:center;cursor:help;position:relative;user-select:none}.tip:hover,.tip:focus{color:var(--t);border-color:var(--t);outline:none}
.tagx{display:none;position:absolute;left:-150px;top:28px;width:260px;max-width:70vw;z-index:30;font-size:13px;font-weight:400;color:var(--t);background:var(--bg);border:1px solid var(--m);border-radius:8px;padding:8px 10px;line-height:1.45;text-align:left;box-shadow:0 4px 14px rgba(0,0,0,.35)}.tip:hover .tagx,.tip:focus .tagx{display:block}
.flag{position:absolute;transform:translate(-1px,-100%);display:flex;flex-direction:column;align-items:flex-start;cursor:move}.flag i{display:block;width:2px;height:22px;background:var(--c)}
.flag span{background:var(--c);color:#fff;font:600 12px/1 system-ui,sans-serif;padding:4px 7px;border-radius:4px 4px 4px 0;white-space:nowrap}
.flag.sel span{outline:2px solid var(--a);outline-offset:2px}
.box{position:absolute;border:4px solid var(--c);background:transparent;box-sizing:border-box;cursor:move}
.box.sel{outline:2px dashed var(--c);outline-offset:3px}
.box .lbl{position:absolute;left:-4px;top:-24px;background:var(--c);color:#fff;font:600 11px/1 system-ui,sans-serif;padding:4px 6px;border-radius:4px;white-space:nowrap}
.rh{position:absolute;right:-8px;bottom:-8px;width:14px;height:14px;background:var(--c);border:2px solid #fff;border-radius:3px;cursor:nwse-resize}
.box.draft{border-style:dashed;pointer-events:none}
#ctl{max-height:220px;overflow:auto;margin:4px 0}#ctl label{display:flex;gap:6px;align-items:center;padding:2px;font-size:12px;margin:0;color:var(--t)}#ctl input{width:auto}#ctl small{color:var(--m)}
#pdl,#pup{margin-top:10px}[hidden]{display:none!important}
main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--main)}#vh{display:flex;gap:10px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--b);background:var(--c);font-size:13px}#vt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#vtabs button{padding:3px 10px;font-size:12px}#vtabs button.on{background:var(--a);border-color:var(--a);color:var(--ai)}#liveview{flex:1;overflow:auto;background:#fff}#liveview .ck{width:100%;background:#f1f1f1}#liveview img{display:block;width:100%;cursor:pointer}#busy{color:var(--warn)}#view{flex:1;border:0;width:100%;background:var(--frame)}#empty{flex:1;display:grid;place-items:center;color:var(--m);text-align:center;padding:30px}
</style>
<aside>
<div class="top"><h1>PDF Studio <small>v__VERSION__ · five steps</small></h1>
<div class="sw" id="tsw" data-v="up" role="group" aria-label="Colour theme"><i></i><button data-th="light" aria-label="Day mode" title="Day mode" aria-pressed="false">☀</button><button data-th="dark" aria-label="Night mode" title="Night mode" aria-pressed="true">☾</button></div></div>
<div id="stat"><div id="msg"></div><div class="bar" id="bar" hidden><i id="barf"></i></div><div class="hint" id="barl"></div></div>
<div class="stepwrap"><div class="track" id="track">

<section class="step" data-step="1"><h2>1 · Add a PDF or web page</h2>
<div class="drop" id="drop">Drop PDFs here, or click to choose<input id="pick" type="file" accept="application/pdf" multiple hidden></div>
<div id="urls"><div class="cap"><input class="urlin" id="url" placeholder="…or paste a web page address"><button id="grab">Open</button></div></div>
<div class="cap"><button id="addurl" style="flex:none">+ Add another address</button><div class="hint" id="urln" style="margin:0;align-self:center">Up to 8. Each is opened one after another.</div></div>
<label style="display:flex;gap:6px;align-items:center;margin-top:6px"><input type="checkbox" id="adb" checked style="width:auto"> Block ads and trackers on web pages</label>
<div class="hint" id="nb"></div>
</section>

<section class="step" data-step="2"><h2>2 · Choose what to keep and save</h2>
<div id="pgp" hidden class="pgp"><div class="hint" id="pgt"></div>
<div class="sw2" id="sa" title="Opens or closes every show/hide section on the page"><button data-sa="show" class="on">Show All</button><button data-sa="hide">Hide All</button></div>
<div class="thmrow"><div class="thml">Page colours for the viewer and the saved PDF</div><div class="swrow"><span>Light</span><button type="button" id="thms" class="tgl" role="switch" aria-checked="false" aria-label="Dark page colours"><i></i></button><span>Dark</span></div><input type="hidden" id="thm" value="light"></div>
<div id="ctl"></div>
<div class="row2"><button class="p" id="psave">Save this page as a PDF</button><button id="pcan">Cancel</button></div>
<div class="hint">The preview on the right updates after each change. Saving keeps exactly what you see.</div></div>
<h3>Files</h3>
<div id="list"></div><div class="hint" id="lh">Click a file to view it. Use ↑ ↓ to reorder. Pages is optional: <b>1-5, 8, 12-</b> keeps just those pages.</div>
<h3>Headers and footers</h3>
<div class="hint" id="sf">Click a file in the list first.</div>
<div class="two"><div><label>Top %</label><input id="top" type="number" min="0" max="40" step="0.5" placeholder="0" disabled></div><div><label>Bottom %</label><input id="bot" type="number" min="0" max="40" step="0.5" placeholder="0" disabled></div></div>
<div class="two"><div><label>First page: cut from the top, % of the page</label><input id="top1" type="number" min="0" max="95" step="0.5" placeholder="0" disabled></div><div><label>Last page: cut from the bottom, % of the page</label><input id="bottom1" type="number" min="0" max="95" step="0.5" placeholder="0" disabled></div></div>
<div class="two"><div><button id="gotop" disabled>Jump to the top of the PDF</button></div><div><button id="gobot" disabled>Jump to the bottom of the PDF</button></div></div>
<div class="row2"><button id="allb" disabled>Use for all files</button></div><div class="hint" id="detout"></div>
<div class="hint">Nothing is cut unless you type a number here, and these boxes do not affect tagging. The text inside a band you set is deleted from the PDF, not just covered. The viewer shows the result so you can check it, then adjust the numbers if real text is too close to the edge.</div>
</section>

<section class="step" data-step="3"><h2>3 · Tag headings, paragraphs and images</h2>
<div class="cap" id="zoombar" style="align-items:center;margin:4px 0 8px"><span style="font-size:12px">Zoom</span><button id="zout" aria-label="Zoom out" style="flex:none;width:32px">−</button><span style="display:inline-flex;align-items:center;gap:2px"><input id="zpct" value="100" inputmode="numeric" aria-label="Zoom percent" style="width:56px;text-align:center"><span>%</span></span><button id="zin" aria-label="Zoom in" style="flex:none;width:32px">+</button></div><label for="tgsel">File to tag</label><select id="tgsel"><option>Add a PDF on step 1 first</option></select>
<label for="prof">Game style</label><select id="prof"></select><div class="hint" id="profh"></div>
<button class="p" id="tagall" style="width:100%;margin:6px 0 4px">Tag All</button><div class="hint">Suggests tags from the headings, text, images and Contents box. Check them afterwards.</div>
<div class="tagrow"><button class="tagbtn" data-k="toc" style="--c:#7048e8">Table of contents</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Box the whole Contents box. The converter reads its columns from it.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="sec" style="--c:#868e96">Section</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Box a whole section, heading and all. Boxes inside it are its sub-sections. The converter reads the nesting from this.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="h1" style="--c:#c92a2a">H1</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Top-level headings (chapters). Click the heading to box its line, or drag a box around it. The title is not tagged; it is saved as the PDF title.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="h2" style="--c:#c2410c">H2</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Headings one level under H1 (rules, subsections). Click to box the line, or drag a box around the heading.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="h3" style="--c:#2f7a3a">H3</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Bold lead-ins one level under H2 (steps, numbered callouts). Click to box a line, or drag a box around exactly the text that should be bold.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="para" style="--c:#1864ab">Paragraph</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Box each block of body text.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="grp" style="--c:#0ca678">Group</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Box items that belong together (a question and its answer, a diagram and its labels). The converter reads everything inside as one unit and never mixes it with text outside.</div></span></div>
<div class="tagrow"><button class="tagbtn" data-k="img" style="--c:#a61e4d">Image</button><span class="tip" tabindex="0" role="button" aria-label="What this does">?<div class="tagx" role="tooltip">Box each image. It's cropped and saved as a PNG.</div></span></div>
<button id="tagreset" style="width:100%;margin-top:6px">Reset tags on this file</button>
<div class="hint">Click a tag to turn it on, then click a heading line or drag a box on the page. Click a mark to select it, drag to move it, drag the corner of a box to resize it, and press Delete to remove it. Press Escape to turn the tag off.</div>
</section>

<section class="step" data-step="4"><h2>4 · Merge (optional)</h2>
<div class="hint">Merging puts the files in the order shown on step 2. Skip it to keep each file separate.</div>
<button class="p" id="mg" style="width:100%;margin-top:8px">Merge the PDFs</button>
<div class="hint" id="mres">Merge first; then choose to download it or upload it.</div>
</section>

<section class="step" data-step="5"><h2>5 · Name and send</h2>
<div class="two"><div><label>Game</label><input id="game" list="games" placeholder="Warhammer 40K"><datalist id="games"></datalist></div>
<div><label>Edition</label><input id="ed" list="eds" placeholder="11th Edition"><datalist id="eds"></datalist></div></div>
<label>Type: where it shows in the Game Pack</label><select id="type"></select>
<label>Title: what the app calls it</label><input id="title" placeholder="Codex: Space Marines">
<div id="facw"><label>Faction (for an army)</label><input id="fac" placeholder="Space Marines"></div>
<div id="secw" hidden><label>Section name: its own column in the Game Pack (blank uses the title)</label><input id="sec" placeholder="Kill Team"></div>
<div id="kw" hidden><label>Rules kind</label><select id="rk"><option value="core">Core Rules</option><option value="updates">Update / FAQ / errata</option></select></div>
<div class="two"><div><label>Version (optional)</label><input id="ver" placeholder="2026-11"></div><div><label>Note (optional)</label><input id="desc"></div></div>
<h3>Send</h3>
<div class="sw" id="sw" data-v="down"><i></i><button data-s="down">Download</button><button data-s="up">Upload to GitHub</button></div>
<div id="pdl"><button class="p" id="dl" style="width:100%" disabled>Download PDF</button></div>
<div id="pup" hidden>
<input type="checkbox" id="up" checked hidden>
<div class="two"><div><label>Repository</label><input id="repo" value="Taptap91/40k-gameforge"></div><div><label>Branch</label><input id="br" value="game-packs"></div></div>
<label>Token <span id="tkn"></span></label><input id="tok" type="password" autocomplete="off" placeholder="GitHub token with Contents: read and write">
<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="rem" style="width:auto"> Remember on this computer</label>
<div style="margin-top:6px"><button id="load">Load folders from GitHub</button></div>
<label>Destination <small>(only folders under packs/incoming)</small></label><select id="dest" disabled><option>Load the folders first</option></select>
<div class="hint" id="path"></div>
<div class="hint">Needs a fine-grained token limited to this one repository (GitHub › Settings › Developer settings › Fine-grained tokens › Contents: Read and write). The token only goes to github.com.</div>
<button class="p" id="save" style="width:100%;margin-top:10px" disabled>Upload to GitHub</button></div>
<div class="hint" id="out"></div>
</section>

</div></div>
<div class="nav"><button id="back">Back</button><button class="p" id="next">Next</button><button id="reset" hidden>Reset</button></div>
<div class="ind" id="ind" aria-label="Steps"><span data-i="1">1</span><span data-i="2">2</span><span data-i="3">3</span><span data-i="4">4</span><span data-i="5">5</span></div>
</aside>
<div id="pop" hidden role="dialog" aria-modal="true" aria-labelledby="popt"><div class="popbox"><p id="popt"></p><div class="row2"><button id="popcancel" hidden>Cancel</button><button class="p" id="popok">Okay</button></div></div></div>
<main><div id="vh"><span id="vt">Nothing to show yet</span><span id="vtabs" hidden><button data-vt="live" class="on">Interactive page</button><button data-vt="pdf">PDF preview</button></span><span id="busy" hidden>Updating…</span></div>
<div id="tagview" hidden></div><div id="empty">Add a PDF, or paste a web page address.<br>It will appear here so you can check it.</div><div id="liveview" hidden></div><iframe id="view" hidden></iframe></main>
<script>
setInterval(()=>fetch("/api/ping").catch(()=>{}),3000);fetch("/api/ping").catch(()=>{});addEventListener("pagehide",()=>navigator.sendBeacon&&navigator.sendBeacon("/api/bye"));
var step=1;const TYPES=__TYPES__,$=id=>document.getElementById(id);let items=[],sel=-1,info={games:{}},pg=null,merged=null,sig="",remote=null;
$("type").innerHTML=Object.entries(TYPES).map(([k,v])=>`<option value="${k}">${k} · ${v}</option>`).join("");
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const msg=(t,c)=>{$("msg").innerHTML=`<span class="${c||""}">${t}</span>`};
const api=async(p,b)=>{const r=await fetch(p,{method:"POST",body:b});const j=await r.json();if(!r.ok)throw Error(j.error);return j};
const prog=(on,pct,label)=>{$("bar").hidden=!on;$("barf").style.width=pct+"%";$("barl").textContent=on?`${pct}%  ${label||""}`:""};
async function job(path,body){const {job:id}=await api(path,body);for(;;){const j=await (await fetch("/api/job?id="+id)).json();prog(true,j.pct,j.label);if(j.done){if(j.error)throw Error(j.error);return j.result}await new Promise(r=>setTimeout(r,250))}}
let viewBase="",viewPages=0;
function show(url,label){viewBase=url;$("empty").hidden=true;$("liveview").hidden=true;$("view").hidden=false;$("view").src=url+"#toolbar=1&navpanes=0&view=FitH";$("vt").textContent=label}
function blank(t){$("liveview").hidden=true;$("vtabs").hidden=true;$("view").hidden=true;$("view").removeAttribute("src");$("empty").hidden=false;$("empty").innerHTML=t;$("vt").textContent="Nothing to show yet"}

/* ---- files list and per-file headers/footers ---- */
function draw(){updateNav();$("list").innerHTML=items.map((f,i)=>`<div class="row ${i===sel?"on":""}" data-s="${i}"><span class="n" data-s="${i}">${esc(f.name)} <small>${f.pages}p${f.top||f.bottom?` · −${f.top||0}%/−${f.bottom||0}%`:""}</small></span><input data-r="${i}" value="${esc(f.range||"")}" placeholder="all pages"><button data-u="${i}">↑</button><button data-d="${i}">↓</button><button data-x="${i}">✕</button></div>`).join("");setTimeout(gate,0);
 const f=items[sel];$("top").disabled=$("bot").disabled=$("top1").disabled=$("bottom1").disabled=$("gotop").disabled=$("gobot").disabled=!f;$("top1").value=f?f.top1||"":"";$("bottom1").value=f?f.bottom1||"":"";$("allb").disabled=!f;$("top").value=f?f.top||"":"";$("bot").value=f?f.bottom||"":"";$("sf").textContent=f?`For: ${f.name}`:"Click a file in the list first."}
$("list").onclick=e=>{const g=k=>e.target.dataset[k];
 if(g("x")!=null){const i=+g("x");items.splice(i,1);sel=Math.min(sel>i?sel-1:sel,items.length-1);draw();items.length?(sel>=0&&preview()):blank("Add a PDF, or paste a web page address.");return}
 if(g("u")>0){const i=+g("u");[items[i-1],items[i]]=[items[i],items[i-1]];if(sel===i)sel--;else if(sel===i-1)sel++;draw();return}
 if(g("d")!=null){const i=+g("d");if(i<items.length-1){[items[i+1],items[i]]=[items[i],items[i+1]];if(sel===i)sel++;else if(sel===i+1)sel--;draw()}return}
 if(g("s")!=null&&e.target.tagName!=="INPUT"){choose(+g("s"))}};
$("list").oninput=e=>{if(e.target.dataset.r!=null){items[e.target.dataset.r].range=e.target.value;if(+e.target.dataset.r===sel)later()}};
function choose(i){if(pg)return msg("Save or cancel the open web page first.","err");sel=i;draw();preview()}
let pt=null;const later=()=>{clearTimeout(pt);pt=setTimeout(preview,700)};
async function preview(){const f=items[sel];if(!f||pg)return;$("busy").hidden=false;try{const r=await job("/api/preview",JSON.stringify({item:{id:f.id,range:f.range||"",top:f.top||0,top1:f.top1||0,bottom1:f.bottom1||0,bottom:f.bottom||0}}));if(items[sel]===f){viewPages=r.pages;show(r.url,`${f.name} · ${r.pages} pages${f.top||f.bottom?" · headers/footers removed":""}`)}}catch(e){msg(e.message,"err")}prog(false);$("busy").hidden=true}
$("top").oninput=$("bot").oninput=$("top1").oninput=$("bottom1").oninput=()=>{const f=items[sel];if(!f)return;f.top=+$("top").value||0;f.bottom=+$("bot").value||0;f.top1=+$("top1").value||0;f.bottom1=+$("bottom1").value||0;later();gate()};
// the viewer is the browser's own PDF viewer: jumping is done by opening the preview at its first page or at its last page (whole page in view)
async function jumpView(last){const f=items[sel];if(!f||pg)return;await preview();if(!viewBase||!viewPages)return;$("view").src=viewBase+`#toolbar=1&navpanes=0&page=${last?viewPages:1}&view=${last?"Fit":"FitH"}`}
$("gotop").onclick=()=>jumpView(false);$("gobot").onclick=()=>jumpView(true);
$("allb").onclick=()=>{const f=items[sel];if(!f)return;items.forEach(x=>{x.top=f.top||0;x.bottom=f.bottom||0});draw();msg("Same header/footer sizes set for every file.","ok");gate()};
async function add(files){for(const f of files){if(!/pdf$/i.test(f.name)){msg(`${f.name} isn't a PDF`,"err");continue}msg(`Reading ${f.name}…`);try{items.push({...await api("/api/upload?name="+encodeURIComponent(f.name),f),top:0,bottom:0});sel=items.length-1}catch(e){msg(e.message,"err")}}draw();msg("");if(!pg)preview()}
$("drop").onclick=()=>$("pick").click();$("pick").onchange=e=>add(e.target.files);
$("drop").ondragover=e=>{e.preventDefault();$("drop").classList.add("on")};$("drop").ondragleave=()=>$("drop").classList.remove("on");
$("drop").ondrop=e=>{e.preventDefault();$("drop").classList.remove("on");add(e.dataTransfer.files)};

/* ---- opening a web page: live preview, tick/untick, then save as a PDF ---- */
function drawCtl(){const rows=pg.controls.filter(c=>c.type!=="button").map(c=>`<div class="ci"><label><input type="checkbox" data-c="${c.i}" ${c.state?"checked":""}> ${esc(c.label)} <small>${c.type==="checkbox"?"checkbox":"section"}</small></label><button class="go" data-go="${c.i}" title="Scroll the page to this item">Go to ⤵</button></div>`).join("");$("ctl").innerHTML=rows||"<small>No checkboxes or show/hide sections found on this page.</small>"}
let rf=false,again=false,vmode="live",chunks=[],showAll=true;
let shotGen=0,CH=1200,pgH=0;
async function shot(focusY,scrollPx){const gen=++shotGen,lv=$("liveview");
 const sz=await api("/api/page/size",JSON.stringify({sid:pg.sid}));if(gen!==shotGen)return;CH=sz.chunk;const n=Math.ceil(sz.height/CH);
 if(lv.children.length!==n||pgH!==sz.height){const st=lv.scrollTop;lv.innerHTML=Array.from({length:n},(_,i)=>{const h=Math.min(CH,sz.height-i*CH);return`<div class="ck" data-ci="${i}" data-y="${i*CH}" data-h="${h}" style="aspect-ratio:1280/${h}"><img draggable="false" hidden></div>`}).join("");lv.scrollTop=st;pgH=sz.height}
 if(scrollPx!=null)lv.scrollTop=scrollPx;
 $("empty").hidden=true;$("view").hidden=true;lv.hidden=false;$("vt").textContent="Click things on the page to show or hide them: "+(pg.title||pg.url);
 const k=lv.clientWidth/1280||1,focus=focusY!=null?focusY:(lv.scrollTop+lv.clientHeight/2)/k;
 const order=[...Array(n).keys()].sort((a,b)=>Math.abs(a*CH+CH/2-focus)-Math.abs(b*CH+CH/2-focus));
 for(const i of order){if(gen!==shotGen)return;const c=lv.children[i];try{const r=await api("/api/page/shot",JSON.stringify({sid:pg.sid,y:+c.dataset.y,h:+c.dataset.h}));if(gen!==shotGen)return;const im=c.firstChild;im.src=r.src;im.hidden=false}catch(e){if(gen===shotGen)msg(e.message,"err");return}}
 $("busy").hidden=true}
async function refresh(){if(!pg)return;if(vmode==="live"){$("busy").hidden=false;try{await shot()}catch(e){msg(e.message,"err")}return}
 if(rf){again=true;return}rf=true;$("busy").hidden=false;try{do{again=false;const r=await api("/api/page/preview",JSON.stringify({sid:pg.sid}));if(pg)show(r.url,"PDF preview of "+(pg.title||pg.url)+" (this is what will be saved)")}while(again&&pg)}catch(e){msg(e.message,"err")}rf=false;$("busy").hidden=true}
$("vtabs").onclick=e=>{const v=e.target.dataset.vt;if(!v||!pg)return;vmode=v;[...$("vtabs").children].forEach(b=>b.classList.toggle("on",b.dataset.vt===v));refresh()};
$("liveview").onclick=async e=>{const c=e.target.closest(".ck");if(!c||!pg)return;const r=c.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*1280,y=+c.dataset.y+(e.clientY-r.top)/r.height*+c.dataset.h;
 $("busy").hidden=false;c.style.outline="2px solid var(--warn)";setTimeout(()=>c.style.outline="",250);
 try{await api("/api/page/click",JSON.stringify({sid:pg.sid,x,y}));shot(y);api("/api/page/controls",JSON.stringify({sid:pg.sid})).then(j=>{if(pg){pg.controls=j.controls;drawCtl()}}).catch(()=>{})}catch(err){msg(err.message,"err");$("busy").hidden=true}};
async function setOps(ops){try{const r=await api("/api/page/set",JSON.stringify({sid:pg.sid,ops}));r.states.forEach(s=>{const c=pg.controls.find(x=>x.i===s.i);if(c){c.state=s.state;if(s.label)c.label=s.label}});drawCtl();refresh()}catch(e){msg(e.message,"err")}}
const URLMAX=8;
const urlIns=()=>[...document.querySelectorAll(".urlin")];
function addUrlRow(){const ins=urlIns();if(ins.length>=URLMAX)return;const d=document.createElement("div");d.className="cap";d.innerHTML='<input class="urlin" placeholder="…another web page address"><button>Open</button><button title="Remove this address" aria-label="Remove this address">✕</button>';
 const inp=d.querySelector("input"),[open,del]=d.querySelectorAll("button");open.onclick=()=>openPage(inp);inp.onkeydown=e=>{if(e.key==="Enter")openPage(inp)};del.onclick=()=>{d.remove();urlState()};$("urls").appendChild(d);urlState();inp.focus()}
function urlState(){$("addurl").disabled=urlIns().length>=URLMAX;$("urln").textContent=`${urlIns().length} of ${URLMAX} address boxes. Each is opened one after another.`}
$("addurl").onclick=addUrlRow;urlState();
let urlSrc=null;
async function openPage(inp){inp=inp&&inp.tagName==="INPUT"?inp:$("url");urlSrc=inp;const u=inp.value.trim();if(!u)return;if(pg)await cancelPage();$("grab").disabled=true;msg("Opening the page…");blank("Opening the page…");try{pg=await job("/api/page/open",JSON.stringify({url:u,adblock:$("adb").checked,theme:$("thm").value}));pg.url=u;$("pgt").textContent=`Opened: ${pg.title||u}. Click things on the page itself, or use the list below.`;drawCtl();$("pgp").hidden=false;vmode="live";$("vtabs").hidden=false;[...$("vtabs").children].forEach(b=>b.classList.toggle("on",b.dataset.vt==="live"));await shot();await applyAll(showAll);msg("Page ready. Click show/hide buttons right on the page, or use the list on the left, then save it.","ok")}catch(e){pg=null;msg(e.message,"err");items.length?preview():blank("Add a PDF, or paste a web page address.")}prog(false);$("grab").disabled=false;updateNav()}
$("grab").onclick=()=>openPage($("url"));$("url").onkeydown=e=>{if(e.key==="Enter")openPage($("url"))};
$("ctl").onchange=e=>{const i=e.target.dataset.c;if(i==null)return;const c=pg.controls.find(x=>x.i==i);setOps([{i:+i,kind:c.type,v:e.target.checked}])};
$("ctl").onclick=async e=>{const g=e.target.dataset.go;if(g==null||!pg)return;try{const r=await api("/api/page/reveal",JSON.stringify({sid:pg.sid,i:+g}));if(r.y==null){msg("That item is not on the page any more. Try Show All or reopen the page.","err");return}
 if(vmode!=="live"){vmode="live";[...$("vtabs").children].forEach(b=>b.classList.toggle("on",b.dataset.vt==="live"))}
 const k=$("liveview").clientWidth/1280||1;await shot(r.y,Math.max(0,r.y*k-60))}catch(err){msg(err.message,"err")}};
function setSwitch(){[...$("sa").children].forEach(b=>b.classList.toggle("on",(b.dataset.sa==="show")===showAll))}
async function applyAll(on){showAll=on;setSwitch();if(!pg)return;const ops=pg.controls.filter(c=>c.type!=="checkbox").map(c=>({i:c.i,kind:c.type,v:/history/i.test(c.label||"")?false:on}))   /* a History section always stays hidden, even with Show All */;if(ops.length)await setOps(ops)}
$("thm").onchange=async()=>{if(!pg)return;try{await api("/api/page/theme",JSON.stringify({sid:pg.sid,theme:$("thm").value}));refresh()}catch(e){msg(e.message,"err")}};
function setTheme(th){const light=th==="light";document.documentElement.classList.toggle("light",light);try{localStorage.setItem("rf-theme",light?"light":"dark")}catch(e){}const s=$("tsw");s.dataset.v=light?"down":"up";[...s.querySelectorAll("button")].forEach(b=>{const on=b.dataset.th===(light?"light":"dark");b.setAttribute("aria-pressed",on);b.classList.toggle("on",on)})}
$("tsw").onclick=e=>{const b=e.target.closest("button[data-th]");if(b)setTheme(b.dataset.th)};
let savedTheme="dark";try{savedTheme=localStorage.getItem("rf-theme")==="light"?"light":"dark"}catch(e){}setTheme(savedTheme);
$("thms").onclick=()=>{const dark=$("thm").value!=="dark";$("thm").value=dark?"dark":"light";$("thms").setAttribute("aria-checked",String(dark));if(pg)$("thm").onchange()};
$("sa").onclick=e=>{const v=e.target.dataset.sa;if(!v||!pg)return;applyAll(v==="show")};
async function cancelPage(){const s=pg&&pg.sid;pg=null;updateNav();$("vtabs").hidden=true;$("pgp").hidden=true;if(s)await api("/api/page/cancel",JSON.stringify({sid:s})).catch(()=>{});msg("");items.length?preview():blank("Add a PDF, or paste a web page address.")}
$("pcan").onclick=cancelPage;
$("psave").onclick=async()=>{$("psave").disabled=true;msg("Saving the page as a PDF…");try{const f=await job("/api/page/print",JSON.stringify({sid:pg.sid}));pg=null;$("vtabs").hidden=true;$("pgp").hidden=true;if(urlSrc){urlSrc.value="";if(urlSrc.id!=="url")urlSrc.parentElement.remove();urlState()}items.push({...f,top:0,top1:0,bottom:0});sel=items.length-1;draw();msg("Saved. Check the preview. Paste another address for the next page.","ok");prog(false);await preview();const nxt=urlIns().find(i=>i.value.trim());if(nxt)openPage(nxt)}catch(e){msg(e.message,"err")}prog(false);$("psave").disabled=false};

/* ---- naming and the Game Pack destination ---- */
function dests(){const o=[];for(const[g,es]of Object.entries(remote.games)){if(!es.length)o.push([g,""]);es.forEach(e=>o.push([g,e]))}return o}
$("load").onclick=async()=>{msg("Reading the repository…");try{remote=await api("/api/remote",JSON.stringify({repo:$("repo").value,branch:$("br").value,token:$("tok").value}));const o=dests();
$("dest").innerHTML=o.map(([g,e],i)=>`<option value="${i}">${g}${e?" / "+e:" (no edition yet)"}</option>`).join("")+`<option value="new">＋ New Game Pack…</option>`;$("dest").disabled=false;$("up").checked=true;pickDest();msg("Folders loaded.","ok")}catch(e){msg(e.message,"err")}};
function paint(){$("path").textContent=$("dest").value==="new"?"A new Game Pack: type its Game and Edition above. The folders are created when you upload.":"Uploads to packs/incoming/"+$("game").value+"/"+($("ed").value||"…")+"/"+$("type").value+"/"}
function pickDest(){const v=$("dest").value,fresh=v==="new";$("game").readOnly=!fresh;$("ed").readOnly=!fresh;
if(!fresh){const[g,e]=dests()[+v];$("game").value=g;$("ed").value=e;$("ed").readOnly=!!e}else{$("game").value=$("ed").value="";$("game").focus()}paint()}
$("dest").onchange=pickDest;for(const id of["type","game","ed"])$(id).addEventListener("input",()=>{if(remote)paint()});$("type").addEventListener("change",()=>{if(remote)paint()});
function sync(){const t=$("type").value;$("facw").hidden=t!=="Factions";$("secw").hidden=t!=="Sections";$("kw").hidden=t!=="Rules";$("eds").innerHTML=(info.games[$("game").value]||[]).map(e=>`<option value="${e}">`).join("")}
$("type").onchange=$("game").oninput=sync;
// Game styles. One entry per game: how Tag All behaves and what the app does with the result. See GAME-TEMPLATES.md for adding a game.
//   cards: true  -> Tag All finds that game's datacards and tags them as text parts (role tags) in place of pictures.
//   cards: false -> the ordinary tags only (the app reads that game's pages with its own parser, e.g. Warhammer 40,000 datasheets).
const PROFILES={
 "40k":{label:"Warhammer 40,000",cards:false,hint:"Warhammer 40,000: Tag All suggests headings, paragraphs and images. Datasheets are read by the app's own 40K parser."},
 /* AoS style held back until the converter side is finished: "aos":{label:"Age of Sigmar",cards:true,finder:"aos",hint:"Age of Sigmar: Tag All finds each ability card (its phase bar with the command point cost, the NAME: line, Declare / Effect, and the Keywords row) and tags those parts as text, so the app draws them as phase-coloured ability cards. Headings, paragraphs and images are tagged as usual."}, */
 "killteam":{label:"Kill Team",cards:true,finder:"killteam",hint:"Kill Team: Tag All finds each datacard (its NAME / ATK / HIT / DMG / WR table) and tags the title, flavour text, stats, weapons, abilities and keywords as text, instead of one picture. The app draws them as a Kill Team card."}
};
let tagProfile="40k";try{tagProfile=localStorage.getItem("rf-ps-profile")||"40k"}catch(e){}
if(!PROFILES[tagProfile])tagProfile="40k";
const form=m=>({profile:tagProfile,github:$("up").checked&&m==="folder"?{repo:$("repo").value,branch:$("br").value,token:$("tok").value,remember:$("rem").checked}:null,mode:m,items:items.map(i=>({id:i.id,range:i.range||"",top:i.top||0,top1:i.top1||0,bottom1:i.bottom1||0,bottom:i.bottom||0,marks:i.marks||[]})),game:$("game").value,edition:$("ed").value,type:$("type").value,title:$("title").value,faction:$("fac").value,section:$("sec").value,rulesKind:$("rk").value,version:$("ver").value,description:$("desc").value});

/* ---- merge, then download or upload ---- */
const signature=()=>JSON.stringify([items.map(i=>[i.id,i.range||"",i.top||0,i.top1||0,i.bottom1||0,i.bottom||0]),$("title").value]);
function mode(v){$("sw").dataset.v=v;$("pdl").hidden=v!=="down";$("pup").hidden=v!=="up"}
$("sw").onclick=e=>{const v=e.target.dataset.s;if(v)mode(v)};
/* A single PDF needs no merge: it is built straight into the save or download. Several PDFs need a merge first. */
const current=()=>merged&&sig===signature()?merged:null;
function gate(){const one=items.length===1,ok=one||!!current();$("save").disabled=$("dl").disabled=!(ok&&items.length);
if(!items.length)$("mres").textContent="Add a PDF first.";
else if(one)$("mres").textContent="One PDF, so no merge is needed: download it or upload it.";
else if(merged&&!current())$("mres").textContent="Something changed since the merge: merge again before downloading or uploading.";
else if(!current())$("mres").textContent="Merge first; then choose to download it or upload it."}
document.addEventListener("input",()=>setTimeout(gate,0));document.addEventListener("click",()=>setTimeout(gate,0));document.addEventListener("change",()=>setTimeout(gate,0));
$("mg").onclick=async()=>{if(!items.length)return msg("Add a PDF first.","err");$("mg").disabled=true;merged=null;gate();prog(true,0,"Starting…");try{const r=await job("/api/merge",JSON.stringify(form("download")));merged=r.merged;sig=signature();$("mres").textContent=`Merged ${r.pages} pages (${r.mb}). Now download it or upload it.`;msg("Merge finished.","ok");gate();show(`/api/pdf?path=merged-${r.merged}.pdf`,`Merged: ${r.name} · ${r.pages} pages`)}catch(e){msg(e.message,"err");prog(false)}$("mg").disabled=false};
$("save").onclick=async()=>{$("save").disabled=true;prog(true,100,"Uploading / saving…");try{const r=await api("/api/save",JSON.stringify({...form("folder"),merged:current()}));msg(r.uploaded?`Saved ${r.pages} pages and uploaded to ${r.uploaded}. The converter will pick it up in a minute or two.`:`Saved ${r.pages} pages to ${r.saved}. Commit and push it to GitHub; the pack converter does the rest.`,"ok")}catch(e){msg(e.message,"err")}prog(false);gate()};
$("dl").onclick=async()=>{try{const r=await api("/api/save",JSON.stringify({...form("download"),merged:current()}));const a=document.createElement("a");a.href=r.download;a.download=r.name;a.click();msg("Downloaded.","ok")}catch(e){msg(e.message,"err")}};
fetch("/api/info").then(r=>r.json()).then(j=>{info=j;$("repo").value=j.repo;$("br").value=j.branch;if(j.hasToken)$("tkn").textContent="(saved: leave blank to use it)";$("games").innerHTML=Object.keys(j.games).map(g=>`<option value="${g}">`).join("");$("out").textContent="Saves into: "+j.out;if(!j.browser)$("nb").textContent="Opening web pages needs Chrome or Edge installed; none was found.";sync()});sync();draw();

/* ---- five-step wizard ---- */
function gateMsg(n){
 if(n===1) return (items.length||pg) ? "" : "Drop in a PDF or open a web page first.";
 if(n===2){ if(pg) return "Save or cancel the open web page first."; return items.length ? "" : "Add a PDF or open a web page first."; }
 return "";
}
function updateNav(){
 const gm=gateMsg(step), next=$("next");
 $("back").hidden = step===1;
 next.hidden = step===5; $("reset").hidden = step!==5;
 next.classList.toggle("dis", !!gm); next.setAttribute("aria-disabled", String(!!gm));
 document.querySelectorAll("#ind span").forEach(s=>{const on=+s.dataset.i===step;s.classList.toggle("on",on);on?s.setAttribute("aria-current","step"):s.removeAttribute("aria-current")});
}
function go(n){
 if(n<1||n>5) return;
 const prev=step;
 step=n; $("track").style.transform=`translateX(-${(n-1)*20}%)`;
 updateNav();
 if(prev===3&&n!==3) leaveTag();
 if(n===3&&prev!==3) showTag();
}
function pop(text,opts){
 const o=opts||{};
 $("popt").textContent=text; $("popok").textContent=o.ok||"Okay"; $("popcancel").hidden=!o.onOk;
 $("popok").onclick=()=>{hidePop(); if(o.onOk) o.onOk()};
 $("popcancel").onclick=hidePop; $("pop").hidden=false; $("popok").focus();
}
function hidePop(){ $("pop").hidden=true }
addEventListener("keydown",e=>{ if(e.key==="Escape" && !$("pop").hidden) hidePop() });
$("next").onclick=()=>{ const m=gateMsg(step); if(m){ pop(m); return } go(step+1) };
$("back").onclick=()=>go(step-1);
document.querySelectorAll("#ind span").forEach(s=>s.onclick=()=>{ const n=+s.dataset.i; if(n<=step || !gateMsg(step)) go(n); else pop(gateMsg(step)) });
function doReset(){ items=[]; sel=-1; merged=null; $("url").value="";urlIns().forEach(i=>{if(i.id!=="url")i.parentElement.remove()});urlState(); draw(); cancelPage(); msg(""); go(1) }
$("reset").onclick=()=>pop("Reset everything? This deletes all added PDFs and web pages and returns to step 1. This can't be undone.",{ok:"Reset",onOk:doReset});
go(1);

/* ---- step 3: tagging on a pdf.js page view ---- */
const TAGS={toc:{c:"#7048e8",n:"Table of contents",box:1},h1:{c:"#c92a2a",n:"H1",box:1},h2:{c:"#c2410c",n:"H2",box:1},h3:{c:"#2f7a3a",n:"H3",box:1},para:{c:"#1864ab",n:"Paragraph",box:1},img:{c:"#a61e4d",n:"Image",box:1},sec:{c:"#868e96",n:"Section",box:1},grp:{c:"#0ca678",n:"Group",box:1}};
const isBoxTool=k=>!!TAGS[k].box;
let tagArmed=null,tagSelId=null,tagPages=[],tagOp=null,tagLoaded=null,pdfjsLib=null,tagSeq=0;
const clamp01=v=>Math.min(1,Math.max(0,v));
function tagMarks(){const f=items[sel];if(!f)return[];f.marks=f.marks||[];return f.marks}
async function loadPdfJs(){if(!pdfjsLib){pdfjsLib=await import("/vendor/pdf.mjs");pdfjsLib.GlobalWorkerOptions.workerSrc="/vendor/pdf.worker.mjs"}return pdfjsLib}
function syncTagButtons(){document.querySelectorAll(".tagbtn").forEach(b=>{const on=b.dataset.k===tagArmed;b.classList.toggle("on",on);b.setAttribute("aria-pressed",String(on))});tagPages.forEach(tp=>tp.wrap.classList.toggle("arm",!!tagArmed))}
document.querySelectorAll(".tagbtn").forEach(b=>b.onclick=()=>{tagArmed=tagArmed===b.dataset.k?null:b.dataset.k;tagSelId=null;syncTagButtons();paintTags()});
$("tgsel").onchange=()=>{sel=+$("tgsel").value;tagLoaded=null;tagSelId=null;showTag()};
async function showTag(){
 $("empty").hidden=true;$("liveview").hidden=true;$("view").hidden=true;$("vtabs").hidden=true;$("tagview").hidden=false;
 $("tgsel").innerHTML=items.length?items.map((f,i)=>`<option value="${i}" ${i===sel?"selected":""}>${esc(f.name)}</option>`).join(""):"<option>Add a PDF on step 1 first</option>";
 const f=items[sel];
 if(!f){$("tagview").innerHTML='<div class="hint" style="padding:30px">Add a PDF on step 1 first.</div>';tagPages=[];$("vt").textContent="Tagging";return}
 $("vt").textContent="Tagging: "+f.name;
 if(tagLoaded!==f.id+"|"+(f.top1||0)+"|"+(f.bottom1||0)){tagLoaded=f.id+"|"+(f.top1||0)+"|"+(f.bottom1||0);try{await renderTagPages(f)}catch(e){msg(e.message,"err")}}
 syncTagButtons();paintTags();
}
function leaveTag(){tagArmed=null;tagSelId=null;tagOp=null;$("tagview").hidden=true;syncTagButtons();if(items.length&&sel>=0)preview();else blank("Add a PDF, or paste a web page address.")}
async function renderTagPages(f){
 const pdfjs=await loadPdfJs();const doc=await pdfjs.getDocument({url:`/api/source?id=${f.id}&top1=${f.top1||0}&bottom1=${f.bottom1||0}`}).promise;
 const host=$("tagview");host.innerHTML="";tagPages=[];
 const width=Math.round(Math.max(300,Math.min(900,host.clientWidth-40))*tagZoom),dpr=window.devicePixelRatio||1;
 for(let i=1;i<=doc.numPages;i++){
  const page=await doc.getPage(i),vp1=page.getViewport({scale:1}),scale=width/vp1.width,vp=page.getViewport({scale});
  const wrap=document.createElement("div");wrap.className="pg";wrap.style.width=width+"px";wrap.style.height=vp.height+"px";
  const ov=document.createElement("div");ov.className="ov";const pn=document.createElement("span");pn.className="pno";pn.textContent="Page "+i;
  host.appendChild(wrap);
  // very tall pages (a whole web page printed as one page) are drawn in slices, because one huge canvas won't draw
  const SL=2400,cvs=[];
  for(let y=0;y<vp.height;y+=SL){const h=Math.min(SL,vp.height-y),cv=document.createElement("canvas");cv.width=Math.floor(vp.width*dpr);cv.height=Math.max(1,Math.floor(h*dpr));cv.style.cssText=`position:absolute;left:0;top:${y}px;width:${width}px;height:${h}px`;wrap.appendChild(cv);cvs.push({cv,y})}
  wrap.append(ov,pn);
  for(const {cv,y} of cvs)await page.render({canvasContext:cv.getContext("2d"),viewport:vp,transform:[dpr,0,0,dpr,0,-y*dpr]}).promise;
  const tp={wrap,ov,index:i-1,page};tagPages.push(tp);wireTagPage(tp);
 }
}
let tagZoom=1;
function zoomShow(pct){$("zpct").value=String(Math.round(tagZoom*100))}
async function setZoom(pct){
 const v=Math.round(Math.min(400,Math.max(25,pct))/1)||100;
 tagZoom=v/100;zoomShow();
 const host=$("tagview");
 tagLoaded=null;   // the next draw of the pages uses the new size
 if(!host.hidden&&items[sel]){const ratio=host.scrollHeight?host.scrollTop/host.scrollHeight:0;await showTag();host.scrollTop=ratio*host.scrollHeight}
}
$("zin").onclick=()=>setZoom(Math.round(tagZoom*100)+10);
$("zout").onclick=()=>setZoom(Math.round(tagZoom*100)-10);
$("zpct").onchange=()=>{const n=parseFloat($("zpct").value.replace(/[^0-9.]/g,""));if(isNaN(n))zoomShow();else setZoom(n)};
$("zpct").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();$("zpct").blur()}};
function tpos(e,wrap){const r=wrap.getBoundingClientRect();return{x:clamp01((e.clientX-r.left)/r.width),y:clamp01((e.clientY-r.top)/r.height)}}
function wireTagPage(tp){
 const w=tp.wrap;
 w.addEventListener("pointerdown",e=>{
  if(!items[sel])return;
  const hit=e.target.closest("[data-m]");
  let edges=null;
  if(hit&&hit.classList.contains("box")&&!e.target.closest(".lbl")){   // near a side of a box: that side is dragged to resize it
   const r=hit.getBoundingClientRect(),E=9;edges={l:e.clientX-r.left<=E,r:r.right-e.clientX<=E,t:e.clientY-r.top<=E,b:r.bottom-e.clientY<=E};
   if(!(edges.l||edges.r||edges.t||edges.b))edges=null}
  let grab=!!hit;
  if(hit&&tagArmed&&hit.classList.contains("box")&&!edges&&!e.target.closest(".lbl"))grab=false;   // drawing inside a box (nested sections)
  if(grab){e.preventDefault();tagSelId=hit.dataset.m;const m=tagMarks().find(q=>q.id===tagSelId);if(!m)return;const p=tpos(e,w);
   tagOp={mode:edges?"edge":"move",edges,m,sx:p.x,sy:p.y,ox:m.x,oy:m.y,ow:m.w,oh:m.h};w.setPointerCapture(e.pointerId);paintTags();return}
  if(!tagArmed){tagSelId=null;paintTags();return}
  e.preventDefault();const p=tpos(e,w);
  if(isBoxTool(tagArmed)||/^h[1-3]$/.test(tagArmed)){const head=/^h[1-3]$/.test(tagArmed);   // a heading tool: a click boxes the heading line under the cursor, a drag boxes the text you drag over
   tagOp={mode:"draw",k:tagArmed,tp,x0:p.x,y0:p.y,x1:p.x,y1:p.y,cx1:e.clientX,cy1:e.clientY,maybeFlag:head,sx:e.clientX,sy:e.clientY};w.setPointerCapture(e.pointerId);startAutoScroll();paintTags()}
  else{const m={id:"t"+(++tagSeq),k:tagArmed,p:tp.index,x:p.x,y:p.y};const ms=tagMarks();
   ms.push(m);tagSelId=m.id;paintTags()}
 });
 w.addEventListener("pointermove",e=>{
  if(!tagOp){const h=e.target.closest&&e.target.closest("[data-m].box");let c="";
   if(h&&!e.target.closest(".lbl")){const r=h.getBoundingClientRect(),E=9,l=e.clientX-r.left<=E,rt=r.right-e.clientX<=E,t=e.clientY-r.top<=E,b=r.bottom-e.clientY<=E;
    c=(l&&t)||(rt&&b)?"nwse-resize":(rt&&t)||(l&&b)?"nesw-resize":l||rt?"ew-resize":t||b?"ns-resize":""}
   w.style.cursor=c;return}const p=tpos(e,w);
  if(tagOp.mode==="draw"){tagOp.x1=p.x;tagOp.y1=p.y;tagOp.cx1=e.clientX;tagOp.cy1=e.clientY}
  else if(tagOp.mode==="move"){const m=tagOp.m;m.x=clamp01(tagOp.ox+(p.x-tagOp.sx));m.y=clamp01(tagOp.oy+(p.y-tagOp.sy));if(m.w!=null){m.x=Math.min(m.x,1-m.w);m.y=Math.min(m.y,1-m.h)}}
  else if(tagOp.mode==="edge"){const m=tagOp.m,E=tagOp.edges,dx=p.x-tagOp.sx,dy=p.y-tagOp.sy,mn=0.01;let x0=tagOp.ox,y0=tagOp.oy,x1=x0+tagOp.ow,y1=y0+tagOp.oh;
   if(E.l)x0=Math.max(0,Math.min(x1-mn,x0+dx));if(E.r)x1=Math.min(1,Math.max(x0+mn,x1+dx));if(E.t)y0=Math.max(0,Math.min(y1-mn,y0+dy));if(E.b)y1=Math.min(1,Math.max(y0+mn,y1+dy));
   m.x=x0;m.y=y0;m.w=x1-x0;m.h=y1-y0}
  else if(tagOp.mode==="resize"){const m=tagOp.m;m.w=Math.max(0.01,Math.min(1-m.x,tagOp.ow+(p.x-tagOp.sx)));m.h=Math.max(0.01,Math.min(1-m.y,tagOp.oh+(p.y-tagOp.sy)))}
  paintTags();
 });
 const end=()=>{
  if(!tagOp)return;
  if(tagOp.mode==="draw"&&tagOp.maybeFlag&&Math.hypot(tagOp.cx1-tagOp.sx,tagOp.cy1-tagOp.sy)<7){const op=tagOp;stopAutoScroll();tagOp=null;clickHeading(op);return}
  if(tagOp.mode==="draw"){/* a minimum in screen pixels, not a fraction of the page: a fraction dropped small boxes on very tall pages */const segs=dragSegs(tagOp).filter(g=>g.w*g.tp.wrap.clientWidth>=6&&g.h*g.tp.wrap.clientHeight>=6),made=[];
   segs.forEach(g=>{const m={id:"t"+(++tagSeq),k:tagOp.k,p:g.tp.index,x:g.x,y:g.y,w:g.w,h:g.h};if(segs.length>1)m.link=made.length?made[0].id:m.id;if(segs.length>1&&!made.length)m.link=m.id;tagMarks().push(m);made.push(m)});
   if(made.length){tagSelId=made[0].id;(async()=>{for(let n=0;n<made.length;n++){const m=made[n];if(m.k==="para"||m.k==="img"||/^h[1-3]$/.test(m.k))await fitBox(m,segs[n].tp);if(!m.link)await reconcile(m,segs[n].tp)}})()}}
  if(tagOp.mode==="resize"||tagOp.mode==="edge"){const m=tagOp.m,tp=tagPages.find(t=>t.index===m.p);if(m.w!=null&&tp&&!m.link)reconcile(m,tp)}
  stopAutoScroll();tagOp=null;paintTags();
 };
 w.addEventListener("pointerup",end);w.addEventListener("pointercancel",end);
}
// A drawn paragraph or image box snaps to the text lines or pictures it holds and takes the standard padding
// (paragraph 5pt left and right, 2pt top and bottom; picture 8pt all round). A box with nothing detected inside stays as drawn.
// Boxes fit together: a bigger kind of box (Section > Group > Paragraph > Heading or Image) must hold the smaller boxes that sit mostly inside it.
// When a box is drawn or resized and another one is half or more inside a bigger one, the bigger box grows to hold it all; a Paragraph that grew
// is then snapped to its text again, so it holds exactly what it should, including the box just added. Works both ways round: draw the
// small box inside an existing big one, or draw the big box over existing small ones.
const TAG_RANK={sec:4,grp:3,para:2,h1:1,h2:1,h3:1,img:1};
async function reconcile(m,tp){
 try{
  const rk=TAG_RANK[m.k];if(!rk||m.w==null)return;
  const others=tagMarks().filter(o=>o!==m&&o.w!=null&&o.p===m.p&&!o.link&&TAG_RANK[o.k]);
  const area=b=>b.w*b.h,inter=(a,b)=>Math.max(0,Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x))*Math.max(0,Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y));
  const grown=new Set();
  const grow=(P,C)=>{if(P.k==="grp")return;   // Group boxes stay exactly as drawn or resized: nothing grows or snaps them
   const f=inter(P,C)/area(C);if(f<0.5||f>0.999)return;
   const x0=Math.min(P.x,C.x),y0=Math.min(P.y,C.y),x1=Math.max(P.x+P.w,C.x+C.w),y1=Math.max(P.y+P.h,C.y+C.h);P.x=x0;P.y=y0;P.w=x1-x0;P.h=y1-y0;grown.add(P)};
  others.forEach(o=>{const ro=TAG_RANK[o.k];if(ro>rk)grow(o,m);else if(ro<rk)grow(m,o)});
  for(const P of grown){if(P.k==="para"){await fitBox(P,tp);others.concat([m]).forEach(o=>{if(o!==P&&TAG_RANK[o.k]<TAG_RANK.para)grow(P,o)})}}
  paintTags();
 }catch(e){}
}
async function clickHeading(op){
 try{
  await ensureData(op.tp);const D=op.tp.data,px=op.x0*D.W,py=D.H-op.y0*D.H;
  const l=D.lines.find(l=>px>=l.x0-4&&px<=l.x1+4&&py>=l.base-l.size*0.3&&py<=l.base+l.size*0.9);
  if(!l){msg("No text under the click. Drag a box around the heading instead.","err");paintTags();return}
  const x0=clamp01((l.x0-5)/D.W),x1=clamp01((l.x1+5)/D.W),y0=clamp01((D.H-(l.base+l.size*0.8+2))/D.H),y1=clamp01((D.H-(l.base-l.size*0.2-2))/D.H);
  const m={id:"t"+(++tagSeq),k:op.k,p:op.tp.index,x:x0,y:y0,w:x1-x0,h:y1-y0};tagMarks().push(m);tagSelId=m.id;paintTags();await reconcile(m,op.tp)
 }catch(e){}
}
async function ensureData(tp){
  if(!tp.data){
   const pdfjs=await loadPdfJs(),page=tp.page,vp=page.getViewport({scale:1}),tc=await page.getTextContent(),ol=await page.getOperatorList(),OPS=pdfjs.OPS;
   const stack=[];let ctm=[1,0,0,1,0,0];const imgs=[],shapes=[];
   ol.fnArray.forEach((fn,j)=>{const a=ol.argsArray[j];
    if(fn===OPS.save)stack.push(ctm);else if(fn===OPS.restore)ctm=stack.pop()||[1,0,0,1,0,0];else if(fn===OPS.transform)ctm=mulM(a,ctm);
    else if(fn===OPS.paintFormXObjectBegin){stack.push(ctm);if(a&&a[0])ctm=mulM(a[0],ctm)}else if(fn===OPS.paintFormXObjectEnd)ctm=stack.pop()||[1,0,0,1,0,0];
    else if(fn===OPS.constructPath&&a[2]&&[OPS.fill,OPS.eoFill,OPS.fillStroke,OPS.eoFillStroke,OPS.closeFillStroke,OPS.closeEOFillStroke].includes(a[0])){
     const mm=a[2],q=[[mm[0],mm[1]],[mm[2],mm[1]],[mm[0],mm[3]],[mm[2],mm[3]]].map(([u,v])=>[ctm[0]*u+ctm[2]*v+ctm[4],ctm[1]*u+ctm[3]*v+ctm[5]]),qx=q.map(z=>z[0]),qy=q.map(z=>z[1]);
     const b={x0:Math.min(...qx),x1:Math.max(...qx),y0:Math.min(...qy),y1:Math.max(...qy)};if(b.x1-b.x0>=4&&b.y1-b.y0>=4)shapes.push(b)}
    else if(fn===OPS.paintImageXObject||fn===OPS.paintInlineImageXObject){
     const pts=[[0,0],[1,0],[0,1],[1,1]].map(([u,v])=>[ctm[0]*u+ctm[2]*v+ctm[4],ctm[1]*u+ctm[3]*v+ctm[5]]),xs=pts.map(q=>q[0]),ys=pts.map(q=>q[1]);
     const b={x0:Math.min(...xs),x1:Math.max(...xs),y0:Math.min(...ys),y1:Math.max(...ys)};if(b.x1-b.x0>6&&b.y1-b.y0>6)imgs.push(b)}});
   tp.data={W:vp.width,H:vp.height,lines:groupLines(tc,vp.height),imgs,shapes};
  }
}
const SNAP_TOL=6;   // points: how far a snapped box may grow beyond the box the user drew
async function fitBox(m,tp){
 try{
  await ensureData(tp);
  const D=tp.data,bx0=m.x*D.W,bx1=(m.x+m.w)*D.W,by1=D.H-m.y*D.H,by0=D.H-(m.y+m.h)*D.H;
  const ov=(a0,a1,b0,b1)=>Math.max(0,Math.min(a1,b1)-Math.max(a0,b0));
  let r=null,px,py;
  if(m.k==="para"||/^h[1-3]$/.test(m.k)){
   const hit=D.lines.filter(l=>{const t=l.base+l.size*0.8,b=l.base-l.size*0.2;return ov(b,t,by0,by1)>0.5*(t-b)&&ov(l.x0,l.x1,bx0,bx1)>0.3*(l.x1-l.x0)});
   if(hit.length){r={x0:Math.min(...hit.map(l=>l.x0)),x1:Math.max(...hit.map(l=>l.x1)),y0:Math.min(...hit.map(l=>l.base-l.size*0.2)),y1:Math.max(...hit.map(l=>l.base+l.size*0.8))};px=5;py=2}
  }else{
   // the picture is everything inside the box: pictures, text and filled shapes. The box is centred on that, with the padding all round.
   const inBox=g=>ov(g.x0,g.x1,bx0,bx1)*ov(g.y0,g.y1,by0,by1)>=0.6*(g.x1-g.x0)*(g.y1-g.y0);
   const parts=[...D.imgs.filter(g=>ov(g.x0,g.x1,bx0,bx1)*ov(g.y0,g.y1,by0,by1)>0.3*Math.min((g.x1-g.x0)*(g.y1-g.y0),(bx1-bx0)*(by1-by0))),
    ...D.shapes.filter(g=>g.x1-g.x0<D.W*0.95&&g.y1-g.y0<D.H*0.6&&inBox(g)),
    ...D.lines.map(l=>({x0:l.x0,x1:l.x1,y0:l.base-l.size*0.2,y1:l.base+l.size*0.8})).filter(inBox)];
   if(parts.length){r={x0:Math.min(...parts.map(g=>g.x0)),x1:Math.max(...parts.map(g=>g.x1)),y0:Math.min(...parts.map(g=>g.y0)),y1:Math.max(...parts.map(g=>g.y1))};px=py=0;
    // contents that fill little of the box (a stray dot) do not move it
    const bigPic=D.imgs.some(g=>g.x1-g.x0>=40&&g.y1-g.y0>=40&&parts.includes(g));   // a real picture in the box is the content, wherever it sits
    if(!bigPic&&(r.x1-r.x0)*(r.y1-r.y0)<0.4*(bx1-bx0)*(by1-by0))r={x0:bx0,x1:bx1,y0:by0,y1:by1}}
  }
  if(!r)return;
  // the snap is a nudge, not a jump: it can tighten the box onto the text inside it, but it never grows the box more than
  // SNAP_TOL points beyond the edges the user drew. This matters on long web pages, where a far-off line or picture
  // could otherwise pull a drawn box a long way.
  const TOL=SNAP_TOL;
  r={x0:Math.max(r.x0,bx0-TOL),x1:Math.min(r.x1,bx1+TOL),y0:Math.max(r.y0,by0-TOL),y1:Math.min(r.y1,by1+TOL)};
  if(r.x1<=r.x0||r.y1<=r.y0)return;
  const x0=clamp01((r.x0-px)/D.W),x1=clamp01((r.x1+px)/D.W),y0=clamp01((D.H-(r.y1+py))/D.H),y1=clamp01((D.H-(r.y0-py))/D.H);
  m.x=x0;m.y=y0;m.w=x1-x0;m.h=y1-y0;paintTags();
 }catch(e){}
}
// A box can be dragged on past the edge of a page: it is cut at the page edges into one box per page, and the pieces are linked
// (they share m.link) so the converter reads them as one. Pictures stay on their own page.
function dragSegs(op){
 const r0=op.tp.wrap.getBoundingClientRect(),sx=r0.left+op.x0*r0.width,sy=r0.top+op.y0*r0.height,ex=op.cx1,ey=op.cy1;
 const L=Math.min(sx,ex),R=Math.max(sx,ex),T=Math.min(sy,ey),B=Math.max(sy,ey),segs=[];
 tagPages.forEach(tp=>{
  if((op.k==="img"||/^h[1-3]$/.test(op.k))&&tp!==op.tp)return;
  const r=tp.wrap.getBoundingClientRect(),t=Math.max(T,r.top),b=Math.min(B,r.bottom),l=Math.max(L,r.left),rr=Math.min(R,r.right);
  if(b-t<=0||rr-l<=0)return;
  segs.push({tp,x:(l-r.left)/r.width,y:(t-r.top)/r.height,w:(rr-l)/r.width,h:(b-t)/r.height});
 });
 return segs;
}
let scrollTimer=null;
function startAutoScroll(){stopAutoScroll();scrollTimer=setInterval(()=>{   // dragging near the top or bottom edge scrolls the pages
 if(!tagOp||tagOp.mode!=="draw")return;const h=$("tagview"),r=h.getBoundingClientRect();
 if(tagOp.cy1>r.bottom-40)h.scrollTop+=Math.min(30,(tagOp.cy1-(r.bottom-40))/2+4);else if(tagOp.cy1<r.top+40)h.scrollTop-=Math.min(30,((r.top+40)-tagOp.cy1)/2+4);
 paintTags();
},30)}
function stopAutoScroll(){if(scrollTimer){clearInterval(scrollTimer);scrollTimer=null}}
function paintTags(){
 const marks=tagMarks();
 tagPages.forEach(tp=>{
  tp.ov.innerHTML="";
  marks.filter(m=>m.p===tp.index).forEach(m=>{
   const T=TAGS[m.k],el=document.createElement("div");el.dataset.m=m.id;el.style.setProperty("--c",T.c);
   el.style.left=(m.x*100)+"%";el.style.top=(m.y*100)+"%";
   if(m.w!=null){el.className="box"+(m.id===tagSelId?" sel":"");el.style.width=(m.w*100)+"%";el.style.height=(m.h*100)+"%";
    el.innerHTML=`<span class="lbl">${T.n}</span><span class="rh" data-h="1"></span>`}
   else{el.className="flag"+(m.id===tagSelId?" sel":"");el.innerHTML=`<span>${T.n}</span><i></i>`}
   tp.ov.appendChild(el);
  });
  if(tagOp&&tagOp.mode==="draw"&&!(tagOp.maybeFlag&&Math.hypot(tagOp.cx1-tagOp.sx,tagOp.cy1-tagOp.sy)<7))dragSegs(tagOp).filter(g=>g.tp===tp).forEach(g=>{const d=document.createElement("div");d.className="box draft";d.style.setProperty("--c",TAGS[tagOp.k].c);
   d.style.left=(g.x*100)+"%";d.style.top=(g.y*100)+"%";d.style.width=(g.w*100)+"%";d.style.height=(g.h*100)+"%";tp.ov.appendChild(d)});
 });
}
document.addEventListener("keydown",e=>{
 if(e.key==="Escape"&&tagArmed){tagArmed=null;syncTagButtons();return}
 if((e.key==="Delete"||e.key==="Backspace")&&step===3&&tagSelId&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){
  e.preventDefault();const marks=tagMarks();const cur=marks.find(m=>m.id===tagSelId);if(cur){const lk=cur.link;for(let i=marks.length-1;i>=0;i--)if(marks[i]===cur||(lk&&marks[i].link===lk))marks.splice(i,1)}tagSelId=null;paintTags();
 }
});
$("tagreset").onclick=()=>{if(!items[sel])return;pop("Remove every tag on this file? This can't be undone.",{ok:"Remove tags",onOk:()=>{items[sel].marks=[];tagSelId=null;paintTags()}})};

/* ---- Tag All: suggests flags and boxes from the text and images on each page ---- */
function mulM(a,b){return[a[0]*b[0]+a[1]*b[2],a[0]*b[1]+a[1]*b[3],a[2]*b[0]+a[3]*b[2],a[2]*b[1]+a[3]*b[3],a[4]*b[0]+a[5]*b[2]+b[4],a[4]*b[1]+a[5]*b[3]+b[5]]}
function isBoldFont(tc,name){const fam=(tc.styles&&tc.styles[name]&&tc.styles[name].fontFamily)||name||"";return /bold|black|heavy|semi|demi/i.test(fam)}
function isItalicItem(tc,co,it,size){let f=null;try{f=co&&co.get(it.fontName)}catch(e){}
 if(f&&(f.italic||/italic|oblique/i.test(f.name||"")))return true;
 return Math.abs(it.transform[2])>0.12*size&&it.transform[2]*it.transform[3]<0;}   // a real italic font, or letters sheared to the right
function groupLines(tc,H,co){
 const lines=[];
 // column gutters: a gap that lines up on at least three other nearby lines is a gutter between columns (it can be narrow), so the lines are cut there
 const rows=[];
 tc.items.forEach(it=>{if(!it.str||!it.str.trim())return;const base=it.transform[5],size=Math.abs(it.transform[3])||Math.abs(it.height)||10;let r=rows.find(r=>Math.abs(r.base-base)<2);if(!r)rows.push(r={base,size,its:[],gaps:[],conf:[]});r.its.push({x0:it.transform[4],x1:it.transform[4]+it.width})});
 rows.forEach(r=>{r.its.sort((a,b)=>a.x0-b.x0);let mx=r.its[0]?r.its[0].x1:0;for(let i=1;i<r.its.length;i++){if(r.its[i].x0-mx>=7)r.gaps.push({a:mx,b:r.its[i].x0});mx=Math.max(mx,r.its[i].x1)}});
 rows.forEach(r=>r.gaps.forEach(g=>{let n=0;rows.forEach(o=>{if(o===r||Math.abs(o.base-r.base)>r.size*16)return;if(o.gaps.some(h=>Math.min(g.b,h.b)-Math.max(g.a,h.a)>=0.6*Math.min(g.b-g.a,h.b-h.a)))n++});if(n>=3)r.conf.push(g)}));
 const cut=(base,x1,x0)=>{const r=rows.find(r=>Math.abs(r.base-base)<2);return !!r&&r.conf.some(g=>g.a<=x1+3&&g.b>=x0-3)};
 tc.items.forEach(it=>{if(!it.str||!it.str.trim())return;const base=it.transform[5],size=Math.abs(it.transform[3])||Math.abs(it.height)||10,x0=it.transform[4],x1=x0+it.width;
  const same=lines.find(l=>Math.abs(l.base-base)<2&&x0-l.x1<Math.max(30,3*size)&&x0>=l.x0-2&&!cut(base,l.x1,x0));   // a wide gap starts a new line: columns stay apart, even with a narrow gutter
  if(same){same.text+=(same.text&&!/\s$/.test(same.text)?" ":"")+it.str;same.x1=Math.max(same.x1,x1);same.size=Math.max(same.size,size);same.bold=same.bold||isBoldFont(tc,it.fontName);same.bc+=isBoldFont(tc,it.fontName)?it.str.trim().length:0;same.nc+=it.str.trim().length;same.ic+=isItalicItem(tc,co,it,size)?it.str.trim().length:0;same.items.push(it)}
  else lines.push({text:it.str,base,x0,x1,size,bold:isBoldFont(tc,it.fontName),bc:isBoldFont(tc,it.fontName)?it.str.trim().length:0,nc:it.str.trim().length,ic:isItalicItem(tc,co,it,size)?it.str.trim().length:0,items:[it]})});
 lines.forEach(l=>{l.wb=l.nc>0&&l.bc>=0.9*l.nc;l.wi=l.nc>0&&l.ic>=0.9*l.nc});   // wb: the whole line is bold
 lines.sort((a,b)=>b.base-a.base||a.x0-b.x0);
 return lines;
}

/* ---- Kill Team datacards: a card has a NAME / ATK / HIT / DMG / WR table; its parts are tagged as text, not as one picture ---- */
/* Age of Sigmar ability cards: a phase bar (timing text, the command point cost at its right), a body that starts "NAME: flavour" then Declare / Effect lines,
   and a Keywords row. Each card's parts are tagged with roles ab-card (the whole card), ab-phase, ab-cp, ab-body and ab-kw. */
function aosCards(P){
 const its=P.tc.items.filter(it=>it.str&&it.str.trim()).map(it=>({s:it.str.trim(),x0:it.transform[4],x1:it.transform[4]+it.width,b:it.transform[5],sz:Math.abs(it.transform[3])||Math.abs(it.height)||10}));
 its.sort((a,b)=>b.b-a.b||a.x0-b.x0);
 const cells=[];
 its.forEach(it=>{const c=cells.find(c=>Math.abs(c.b-it.b)<=2.5&&it.x0-c.x1<Math.max(1.5,it.sz*0.36)&&it.x0>=c.x0-1);
  if(c){c.s+=(/\s$/.test(c.s)?"":" ")+it.s;c.x1=Math.max(c.x1,it.x1);c.sz=Math.max(c.sz,it.sz)}else cells.push({s:it.s,x0:it.x0,x1:it.x1,b:it.b,sz:it.sz})});
 const up=c=>c.s.toUpperCase().replace(/[^A-Z]/g,"");
 const box=(cs,padx=5,pady=2)=>[Math.min(...cs.map(c=>c.x0))-padx,Math.min(...cs.map(c=>c.b-c.sz*0.2))-pady,Math.max(...cs.map(c=>c.x1))+padx,Math.max(...cs.map(c=>c.b+c.sz*0.8))+pady];
 const szs=cells.map(c=>c.sz).sort((a,b)=>a-b),bodySz=szs[Math.floor(szs.length/2)]||10;
 const TIMING=/^(your|any|reaction|once per|start of|end of|passive|deployment|before|after)\b|\b(phase|turn|battle round)\s*$/i;
 const kws=cells.filter(c=>up(c)==="KEYWORDS").sort((a,b)=>b.b-a.b);
 const out=[];
 kws.forEach((kl,ki)=>{
  const floor=kws[ki-1]?kws[ki-1].b-kws[ki-1].sz*2:P.H+5;   // the card above ends at its own Keywords row
  const upper=cells.filter(c=>c.b>kl.b+kl.sz*0.5&&c.b<floor&&c.b<kl.b+420);
  const timing=upper.filter(c=>TIMING.test(c.s)&&c.sz>=bodySz*1.12).sort((a,b)=>a.b-b.b)[0];
  if(!timing)return;
  const kvals=cells.filter(c=>c!==kl&&c.x0>kl.x1-2&&c.b<=kl.b+3&&kl.b-c.b<kl.sz*1.9);
  const cp=cells.find(c=>c!==timing&&Math.abs(c.b-timing.b)<=4&&c.x0>timing.x1+10&&/^(\d{1,2}|X)$/.test(c.s));
  const body=cells.filter(c=>c.b<timing.b-timing.sz*0.4&&c.b>kl.b+kl.sz*0.9&&c!==cp&&c.x0<(kvals.length?Math.max(...kvals.map(k=>k.x1)):1e9));
  if(!body.length||!kvals.length)return;
  const left=Math.min(kl.x0,timing.x0-60,...body.map(c=>c.x0))-4,right=Math.max(...body.map(c=>c.x1),...kvals.map(c=>c.x1),cp?cp.x1:0)+8;
  const top=timing.b+timing.sz+8,bottom=Math.min(...kvals.map(c=>c.b))-kl.sz*0.5-4;
  const tags=[],T=(k,role,bx)=>tags.push({k,role,box:bx});
  T("grp","ab-card",[left,bottom,right,top]);
  T("h2","ab-phase",box([timing]));
  if(cp)T("h3","ab-cp",box([cp],3,2));
  T("para","ab-body",box(body));
  T("h3","ab-kw",box(kvals));
  out.push({rect:[left,bottom,right,top],tags});
 });
 return out;
}

function datacardCards(P){
 const its=P.tc.items.filter(it=>it.str&&it.str.trim()).map(it=>({s:it.str.trim(),x0:it.transform[4],x1:it.transform[4]+it.width,b:it.transform[5],sz:Math.abs(it.transform[3])||Math.abs(it.height)||10}));
 its.sort((a,b)=>b.b-a.b||a.x0-b.x0);
 const cells=[];
 its.forEach(it=>{const c=cells.find(c=>Math.abs(c.b-it.b)<=2.5&&it.x0-c.x1<Math.max(1.5,it.sz*0.36)&&it.x0>=c.x0-1);
  if(c){c.s+=(/\s$/.test(c.s)?"":" ")+it.s;c.x1=Math.max(c.x1,it.x1);c.sz=Math.max(c.sz,it.sz);c.items.push(it)}else cells.push({s:it.s,x0:it.x0,x1:it.x1,b:it.b,sz:it.sz,items:[it]})});
 const up=c=>c.s.toUpperCase().replace(/[^A-Z]/g,""),cx=c=>(c.x0+c.x1)/2;
 const box=(cs,padx=5,pady=2)=>[Math.min(...cs.map(c=>c.x0))-padx,Math.min(...cs.map(c=>c.b-c.sz*0.2))-pady,Math.max(...cs.map(c=>c.x1))+padx,Math.max(...cs.map(c=>c.b+c.sz*0.8))+pady];
 const hdrs=[];
 cells.forEach(a=>{if(up(a)==="ATK"){const h=cells.find(c=>up(c)==="HIT"&&Math.abs(c.b-a.b)<=3),d=cells.find(c=>up(c)==="DMG"&&Math.abs(c.b-a.b)<=3);
  if(h&&d)hdrs.push({atk:a,hit:h,dmg:d,name:cells.find(c=>up(c)==="NAME"&&Math.abs(c.b-a.b)<=3),wr:cells.find(c=>up(c)==="WR"&&Math.abs(c.b-a.b)<=3),b:a.b})}});
 hdrs.sort((a,b)=>b.b-a.b);
 const out=[];let ceiling=P.H+5;
 hdrs.forEach((H,hi)=>{
  const tags=[],T=(k,role,bx)=>tags.push({k,role,box:bx});
  const above=cells.filter(c=>c.b>H.b+4&&c.b<ceiling&&c.b<H.b+330);
  const isLabel=c=>/^(APL|CONTROL|MOVE|SAVE|WOUNDS)$/.test(up(c));
  const labels=above.filter(isLabel);
  const labRow=labels.length?labels.filter(l=>Math.abs(l.b-labels[0].b)<=4):[];
  const numRe=/^\d{1,2}\s*["”″]?\+?$/;
  const vals=labRow.map(l=>above.filter(c=>c.b<l.b-2&&c.b>H.b+4&&Math.abs(cx(c)-cx(l))<48&&numRe.test(c.s)).sort((a,b)=>b.b-a.b)[0]).filter(Boolean);
  const firstLab=labRow.length?Math.min(...labRow.map(l=>l.x0)):Infinity;
  const valTop=labRow.length?Math.max(...labRow.map(l=>l.b+l.sz)):0,valBot=vals.length?Math.min(...vals.map(v=>v.b)):0;
  const inStats=c=>c.x0>=firstLab-14&&c.b<=valTop+2&&c.b>=valBot-6;
  const rest=above.filter(c=>!isLabel(c)&&!vals.includes(c)&&!numRe.test(c.s)&&!inStats(c));
  // the title: the biggest text above the table that is not a stat; the cells on its line to the right are its type
  const tcand=rest.filter(c=>c.s.length>=3&&c.x0<firstLab-2).sort((a,b)=>b.sz-a.sz||b.b-a.b);
  const topSz=tcand.length?tcand[0].sz:0,title=tcand.filter(c=>c.sz>=topSz*0.8).sort((a,b)=>a.b-b.b)[0];   // among the largest text, the one nearest the table (a section heading above the card is bigger but higher)
  if(!title)return;
  const sameLine=rest.filter(c=>c!==title&&Math.abs(c.b-title.b)<=4);
  T("h2","title",box([title]));
  sameLine.filter(c=>c.x0>title.x1).forEach(c=>T("h3","type",box([c])));
  // flavour: the italic lines under the title, left of the stats
  const flav=rest.filter(c=>c.b<title.b-title.sz*0.4&&!sameLine.includes(c)&&c.x0<firstLab-2);
  if(flav.length)T("para","flavour",box(flav));
  labRow.forEach(l=>T("h3","stat-label",box([l])));
  vals.forEach(v=>T("h2","stat-value",box([v])));
  ["name","atk","hit","dmg","wr"].forEach(k=>{if(H[k])T("h3","head-"+k,box([H[k]]))});
  // weapon rows
  const below=cells.filter(c=>c.b<H.b-3&&c.b>(hdrs[hi+1]?hdrs[hi+1].b:-1e9));
  const groups=[];below.forEach(c=>{const g=groups.find(g=>Math.abs(g.b-c.b)<=3);if(g)g.cells.push(c);else groups.push({b:c.b,cells:[c]})});
  groups.sort((a,b)=>b.b-a.b);
  const statRe=/^(\d{1,2}\+?|\d{1,2}\s*\/\s*\d{1,2}|-|—)$/;
  const near=(c,h,tol)=>Math.abs(cx(c)-cx(h))<=tol;
  const inName=c=>c.x1<H.atk.x0-6,inWr=c=>H.wr&&c.x0>=H.wr.x0-14;
  const rowsG=[],loose=[];let lastB=H.b,endIdx=0;
  for(let gi=0;gi<groups.length;gi++){const g=groups[gi];
   if(lastB-g.b>Math.max(40,g.cells[0].sz*3.4))break;
   const nums=g.cells.filter(c=>statRe.test(c.s)&&(near(c,H.atk,16)||near(c,H.hit,16)||near(c,H.dmg,18)));
   if(nums.length>=2){rowsG.push(g);lastB=g.b;endIdx=gi+1}
   else if(g.cells.every(c=>inName(c)||inWr(c))&&(rowsG.length||gi<3)){loose.push(g);lastB=g.b;endIdx=gi+1}   // the first or second line of a name or special rule that wraps
   else if(rowsG.length)break;
   else lastB=g.b;}
  // a wrapped line belongs to the nearest row; lines after the last row only count while they sit close to it
  const rows=rowsG.map(g=>({g,extra:[]}));
  loose.forEach(l=>{const r=rows.reduce((m,r)=>Math.abs(r.g.b-l.b)<Math.abs(m.g.b-l.b)?r:m,rows[0]);if(r&&Math.abs(r.g.b-l.b)<r.g.cells[0].sz*3)r.extra.push(l)});
  const cardX0=title.x0-5;
  rows.forEach(r=>{
   const all=[...r.g.cells,...r.extra.flatMap(e=>e.cells)],nameCells=all.filter(inName),wrCells=all.filter(c=>inWr(c)&&!/^[-—]$/.test(c.s));
   const dmgC=r.g.cells.filter(c=>near(c,H.dmg,18)&&statRe.test(c.s))[0]||r.g.cells.filter(c=>statRe.test(c.s)).sort((a,b)=>b.x1-a.x1)[0];
   const left=Math.max(cardX0,(nameCells.length?Math.min(...nameCells.map(c=>c.x0)):H.atk.x0)-44);
   const rb=box(all.filter(c=>!inWr(c)));
   T("para","weapon",[left,rb[1],(dmgC?dmgC.x1:H.dmg.x1)+8,rb[3]]);
   if(wrCells.length)T("h3","weapon-wr",box(wrCells));
  });
  const tableEnd=rows.length?Math.min(...[...rows.map(r=>r.g.b),...rows.flatMap(r=>r.extra.map(e=>e.b))])-groups[0].cells[0].sz*0.4:H.b-8;
  // footer: keywords on the left, base size on the right, on one line
  const baseRe=/^[⌀Øø∅]?\s*\d+\s*mm$/i;
  const rest2=below.filter(c=>c.b<tableEnd+4&&!groups.slice(0,endIdx).some(g=>g.cells.includes(c)));
  const base=rest2.filter(c=>baseRe.test(c.s)).sort((a,b)=>a.b-b.b)[0];
  let footB=null;
  if(base){const kw=rest2.filter(c=>Math.abs(c.b-base.b)<=4&&c!==base&&c.x1<=base.x0+2);
   if(kw.length)T("h3","keywords",box(kw));T("h3","base",box([base]));footB=base.b+base.sz}
  // abilities: "Name: rule text" blocks between the table and the footer
  const ab=rest2.filter(c=>footB===null||c.b>footB+4).filter(c=>c!==base);   // the ability text sits ABOVE the footer bar (bigger b), not below it
  const lines=[];ab.forEach(c=>{const l=lines.find(l=>Math.abs(l.b-c.b)<=3);if(l)l.cells.push(c);else lines.push({b:c.b,cells:[c]})});
  lines.sort((a,b)=>b.b-a.b);
  const blocks=[];
  lines.forEach(l=>{l.cells.sort((a,b)=>a.x0-b.x0);const t=l.cells.map(c=>c.s).join(" ").trim();
   const nameColon=/^[^:.]{3,45}:/.test(t)&&/^[A-Z]/.test(t);   // a real ability name starts with a capital, not a wrapped line that happens to hit a ")"
   const bannerTitle=/^[A-Z][A-Z '-]{2,24}$/.test(t)&&l.cells.length<=3;   // a short all-caps title on its own line (a Strategy/Firefight-ploy-style box inside the card), no colon
   if(nameColon){
    let nx=null;for(const c of l.cells){for(const it of c.items){const k=it.s.indexOf(":");if(k>=0&&nx===null){nx=it.x0+(it.x1-it.x0)*(k+1)/it.s.length}}}
    const c0=l.cells[0];blocks.push({lines:[l],name:{x0:c0.x0,x1:nx==null?c0.x1:nx,b:c0.b,sz:c0.sz}})}
   else if(bannerTitle)blocks.push({lines:[l],name:{x0:l.cells[0].x0,x1:l.cells[l.cells.length-1].x1,b:l.cells[0].b,sz:l.cells[0].sz}})
   else if(blocks.length&&blocks[blocks.length-1].lines[blocks[blocks.length-1].lines.length-1].b-l.b<l.cells[0].sz*2.6)blocks[blocks.length-1].lines.push(l)});
  blocks.forEach(bk=>{const cs=bk.lines.flatMap(l=>l.cells);T("para","ability",box(cs));T("h3","ability-name",box([bk.name],4,2))});
  // the card's area, so tags made another way inside it can be removed
  const xs=[...tags.map(t=>t.box[2])],ys=tags.map(t=>t.box[1]),yt=tags.map(t=>t.box[3]);
  const rect=[title.x0-14,Math.min(...ys)-6,Math.max(...xs)+10,Math.max(...yt)+14];
  out.push({P,rect,tags});ceiling=rect[1];
 });
 return out;
}
async function autoTagFile(f){
 const src=`/api/source?id=${f.id}&top1=${f.top1||0}&bottom1=${f.bottom1||0}`;
 const pdfjs=await loadPdfJs();const doc=await pdfjs.getDocument({url:src}).promise;
 const pages=[];
 for(let i=1;i<=doc.numPages;i++){const page=await doc.getPage(i),vp=page.getViewport({scale:1}),tc=await page.getTextContent(),ol=await page.getOperatorList();pages.push({i:i-1,W:vp.width,H:vp.height,tc,ol,page})}
 // body size: the most common text size, weighted by characters
 const weight={};pages.forEach(P=>P.tc.items.forEach(it=>{const s=Math.round(Math.abs(it.transform[3])*2)/2;if(s>0)weight[s]=(weight[s]||0)+it.str.length}));
 const body=+Object.entries(weight).sort((a,b)=>b[1]-a[1])[0]?.[0]||10;
 const marks=[];let seq=0;const add=m=>{m.id="a"+(++seq);marks.push(m);return m};
 const nrm=(P,x0,y0,x1,y1)=>({x:clamp01(x0/P.W),y:clamp01((P.H-y1)/P.H),w:Math.min(1,(x1-x0)/P.W),h:Math.min(1,(y1-y0)/P.H)});
 // lines that sit in the removed header/footer areas, or above the (cropped) page, are not part of the document
 const topPct=P=>0,botPct=0;   // the Top/Bottom % boxes do not affect tagging; repeated headers and footers are found by repetition
 const inBand=(P,yTop,yBot)=>{const fromTop=(P.H-yTop)/P.H*100,fromBot=yBot/P.H*100;return yTop>P.H+2||yBot<-2||fromTop<topPct(P)||fromBot<botPct};
 const all=[];pages.forEach(P=>groupLines(P.tc,P.H,P.page.commonObjs).forEach(l=>{if(l.x1>0&&l.x0<P.W&&!inBand(P,l.base+l.size*0.8,l.base-l.size*0.2))all.push({...l,P,key:normT(l.text)})}));
 const order=(a,b)=>a.P.i-b.P.i||b.base-a.base||a.x0-b.x0;all.sort(order);
 // page furniture: the same line (digits aside) in the same place on many pages (a browser's print header and footer, running titles, page numbers)
 if(pages.length>=4){const seen=new Map();
  all.forEach(l=>{const k=l.text.toLowerCase().replace(/\d+/g,"#").replace(/\s+/g,"")+"@"+Math.round(((l.P.H-l.base)/l.P.H)*60);(seen.get(k)||seen.set(k,new Set()).get(k)).add(l.P.i)});
  for(let i=all.length-1;i>=0;i--){const l=all[i],k=l.text.toLowerCase().replace(/\d+/g,"#").replace(/\s+/g,"")+"@"+Math.round(((l.P.H-l.base)/l.P.H)*60);if(seen.get(k).size>=Math.max(3,pages.length*0.35))all.splice(i,1)}}
 // pictures and links on each page
 for(const P of pages){
  const stack=[];let ctm=[1,0,0,1,0,0];const boxes=[],paths=[];const OPS=pdfjs.OPS;
  P.ol.fnArray.forEach((fn,j)=>{const a=P.ol.argsArray[j];
   if(fn===OPS.save)stack.push(ctm);
   else if(fn===OPS.restore)ctm=stack.pop()||[1,0,0,1,0,0];
   else if(fn===OPS.transform)ctm=mulM(a,ctm);
   else if(fn===OPS.paintFormXObjectBegin){stack.push(ctm);if(a&&a[0])ctm=mulM(a[0],ctm)}   // pictures inside a form sit in the form's own coordinates
   else if(fn===OPS.paintFormXObjectEnd)ctm=stack.pop()||[1,0,0,1,0,0];
   else if(fn===OPS.constructPath){const mm=a&&a[2];   // painted shapes (frames, boxes) are remembered, to find diagrams drawn from vectors
    if(mm&&[OPS.stroke,OPS.closeStroke,OPS.fill,OPS.eoFill,OPS.fillStroke,OPS.eoFillStroke,OPS.closeFillStroke,OPS.closeEOFillStroke].includes(a[0])){
     const q=[[mm[0],mm[1]],[mm[2],mm[1]],[mm[0],mm[3]],[mm[2],mm[3]]].map(([u,v])=>[ctm[0]*u+ctm[2]*v+ctm[4],ctm[1]*u+ctm[3]*v+ctm[5]]),qx=q.map(z=>z[0]),qy=q.map(z=>z[1]);
     paths.push({x0:Math.min(...qx),x1:Math.max(...qx),y0:Math.min(...qy),y1:Math.max(...qy),fill:![OPS.stroke,OPS.closeStroke].includes(a[0])})}}
   else if(fn===OPS.paintImageXObject||fn===OPS.paintInlineImageXObject){   // image masks are icons and backgrounds, not pictures
    const pts=[[0,0],[1,0],[0,1],[1,1]].map(([u,v])=>[ctm[0]*u+ctm[2]*v+ctm[4],ctm[1]*u+ctm[3]*v+ctm[5]]);
    const xs=pts.map(q=>q[0]),ys=pts.map(q=>q[1]);
    boxes.push({x0:Math.min(...xs),x1:Math.max(...xs),y0:Math.min(...ys),y1:Math.max(...ys)});
   }});
  const textOn=b=>all.filter(l=>l.P===P&&l.base>=b.y0&&l.base<=b.y1&&l.x0>=b.x0-2&&l.x1<=b.x1+2).reduce((n,l)=>n+l.text.length,0);
  P.imgs=boxes.filter(b=>(b.x1-b.x0)>=48&&(b.y1-b.y0)>=48&&(b.x1-b.x0)*(b.y1-b.y0)<0.6*P.W*P.H&&textOn(b)<40&&!inBand(P,b.y1,b.y0));
  P.paths=paths;P.small=boxes.filter(b=>{const w=b.x1-b.x0,h=b.y1-b.y0;return w>=8&&h>=8&&w<80&&h<80&&!(w>=48&&h>=48)&&!inBand(P,b.y1,b.y0)});
  P.links=[];try{P.links=(await P.page.getAnnotations()).filter(a=>a.subtype==="Link"&&a.rect&&a.rect[3]-a.rect[1]<30).map(a=>({x0:a.rect[0],y0:a.rect[1],x1:a.rect[2],y1:a.rect[3]}))}catch(e){}
 }
 // ---- diagrams drawn from vectors (flow charts, boxes joined by arrows): a few small pictures close together, grown out to the frames and text around them ----
 {const seenRect=new Map(),rk=b=>[b.x0,b.y0,b.x1,b.y1].map(v=>Math.round(v)).join(",");
  pages.forEach(P=>new Set(P.small.map(rk)).forEach(k=>seenRect.set(k,(seenRect.get(k)||0)+1)));
  pages.forEach(P=>{
   P.diags=[];if(P.i===0)return;
   const headIcon=b=>all.some(l=>l.P===P&&l.size>=body*1.15&&Math.abs((b.y0+b.y1)/2-(l.base+l.size*0.3))<l.size*0.9&&l.x0-b.x1>-2&&l.x0-b.x1<16);   // the little icon beside a heading
   const sm=P.small.filter(b=>!(pages.length>=4&&seenRect.get(rk(b))>=3)&&!(b.x0>P.W*0.78&&b.y1<P.H*0.25)&&!headIcon(b));   // the same small picture on many pages is page furniture
   const near=(a,b,d)=>a.x0-d<b.x1&&a.x1+d>b.x0&&a.y0-d<b.y1&&a.y1+d>b.y0,used=new Set();
   sm.forEach(b=>{
    if(used.has(b))return;const cl=[b];used.add(b);
    for(let k=0;k<cl.length;k++)sm.forEach(o=>{if(!used.has(o)&&near(cl[k],o,130)){used.add(o);cl.push(o)}});
    if(cl.length<3)return;
    // small pictures that each sit right before a line of text are list bullets (action icons), not a diagram
    const bulletLike=c=>all.some(l=>l.P===P&&Math.abs((c.y0+c.y1)/2-(l.base+l.size*0.3))<l.size*0.9&&l.x0-c.x1>=0&&l.x0-c.x1<14);
    if(cl.filter(bulletLike).length>cl.length*0.6)return;
    let r={x0:Math.min(...cl.map(c=>c.x0)),x1:Math.max(...cl.map(c=>c.x1)),y0:Math.min(...cl.map(c=>c.y0)),y1:Math.max(...cl.map(c=>c.y1))};
    const shapes=P.paths.filter(z=>z.fill&&z.x1-z.x0>=10&&z.y1-z.y0>=10&&z.x1-z.x0<300&&z.y1-z.y0<110);   // boxes and frames of the diagram, not the panels around it
    for(let it=0;it<6;it++){let grew=false;
     shapes.forEach(z=>{if(near(r,z,8)&&(z.x0<r.x0-0.5||z.x1>r.x1+0.5||z.y0<r.y0-0.5||z.y1>r.y1+0.5)){r={x0:Math.min(r.x0,z.x0),x1:Math.max(r.x1,z.x1),y0:Math.min(r.y0,z.y0),y1:Math.max(r.y1,z.y1)};grew=true}});
     if(!grew)break}
    if(r.x1-r.x0<100||r.y1-r.y0<50||(r.x1-r.x0)*(r.y1-r.y0)>0.5*P.W*P.H)return;
    if(P.imgs.some(g=>{const ix=Math.max(0,Math.min(g.x1,r.x1)-Math.max(g.x0,r.x0)),iy=Math.max(0,Math.min(g.y1,r.y1)-Math.max(g.y0,r.y0));return ix*iy>0.3*(g.x1-g.x0)*(g.y1-g.y0)}))return;
    P.diags.push(r);P.imgs.push(r);
   });
  });}
 // ---- Contents: the heading "Contents" / "Table of Contents", and every entry under it, column by column ----
 let toc=null;const tocIds=new Set(),entryIds=new Set();
 for(const P of pages.slice(0,3)){const t=findToc(all,P);if(t){toc=t;break}}
 const inToc=l=>toc&&l.P===toc.P&&l.base>=toc.y0&&l.base<=toc.y1&&l.x0>=toc.x0-2&&l.x1<=toc.x1+2;
 const tocLines=all.filter(l=>inToc(l)&&l!==toc.head);
 const cols=[];
 if(toc){[...tocLines].sort((a,b)=>a.x0-b.x0).forEach(l=>{const c=cols.find(c=>l.x0-c.x0<60&&l.x0>=c.x0-8);if(c){c.lines.push(l);c.x0=Math.min(c.x0,l.x0)}else cols.push({x0:l.x0,lines:[l]})});
  cols.sort((a,b)=>a.x0-b.x0);
  tocIds.add(add({k:"toc",p:toc.P.i,...nrm(toc.P,toc.x0,toc.y0,toc.x1,toc.y1)}).id);
  if(cols.length>1)cols.forEach(c=>{const x0=Math.min(...c.lines.map(l=>l.x0))-4,x1=Math.max(...c.lines.map(l=>l.x1))+4,y0=Math.min(...c.lines.map(l=>l.base-l.size*0.2))-3,y1=Math.max(...c.lines.map(l=>l.base+l.size*0.8))+3;tocIds.add(add({k:"toc",p:toc.P.i,...nrm(toc.P,x0,y0,x1,y1)}).id)})}
 // entry levels: by indent inside its column, else by size, else bold
 const entries=[];
 cols.forEach(c=>{const xs=[];c.lines.map(l=>l.x0).sort((a,b)=>a-b).forEach(x=>{if(!xs.length||x-xs[xs.length-1].max>7)xs.push({min:x,max:x});else xs[xs.length-1].max=x});
  const rank=x=>xs.findIndex(g=>x>=g.min-1&&x<=g.max+1);const sz=[...new Set(c.lines.map(l=>Math.round(l.size)))].sort((a,b)=>b-a);
  const nb=c.lines.filter(l=>l.bold).length;
  c.lines.sort(order).forEach(l=>{let lv;
   if(xs.length>1)lv=1+Math.min(2,rank(l.x0));
   else if(sz.length>1)lv=1+Math.min(2,sz.indexOf(Math.round(l.size)));
   else if(nb&&nb<c.lines.length)lv=l.bold?1:2;else lv=1;
   const key=normT(l.text.replace(/[\s.·…_-]*\d+\s*$/,""));if(key.length>=3&&!/^(books?|contents)$/.test(key))entries.push({key,lv});});});
window.__ent=entries.map(e=>e.lv+" "+e.key).concat(["COLS "+cols.map(c=>Math.round(c.x0)+":"+c.lines.length).join(",")]);
 // ---- title: the first line of the page after the removed header; kept out of the headings (it is saved as the PDF title) ----
 const first=pages[0];
 const title1=all.filter(l=>l.P===first&&!(toc&&l===toc.head)).sort((a,b)=>b.base+b.size-(a.base+a.size))[0];
 // Game and Edition, guessed from the header of page 1: a best-effort suggestion the user can edit or clear, not a reliable fact
 const detect={game:"",edition:""};
 {const P=first,band=all.filter(l=>l.P===P&&l.base>P.H-150);   // the top ~150pt, where a site's logo and nav sit, above the document's own title
  const inPic=l=>(P.imgs||[]).some(b=>l.x0>=b.x0-2&&l.x1<=b.x1+2&&l.base>=b.y0-2&&l.base<=b.y1+2);
  const ordinal=n=>n+(/1$/.test(n)&&!/11$/.test(n)?"st":/2$/.test(n)&&!/12$/.test(n)?"nd":/3$/.test(n)&&!/13$/.test(n)?"rd":"th")+" Edition";
  const edRe=/(\d{1,2})\s*(?:st|nd|rd|th)\s*edition\b/i;
  let hit=null;
  for(const l of band){const mm=l.text.match(edRe);if(mm){hit=mm;break}}
  if(!hit)for(const l of all){if(l.P!==P)continue;const mm=l.text.match(edRe);if(mm){hit=mm;break}}
  if(hit)detect.edition=ordinal(hit[1]);
  if(!detect.edition){   // a Books table row: an "Edition" column header with a plain number beneath it
   const hdr=all.find(l=>l.P===P&&/^edition$/i.test(l.text.trim()));
   if(hdr){const val=all.find(l=>l.P===P&&l!==hdr&&Math.abs(l.x0-hdr.x0)<30&&hdr.base-l.base>0&&hdr.base-l.base<40&&/^\d{1,2}$/.test(l.text.trim()));
    if(val)detect.edition=ordinal(val.text.trim())}}
  const KNOWN=[   // the more specific spin-off game first: its own rulebook usually also names the parent setting (e.g. Kill Team mentions "Warhammer 40,000")
   [/\bkill\s*team\b/i,"Kill Team"],[/horus\s*heresy/i,"Warhammer: The Horus Heresy"],[/\bnecromunda\b/i,"Necromunda"],[/\bblood\s*bowl\b/i,"Blood Bowl"],
   [/halo[:\s]*flashpoint/i,"Halo: Flashpoint"],[/warhammer\s*underworlds/i,"Warhammer Underworlds"],[/\bwarcry\b/i,"Warcry"],
   [/warhammer\s*40[,.]?000|warhammer\s*40k\b/i,"Warhammer 40,000"],[/warhammer\s*age\s*of\s*sigmar|\bage\s*of\s*sigmar\b/i,"Warhammer Age of Sigmar"]];
  const chrome=/^(search(\s+this\s+site.*)?|datasheets|the rules|kill teams|missions|battles|army builder|roster|login|sign in|subscribe|home|contact|about|faq|menu|books?|skip to content|contents|table of contents|index|introduction|foreword|credits|acknowledge?ments?)$/i;
  const cand=band.filter(l=>!inPic(l)&&l!==title1&&l.text.trim().length>=2&&l.text.trim().length<=40&&!chrome.test(l.text.trim())&&!edRe.test(l.text));
  for(const[re,name] of KNOWN)if(cand.some(l=>re.test(l.text))){detect.game=name;break}
  if(!detect.game)for(const[re,name] of KNOWN)if(all.some(l=>l.P===P&&re.test(l.text))){detect.game=name;break}   // said in the body text, even when the logo is a picture
  if(!detect.game&&cand.length){const big=cand.slice().sort((a,b)=>b.size-a.size)[0];if(big&&big.size>=10)detect.game=big.text.trim()}}

 // lines above the Contents on its page (the book's name, "Books" and the small block under it) are ignored
 const above=l=>toc&&l.P===toc.P&&l.base>toc.head.base+1;
 const skip=new Set(all.filter(l=>l===title1||inToc(l)||l===(toc&&toc.head)));
 all.forEach(l=>{const P=l.P,cy=l.base+l.size*0.3;if((P.diags||[]).some(r=>l.x0>=r.x0-4&&l.x1<=r.x1+4&&cy>=r.y0&&cy<=r.y1))skip.add(l)});   // text inside a diagram belongs to the picture
 // a title that wraps onto a second line (same size, right under it) is still the title
 if(title1){let prev=title1;for(const l of all){if(l.P===title1.P&&Math.abs(l.size-title1.size)<0.6&&prev.base-l.base>0&&prev.base-l.base<title1.size*1.8&&!skip.has(l)){skip.add(l);prev=l}}}
 // entries become flags: each entry takes the first unused matching line that comes after the previous match
 const flagged=new Set();let at=0;
 const isHeadLike=l=>l.size>body*1.08||l.bold;
 entries.forEach(e=>{
  const hits=[];
  for(let j=at;j<all.length;j++){const l=all[j];if(skip.has(l)||flagged.has(l))continue;const rest=l.key.startsWith(e.key)?l.key.slice(e.key.length):null;
   if(l.key===e.key||(rest!==null&&rest.length<=4&&/\d/.test(rest)))hits.push(j)}
  // a diagram or a side note can repeat the same words in plain text: prefer the occurrence that is set as a heading
  const j=hits.find(j=>isHeadLike(all[j]))??hits[0];
  if(j===undefined)return;
  const l=all[j];flagged.add(l);entryIds.add(add({k:"h"+e.lv,p:l.P.i,...nrm(l.P,l.x0-5,l.base-l.size*0.2-2,l.x1+5,l.base+l.size*0.8+2)}).id);at=j+1;
 });
 // a "Contents" with no real entry list under it (too few entries found their heading in the body) is dropped, as if there were none
 if(toc&&flagged.size<5){for(let i=marks.length-1;i>=0;i--)if(tocIds.has(marks[i].id)||entryIds.has(marks[i].id))marks.splice(i,1);flagged.clear();toc=null;skip.clear();if(title1)skip.add(title1)}
 // on the Contents page, whatever comes before the first matched heading (the book's name, intro text, a Books table) is ignored
 if(toc){const fi=all.findIndex(l=>flagged.has(l));all.forEach((l,i)=>{if(l.P===toc.P&&(fi<0||i<fi))skip.add(l)})}
 // with no usable Contents, headings are found from the document's own styles: short lines set bigger than the body, ranked by size
 const extra=[];
 if(flagged.size<8){
  const bodyLines=all.filter(l=>Math.abs(l.size-body)<0.6),bodyBold=bodyLines.length&&bodyLines.filter(l=>l.bold).length>bodyLines.length*0.5;   // when most body text is "bold", bold means nothing
  const caps=t=>/[A-Z]{3}/.test(t)&&t===t.toUpperCase();
  const cand=l=>{const t=l.text.trim();if(skip.has(l)||flagged.has(l)||t.length<2||t.length>70||/^\(?\d+[.)]?$/.test(t)||/[.;,]$/.test(t)||/^(•|◦|-|–|Q:|A:)/.test(t)||(/^\d+\./.test(t)&&l.size<body*1.12)||!/[A-Za-z]{2}/.test(t))return false;   // "2. Ready" is a heading when it is set larger than the body; at body size it is a list item
   return l.size>=body*1.12||(!bodyBold&&l.bold&&l.size>=body*0.95)||(caps(t)&&t.length<=40&&l.bold&&l.size>=body*0.98)};
  const cs=all.filter(cand),sizes=[];[...new Set(cs.map(l=>l.size))].sort((a,b)=>b-a).forEach(x=>{const g=sizes.find(g=>Math.abs(g.v-x)/g.v<0.06);if(g)g.list.push(x);else sizes.push({v:x,list:[x]})});
  const lvOf=l=>{const i=sizes.findIndex(g=>g.list.includes(l.size));return"h"+(1+Math.min(2,Math.max(0,i)))};
  cs.forEach(l=>{extra.push(l);add({k:lvOf(l),p:l.P.i,...nrm(l.P,l.x0-5,l.base-l.size*0.2-2,l.x1+5,l.base+l.size*0.8+2)})});
 }
 // bold text set at the body size (smaller than any heading) is a bold lead-in: it becomes an H3 box. A short run of bold lines is one box;
 // a long run is a bold paragraph, not a heading, and a run that holds a Q:/A: line is a question and is left alone. Skipped when most body text is bold.
 {const bl0=all.filter(l=>Math.abs(l.size-body)<0.6),bb=bl0.length&&bl0.filter(l=>l.wb).length>bl0.length*0.4;
  if(!bb){const used=new Set([...flagged,...extra,...skip]);
   const cand=all.filter(l=>l.wb&&!used.has(l)&&l.size>=body*0.9&&l.size<=body*1.1&&l.text.trim().length>=2&&/[A-Za-z]{2}/.test(l.text));
   const byP=new Map();cand.forEach(l=>{if(!byP.has(l.P))byP.set(l.P,[]);byP.get(l.P).push(l)});
   byP.forEach(ls=>{ls.sort((a,b)=>b.base-a.base);const runs=[];
    ls.forEach(l=>{const r=runs[runs.length-1],pv=r&&r[r.length-1];if(pv&&Math.abs(pv.x0-l.x0)<6&&pv.base-l.base>0&&pv.base-l.base<l.size*1.7)r.push(l);else runs.push([l])});
    runs.forEach(r=>{if(r.length>3||r.some(l=>/^\s*(•|◦|Q:|A:)/.test(l.text)||/^\(?\d+[.)]?\s*$/.test(l.text.trim()))||r.reduce((n,l)=>n+l.text.length,0)>160)return;
     const P=r[0].P;r.forEach(l=>extra.push(l));
     add({k:"h3",p:P.i,...nrm(P,Math.min(...r.map(l=>l.x0))-5,Math.min(...r.map(l=>l.base-l.size*0.2))-2,Math.max(...r.map(l=>l.x1))+5,Math.max(...r.map(l=>l.base+l.size*0.8))+2)})})})}}
 // the bold "Q:" and "A:" labels get an H3 box of their own, just around the label. The line itself stays in its paragraph, so a question and its answer still group.
 {const hb=new Set([...flagged,...extra]);
  all.forEach(l=>{if(skip.has(l)||hb.has(l)||!l.items||!l.items.length)return;
   const it=l.items.slice().sort((a,b)=>a.transform[4]-b.transform[4])[0];
   if(!it||!/^[QA]:$/.test(it.str.trim())||Math.abs(it.transform[4]-l.x0)>3)return;
   const lx0=it.transform[4],lx1=lx0+(it.width||l.size*1.2);
   add({k:"h3",p:l.P.i,...nrm(l.P,lx0-3,l.base-l.size*0.2-2,lx1+3,l.base+l.size*0.8+2)})})}
 // a short line alone on a coloured bar (a ploy or rule name on a banner, narrower than the page) is a heading, though it is set at body size
 const barHeads=[];
 {const taken0=new Set([...flagged,...extra,...skip]),tagged0=new Set([...flagged,...extra]);
  all.forEach(l=>{if(skip.has(l))return;const t=l.text.trim();
   if(t.length<3||t.length>60||/[.;,:]$/.test(t)||!/[A-Za-z]{3}/.test(t)||/^(•|◦|Q:|A:)/.test(t)||l.size<body*0.85)return;
   const P=l.P;let bar=null;
   const capsT=t===t.toUpperCase();   // an outlined box only counts when its text is in capitals
   P.paths.forEach(z=>{if(!z.fill&&!capsT)return;const w=z.x1-z.x0,h=z.y1-z.y0;
    if(w<120||w>P.W*0.75||h>l.size*3.2||h<l.size*0.9)return;
    if(z.y0>l.base-l.size*0.2||z.y1<l.base+l.size*0.7||z.x0>l.x0+3||z.x1<l.x1-3)return;
    if(!bar||h*w<(bar.y1-bar.y0)*(bar.x1-bar.x0))bar=z});
   if(!bar)return;
   if(all.some(o=>o!==l&&o.P===P&&o.base>bar.y0&&o.base<bar.y1&&o.x1>bar.x0&&o.x0<bar.x1))return;   // more than one line on the bar: a table row, not a heading
   barHeads.push({l,bar});if(tagged0.has(l))return;   // already a heading by size or by the Contents: only its group is added
   extra.push(l);add({k:"h2",p:P.i,...nrm(P,l.x0-5,l.base-l.size*0.2-2,l.x1+5,l.base+l.size*0.8+2)})});}
 const headSet=new Set([...flagged,...extra,...skip]);
 // a caption: the short centred text right under a picture (a token, an icon), up to three lines. It becomes a paragraph, and the picture and caption are grouped.
 const keepImg=(P,b)=>{if(toc&&P===toc.P&&inside(b,toc))return false;const n=nrm(P,b.x0,b.y0,b.x1,b.y1);if(n.w<0.12&&n.h<0.1&&n.x>0.78&&n.y>0.75)return false;if(P.i===0&&n.y<0.1&&n.w>0.4)return false;return true};
 const capLines=new Set(),capGroups=[];
 pages.forEach(P=>{
  // text pieces: a line is cut wherever there is a wide gap, since captions of neighbouring pictures can share one baseline
  const segs=[];all.forEach(l=>{if(l.P!==P||headSet.has(l)||!l.items||!l.items.length)return;
   const its=l.items.filter(it=>it.str&&it.str.trim()).slice().sort((a,b)=>a.transform[4]-b.transform[4]);let cur=null;
   its.forEach(it=>{const x0=it.transform[4],x1=x0+(it.width||0);
    if(cur&&x0-cur.x1<l.size*1.5){cur.text+=(/\s$/.test(cur.text)?"":" ")+it.str.trim();cur.x1=Math.max(cur.x1,x1)}
    else{cur={text:it.str.trim(),x0,x1,base:l.base,size:l.size,line:l};segs.push(cur)}})});
  const taken=new Set();
  P.imgs.filter(b=>keepImg(P,b)).forEach(b=>{
   const bw=b.x1-b.x0,cxb=(b.x0+b.x1)/2,ok=g=>!taken.has(g)&&g.size<=body*1.15&&g.x1-g.x0<=bw*2&&Math.abs((g.x0+g.x1)/2-cxb)<=bw*0.3+6;
   const first=segs.filter(g=>ok(g)&&b.y0-(g.base+g.size*0.8)>=-3&&b.y0-(g.base+g.size*0.8)<=16).sort((a,c)=>c.base-a.base)[0];
   if(!first)return;
   const run=[first];
   for(;run.length<3;){const pv=run[run.length-1],nx=segs.filter(g=>ok(g)&&!run.includes(g)&&pv.base-g.base>0&&pv.base-g.base<pv.size*2.0).sort((a,c)=>c.base-a.base)[0];if(!nx)break;run.push(nx)}
   if(run.reduce((n,g)=>n+g.text.length,0)>90)return;
   run.forEach(g=>taken.add(g));
   const x0=Math.min(...run.map(g=>g.x0)),x1=Math.max(...run.map(g=>g.x1)),y0=Math.min(...run.map(g=>g.base-g.size*0.2)),y1=Math.max(...run.map(g=>g.base+g.size*0.8));
   add({k:"para",p:P.i,...nrm(P,x0-5,y0-2,x1+5,y1+2)});
   capGroups.push({P,x0:Math.min(b.x0,x0),x1:Math.max(b.x1,x1),y0:Math.min(b.y0,y0),y1:b.y1})});
  all.forEach(l=>{if(l.P!==P)return;const mine=segs.filter(g=>g.line===l);if(mine.length&&mine.every(g=>taken.has(g)))capLines.add(l)});});
 const isBullet=t=>/^\s*[•◦▪▫■□●○◆◇‣⁃∙·*–—-]\s*\S/.test(t)||/^\s*[•◦▪▫■□●○◆◇‣⁃∙·]\s*$/.test(t);
 pages.forEach(P=>{
  const blocks=[];
  const heads=all.filter(l=>l.P===P&&headSet.has(l));
  all.filter(l=>l.P===P&&!headSet.has(l)&&!capLines.has(l)).forEach(l=>{
   const bu=isBullet(l.text);   // list items are indented and spaced out more than ordinary lines
   const b=blocks.find(b=>{const lst=bu||b.bullet;return b.wi===!!l.wi&&Math.abs(b.x0-l.x0)<(lst?44:24)&&b.last.base-l.base>0&&b.last.base-l.base<(lst?3.2:2.4)*l.size
     &&!heads.some(h=>h.base<b.last.base-1&&h.base>l.base+1&&h.x1>l.x0-4&&h.x0<Math.max(b.x1,l.x1)+4)});   // a heading between two lines splits the paragraph
   if(b){b.lines.push(l);b.last=l;b.x0=Math.min(b.x0,l.x0);b.x1=Math.max(b.x1,l.x1);if(bu)b.bullet=true}
   else blocks.push({x0:l.x0,x1:l.x1,lines:[l],last:l,bullet:bu,wi:!!l.wi});
  });
  // text sitting within 5 points under a picture (a caption) is kept even if it is short or a single line
  const underImg=b=>{const top=Math.max(...b.lines.map(l=>l.base+l.size*0.8));return P.imgs.some(g=>g.y0-top>=-3&&g.y0-top<=5&&b.x0<g.x1&&b.x1>g.x0)};
  P.blocks=blocks.map(b=>({wi:b.wi,ok:b.bullet||(b.lines.length>=2&&b.lines.reduce((n,l)=>n+l.text.length,0)>=60)||underImg(b)||(b.wi&&b.lines.reduce((n,l)=>n+l.text.length,0)>=25),x0:b.x0,x1:b.x1,top:Math.max(...b.lines.map(l=>l.base+l.size*0.8)),bot:Math.min(...b.lines.map(l=>l.base-l.size*0.2)),size:b.lines[0].size,n:b.lines.length,first:b.lines[0].text.trim()}));
  P.tagImgs=[];
  blocks.filter(b=>b.bullet||(b.lines.length>=2&&b.lines.reduce((n,l)=>n+l.text.length,0)>=60)||underImg(b)||(b.wi&&b.lines.reduce((n,l)=>n+l.text.length,0)>=25)).forEach(b=>{
   const top=Math.max(...b.lines.map(l=>l.base+l.size*0.8)),bot=Math.min(...b.lines.map(l=>l.base-l.size*0.2));
   add({k:"para",p:P.i,...nrm(P,b.x0-5,bot-2,b.x1+5,top+2)});   // 5pt padding left and right, 2pt top and bottom
  });
 });
 // pictures (never the Contents area, the hexagon icons, or the site's menu bar)
 for(const P of pages)P.imgs.forEach(b=>{
  if(!keepImg(P,b))return;
  add({k:"img",p:P.i,...nrm(P,b.x0,b.y0,b.x1,b.y1)});P.tagImgs.push(b);   // the box's inside touches the picture's outside: no padding
 });
 capGroups.forEach(c=>add({k:"grp",p:c.P.i,...nrm(c.P,c.x0-4,c.y0-4,c.x1+4,c.y1+4)}));
 // ---- groups: a picture with the text beside it, and a "Q:" with its answer ----
 for(const P of pages){
  const G=(x0,y0,x1,y1,pad)=>add({k:"grp",p:P.i,...nrm(P,x0-pad,y0-pad,x1+pad,y1+pad)});
  // a name on a bar or in a box, with its italic description and its rule text: one group
  barHeads.filter(h=>h.l.P===P).forEach(({l:h,bar})=>{
   const col=b=>b.x0>=bar.x0-12&&b.x0<=bar.x1&&b.top<=h.base+h.size;
   const others=[...headSet].filter(o=>o.P===P&&o!==h&&o.base<h.base&&o.x0<bar.x1&&o.x1>bar.x0);
   const bs=(P.blocks||[]).filter(col).sort((a,b)=>b.top-a.top);
   const run=[];let prevBot=bar.y0;
   for(const b of bs){if(run.length>=4||prevBot-b.top>2.6*h.size||prevBot-b.top<-2)break;
    if(others.some(o=>o.base+o.size*0.8>b.bot&&o.base<prevBot+h.size))break;run.push(b);prevBot=b.bot}
   if(!run.length)return;   // whatever text (one combined paragraph, or a separate description and rule) sits right under this heading
   G(Math.min(bar.x0,...run.map(b=>b.x0)),Math.min(...run.map(b=>b.bot)),Math.max(bar.x1,...run.map(b=>b.x1)),Math.max(bar.y1,h.base+h.size*0.8),4);});
  const bl=(P.blocks||[]).slice().sort((a,b)=>b.top-a.top);
  const used=new Set();
  P.tagImgs.forEach(g=>{
   if(toc&&P===toc.P&&inside(g,toc))return;
   const beside=bl.filter(b=>b.x0>=g.x1-3&&b.x0-g.x1<50&&Math.min(b.top,g.y1)-Math.max(b.bot,g.y0)>0.4*Math.min(b.top-b.bot,g.y1-g.y0));
   if(!beside.length||beside.reduce((n,b)=>n+b.n,0)>4||Math.max(...beside.map(b=>b.top))-Math.min(...beside.map(b=>b.bot))>1.6*(g.y1-g.y0)+10)return;   // a short note beside the picture, not a long text
   const u={x0:Math.min(g.x0,...beside.map(b=>b.x0)),x1:Math.max(g.x1,...beside.map(b=>b.x1)),y0:Math.min(g.y0,...beside.map(b=>b.bot)),y1:Math.max(g.y1,...beside.map(b=>b.top))};
   if(P.tagImgs.some(o=>o!==g&&inside(o,u)))return;   // two pictures in one box: left as they are
   beside.forEach(b=>{used.add(b);if(!b.ok)add({k:"para",p:P.i,...nrm(P,b.x0-5,b.bot-2,b.x1+5,b.top+2)})});G(u.x0,u.y0,u.x1,u.y1,4);   // short text beside a picture is a paragraph too
  });
  for(let i=0;i<bl.length;i++){
   if(used.has(bl[i])||!/^Q:/.test(bl[i].first))continue;
   const run=[bl[i]];
   for(let j=i+1;j<bl.length&&run.length<4;j++){const a=run[run.length-1],b=bl[j];
    if(/^Q:/.test(b.first)||used.has(b)||Math.abs(b.x0-run[0].x0)>40||a.bot-b.top>3*a.size||a.bot-b.top<-2)break;run.push(b)}
   if(run.length<2)continue;
   run.forEach(b=>used.add(b));
   G(Math.min(...run.map(b=>b.x0)),Math.min(...run.map(b=>b.bot)),Math.max(...run.map(b=>b.x1)),Math.max(...run.map(b=>b.top)),4);
  }
 }
 // Kill Team: each datacard is tagged as text (title, flavour, stats, weapons, abilities, keywords, base) in place of the pictures and paragraphs found above
 if(PROFILES[tagProfile]&&PROFILES[tagProfile].cards){
  const finder={killteam:datacardCards,aos:aosCards}[PROFILES[tagProfile].finder]||datacardCards;
  pages.forEach(P=>{let cards=[];try{cards=finder(P)}catch(e){console.warn("cards",e)}
   cards.forEach(c=>{const R=c.rect,n0=nrm(P,R[0],R[1],R[2],R[3]);
    for(let i=marks.length-1;i>=0;i--){const m=marks[i];if(m.p!==P.i||m.k==="toc")continue;const mx=m.x+(m.w||0)/2,my=m.y+(m.h||0)/2;if(mx>=n0.x&&mx<=n0.x+n0.w&&my>=n0.y&&my<=n0.y+n0.h)marks.splice(i,1)}
    c.tags.forEach(t=>add({k:t.k,role:t.role,p:P.i,...nrm(P,t.box[0],t.box[1],t.box[2],t.box[3])}));
   })})}
 // nothing but the Contents tags inside the Contents area
 const tocBoxes=marks.filter(m=>m.k==="toc");
 const out=marks.filter(m=>{
  if(m.k==="toc"||!tocBoxes.length)return true;
  const t=tocBoxes[0];if(m.p!==t.p)return true;
  const w=m.w||0,h=m.h||0;
  const ix=Math.max(0,Math.min(m.x+w,t.x+t.w)-Math.max(m.x,t.x)),iy=Math.max(0,Math.min(m.y+h,t.y+t.h)-Math.max(m.y,t.y));
  if(w&&h)return ix*iy<0.3*w*h;
  return !(m.x>=t.x&&m.x<=t.x+t.w&&m.y>=t.y&&m.y<=t.y+t.h);
 });
 out.meta={game:detect.game,edition:detect.edition};
 return out;
}
const normT=t=>String(t||"").toLowerCase().replace(/[^a-z0-9]+/g,"");   // no spaces at all: "Fire fi ght" matches "FIREFIGHT"
function inside(b,t){const ix=Math.max(0,Math.min(b.x1,t.x1)-Math.max(b.x0,t.x0)),iy=Math.max(0,Math.min(b.y1,t.y1)-Math.max(b.y0,t.y0));return ix*iy>0.8*(b.x1-b.x0)*(b.y1-b.y0)}
// finds "Contents" or "Table of Contents" on a page, and the area holding its entries (every column), in page coordinates
function findToc(all,P){
 const head=all.find(q=>q.P===P&&/^(table of )?contents$/i.test(q.text.trim()));
 if(!head)return null;
 const below=P.links.filter(k=>k.y1<=head.base+4);
 const spans=below.length>=5?below:all.filter(q=>q.P===P&&q.base<head.base-2&&q.text.trim().length>1).map(q=>({x0:q.x0,x1:q.x1,y0:q.base-q.size*0.2-2,y1:q.base+q.size*0.8}));
 if(!spans.length)return null;
 // the entries run on without a big gap; whatever sits further down the page (the Books table) is not part of them
 spans.sort((a,b)=>b.y1-a.y1);let low=spans[0].y0,keepN=1;for(let i=1;i<spans.length;i++){if(low-spans[i].y1>45)break;low=Math.min(low,spans[i].y0);keepN++}
 spans.length=keepN;
 const r={P,head,x0:Math.min(head.x0,...spans.map(k=>k.x0))-4,x1:Math.max(head.x1,...spans.map(k=>k.x1))+4,y0:Math.min(...spans.map(k=>k.y0))-4,y1:head.base+head.size*0.8+4};
 return r;
}
async function tagAll(){
 const f=items[sel];if(!f)return;
 const go=async()=>{try{msg("Tagging the file…");const m=await autoTagFile(f);f.marks=m;tagSelId=null;paintTags();
  const guess=m.meta;if(guess){if(guess.game&&!$("game").value.trim())$("game").value=guess.game;if(guess.edition&&!$("ed").value.trim())$("ed").value=guess.edition;if((guess.game||guess.edition)&&typeof paint==="function")paint()}
  msg(`Tag All added ${m.length} tags. Check them, then drag or delete any that are wrong.`+(guess&&(guess.game||guess.edition)?` Guessed ${[guess.game,guess.edition].filter(Boolean).join(", ")} for the Name step — check it.`:""),"ok")}catch(e){msg("Tag All couldn't read this file: "+e.message,"err")}};
 if((f.marks||[]).length)pop("Replace the current tags with the suggested ones?",{ok:"Replace",onOk:go});else go();
}
$("prof").innerHTML=Object.keys(PROFILES).map(k=>`<option value="${k}">${PROFILES[k].label}</option>`).join("");const showProf=()=>{$("prof").value=tagProfile;$("profh").textContent=PROFILES[tagProfile].hint};showProf();$("prof").onchange=()=>{tagProfile=$("prof").value;showProf();try{localStorage.setItem("rf-ps-profile",tagProfile)}catch(e){}};
$("tagall").onclick=tagAll;
</script></html>
"""

def _alert(text):
    try: (Path(__file__).resolve().parent / "pdf-studio-error.txt").write_text(text, "utf-8")
    except Exception: pass
    try:
        import tkinter, tkinter.messagebox
        r = tkinter.Tk(); r.withdraw(); r.attributes("-topmost", True); tkinter.messagebox.showerror("RulesForge PDF Studio", text); r.destroy()
    except Exception:
        try:
            import ctypes; ctypes.windll.user32.MessageBoxW(0, text, "RulesForge PDF Studio", 0x10)
        except Exception: print(text)


def main():
    _load_files()
    port = int(os.environ.get("PORT", 8960))
    st = claim_port(port)
    if st == "same":   # already running this exact version: just open it
        if not os.environ.get("NO_BROWSER"): webbrowser.open(f"http://127.0.0.1:{port}")
        return
    for attempt in range(25):   # an older copy may still be running on the same port; use the next free one
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H); SRV[0] = srv; break
        except OSError:
            port += 1
    else:
        raise SystemExit("Couldn't find a free port. Close other PDF Studio windows and try again.")
    if port != int(os.environ.get("PORT", 8960)):
        print("NOTE: an older PDF Studio is still open in another window; this new one is using a different address. Close the old window.")
    print(f"PDF Studio is running at http://127.0.0.1:{port}  (Ctrl+C to quit)\nSaving to: {OUT}")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    if not os.environ.get("NO_BROWSER"): threading.Thread(target=watch, args=(srv,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _save_files()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback; _alert("PDF Studio couldn't start:\n\n" + traceback.format_exc()[-900:]); sys.exit(1)
