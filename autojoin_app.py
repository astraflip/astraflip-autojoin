import asyncio, time, collections
from playwright.async_api import async_playwright

import json, os, html as _html
import sys, subprocess, threading as _th
BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
PROFILES = os.path.join(BASE, "profiles")

# ---- version + signed auto-update ----
APP_VERSION = "1.1.1"
UPDATE_PUBKEY = "__SET_BY_RELEASE__"   # filled in by release.py with your public signing key (safe to share)
UPDATE_REPO = "astraflip/astraflip-autojoin"
UPDATE_LATEST = f"https://github.com/{UPDATE_REPO}/releases/latest/download/"
UPDATE_ALLOWED = f"https://github.com/{UPDATE_REPO}/releases/download/"

# Packaged .exe: use the normal shared browser folder (%LOCALAPPDATA%\ms-playwright), not a temp folder
if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.environ.get("LOCALAPPDATA", BASE), "ms-playwright")
SETTINGS_FILE = os.path.join(BASE, "settings.json")
DATA_FILE = os.path.join(BASE, "data.json")

DEFAULTS = {
    # general
    "url": "https://astraflip.com", "accounts": 4, "auto_start": False, "headless": False,
    # giveaways
    "auto_join": True,
    "tick": 1.0,          # seconds between giveaway checks
    "arrow_every": 2.5,   # seconds between arrow clicks per account (the site also auto-scrolls)
    "join_retry": 3.0,    # never click Join on the same giveaway again within this many seconds
    "fast_join": True,    # join new giveaways straight from the site's data, without waiting for the Join button
    "join_stagger": 0.3,  # account N waits (N-1) x this before its first Join click
    "limit_wait": 2.0,    # seconds to wait after 'Too many requests'
    # marketplace
    "market_tick": 0.05, "m_max_rate": 2.5, "m_max_price": 20.0, "m_budget": 50.0, "m_watch_only": True,
    # network: the coinflip page keeps re-downloading the coinflip list; the bot doesn't need it
    "skip_coinflips": True,
    # log + data
    "hide_timeouts": False, "traffic_log": True, "log_lines": 250, "save_data": True,
    # watchdog + alerts
    "watchdog_min": 3.0,          # reload a window stuck without the giveaway box for this many minutes (0 = off)
    "notify_windows": True, "discord_webhook": "",
    "notify_wins": True, "notify_buys": True, "notify_problems": True,
    # community stats + giveaway feed go through the relay (it holds the Discord bot token / webhook, never the app)
    "relay_url": "https://astraflip-stats.astraflip-autojoin.workers.dev", "share_stats": True, "post_feed": True,
    "install_id": "",                # random ID for this copy, made on first run (no personal info)
    "skip_extras": True,             # non-buyer windows skip chat / deposit / withdrawal refreshes (less traffic)
    "auto_update_check": True,
}
CFG = dict(DEFAULTS)
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        CFG.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
except Exception:
    pass
if ".workers.dev" in CFG.get("relay_url", "") and "astraflip-autojoin" not in CFG["relay_url"]:
    CFG["relay_url"] = DEFAULTS["relay_url"]   # relay moved to its new address


if not CFG.get("install_id"):
    import secrets as _secrets
    CFG["install_id"] = _secrets.token_hex(16)
    _NEW_INSTALL = True
else:
    _NEW_INSTALL = False


def save_cfg():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(CFG, f, indent=2)
    except Exception:
        pass


def skip_feeds():
    return ("/api/coinflips",) if CFG["skip_coinflips"] else ()

# Cheap check: has anything on the page changed since last time? (MutationObserver sets a flag)
DIRTY_JS = """
() => {
  if (!window.__ajObs && document.body) {
    window.__ajDirty = true;
    window.__ajObs = new MutationObserver(() => { window.__ajDirty = true; });
    window.__ajObs.observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  const d = !!window.__ajDirty; window.__ajDirty = false; return d;
}
"""

# Any message the site is showing (toasts / alerts), e.g. why a purchase failed
SITE_MSG_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const els = [...document.querySelectorAll('[role="alert"], [role="status"], [class*="toast" i], [class*="alert" i]')]
    .filter(e => vis(e) && e.innerText && e.innerText.trim().length > 2 && e.innerText.trim().length < 160);
  return els.map(e => e.innerText.trim().replace(/\\s+/g, ' ')).filter((t, i, a) => a.indexOf(t) === i).join(' | ');
}
"""

# Cheap cart read, used while waiting for the clicked item to land in the cart
CART_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const buy = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Buy' && vis(b));
  if (buy) buy.setAttribute('data-ajm', 'buy');
  const tot = [...document.querySelectorAll('p, span, div')].find(e => e.children.length === 0 && vis(e) && e.textContent.trim() === 'Total');
  let total = 0;
  if (tot) { const m = tot.parentElement.innerText.match(/\\$\\s*([\\d,]+(?:\\.\\d+)?)/); if (m) total = parseFloat(m[1].replace(/,/g, '')); }
  return { hasBuy: !!buy, total };
}
"""

MAIN_SEL = '[data-aj="main"]'
PREV_SEL = 'button[aria-label="Previous giveaway"]'
NEXT_SEL = 'button[aria-label="Next giveaway"]'

# Runs in each account's page every tick: reads login info + giveaway box, updates the badge.
SCAN_JS = """
([joins, paused]) => {
  // --- login info: header shows Withdraw / Deposit / $balance / avatar + username when logged in
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const dep = [...document.querySelectorAll('button, a')].find(e => e.textContent.trim() === 'Deposit' && vis(e));
  let user = '', balance = '', avatar = '';
  if (dep) {
    let bar = dep;
    for (let k = 0; k < 6 && bar.parentElement && !user; k++) {
      bar = bar.parentElement;
      const leaves = [...bar.querySelectorAll('*')].filter(e => e.children.length === 0 && vis(e) && e.textContent.trim());
      const dr = dep.getBoundingClientRect(), mid = dr.top + dr.height / 2;
      const sameRow = e => { const r = e.getBoundingClientRect(); return Math.abs(r.top + r.height / 2 - mid) < 30; };
      const right = leaves.filter(e => dep.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING
                                       && !dep.contains(e) && sameRow(e));
      const names = right.filter(e => { const t = e.textContent.trim();
        return !/^(Withdraw|Deposit)$/i.test(t) && !/[$]/.test(t) && !/^[\\d.,\\sKMB]+$/i.test(t); });
      const SKIP = /^(faq|help|support|terms|terms of service|privacy|privacy policy|discord|twitter|rules|fair|provably fair|affiliates?|rewards?|leaderboard|login|sign ?in|register)$/i;
      const cand = names.filter(e => !SKIP.test(e.textContent.trim()));
      const nearImg = el => { let n = el; for (let u = 0; u < 3 && n; u++, n = n.parentElement)
                                if (n.querySelector && [...n.querySelectorAll('img')].some(vis)) return true; return false; };
      if (cand.length) {
        const leaf = cand.filter(nearImg).pop() || cand[cand.length - 1];
        user = leaf.textContent.trim();
        let n = leaf;
        for (let u = 0; u < 4 && n && !avatar; u++, n = n.parentElement) {
          const im = n.querySelector && [...n.querySelectorAll('img')].find(vis);
          if (im) avatar = im.src;
        }
        const m = bar.innerText.match(/\\$\\s*[\\d.,]+[KMB]?/i);
        if (m) balance = m[0].replace(/\\s/g, '');
      }
    }
  }
  const logged = !!dep;

  // --- giveaway box
  const prev = document.querySelector('button[aria-label="Previous giveaway"]');
  const next = document.querySelector('button[aria-label="Next giveaway"]');
  document.querySelectorAll('[data-aj]').forEach(e => e.removeAttribute('data-aj'));
  let main = prev && prev.nextElementSibling !== next ? prev.nextElementSibling : null;
  if (!main) {
    // only one giveaway running: there are no arrows, so find the Join/Joined button (or winner text) directly
    main = [...document.querySelectorAll('button')].find(b => vis(b) && /^join(ed)?$/i.test(b.textContent.trim()))
        || [...document.querySelectorAll('div, p, span')].find(e => vis(e) && e.children.length <= 3
              && /won the giveaway/i.test(e.textContent) && e.textContent.length < 80);
  }
  if (main) main.setAttribute('data-aj', 'main');
  const text = main ? main.textContent.trim() : '';
  let card = prev ? prev.parentElement && prev.parentElement.parentElement : null;
  if (!card && main) {
    card = main;
    for (let u = 0; u < 6 && card.parentElement && !card.querySelector('[title]'); u++) card = card.parentElement;
  }
  const nameEl = card ? card.querySelector('[title]') : null;
  const name = nameEl ? nameEl.getAttribute('title') : '';
  const itemImg = card && card.querySelector('img') ? card.querySelector('img').src : '';
  const valEl = card ? [...card.querySelectorAll('p')].find(p => /^[\\d.,]+\\s*[KMB]?$/i.test(p.textContent.trim())) : null;
  const value = valEl ? valEl.textContent.trim() : '';
  const limited = /too many requests/i.test(document.body ? document.body.innerText : '');

  // --- small in-page badge
  let badge = document.getElementById('aj-badge');
  if (!badge && document.body) {
    badge = document.createElement('div');
    badge.id = 'aj-badge';
    badge.style.cssText = 'position:fixed;bottom:8px;left:8px;z-index:99999;padding:4px 8px;border-radius:6px;font:12px sans-serif;color:#fff;opacity:.85;pointer-events:none';
    document.body.appendChild(badge);
  }
  if (badge) {
    badge.textContent = paused ? `AutoJoin ⏸ paused · joins: ${joins}`
      : main ? `AutoJoin ✓ watching (${text.slice(0, 20)}) · joins: ${joins}`
      : 'AutoJoin ✗ giveaway not found';
    badge.style.background = paused ? '#9a6700' : main ? '#1a7f37' : '#a40e26';
  }

  const offline = /could not reach the server/i.test(document.body ? document.body.innerText : '');
  const base = { found: !!main, logged, user, balance, avatar, limited, offline };
  if (!main) return base;
  const wm = text.match(/^(.*?)\\s*won the giveaway/i);
  const winner = wm ? wm[1].trim() : '';
  const isBtn = !!main && main.tagName === 'BUTTON';
  const isJoin = isBtn && !main.disabled && /^join/i.test(text) && !/^joined/i.test(text);
  // whole box text (host name + countdown live in here), widened until it has more than the button
  let tc = card;
  for (let u = 0; u < 3 && tc && tc.parentElement && (tc.innerText || '').split('\\n').length < 5; u++) tc = tc.parentElement;
  const cardText = tc ? (tc.innerText || '').slice(0, 600) : '';
  return Object.assign(base, { text, winner, name, itemImg, value, cardText, isJoin, isBtn, disabled: !!main && !!main.disabled,
                               prevOff: !prev || prev.disabled, nextOff: !next || next.disabled });
}
"""


MARKET_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const money = t => parseFloat(String(t).replace(/[^0-9.]/g, '')) || 0;
  const num = t => { const m = String(t).match(/([\d.,]+)\s*([KMB]?)/i); if (!m) return 0;
    return parseFloat(m[1].replace(/,/g, '')) * ({K: 1e3, M: 1e6, B: 1e9}[m[2].toUpperCase()] || 1); };
  document.querySelectorAll('[data-ajm]').forEach(e => e.removeAttribute('data-ajm'));
  const cards = [], seen = new Set();
  const priceLeaves = [...document.querySelectorAll('*')].filter(e => e.children.length === 0 && vis(e)
                       && /^\$[\d,]+(\.\d+)?$/.test(e.textContent.trim()));
  for (const leaf of priceLeaves) {
    let n = leaf;
    for (let up = 0; up < 6 && n.parentElement; up++) {
      n = n.parentElement;
      const lines = [...n.querySelectorAll('*')].filter(e => e.children.length === 0 && vis(e))
                      .map(e => e.textContent.trim()).filter(Boolean);
      if (lines.filter(l => /^\$[\d,]+(\.\d+)?$/.test(l)).length > 1) break;  // grew past one listing
      const rate = lines.find(l => /^\d+\.\d+$/.test(l));
      const pct = lines.find(l => /^[+-]?[\d.]+%$/.test(l));
      if (rate && pct) {
        if (seen.has(n)) break;
        seen.add(n);
        const value = lines.find(l => /^[\d.,]+\s*[KMB]?$/i.test(l) && l !== rate);
        const k = cards.length;
        n.setAttribute('data-ajm', String(k));
        // the price/rate block may be only the bottom half of the card: widen to the whole card for its picture
        let box = n;
        const prices = el => [...el.querySelectorAll('*')].filter(e => e.children.length === 0 && vis(e)
                               && /^\$[\d,]+(\.\d+)?$/.test(e.textContent.trim())).length;
        for (let up2 = 0; up2 < 4 && box.parentElement && prices(box.parentElement) === 1; up2++) box = box.parentElement;
        const im = [...box.querySelectorAll('img')].find(x => x.currentSrc || x.src || x.dataset.src);
        let pic = im ? (im.currentSrc || im.src || im.dataset.src) : '';
        if (!pic) for (const e of box.querySelectorAll('*')) {
          const bg = getComputedStyle(e).backgroundImage;
          if (bg && bg.startsWith('url(')) { pic = bg.slice(4, -1).replace(/["']/g, ''); break; }
        }
        const img = { src: pic };
        cards.push({ k, name: lines.find(l => !/^[\d$+.,%\sKMB-]+$/i.test(l)) || lines[0],
                     rate: parseFloat(rate), price: money(leaf.textContent), value: num(value || ''),
                     img: img ? img.src : '' });
        break;
      }
    }
  }
  const buy = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Buy' && vis(b));
  if (buy) buy.setAttribute('data-ajm', 'buy');
  const totLeaf = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && vis(e) && e.textContent.trim() === 'Total');
  let cartTotal = 0;
  if (totLeaf) { const m = totLeaf.parentElement.innerText.match(/\$\s*[\d,]+(\.\d+)?/); if (m) cartTotal = money(m[0]); }
  const SORTS = ['Highest value', 'Lowest value', 'Lowest rate', 'Best deal', 'Newest'];
  const sortBtn = [...document.querySelectorAll('button, [role="combobox"], [aria-haspopup]')]
                   .find(e => vis(e) && SORTS.includes(e.textContent.trim()));
  if (sortBtn) sortBtn.setAttribute('data-ajm', 'sort');
  const sort = sortBtn ? sortBtn.textContent.trim() : '';
  const limited = /too many requests/i.test(document.body ? document.body.innerText : '');
  return { cards, sort, hasBuy: !!buy, buyDisabled: !!(buy && buy.disabled), cartTotal, limited };
}
"""

UI_HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"><title>AstraFlip AutoJoin</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root { --g: #3ddc84; --g2: #16a34a; --txt: #e8fff1; --mut: #8fb5a0; }
  html, body { min-height: 100%; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif; color: var(--txt); overflow-y: auto; background-attachment: fixed;
    background: radial-gradient(circle at 15% 10%, #134d33 0%, transparent 45%),
                radial-gradient(circle at 90% 85%, #0f5a36 0%, transparent 45%), #06100c;
  }
  .blob { position: fixed; border-radius: 50%; filter: blur(60px); opacity: .55; z-index: 0;
          animation: float 18s ease-in-out infinite alternate; }
  .b1 { width: 260px; height: 260px; background: #22c55e; top: -60px; left: -40px; }
  .b2 { width: 220px; height: 220px; background: #0ea56b; bottom: 40px; right: -60px; animation-delay: -6s; }
  .b3 { width: 160px; height: 160px; background: #84f5b5; top: 45%; left: 55%; opacity: .25; animation-delay: -12s; }
  @keyframes float { to { transform: translate(40px, 30px) scale(1.1); } }
  .wrap { position: relative; z-index: 1; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .glass {
    position: relative; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.13);
    border-radius: 18px; backdrop-filter: blur(20px) saturate(150%); -webkit-backdrop-filter: blur(20px) saturate(150%);
    box-shadow: 0 8px 32px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.12); padding: 14px;
  }
  header { display: flex; align-items: center; justify-content: space-between; }
  .logo { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 18px; }
  .dot { width: 22px; height: 22px; border-radius: 50%; background: conic-gradient(var(--g) 0 50%, #0b1f16 0);
         border: 3px solid var(--g); box-shadow: 0 0 14px var(--g); }
  .pill { padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 600;
          background: rgba(255,255,255,.08); border: 1px solid rgba(255,255,255,.15); }
  .pill.run { color: #062; background: var(--g); box-shadow: 0 0 16px rgba(61,220,132,.6); }
  .pill.pause { color: #3b2a00; background: #facc15; }
  .row { display: flex; gap: 10px; align-items: center; }
  label { font-size: 11px; color: var(--mut); text-transform: uppercase; letter-spacing: .06em; }
  input { width: 100%; padding: 10px 12px; border-radius: 12px; color: var(--txt); font-size: 13px;
          background: rgba(0,0,0,.25); border: 1px solid rgba(255,255,255,.14); outline: none; }
  input:focus { border-color: var(--g); box-shadow: 0 0 0 3px rgba(61,220,132,.2); }
  .num { width: 70px; text-align: center; }
  button { cursor: pointer; border: none; border-radius: 12px; padding: 10px 16px; font-weight: 700; font-size: 13px;
           color: var(--txt); background: rgba(255,255,255,.09); border: 1px solid rgba(255,255,255,.16); transition: .15s; }
  button:hover:not(:disabled) { background: rgba(255,255,255,.16); transform: translateY(-1px); }
  button:disabled { opacity: .35; cursor: default; }
  button.primary { color: #04210f; background: linear-gradient(135deg, #5ff0a0, var(--g2)); border-color: transparent;
                   box-shadow: 0 4px 18px rgba(61,220,132,.45); }
  button.danger:hover:not(:disabled) { background: rgba(239,68,68,.25); }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .stat .v { font-size: 22px; font-weight: 800; }
  .stat .k { font-size: 11px; color: var(--mut); }
  .accs { display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; }
  .acc { display: grid; grid-template-columns: 38px 1fr auto; gap: 10px; align-items: center; padding: 10px 12px;
         border-radius: 14px; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.09); }
  .idx { width: 26px; height: 26px; border-radius: 8px; display: grid; place-items: center; font-size: 12px; font-weight: 700;
         background: rgba(61,220,132,.15); color: var(--g); }
  .av { width: 38px; height: 38px; border-radius: 50%; object-fit: cover; background: rgba(61,220,132,.15);
        border: 2px solid rgba(61,220,132,.5); display: grid; place-items: center; font-weight: 700; color: var(--g); }
  .gw { display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 10px; max-height: 300px; overflow-y: auto; }
  .gcard { padding: 10px; border-radius: 14px; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.09);
           text-align: center; }
  .gcard img.item { width: 64px; height: 64px; object-fit: contain; filter: drop-shadow(0 4px 10px rgba(0,0,0,.5)); }
  .won .gcard { border-color: rgba(252,211,77,.35); background: rgba(252,211,77,.07); }
  .won .gval { color: #fcd34d; }
  .gname { font-size: 12px; font-weight: 700; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .gval { font-size: 12px; color: var(--g); font-weight: 700; }
  .chips { display: flex; justify-content: center; margin-top: 6px; }
  .chips img, .chips span { width: 20px; height: 20px; border-radius: 50%; margin-left: -6px; border: 2px solid #0b1f16;
                            background: #134d33; font-size: 9px; display: grid; place-items: center; object-fit: cover; }
  .uname { font-weight: 700; font-size: 14px; }
  .sub { font-size: 11px; color: var(--mut); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 230px; }
  .right { text-align: right; }
  .joins { font-weight: 800; color: var(--g); }
  .st { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--mut); }
  .led { width: 8px; height: 8px; border-radius: 50%; background: #64748b; }
  .led.ok { background: var(--g); box-shadow: 0 0 8px var(--g); }
  .led.warn { background: #facc15; box-shadow: 0 0 8px #facc15; }
  .led.bad { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
  .empty { font-size: 12px; color: var(--mut); text-align: center; padding: 14px; }
  .logbox { height: 200px; display: flex; flex-direction: column; }
  #log { flex: 1; overflow-y: auto; font: 11.5px Consolas, monospace; color: #b9e8cc; line-height: 1.55; margin-top: 8px; }
  .page { display: flex; flex-direction: column; gap: 12px; }
  .tabs { display: flex; gap: 8px; padding: 8px; }
  .tab { flex: 1; background: transparent; border-color: transparent; color: var(--mut); }
  .tab.on { background: rgba(61,220,132,.15); border-color: rgba(61,220,132,.4); color: var(--g); }
  .field { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 0;
           border-bottom: 1px solid rgba(255,255,255,.06); }
  .field:last-child { border-bottom: none; }
  .field span { font-size: 13px; }
  .field input[type=number] { width: 110px; text-align: right; }
  .field input[type=text] { width: 260px; }
  .field input[type=checkbox] { width: 18px; height: 18px; accent-color: #3ddc84; }
  .coin { width: 38px; height: 38px; border-radius: 50%; object-fit: cover; box-shadow: 0 0 14px rgba(0,0,0,.35); }
  .coin.btc { background: #f7931a; } .coin.eth { background: #627eea; }
  .coin.sol { background: linear-gradient(135deg, #9945ff, #14f195); } .coin.ltc { background: #345d9d; }
  .addr { font: 11px Consolas, monospace; color: #b9e8cc; word-break: break-all; }
  h3 { font-size: 12px; color: var(--mut); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
  ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.18); border-radius: 6px; }
</style></head><body>
<div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
<div class="wrap">
  <header class="glass">
    <div class="logo"><div class="dot"></div>astraflip <span style="color:var(--g);font-weight:600">autojoin</span></div>
    <span id="status" class="pill">Idle</span>
  </header>

  <div class="tabs glass">
    <button id="tDash" class="tab on">Dashboard</button>
    <button id="tSet" class="tab">Settings</button>
    <button id="tTip" class="tab">Tip ♥</button>
  </div>

  <div id="pSet" class="page" style="display:none">
    <div class="glass">
      <div class="row">
        <button id="sSave" class="primary" style="flex:1">Save settings</button>
        <button id="sReset">Reset to defaults</button>
        <button id="sTest">Test alert</button>
        <button id="sUpd">Check for updates</button>
        <button id="sClearData" class="danger">Clear saved data</button>
      </div>
      <div class="sub" id="sNote" style="margin-top:8px;max-width:none;white-space:normal">Site URL, accounts, hidden windows and the coinflip setting apply after Stop + Start. Settings save to settings.json; history saves to data.json next to the app.</div>
    </div>
    <div id="setForm"></div>
  </div>

  <div id="pTip" class="page" style="display:none">
    <div class="glass" style="text-align:center;padding:22px">
      <h3>Total tips received</h3>
      <div id="tipTotal" style="font-size:40px;font-weight:800;color:var(--g);text-shadow:0 0 24px rgba(61,220,132,.45)">—</div>
      <div class="sub" id="tipUpd" style="max-width:none;margin-top:4px">since Nov 9, 2026</div>
      <button id="tipRefresh" style="margin-top:10px;padding:6px 14px;font-size:12px">Refresh</button>
    </div>
    <div class="glass">
      <h3>Tip the creator</h3>
      <div class="sub" style="max-width:none;white-space:normal;margin-bottom:10px">Enjoying the bot? Send a tip to any address below. Thank you! 💚</div>
      <div id="tipList" class="accs" style="max-height:none"></div>
      <div class="sub" style="max-width:none;white-space:normal;margin-top:10px">ETH address: tips on Ethereum mainnet (ETH + tokens like USDT/USDC) are counted. Solana counts native SOL. Amounts come from public blockchain explorers and refresh every 10 minutes.</div>
    </div>
  </div>

  <div id="updBar" class="glass" style="display:none;border-color:rgba(61,220,132,.6)">
    <div class="row" style="justify-content:space-between">
      <div><div class="uname" id="updTitle"></div><div class="sub" id="updSub" style="max-width:none;white-space:normal"></div></div>
      <button id="updGo" class="primary">Update now</button>
    </div>
  </div>

  <div id="pDash" class="page">
  <div class="glass">
    <div class="sub" id="cfgSum" style="max-width:none;margin-bottom:10px"></div>
    <div class="row">
      <button id="start" class="primary" style="flex:1">Start</button>
      <button id="pause" disabled>Pause</button>
      <button id="stop" class="danger" disabled>Stop</button>
    </div>
  </div>

  <div class="glass">
    <h3>Stats</h3>
    <div class="stats" style="grid-template-columns:repeat(4,1fr)">
      <div class="stat"><div class="v" id="stWins" style="color:#fcd34d">0</div><div class="k">Wins</div></div>
      <div class="stat"><div class="v" id="stValue" style="color:#fcd34d">0</div><div class="k">Value won</div></div>
      <div class="stat"><div class="v" id="stSpent">$0</div><div class="k">Auto-buy spent</div></div>
      <div class="stat"><div class="v" id="stBought">0</div><div class="k">Items bought</div></div>
    </div>
    <div id="stPer" class="sub" style="max-width:none;white-space:normal;margin-top:8px"></div>
  </div>

  <div class="stats">
    <div class="glass stat"><div class="v" id="total">0</div><div class="k">Total joins</div></div>
    <div class="glass stat"><div class="v" id="logged">0/0</div><div class="k">Logged in</div></div>
    <div class="glass stat"><div class="v" id="watching">0</div><div class="k">Watching</div></div>
  </div>

  <div class="glass">
    <h3>Accounts</h3>
    <div id="accs" class="accs"><div class="empty">Press Start to open your accounts</div></div>
  </div>

  <div class="glass">
    <div class="row" style="justify-content:space-between;margin-bottom:10px">
      <h3 style="margin:0">Marketplace auto-buy</h3>
      <span id="mStatus" class="pill">Off</span>
    </div>
    <div class="row" style="margin-bottom:10px">
      <div style="flex:1"><label>Max rate</label><input id="mRate" type="number" step="0.01" value="2.50"></div>
      <div style="flex:1"><label>Max price $</label><input id="mPrice" type="number" step="0.5" value="20"></div>
      <div style="flex:1"><label>Budget $</label><input id="mBudget" type="number" step="1" value="50"></div>
    </div>
    <div class="row" style="justify-content:space-between">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:12px">
        <input id="mWatch" type="checkbox" checked style="width:auto;accent-color:#3ddc84"> Watch only (don't buy)</label>
      <button id="mToggle" class="primary">Enable</button>
    </div>
    <div class="sub" id="mInfo" style="margin-top:8px;max-width:none"></div>
    <div id="mList" class="gw" style="margin-top:10px"></div>
  </div>

  <div class="glass won">
    <h3>Giveaways won <span id="wcount" style="color:#fcd34d"></span></h3>
    <div id="wn" class="gw"><div class="empty">No wins yet</div></div>
  </div>

  <div class="glass">
    <h3>Giveaways entered <span id="gcount" style="color:var(--g)"></span></h3>
    <div id="gw" class="gw"><div class="empty">Nothing entered yet</div></div>
  </div>

  <div class="glass logbox">
    <div class="row" style="justify-content:space-between;margin-bottom:0">
      <h3 style="margin:0">Log</h3>
      <div class="row" style="gap:8px">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:12px">
          <input id="hideTo" type="checkbox" style="width:auto;accent-color:#3ddc84"> Hide timeouts</label>
        <button id="clear" style="padding:5px 10px;font-size:12px">Clear</button>
      </div>
    </div>
    <div id="log"></div>
  </div>
  </div>
</div>
<script>
  const $ = id => document.getElementById(id);
  $('start').onclick = () => window.ajStart();
  // ---- tabs
  const show = p => { ['Dash', 'Set', 'Tip'].forEach(n => { $('p' + n).style.display = n === p ? '' : 'none';
                                                             $('t' + n).classList.toggle('on', n === p); }); };
  $('tDash').onclick = () => show('Dash');
  $('tSet').onclick = () => { show('Set'); fillSettings(window.__cfg); };
  $('tTip').onclick = () => show('Tip');
  $('tipRefresh').onclick = () => window.ajTipsRefresh();
  const LOGOS = __COIN_LOGOS__;
  window.copyAddr = (btn, text) => {
    const done = () => { btn.textContent = 'Copied ✓'; setTimeout(() => btn.textContent = 'Copy', 1500); };
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) {} ta.remove();
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, () => {});
  };
  const fmt = (n, d) => n == null ? '…' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
  // ---- settings form
  const FIELDS = [
    ['General', [['url', 'Site URL', 'text'], ['accounts', 'Number of accounts (1-10)', 'number', 1],
                 ['auto_start', 'Start automatically when the app opens', 'check'],
                 ['headless', 'Hide account browser windows (log in with them visible first)', 'check']]],
    ['Giveaways', [['auto_join', 'Auto-join giveaways', 'check'],
                   ['tick', 'Check interval (seconds)', 'number', 0.25],
                   ['arrow_every', 'Seconds between giveaway arrow clicks', 'number', 0.5],
                   ['join_retry', 'Seconds before re-clicking the same Join', 'number', 0.5],
                   ['fast_join', 'Fast-join (join the instant the site lists a giveaway)', 'check'],
                   ['join_stagger', 'Delay between accounts joining (seconds)', 'number', 0.1],
                   ['limit_wait', 'Wait after "Too many requests" (seconds)', 'number', 0.5]]],
    ['Marketplace', [['market_tick', 'Listing check interval (seconds)', 'number', 0.05],
                     ['m_max_rate', 'Max rate', 'number', 0.01], ['m_max_price', 'Max price ($)', 'number', 0.5],
                     ['m_budget', 'Budget per session ($)', 'number', 1],
                     ['m_watch_only', "Watch only (don't buy)", 'check']]],
    ['Network', [['skip_coinflips', 'Skip the coinflip list in bot windows (less traffic)', 'check']]],
    ['Log', [['hide_timeouts', 'Hide timeouts', 'check'], ['traffic_log', 'Show traffic summaries', 'check'],
             ['log_lines', 'Log lines to keep', 'number', 50]]],
    ['Watchdog', [['watchdog_min', 'Reload a stuck window after (minutes, 0 = off)', 'number', 0.5]]],
    ['Alerts', [['notify_windows', 'Windows notifications', 'check'],
                ['discord_webhook', 'Discord webhook URL (optional)', 'text'],
                ['notify_wins', 'Alert when an account wins', 'check'],
                ['notify_buys', 'Alert when auto-buy purchases something', 'check'],
                ['notify_problems', 'Alert on problems (logged out, stuck window)', 'check'],
                ['share_stats', 'Add my totals to the community stats in Discord', 'check'],
                ['post_feed', 'Help post new giveaways to the Discord feed', 'check']]],
    ['Updates', [['auto_update_check', 'Check for updates when the app opens', 'check']]],
    ['Saved data', [['save_data', 'Save wins, entries, purchases, stats and join counts between runs', 'check']]],
  ];
  $('setForm').innerHTML = FIELDS.map(([g, fs]) => `<div class="glass" style="margin-bottom:12px"><h3>${g}</h3>` +
    fs.map(([k, label, t, step]) => `<div class="field"><span>${label}</span>` +
      (t === 'check' ? `<input id="s_${k}" type="checkbox">`
       : `<input id="s_${k}" type="${t}" ${step ? `step="${step}" min="0"` : ''}>`) + '</div>').join('') + '</div>').join('');
  $('s_accounts').onchange = () => window.ajSaveSettings({ accounts: $('s_accounts').value });  // auto-save
  const fillSettings = c => { if (!c) return; FIELDS.forEach(([, fs]) => fs.forEach(([k, , t]) => {
      const el = $('s_' + k); if (t === 'check') el.checked = !!c[k]; else el.value = c[k]; })); };
  const readSettings = () => { const o = {}; FIELDS.forEach(([, fs]) => fs.forEach(([k, , t]) => {
      const el = $('s_' + k); o[k] = t === 'check' ? el.checked : el.value; })); return o; };
  const flash = t => { $('sNote').dataset.t = t; $('sSave').textContent = t; setTimeout(() => $('sSave').textContent = 'Save settings', 1500); };
  $('sSave').onclick = () => { const o = readSettings(); window.ajSaveSettings(o); window.__mInit = false; flash('Saved ✓'); };
  $('sReset').onclick = () => { window.ajResetSettings(); window.__fillNext = true; window.__mInit = false; flash('Reset ✓'); };
  $('sUpd').onclick = () => { window.ajCheckUpdate(); flash('Checking…'); };
  $('updGo').onclick = () => { if (confirm('Install the update now? Running accounts will stop and the app will restart.')) window.ajInstallUpdate(); };
  $('sTest').onclick = () => { window.ajSaveSettings(readSettings()); setTimeout(() => window.ajTestAlert(), 300); flash('Alert sent'); };
  $('sClearData').onclick = () => { if (confirm('Clear saved wins, entries, purchases and join counts?')) window.ajClearData(); };
  $('pause').onclick = () => window.ajPause();
  $('stop').onclick = () => window.ajStop();
  const mSend = (enabled) => window.ajMarket({ enabled, watch_only: $('mWatch').checked,
      max_rate: parseFloat($('mRate').value) || 0, max_price: parseFloat($('mPrice').value) || 0,
      budget: parseFloat($('mBudget').value) || 0 });
  $('mToggle').onclick = () => mSend(!window.__mOn);
  ['mRate', 'mPrice', 'mBudget', 'mWatch'].forEach(id => $(id).onchange = () => window.__mOn && mSend(true));
  $('clear').onclick = () => { window.ajClearLog(); $('log').innerHTML = ''; };
  $('hideTo').onchange = () => { window.__tail = null; window.ajSaveSettings({ hide_timeouts: $('hideTo').checked }); };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const LED = { watching: 'ok', joining: 'ok', 'rate limited': 'warn', paused: 'warn', opening: '',
                'logged out': 'bad', 'not found': 'bad', closed: '' };
  let lastLogLen = 0;
  window.render = s => {
    const c = s.cfg; window.__cfg = c;
    if (!window.__mInit) {   // load saved values into the dashboard inputs once
      window.__mInit = true;
      $('mRate').value = c.m_max_rate; $('mPrice').value = c.m_max_price; $('mBudget').value = c.m_budget;
      $('mWatch').checked = c.m_watch_only; $('hideTo').checked = c.hide_timeouts;
    }
    if (window.__fillNext) { window.__fillNext = false; fillSettings(c); }
    $('cfgSum').textContent = `${c.accounts} account(s) · ${c.url}` + (c.headless ? ' · hidden windows' : '')
                              + (c.auto_join ? '' : ' · auto-join OFF');
    const U = s.update;
    $('updBar').style.display = U.latest && (U.available || U.busy) ? '' : 'none';
    $('updTitle').textContent = U.available ? `Update available: v${U.latest}` : '';
    $('updSub').textContent = `You have v${U.current}` + (U.status ? ' · ' + U.status : '') + (U.notes ? ' · ' + U.notes : '');
    $('updGo').disabled = !!U.busy || !U.can_install;
    $('sNote').dataset.v = U.current;
    const S = s.stats, kfmt = n => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : Math.round(n);
    $('stWins').textContent = S.wins; $('stValue').textContent = '◆ ' + kfmt(S.value_won);
    $('stSpent').textContent = '$' + S.spent.toFixed(2); $('stBought').textContent = S.bought;
    $('stPer').textContent = S.per.length ? 'Wins per account: ' + S.per.map(p =>
      `#${p.i} ${p.user || ''} — ${p.wins} win${p.wins > 1 ? 's' : ''} (◆ ${kfmt(p.value)})`).join(' · ') : '';
    const T = s.tips;
    $('tipTotal').textContent = T.total_usd == null ? (T.loading ? 'checking…' : '—') : '$' + fmt(T.total_usd, 2);
    $('tipUpd').textContent = 'since Nov 9, 2026' + (T.updated ? ' · updated ' + T.updated : '') + (T.loading ? ' · checking…' : '');
    const tHtml = T.coins.map(c => `
      <div class="acc">
        <img class="coin" src="${LOGOS[c.key]}" alt="${c.ticker}">
        <div style="min-width:0">
          <div class="uname">${esc(c.name)} <span class="sub" style="margin-left:4px">${c.ticker}</span></div>
          <div class="addr">${esc(c.addr)}</div>
          <div class="sub" style="max-width:none;white-space:normal;margin-top:3px">${c.error ? '⚠ ' + esc(c.error)
            : `Received: ${fmt(c.amount, 8)} ${c.ticker}` + (c.usd != null ? ` · $${fmt(c.usd, 2)}` : '')
              + Object.entries(c.tokens || {}).map(([k, v]) => ` · ${fmt(v, 4)} ${esc(k)}`).join('')}</div>
        </div>
        <button style="padding:6px 12px;font-size:12px" onclick="copyAddr(this, '${c.addr}')">Copy</button>
      </div>`).join('');
    if (tHtml !== window.__t) { $('tipList').innerHTML = tHtml; window.__t = tHtml; }
    const st = $('status');
    st.textContent = s.stopping ? 'Stopping…' : s.running ? (s.paused ? 'Paused' : 'Running') : 'Idle';
    st.className = 'pill ' + (s.running ? (s.paused ? 'pause' : 'run') : '');
    $('start').disabled = s.running || s.stopping;
    $('pause').disabled = !s.running; $('stop').disabled = !s.running;
    $('pause').textContent = s.paused ? 'Resume' : 'Pause';

    const a = s.accounts;
    $('total').textContent = a.reduce((t, x) => t + x.joins, 0);
    $('logged').textContent = a.filter(x => x.logged).length + '/' + a.length;
    $('watching').textContent = a.filter(x => x.status === 'watching' || x.status === 'joining').length;
    $('accs').innerHTML = a.length ? a.map(x => `
      <div class="acc">
        ${x.avatar ? `<img class="av" src="${esc(x.avatar)}" title="#${x.i}">` : `<div class="av">${x.i}</div>`}
        <div>
          <div class="uname">${x.logged ? esc(x.user || 'Logged in') : '<span style="color:#f87171">Not logged in</span>'}
            ${x.balance ? `<span class="sub" style="margin-left:6px">${esc(x.balance)}</span>` : ''}</div>
          <div class="sub">${x.giveaway ? '🎁 ' + esc(x.giveaway) + (x.btn ? ' · ' + esc(x.btn) : '') : '—'}</div>
        </div>
        <div class="right">
          <div class="joins">${x.joins} joins</div>
          <div class="st"><span class="led ${LED[x.status] || ''}"></span>${esc(x.status)}</div>
        </div>
      </div>`).join('') : '<div class="empty">Press Start to open your accounts</div>';
    const byI = Object.fromEntries(a.map(x => [x.i, x]));
    $('gcount').textContent = s.entered.length ? '· ' + s.entered.length : '';
    const gHtml = s.entered.length ? s.entered.map(e => `
      <div class="gcard">
        ${e.img ? `<img class="item" src="${esc(e.img)}" data-alt="${esc(e.alt && e.alt !== e.img ? e.alt : '')}" onerror="if(this.dataset.alt){this.src=this.dataset.alt;this.dataset.alt=''}">` : '<div style="height:64px"></div>'}
        <div class="gname" title="${esc(e.name)}">${esc(e.name)}</div>
        <div class="gval">${e.value ? '◆ ' + esc(e.value) : ''}</div>
        ${e.host ? `<div class="sub" style="margin:2px auto 0">🎙 ${esc(e.host)}</div>` : ''}
        <div class="chips">${e.accounts.map(n => byI[n] && byI[n].avatar
            ? `<img src="${esc(byI[n].avatar)}" title="${esc(byI[n].user || '#' + n)}">`
            : `<span title="#${n}">${n}</span>`).join('')}</div>
      </div>`).join('') : '<div class="empty">Nothing entered yet</div>';
    if (gHtml !== window.__g) { $('gw').innerHTML = gHtml; window.__g = gHtml; }
    const m = s.market; window.__mOn = m.enabled;
    $('mToggle').textContent = m.enabled ? 'Disable' : 'Enable';
    $('mToggle').className = m.enabled ? 'danger' : 'primary';
    $('mStatus').textContent = !m.enabled ? 'Off' : m.watch_only ? 'Watching' : 'Buying';
    $('mStatus').className = 'pill ' + (!m.enabled ? '' : m.watch_only ? 'pause' : 'run');
    $('mInfo').textContent = m.enabled
      ? `Buyer: ${m.buyer || 'choosing…'} · spent $${m.spent.toFixed(2)} of $${m.budget.toFixed(2)} · ${m.note || ''}` : '';
    const mHtml = m.items.map(d => `
      <div class="gcard" style="${d.bought ? 'border-color:rgba(61,220,132,.6)' : ''}">
        ${d.img ? `<img class="item" src="${esc(d.img)}" data-alt="${esc(d.alt && d.alt !== d.img ? d.alt : '')}" onerror="if(this.dataset.alt){this.src=this.dataset.alt;this.dataset.alt=''}">` : '<div style="height:64px"></div>'}
        <div class="gname" title="${esc(d.name)}">${esc(d.name)}</div>
        <div class="gval">$${d.price.toFixed(2)} · rate ${d.rate.toFixed(2)}</div>
        <div class="sub" style="margin:4px auto 0">${d.bought ? '✅ bought' : d.failed ? '⚠ ' + esc(d.failed) : '👀 would buy'}</div>
      </div>`).join('');
    if (mHtml !== window.__m) { $('mList').innerHTML = mHtml; window.__m = mHtml; }
    $('wcount').textContent = s.won.length ? '· ' + s.won.length : '';
    const wHtml = s.won.length ? s.won.map(w => `
      <div class="gcard">
        ${w.img ? `<img class="item" src="${esc(w.img)}" data-alt="${esc(w.alt && w.alt !== w.img ? w.alt : '')}" onerror="if(this.dataset.alt){this.src=this.dataset.alt;this.dataset.alt=''}">` : '<div style="height:64px"></div>'}
        <div class="gname" title="${esc(w.name)}">${esc(w.name)}</div>
        <div class="gval">${w.value ? '◆ ' + esc(w.value) : ''}</div>
        <div class="sub" style="margin:4px auto 0">🏆 ${esc(w.user || '#' + w.i)}</div>
      </div>`).join('') : '<div class="empty">No wins yet</div>';
    if (wHtml !== window.__w) { $('wn').innerHTML = wHtml; window.__w = wHtml; }
    if (s.log.length !== lastLogLen || s.logTail !== window.__tail) {
      const box = $('log'), atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
      const lines = $('hideTo').checked ? s.log.filter(l => !/timeout|timed out/i.test(l)) : s.log;
      box.innerHTML = lines.map(esc).join('<br>');
      if (atBottom) box.scrollTop = box.scrollHeight;
      lastLogLen = s.log.length; window.__tail = s.logTail;
    }
  };
</script></body></html>
"""


COIN_LOGOS = {"btc": "data:image/png;base64,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", "sol": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAxYElEQVR42u19a5Rc1XXmt/c5t6q6S61uSQhJLalBdOuBJPQWIAGSAckKfiwc4+CMY+xhxgZjY8cEE08Se5DXTH5MMlmsyWR5Jc54Ymbs8WtlYs+K4wExWNgEiMA8BQgsAWpJtIQe3S2pH1V1z9nz45xz761SdasldbckUPW6q6qr7r116+599vPbexPOswcRETMzEZExxoiIZD9vaWlpmTVr1qyZM2e2LVu2bE2lXGmc0TpjzfHjx6Pe3l5VKBSWHD9+XFlrYa0FADAzmBlNTU1mcHDwxWKxaJqamioHDhx4Moqi/ueff/7Jffv2de7du3dvT09PT+31KKWUiIi11tZezzl/P88nogOAMcZkP5s9e/bsJUuWrLz88suvi6JoeW9v7+X79u2btGfPnvzevXvR09ODcrl8Wt8bRRGam5sxc+ZMzJo1q9TW1tY9adKkVyvlynOvvPrKr1588cVf79mzZ0/2GKWUAoDzhRnOaQbIrvTw3sSJEyeuWLFi9bJly26Komj966+/vui5555r6OzsrD1cANgMD8FvPNx3iogVEXjaBQJy9l4ppTBnzhwsWbJkoL29/eVKpfLYiy+++PNnnnnm6aNHjx7NMkOQDBcY4BQetTcun8/nN2zYsOnyyy//SG9v78Zt27bN2r59OzJ8YYhIoigiEWERgbWWPEFxJguRmQPjCBHBGGOtteLvnQIArTWWLl2KtWvX7p0wYcKWl1566Sdbtmx5qFQqlYZi5AsMMAThs6JzwYIFC2644YbfjeP4448//viCV155JexqicgqpYiI2FpL1lqICIio3qoOYqDq/4yKqft+9rPacymlwMxCRDaOY7HWspcUWL58OdavX78jjuMfPvLIIz/YsWPHjqwqO5cYgc7FFb9hw4aNq1evvvv555/ftHXr1vzAwAAAWK21BcDWWrbWVhGnlshjzQDhfSKCiMCbKBaANcYwAG5qasL73//+0qJFix568skn/2rLli1bznWJMO46Phh3AHD99ddv/PKXv/zwqlWrxOtfARArpYxSSogo+74AqPveeG1eLZxwDcwsSikDIA7vrVmzRu69996Hb7zxxo1D/f73zCO4TuH/G2+8ceN999338KJFi8JNNFrrOJfLWWauutlni9inyyBKKRtFUQzAAJDly5fLH/7hHz68YcOGjVkJSPVE17vxkSX84sWLF//BH/zB91euXJkQPpfLxVEUhVV03hF9qE0pJUqphBHWrFkj99133/cXL168uN69eVev+lwul7vrrrvu37RpUym74gPBz8fVPhJpwMwSRZFkJcItt9xS+sIXvnB/LpfLvWulQVbPbdy4cdMXv/jF7c3NzQJAisVirLWWIOrPtl4/CxJBpkyZIl/5yle2v//9799U756d1w+ttQ7P99xzzwNr164NhlIln89bZpawZVf/u50Jwm9USlkAFQByww03yL333vtA9p69K4g/d+7cuXfffffjEyZMEACmoaHB1Ir7etu7XQrUqAYDwEyYMEH+6I/+6PG5c+fOPW+ZIBu7v/nmm2+96aabDvsfXNFaJz+6djW8Fwh+MrUQpMHHP/7xw7/92799azZucN7p+zvuuGPzwoULBYAUCoU4iqILBD/JxsySy+ViALJixQr5/Oc/v/m8sQvCBTY1NTXddddd3544caIAiKMoslk9f4HQw6sErbXkcjkLIJ4+fbrcc889325qamo6p5kgXNikSZMmfelLX9rmrdxy8Onfa7r9DINHopQSrbUAKCul5Gtf+9q2SZMmTTonmSBL/M985jPb/A8pK6Uka+VnGeECsUduFzBzGYB86UtfOveYIEv83/u939sGQKIoqgRiX2CAUZMIFQDy2c9+9txhgizxb7vttoT4wcWrJf4FBjh9w1ApJVEUVQDInXfeefaZIEv8T33qU1Urv15QJ0v4Cwxw6gwQFlQul6sAkM9//vNnjwnIPxobGxu/+MUvbgMgWuvKezGQM94pZ2YWrXUFgHz1q1/d1tjY2BjoMe4RvjvvvPM7WmvJ5/PlC4QfWwaodROZuay1li9/+cvfGdeIYfii22+/fXNjY6Mopcq5XO49G8odTwYIr70UEGYuNzc3yx133LF5XJggpHM/8IEPfOySSy4RAImff8HQG3sVUMsIPrJaXrBggXz4wx/+2OlgCvhUjD5rrZ0zZ85lfX1939q9e7dorXUcx7jwGN9HQDobY5DP5/WOHTukUql867LLLrvMWmtH3SgMYA6llLr99tufAiC5KBdfcPVOZ6M626lJgGwWkZmlUCjEAOSuu+56KtBpVI3CIFY+97nPPaCUknw+XxnO1TtTBiBA2D+DIJTZzn8G4Dpb/Xvg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", "eth": "data:image/png;base64,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", "ltc": "data:image/png;base64,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"}
UI_HTML = UI_HTML.replace('__COIN_LOGOS__', json.dumps(COIN_LOGOS))

# ---------------- Tip the creator ----------------
import urllib.request, urllib.parse, threading
from datetime import datetime, timezone, timedelta

TIP_SINCE = datetime(2026, 11, 9, tzinfo=timezone(timedelta(hours=-8))).timestamp()   # Nov 9 2026, Pacific
TIP_WALLETS = [
    {"key": "btc", "name": "Bitcoin", "ticker": "BTC", "cg": "bitcoin",
     "addr": "bc1q08x87amdzzu5zcgzg4qpzfpffk70qy0jckj9dt"},
    {"key": "eth", "name": "Ethereum", "ticker": "ETH", "cg": "ethereum",
     "addr": "0x28A22a84732DE75C647d1dc5ef546a5b9bb8bA5A"},
    {"key": "sol", "name": "Solana", "ticker": "SOL", "cg": "solana",
     "addr": "DDYwMHc4PigW82EWyLasRAYCWrELAeMGnFtofngj9zB3"},
    {"key": "ltc", "name": "Litecoin", "ticker": "LTC", "cg": "litecoin",
     "addr": "LTXHQk7fFBit3nKUPvjf5HFksKqxvz3Uef"},
]
STABLES = {"USDT", "USDC", "DAI", "PYUSD", "USDE", "FDUSD"}


def _http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"User-Agent": "astraflip-autojoin", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


# ---- item icons from Rolimons (item name -> Roblox asset id) + Roblox thumbnails (asset id -> picture)
ICON_FILE = os.path.join(BASE, "icons.json")
_ICONS = {"ids": {}, "ids_at": 0, "urls": {}}       # urls: asset id -> (image url, fetched at)
_icon_lock = _th.Lock()
try:
    with open(ICON_FILE, encoding="utf-8") as f:
        _ICONS.update(json.load(f))
except Exception:
    pass


def _rolimons_ids():
    if _ICONS["ids"] and time.time() - _ICONS["ids_at"] < 86400:
        return _ICONS["ids"]
    data = _http("https://api.rolimons.com/items/v1/itemdetails")
    ids = {}
    for aid, row in (data.get("items") or {}).items():
        if row and row[0]:
            ids[str(row[0]).strip().lower()] = aid
            if len(row) > 1 and row[1]:
                ids.setdefault(str(row[1]).strip().lower(), aid)      # acronyms too
    if ids:
        _ICONS["ids"], _ICONS["ids_at"] = ids, time.time()
    return _ICONS["ids"]


def resolve_icons(names):
    """Return {name: image url} for the names Rolimons knows. Cached; thumbnail links refreshed every 12 h."""
    out = {}
    with _icon_lock:
        try:
            ids = _rolimons_ids()
        except Exception:
            ids = _ICONS["ids"]
        want = {}
        for n in names:                          # Rolimons = limiteds only (no catalog / UGC lookups)
            aid = ids.get(str(n).strip().lower())
            if aid:
                want[n] = aid
        stale = sorted({a for a in want.values()
                        if a not in _ICONS["urls"] or time.time() - _ICONS["urls"][a][1] > 43200})
        for k in range(0, len(stale), 50):
            chunk = stale[k:k + 50]
            try:
                res = _http("https://thumbnails.roblox.com/v1/assets?size=150x150&format=Png&isCircular=false"
                            "&assetIds=" + ",".join(chunk))
                for t in res.get("data", []):
                    if t.get("state") == "Completed" and t.get("imageUrl"):
                        _ICONS["urls"][str(t["targetId"])] = (t["imageUrl"], time.time())
            except Exception:
                pass
        for n, aid in want.items():
            if aid in _ICONS["urls"]:
                out[n] = _ICONS["urls"][aid][0]
        try:
            with open(ICON_FILE, "w", encoding="utf-8") as f:
                json.dump(_ICONS, f)
        except Exception:
            pass
    return out


def _utxo_received(api, addr):
    """BTC / LTC via mempool.space-style API: incoming amount (minus change) since TIP_SINCE."""
    total, last = 0, None
    for _ in range(40):
        txs = _http(f"{api}/address/{addr}/txs" + (f"/chain/{last}" if last else ""))
        if not txs:
            break
        older = False
        for tx in txs:
            t = tx.get("status", {}).get("block_time") or time.time()
            if t < TIP_SINCE:
                older = True
                continue
            out = sum(v.get("value", 0) for v in tx.get("vout", []) if v.get("scriptpubkey_address") == addr)
            spent = sum((v.get("prevout") or {}).get("value", 0) for v in tx.get("vin", [])
                        if (v.get("prevout") or {}).get("scriptpubkey_address") == addr)
            if out > spent:
                total += out - spent
        confirmed = [t for t in txs if t.get("status", {}).get("confirmed")]
        if older or len(confirmed) < 25:
            break
        last = confirmed[-1]["txid"]
    return total / 1e8, {}


def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _eth_received(addr):
    """ETH + ERC-20 tokens on Ethereum mainnet via Blockscout (no API key needed)."""
    base = f"https://eth.blockscout.com/api/v2/addresses/{addr}"
    wei, tokens = 0, {}
    for path, kind in (("transactions?filter=to", "tx"), ("token-transfers?type=ERC-20&filter=to", "tok")):
        params = ""
        for _ in range(20):
            d = _http(f"{base}/{path}{params}")
            older = False
            for it in d.get("items", []):
                if _iso(it["timestamp"]) < TIP_SINCE:
                    older = True
                    continue
                if kind == "tx":
                    if it.get("status") != "error":
                        wei += int(it.get("value") or 0)
                else:
                    tot, tok = it.get("total") or {}, it.get("token") or {}
                    sym = (tok.get("symbol") or "?").upper()
                    dec = int(tot.get("decimals") or tok.get("decimals") or 18)
                    tokens[sym] = tokens.get(sym, 0) + int(tot.get("value") or 0) / 10 ** dec
            nxt = d.get("next_page_params")
            if older or not nxt:
                break
            params = "&" + urllib.parse.urlencode(nxt)
    return wei / 1e18, tokens


def _sol_received(addr):
    """SOL received via the public Solana RPC (native SOL only)."""
    rpc = "https://api.mainnet-beta.solana.com"
    call = lambda m, p: _http(rpc, {"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).get("result")
    sigs = [s for s in (call("getSignaturesForAddress", [addr, {"limit": 200}]) or [])
            if (s.get("blockTime") or time.time()) >= TIP_SINCE and not s.get("err")]
    lamports = 0
    for s in sigs[:80]:
        tx = call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        time.sleep(0.25)                      # be gentle with the free public RPC
        if not tx:
            continue
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        if addr in keys:
            j = keys.index(addr)
            delta = tx["meta"]["postBalances"][j] - tx["meta"]["preBalances"][j]
            if delta > 0:
                lamports += delta
    return lamports / 1e9, {}


def fetch_tips():
    """Runs in a background thread. Returns per-coin amounts + USD total."""
    fns = {"btc": lambda a: _utxo_received("https://mempool.space/api", a),
           "ltc": lambda a: _utxo_received("https://litecoinspace.org/api", a),
           "eth": _eth_received, "sol": _sol_received}
    try:
        prices = _http("https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&ids="
                       + ",".join(w["cg"] for w in TIP_WALLETS))
    except Exception:
        prices = {}
    for w in TIP_WALLETS:                       # backup price source if CoinGecko refuses or misses a coin
        if not (prices.get(w["cg"]) or {}).get("usd"):
            try:
                amt = _http(f"https://api.coinbase.com/v2/prices/{w['ticker']}-USD/spot")["data"]["amount"]
                prices[w["cg"]] = {"usd": float(amt)}
            except Exception:
                pass
    coins, total = [], 0.0
    for w in TIP_WALLETS:
        c = {**w, "amount": None, "usd": None, "tokens": {}, "error": ""}
        try:
            amt, toks = fns[w["key"]](w["addr"])
            c["amount"], c["tokens"] = amt, toks
            px = (prices.get(w["cg"]) or {}).get("usd")
            stable = sum(v for k, v in toks.items() if k in STABLES)
            c["usd"] = amt * px + stable if px is not None else (stable or None)
            if c["usd"] is not None:
                total += c["usd"]
            elif amt:
                c["error"] = "received, but couldn't get the price"
        except Exception as e:
            c["error"] = str(e)[:80]
        coins.append(c)
    return {"coins": coins, "total_usd": total, "updated": time.strftime("%H:%M"), "priced": bool(prices)}


# ---------------- Alerts ----------------
_TOAST_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:AJ_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:AJ_MSG)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def _send_alert(title, msg, color):
    if CFG["notify_windows"] and os.name == "nt":
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _TOAST_PS],
                             env={**os.environ, "AJ_TITLE": title, "AJ_MSG": msg}, creationflags=0x08000000)
        except Exception:
            pass
    hook = CFG["discord_webhook"].strip()
    if hook.startswith("https://"):
        try:
            req = urllib.request.Request(hook, data=json.dumps({"embeds": [{"title": title, "description": msg,
                                         "color": color}]}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "astraflip-autojoin"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass


def parse_card(text, name="", value=""):
    """Pull (host, seconds_left) out of the giveaway box text. Either can be '' / None if not shown."""
    import re
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    host, secs = "", None
    for k, l in enumerate(lines):
        m = re.match(r"^(?:hosted\s+by|host(?:ed)?\s*:?|by)\s*[:\-]?\s*(.+)$", l, re.I)
        if m and m.group(1).strip():
            host = m.group(1).strip()
        elif re.fullmatch(r"(?:hosted\s+by|host(?:ed)?)\s*:?", l, re.I) and k + 1 < len(lines):
            host = lines[k + 1]
        if host:
            break
    for l in lines:
        t = re.sub(r"^(?:ends?\s*(?:in|at)?|time\s*left|remaining)\s*:?\s*", "", l, flags=re.I)
        m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", t)
        if m:
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            break
        parts = re.findall(r"(\d+)\s*(d|h|m|s)\b", t, re.I)
        if parts and re.fullmatch(r"(?:\d+\s*[dhms]\s*)+", t, re.I):
            secs = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u.lower()] for n, u in parts)
            break
    if host in (name, value):
        host = ""
    return host[:40], secs


def _relay(path, payload):
    """POST to the stats relay. The relay holds the Discord secrets; this copy never sees them."""
    url = CFG.get("relay_url", "").rstrip("/")
    if not url.startswith("https://"):
        return None
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "astraflip-autojoin"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _post_giveaway(name, value, img, card_text="", info=None, gid=""):
    if not CFG.get("post_feed", True) or not gid:
        return
    info = info or {}
    host, secs = parse_card(card_text, name, value)
    host = info.get("host") or host                       # site data is exact; box text is the fallback
    end = int(info["end"]) if info.get("end") else (int(time.time() + secs) if secs else 0)
    img = info.get("img") or img
    try:
        img = resolve_icons([name]).get(name) or img
    except Exception:
        pass
    try:
        _relay("/giveaway", {"install_id": CFG["install_id"], "giveaway_id": str(gid), "name": name,
                             "value": value, "img": img or "", "host": host or "", "end": end})
    except Exception:
        pass


def _money(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip()) if str(s).strip() else None
    except ValueError:
        return None


def parse_value(v):
    """'1.89K' -> 1890, '35K' -> 35000, '987' -> 987"""
    try:
        v = str(v).strip().upper().replace(",", "")
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(v[-1:], 1)
        return float(v[:-1] if mult != 1 else v) * mult
    except ValueError:
        return 0.0


# ---------------- Signed auto-update ----------------
from ed25519_min import verify as _ed_verify
import hashlib, base64

PENDING_FILE = os.path.join(BASE, "update_pending.json")


def _ver(v):
    return tuple(int(x) for x in str(v).lstrip("v").split(".")[:3])


def _get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": f"astraflip-autojoin/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response too large")
    return data


def fetch_verified_manifest():
    """Download latest.json + its signature and verify it with the embedded public key."""
    if len(UPDATE_PUBKEY) != 64:
        raise ValueError("this build has no update key yet")
    raw = _get(UPDATE_LATEST + "latest.json", 64 * 1024)
    sig = base64.b64decode(_get(UPDATE_LATEST + "latest.json.sig", 1024).strip())
    if not _ed_verify(bytes.fromhex(UPDATE_PUBKEY), raw, sig):
        raise ValueError("signature check FAILED — update refused")
    m = json.loads(raw)
    if not str(m.get("url", "")).startswith(UPDATE_ALLOWED) or not m.get("sha256") or not m.get("size"):
        raise ValueError("update info is not valid")
    return m


def download_verified_exe(m):
    """Download the new .exe, refusing it unless its size and SHA-256 match the signed info."""
    tmp = os.path.join(BASE, "AstraFlip AutoJoin.update.exe")
    h, n = hashlib.sha256(), 0
    req = urllib.request.Request(m["url"], headers={"User-Agent": f"astraflip-autojoin/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > int(m["size"]):
                break
            h.update(chunk)
            f.write(chunk)
    if n != int(m["size"]) or h.hexdigest() != m["sha256"].lower():
        os.remove(tmp)
        raise ValueError("downloaded file doesn't match the signed fingerprint — deleted")
    return tmp


def apply_update_and_restart(new_path):
    """Windows lets a running .exe be renamed: keep the old one as a backup, put the new one in place, launch it."""
    exe = sys.executable
    old = exe[:-4] + ".old.exe"
    if os.path.exists(old):
        os.remove(old)
    os.rename(exe, old)
    os.rename(new_path, exe)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump({"from": APP_VERSION, "old": old, "time": time.time()}, f)
    subprocess.Popen([exe, "--after-update"], cwd=BASE, creationflags=0x00000008 | 0x00000200)  # detached


def finish_update_if_pending():
    """Called once the new version's window is up: the update worked, so remove the backup."""
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            info = json.load(f)
        os.remove(PENDING_FILE)
        if os.path.exists(info["old"]):
            os.remove(info["old"])
        return info.get("from")
    except Exception:
        return None


def rollback_update_if_pending():
    """The new version crashed on start: put the previous .exe back and launch it."""
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            info = json.load(f)
        os.remove(PENDING_FILE)
        exe = sys.executable
        if getattr(sys, "frozen", False) and os.path.exists(info["old"]):
            bad = exe[:-4] + ".bad.exe"
            if os.path.exists(bad):
                os.remove(bad)
            os.rename(exe, bad)
            os.rename(info["old"], exe)
            subprocess.Popen([exe], cwd=BASE, creationflags=0x00000008 | 0x00000200)
            return True
    except Exception:
        pass
    return False


PAUSED_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>AutoJoin paused</title></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#07130d;
font-family:Segoe UI,Arial,sans-serif;color:#e8fff3"><div style="text-align:center">
<div style="font-size:28px;font-weight:700">AutoJoin is paused</div>
<p style="color:#9fc9b4;margin:10px 0 22px">Nothing is being sent. You can still open the site here, for example to log in.</p>
<a href="{URL}" style="display:inline-block;padding:14px 26px;border-radius:12px;background:#3ddc84;color:#04140b;
font-weight:700;text-decoration:none">Open site / Log in</a></div></body></html>"""

class Controller:
    def __init__(self, p):
        self.p = p
        self.running = False
        self.stopping = False
        self.paused = False
        self.accounts = {}            # i -> state dict
        self.ctxs, self.tasks = [], []
        self.log_lines = collections.deque(maxlen=int(CFG["log_lines"]))
        self.won = {}                 # giveaway name -> {name, img, value, i, user, time}
        self.pages = {}               # i -> page
        self.url = CFG["url"]
        self.market = {"enabled": False, "watch_only": CFG["m_watch_only"], "max_rate": CFG["m_max_rate"],
                       "max_price": CFG["m_max_price"], "budget": CFG["m_budget"], "spent": 0.0,
                       "buyer": None, "note": ""}
        self.market_items = []        # recent deals / purchases for the UI
        self.market_tried = {}
        self.market_nav_at = 0.0
        self._click_warned = {}
        self.hunt = {}                # giveaway name -> when an account first saw it open (others rush to it)
        self.market_buying = False    # True only during click-item -> Buy, so giveaway clicks wait
        self.entered = {}             # giveaway name -> {name, img, value, accounts, time}
        self.posted = {}              # giveaway name -> when it was posted to the giveaway feed webhook
        self.join_tpl = {}            # the site's join request (learned from one real Join click), for fast-join
        self.fast_tried = {}          # giveaway id -> when fast-join fired
        self._sampled, self._tpl_warned, self._ping_fetch_at = set(), False, 0.0
        self.posted_ids = {}          # giveaway id -> when it was posted to the Discord feed
        self.ginfo = {}               # item name -> {host, end, img} from the site's giveaway data
        self.join_hdrs = {}           # "account number" -> that account's own join headers (x-device)
        self.icons = {}               # item name -> Rolimons/Roblox thumbnail url (site pictures expire)
        self.saved_joins = {}         # "account number" -> lifetime join count
        self.update = {"latest": "", "available": False, "status": "", "notes": "", "busy": False}
        self.exit_requested = False
        self.cart_warned = False
        self.stats = {"spent": 0.0, "bought": 0}   # lifetime auto-buy totals
        self.tips = {"coins": [{**w, "amount": None, "usd": None, "tokens": {}, "error": ""} for w in TIP_WALLETS],
                     "total_usd": None, "updated": "", "loading": False}
        self.load_data()

    def log(self, msg):
        self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def state(self):
        per = {}
        for w in self.won.values():
            p = per.setdefault(w["i"], {"i": w["i"], "user": w.get("user", ""), "wins": 0, "value": 0.0})
            p["wins"] += 1
            p["value"] += parse_value(w.get("value", 0))
        stats = {**self.stats, "wins": len(self.won), "value_won": sum(p["value"] for p in per.values()),
                 "per": sorted(per.values(), key=lambda p: -p["value"])}
        ic = lambda x: {**x, "img": self.icons.get(x.get("name")) or x.get("img", ""), "alt": x.get("img", "")}
        return {"update": {**self.update, "current": APP_VERSION,
                           "can_install": bool(getattr(sys, "frozen", False))}, "stats": stats, "tips": self.tips, "cfg": CFG, "running": self.running, "stopping": self.stopping, "paused": self.paused,
                "accounts": [self.accounts[k] for k in sorted(self.accounts)],
                "market": {**self.market, "buyer": (f"#{self.market['buyer']} "
                           f"{self.accounts.get(self.market['buyer'], {}).get('user', '')}") if self.market["buyer"] else "",
                           "items": [ic(x) for x in self.market_items[:24]]},
                "won": sorted((ic(w) for w in self.won.values()), key=lambda w: -w["time"]),
                "entered": sorted((ic({**e, "accounts": sorted(e["accounts"])}) for e in self.entered.values()),
                                  key=lambda e: -e["time"]),
                "log": list(self.log_lines), "logTail": self.log_lines[-1] if self.log_lines else ""}

    # ---- buttons
    async def start(self):
        if self.running or self.stopping:
            return
        url, n = CFG["url"], max(1, min(10, int(CFG["accounts"])))
        self.url = url.rstrip('/')
        self.market["buyer"] = None
        self.running, self.paused = True, False
        self.accounts = {i: {"i": i, "user": "", "balance": "", "logged": False, "status": "opening", "avatar": "",
                             "giveaway": "", "btn": "", "joins": self.saved_joins.get(str(i), 0)}
                         for i in range(1, n + 1)}
        self.log(f"Starting {n} account(s)…")
        for i in range(1, n + 1):
            try:
                ctx = await self.p.chromium.launch_persistent_context(
                    os.path.join(PROFILES, f"acc{i}"), headless=bool(CFG["headless"]), no_viewport=True)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                self.ctxs.append(ctx)
                self.pages[i] = page
                self.log(f"Account {i} opened")
                self.tasks.append(asyncio.create_task(self._open_and_watch(i, page, url)))
            except Exception as e:
                self.accounts[i]["status"] = "closed"
                self.log(f"Account {i}: failed to open ({str(e).splitlines()[0][:100]})")

    async def set_market(self, cfg):
        was = self.market["enabled"]
        for k in ("enabled", "watch_only", "max_rate", "max_price", "budget"):
            if k in cfg:
                self.market[k] = cfg[k]
        for k in ("watch_only", "max_rate", "max_price", "budget"):
            if k in cfg:
                CFG["m_" + k] = cfg[k]
        save_cfg()
        if self.market["enabled"] and not was:
            self.market["spent"], self.market["buyer"] = 0.0, None
            self.log(f"Auto-buy ON ({'watch only' if self.market['watch_only'] else 'BUYING'}) · max rate "
                     f"{self.market['max_rate']} · max ${self.market['max_price']} · budget ${self.market['budget']}")
        elif was and not self.market["enabled"]:
            self.log("Auto-buy OFF")

    async def _empty_cart(self, page):
        """Take whatever is in the cart back out, so one failed buy doesn't block every later deal."""
        try:
            removed = await page.evaluate("""() => {
              const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
              const cart = [...document.querySelectorAll('*')].find(e => e.children.length === 0 && vis(e)
                             && e.textContent.trim() === 'Cart');
              let box = cart; for (let k = 0; k < 4 && box && box.parentElement; k++) box = box.parentElement;
              if (!box) return 0;
              const btns = [...box.querySelectorAll('button')].filter(b => vis(b) && b.textContent.trim() !== 'Buy'
                              && (/remove|delete|clear|×|✕/i.test(b.getAttribute('aria-label') || b.textContent || '')
                                  || b.querySelector('svg')));
              btns.forEach(b => b.click());
              return btns.length;
            }""")
            await asyncio.sleep(0.6)
            if removed:
                cart = await page.evaluate(CART_JS)
                self.log("Auto-buy: emptied the cart" if not cart["hasBuy"] else
                         "Auto-buy: couldn't empty the cart — remove the item in the buyer window")
        except Exception:
            pass

    def _sample_market(self, page):
        """Save one example of the site's market data and buy request (no logins / cookies) so the
        next update can buy directly instead of clicking through the cart."""
        path = os.path.join(BASE, "market_sample.txt")
        if os.path.exists(path):
            return
        got = {}

        def on_req(r):
            if r.method == "POST" and "/api/market/buy" in r.url and "buy" not in got:
                got["buy"] = {"url": r.url, "body": (r.post_data or "")[:2000]}
                _write()

        async def on_resp(r):
            if r.request.method == "GET" and "/api/market" in r.url and "/buy" not in r.url and "list" not in got:
                try:
                    got["list"] = {"url": r.url, "body": (await r.text())[:6000]}
                    _write()
                except Exception:
                    pass

        def _write():
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(got, f, indent=1)
            except Exception:
                pass
        page.on("request", on_req)
        page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    def _pick_buyer(self):
        best, best_bal = None, -1.0
        for a in self.accounts.values():
            if a["logged"] and a["status"] != "closed" and a["i"] in self.pages:
                try:
                    bal = float(a["balance"].replace("$", "").replace(",", "")) if a["balance"] else 0.0
                except ValueError:
                    bal = 0.0
                if bal > best_bal:
                    best, best_bal = a["i"], bal
        return best

    async def _market_loop(self):
        last_scan = 0.0
        while True:
            await asyncio.sleep(CFG["market_tick"])
            self.market_buying = False
            m = self.market
            if not (self.running and m["enabled"]) or self.paused:
                continue
            try:
                if not m["buyer"] or m["buyer"] not in self.pages or self.pages[m["buyer"]].is_closed():
                    m["buyer"] = self._pick_buyer()
                    if not m["buyer"]:
                        m["note"] = "no logged-in account yet"
                        continue
                    a = self.accounts[m["buyer"]]
                    self.log(f"Auto-buy: using account #{a['i']} {a['user']} ({a['balance']})")
                    self._sample_market(self.pages[m["buyer"]])
                page = self.pages[m["buyer"]]
                # only do the full listing scan when something on the page changed (or once a second)
                if not await page.evaluate(DIRTY_JS) and time.time() - last_scan < 1:
                    continue
                last_scan = time.time()
                st = await page.evaluate(MARKET_JS)
                if not st["sort"] and not st["cards"]:
                    # not on the marketplace: click the site's own Marketplace tab (no reload), at most every 10s
                    if time.time() - self.market_nav_at > 10:
                        self.market_nav_at = time.time()
                        m["note"] = "opening marketplace"
                        try:
                            await page.get_by_text("Marketplace", exact=True).first.click(timeout=2000)
                        except Exception:
                            from urllib.parse import urlsplit
                            u = urlsplit(page.url)
                            await page.goto(f"{u.scheme}://{u.netloc}/marketplace")
                        self.log("Auto-buy: opened the Marketplace tab")
                    continue
                if st["limited"]:
                    m["note"] = "rate limited"
                    await asyncio.sleep(CFG["limit_wait"])
                    continue
                if st["sort"] and st["sort"] != "Lowest rate":
                    m["note"] = "switching sort to Lowest rate"
                    await page.click('[data-ajm="sort"]', timeout=2000)
                    await asyncio.sleep(0.3)
                    await page.get_by_text("Lowest rate", exact=True).last.click(timeout=2000)
                    self.log("Auto-buy: sorted marketplace by Lowest rate")
                    await asyncio.sleep(0.5)
                    continue
                m["note"] = f"{len(st['cards'])} listings seen · sort: {st['sort'] or '?'}"
                left = m["budget"] - m["spent"]
                now = time.time()
                bal = _money(self.accounts.get(m["buyer"], {}).get("balance", ""))
                deals = sorted((c for c in st["cards"]
                                if 0 < c["rate"] <= m["max_rate"] and 0 < c["price"] <= m["max_price"]
                                and c["price"] <= left and (bal is None or c["price"] <= bal + 0.001)
                                and now - self.market_tried.get((c["name"], c["price"]), 0) > 60),
                               key=lambda c: c["rate"])
                if not deals:
                    continue
                d = deals[0]
                key = (d["name"], d["price"])
                self.market_tried[key] = now
                item = {**d, "bought": False, "failed": ""}
                self.market_items.insert(0, item)
                if m["watch_only"]:
                    self.log(f"Auto-buy (watch only): would buy '{d['name']}' ${d['price']:.2f} at rate {d['rate']}")
                    continue
                # safety: only buy when the cart starts empty, so we never pay for something else
                if st["hasBuy"] or st["cartTotal"] > 0:
                    item["failed"] = "cart not empty"
                    if not self.cart_warned:
                        self.cart_warned = True
                        self.log("Auto-buy: cart has an item — trying to empty it")
                    await self._empty_cart(page)
                    continue
                self.cart_warned = False
                self.market_buying = True
                await page.click(f'[data-ajm="{d["k"]}"]', timeout=2000)
                # poll the cart every 50 ms (up to 3 s) until it shows exactly this item's price
                cart, t0 = {"hasBuy": False, "total": 0}, time.time()
                while time.time() - t0 < 3:
                    cart = await page.evaluate(CART_JS)
                    if cart["hasBuy"] and abs(cart["total"] - d["price"]) <= 0.01:
                        break
                    await asyncio.sleep(0.05)
                if not cart["hasBuy"] or abs(cart["total"] - d["price"]) > 0.01:
                    item["failed"] = f"cart total ${cart['total']:.2f}"
                    self.log(f"Auto-buy: cart shows ${cart['total']:.2f}, expected ${d['price']:.2f} — not buying")
                    self.market_buying = False
                    await self._empty_cart(page)
                    continue
                self.log(f"Auto-buy: BUYING '{d['name']}' ${d['price']:.2f} at rate {d['rate']}")
                await page.click('[data-ajm="buy"]', timeout=2000)
                await asyncio.sleep(2.5)
                st3 = await page.evaluate(MARKET_JS)
                if st3["cartTotal"] == 0 and not st3["hasBuy"]:
                    item["bought"] = True
                    m["spent"] += d["price"]
                    self.log(f"Auto-buy: ✅ bought '{d['name']}' · spent ${m['spent']:.2f}/${m['budget']:.2f}")
                    self.stats["spent"] += d["price"]
                    self.stats["bought"] += 1
                    self.alert("buys", "🛒 Auto-buy purchase",
                               f"Bought {d['name']} for ${d['price']:.2f} (rate {d['rate']})")
                    if m["budget"] - m["spent"] < 0.01:
                        self.log("Auto-buy: budget used up, turning off")
                        m["enabled"] = False
                else:
                    why = await page.evaluate(SITE_MSG_JS)
                    item["failed"] = why[:40] if why else "buy not confirmed"
                    self.log(f"Auto-buy: purchase of '{d['name']}' not confirmed"
                             + (f" — site says: {why}" if why else ""))
                    if "insufficient" in why.lower() or "balance" in why.lower():
                        m["buyer"] = None             # pick the account with the most money next time
                    await self._empty_cart(page)
                    self.alert("problems", "⚠ Auto-buy", f"Purchase of {d['name']} wasn't confirmed — check the window",
                               0xEF4444)
                del self.market_items[30:]
            except asyncio.CancelledError:
                break
            except Exception as e:
                msg = str(e).splitlines()[0][:120]
                m["note"] = "error"
                if "Execution context was destroyed" not in msg:
                    self.log(f"Auto-buy: {msg}")

    # ---- settings
    async def save_settings(self, new):
        changed = []
        for k, v in (new or {}).items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            try:
                v = bool(v) if isinstance(d, bool) else int(float(v)) if isinstance(d, int) else \
                    float(v) if isinstance(d, float) else str(v).strip()
            except (TypeError, ValueError):
                continue
            if CFG[k] != v:
                CFG[k] = v
                changed.append(k)
        CFG["accounts"] = max(1, min(10, CFG["accounts"]))
        CFG["log_lines"] = max(50, min(5000, CFG["log_lines"]))
        if "log_lines" in changed:
            self.log_lines = collections.deque(self.log_lines, maxlen=CFG["log_lines"])
        for k in ("max_rate", "max_price", "budget", "watch_only"):
            self.market[k] = CFG["m_" + k]
        save_cfg()
        if changed and not set(changed) <= {"hide_timeouts", "m_max_rate", "m_max_price", "m_budget", "m_watch_only"}:
            restart = {"url", "accounts", "headless", "skip_coinflips"} & set(changed)
            self.log("Settings saved" + (" (Stop + Start to apply: " + ", ".join(sorted(restart)) + ")"
                                         if restart and self.running else ""))

    async def reset_settings(self):
        CFG.clear(); CFG.update(DEFAULTS)
        await self.save_settings({})
        self.log("Settings reset to defaults")

    # ---- saved data (wins, entries, purchases, join counts)
    def load_data(self):
        if not CFG["save_data"]:
            return
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.won = d.get("won", {})
            self.entered = {k: {**v, "accounts": set(v.get("accounts", []))} for k, v in d.get("entered", {}).items()}
            self.market_items = d.get("market_items", [])
            self.saved_joins = d.get("joins", {})
            self.posted = d.get("posted", {})
            self.join_tpl = d.get("join_tpl", {})
            self.join_hdrs = d.get("join_hdrs", {})
            self.posted_ids = d.get("posted_ids", {})
            if not self.posted:                   # first run with saved feed history: don't repost entered ones
                self.posted = {k: {"seen": v.get("time", 0), "until": v.get("time", 0) + 3 * 3600}
                               for k, v in self.entered.items()}
            self.stats.update(d.get("stats", {}))
            if not self.stats.get("bought"):      # purchases made before stats were tracked
                b = [x for x in self.market_items if x.get("bought")]
                self.stats = {"spent": round(sum(x.get("price", 0) for x in b), 2), "bought": len(b)}
        except Exception:
            pass

    def save_data(self):
        if not CFG["save_data"]:
            return
        for a in self.accounts.values():
            self.saved_joins[str(a["i"])] = a["joins"]
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"won": self.won,
                           "entered": {k: {**v, "accounts": sorted(v["accounts"])} for k, v in self.entered.items()},
                           "market_items": self.market_items[:30], "joins": self.saved_joins,
                           "stats": self.stats,
                           "join_tpl": self.join_tpl, "join_hdrs": self.join_hdrs,
                           "posted_ids": {k: v for k, v in self.posted_ids.items() if time.time() - v < 3 * 86400},
                           "posted": {k: v for k, v in self.posted.items()
                                      if isinstance(v, dict) and time.time() - v["seen"] < 86400}}, f)
        except Exception:
            pass

    async def clear_data(self):
        self.won, self.entered, self.market_items, self.saved_joins = {}, {}, [], {}
        self.stats = {"spent": 0.0, "bought": 0}
        for a in self.accounts.values():
            a["joins"] = 0
        try:
            os.remove(DATA_FILE)
        except OSError:
            pass
        self.log("Saved data cleared")

    async def refresh_tips(self):
        if self.tips.get("loading"):
            return
        self.tips["loading"] = True
        try:
            res = await asyncio.to_thread(fetch_tips)
            self.tips = {**res, "loading": False}
            self.log("Tips: " + " · ".join(
                f"{c['ticker']} " + (f"⚠ {c['error']}" if c["error"] else f"{c['amount']:.8g}") for c in res["coins"])
                + f" · total ${res['total_usd']:.2f}")
        except Exception as e:
            self.tips["loading"] = False
            self.log(f"Tips: couldn't check balances ({str(e)[:60]})")

    async def _icons_loop(self):
        """Look up item pictures on Rolimons for everything shown (wins, entries, auto-buy)."""
        while True:
            names = {x["name"] for x in list(self.won.values()) + list(self.entered.values()) + self.market_items
                     if x.get("name")}
            if names - set(self.icons) or not self.icons:
                try:
                    self.icons.update(await asyncio.to_thread(resolve_icons, sorted(names)))
                except Exception:
                    pass
            await asyncio.sleep(30)

    async def _discord_stats_loop(self):
        """Send this copy's totals to the relay every 5 min; the relay adds everyone up and updates Discord."""
        await asyncio.sleep(20)
        while True:
            if CFG.get("share_stats", True) and not self.paused:
                payload = {"install_id": CFG["install_id"],
                           "entered": sum(len(e["accounts"]) for e in list(self.entered.values())),
                           "spent": round(float(self.stats.get("spent", 0)), 2)}
                try:
                    await asyncio.to_thread(_relay, "/report", payload)
                except Exception as e:
                    self.log(f"Community stats: couldn't reach the relay ({str(e)[:60]})")
            await asyncio.sleep(300)

    async def _tips_loop(self):
        while True:
            if not self.paused:
                await self.refresh_tips()
            await asyncio.sleep(600)          # re-check every 10 minutes

    def alert(self, kind, title, msg, color=0x3DDC84):
        if CFG.get("notify_" + kind, True):
            _th.Thread(target=_send_alert, args=(title, msg, color), daemon=True).start()

    async def test_alert(self):
        self.alert("wins", "AstraFlip AutoJoin", "Test alert — notifications are working ✅")
        self.log("Test alert sent")

    # ---- signed auto-update ----
    async def check_update(self, quiet=False):
        u = self.update
        if u.get("busy"):
            return
        u.update(status="checking…", busy=True)
        try:
            m = await asyncio.to_thread(fetch_verified_manifest)
            newer = _ver(m["version"]) > _ver(APP_VERSION)
            u.update(latest=m["version"], available=newer, manifest=m if newer else None,
                     notes=m.get("notes", ""), status="" if newer else "you're up to date")
            if newer:
                self.log(f"Update available: v{m['version']} (you have v{APP_VERSION})")
            elif not quiet:
                self.log(f"Updates: you're on the latest version (v{APP_VERSION})")
        except Exception as e:
            u.update(status=f"couldn't check ({str(e)[:60]})")
            if not quiet:
                self.log(f"Updates: couldn't check ({str(e)[:80]})")
        finally:
            u["busy"] = False

    async def install_update(self):
        u = self.update
        m = u.get("manifest")
        if not m or u.get("busy"):
            return
        if not getattr(sys, "frozen", False):
            self.log("Updates install into the .exe version only — rebuild with build.bat when running the .py")
            return
        u.update(busy=True, status="downloading…")
        try:
            new_path = await asyncio.to_thread(download_verified_exe, m)
            u["status"] = "verified ✓ — restarting…"
            self.log(f"Update v{m['version']} downloaded and verified — restarting")
            await self.stop()
            self.save_data()
            apply_update_and_restart(new_path)
            self.exit_requested = True
        except Exception as e:
            u.update(busy=False, status=f"update failed: {str(e)[:80]}")
            self.log(f"Update failed — kept current version ({str(e)[:100]})")

    async def clear_log(self):
        self.log_lines.clear()

    def _is_us(self, winner):
        w = winner.rstrip('.…').strip().lower()
        if not w:
            return None
        for a in self.accounts.values():
            u = (a.get("user") or "").lower()
            if u and (u == w or u.startswith(w)):
                return a
        return None

    async def pause(self):
        """Pause = zero traffic: every account window is parked on an offline page, so the site's own
        background refreshes, chat and connections stop too. That page has a button to open the site
        (e.g. to log in); the bot itself stays paused either way. Resume reloads the site."""
        if not self.running:
            return
        self.paused = not self.paused
        for i, page in list(self.pages.items()):
            try:
                if self.paused:
                    await page.goto("about:blank")
                    await page.set_content(PAUSED_HTML.replace("{URL}", _html.escape(self.url, quote=True)))
                    self.accounts[i]["status"] = "paused"
                else:
                    await page.goto(self.url)
            except Exception:
                pass
        self.log("Paused — all accounts disconnected, nothing is being sent" if self.paused
                 else "Resumed — reconnecting all accounts")


    async def stop(self):
        if not self.running:
            return
        self.stopping = True
        for t in self.tasks:
            t.cancel()
        for c in self.ctxs:
            try:
                await c.close()
            except Exception:
                pass
        self.ctxs, self.tasks, self.pages = [], [], {}
        self.market["buyer"] = None
        for a in self.accounts.values():
            a["status"] = "closed"
        self.running, self.stopping, self.paused = False, False, False
        self.log("Stopped")

    # ---- per-account loop
    def _monitor(self, i, page):
        """Count what each account sends to the site, and log a summary every 15 s."""
        import re
        counts, state = collections.Counter(), {"t": time.time()}
        def bump(kind, label):
            counts[f"{kind} {label}"] += 1
            if time.time() - state["t"] >= 15 and CFG["traffic_log"]:
                total = sum(counts.values())
                top = ", ".join(f"{k} x{v}" for k, v in counts.most_common(3))
                self.log(f"Account {i} traffic: {total} sent in 15s · {top}")
                counts.clear(); state["t"] = time.time()
        def on_request(req):
            if req.resource_type in ("xhr", "fetch") and not any(f in req.url for f in skip_feeds()):
                path = re.sub(r"/[0-9a-f-]{6,}|/\d+", "/:id", req.url.split("?")[0].split("//", 1)[-1].split("/", 1)[-1])
                bump(req.method, "/" + path[:40])
        def learn_join(req):
            # remember the exact request the Join button sends, so fast-join can send it without clicking
            m = re.search(r"/api/giveaways/([A-Za-z0-9_-]{10,})", req.url)
            if req.method == "POST" and m:
                gid = m.group(1)
                skip = {"cookie", "host", "content-length", "origin", "referer", "user-agent", "connection",
                        "accept-encoding"}
                tpl = {"url": req.url.replace(gid, "{id}"), "body": (req.post_data or "").replace(gid, "{id}"),
                       "headers": {k: v for k, v in req.headers.items()
                                   if k.lower() not in skip and not k.lower().startswith("sec-")}}
                self.join_hdrs[str(i)] = tpl["headers"]       # x-device etc. differ per account
                if tpl["url"] != self.join_tpl.get("url") or tpl["body"] != self.join_tpl.get("body"):
                    self.join_tpl = {"url": tpl["url"], "body": tpl["body"]}
                    self.log("Fast-join: learned the site's join request")
        def on_ws(ws):
            ws.on("framesent", lambda payload: bump("ws", (str(payload)[:24]).replace("\n", " ")))
            def on_frame(payload):
                txt = payload if isinstance(payload, str) else ""
                if "giveaway" in txt.lower():
                    self._sample("ws", txt)
                    try:
                        self._fast_scan(json.loads(txt[txt.index("["):]))
                    except Exception:
                        pass
                    # the site only says "giveaway:changed" and refetches slowly: fetch the list ourselves now
                    if time.time() - self._ping_fetch_at > 0.3 and CFG.get("fast_join", True):
                        self._ping_fetch_at = time.time()
                        asyncio.ensure_future(self._fetch_list(i, page))
            ws.on("framereceived", on_frame)
        page.on("request", on_request)
        page.on("request", learn_join)
        page.on("websocket", on_ws)

        async def on_response(resp):
            try:
                path = resp.url.split("?")[0]
                if resp.request.method == "GET" and path.rstrip("/").endswith("/api/giveaways") and resp.ok:
                    txt = await resp.text()
                    self._sample("list", txt)
                    self._fast_scan(json.loads(txt))
                if resp.request.method == "POST" and "/api/giveaways/" in resp.url:
                    body = ""
                    try:
                        body = (await resp.text())[:120].replace("\n", " ")
                    except Exception:
                        pass
                    self.log(f"Account {i}: join answer {resp.status} {body}")
            except Exception:
                pass
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    # ---- fast-join: join straight from the site's giveaway data (list poll / websocket), no waiting for the box
    def _sample(self, kind, txt):
        """Save one example of the site's giveaway data per kind (for tuning)."""
        if kind in self._sampled:
            return
        self._sampled.add(kind)
        try:
            with open(os.path.join(BASE, f"giveaway_sample_{kind}.txt"), "w", encoding="utf-8") as f:
                f.write(txt[:20000])
        except Exception:
            pass

    def _fast_scan(self, data):
        if not self.running:
            return
        now, found, feed = time.time(), [], []

        def ts(v):
            if isinstance(v, (int, float)):
                return v / 1000 if v > 1e11 else v
            if isinstance(v, str):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return None
            return None

        def walk(o, depth=0):
            if depth > 6:
                return
            if isinstance(o, list):
                for x in o:
                    walk(x, depth + 1)
            elif isinstance(o, dict):
                gid = o.get("id") or o.get("_id")
                if isinstance(gid, str) and len(gid) >= 10:
                    low = {k.lower(): v for k, v in o.items()}
                    status = str(low.get("status") or low.get("state") or "").lower()
                    end = next((ts(low[k]) for k in ("endsat", "endat", "ends_at", "end_at", "endtime",
                                                       "expiresat", "drawat") if k in low), None)
                    winner = low.get("winner") or low.get("winnerid") or low.get("winner_id")
                    closed = status in ("ended", "closed", "drawn", "finished", "completed", "cancelled")
                    open_ = status in ("open", "active", "running", "live", "ongoing", "started") or \
                            (end is not None and end > now)
                    if open_ and not closed and not winner and (end is None or end > now):
                        cr = low.get("creator") or low.get("host") or {}
                        it = (low.get("items") or [{}])[0] if isinstance(low.get("items"), list) else {}
                        if isinstance(it, dict) and it.get("name"):
                            self.ginfo[it["name"]] = {"host": cr.get("username", "") if isinstance(cr, dict) else "",
                                                      "end": end, "img": it.get("thumbnailUrl", ""), "gid": gid}
                            # an older card saved by item name for this same (still open) giveaway: merge it in
                            created = ts(low.get("createdat")) or 0
                            old = self.entered.get(it["name"])
                            if old is not None and old.get("time", 0) >= created - 60:
                                cur = self.entered.setdefault(gid, {**old, "accounts": set()})
                                cur["accounts"] |= set(old.get("accounts", ()))
                                cur["host"] = self.ginfo[it["name"]]["host"]
                                del self.entered[it["name"]]
                            if gid in self.entered and self.ginfo[it["name"]]["host"]:
                                self.entered[gid]["host"] = self.ginfo[it["name"]]["host"]
                            feed.append((gid, it["name"], low.get("value") or it.get("value") or 0,
                                         ts(low.get("createdat")) or now, dict(self.ginfo[it["name"]])))
                        found.append((gid, cr.get("username", "") if isinstance(cr, dict) else str(cr)))
                for v in o.values():
                    if isinstance(v, (list, dict)):
                        walk(v, depth + 1)
        walk(data)
        # Discord giveaway feed: once per giveaway id, only if it started in the last 10 min (no reposts on restart)
        for gid, name, val, created, info in feed:
            if gid in self.posted_ids or now - created > 600:
                continue
            self.posted_ids[gid] = now
            v = float(val or 0)
            vtxt = f"{v / 1e6:.3g}M" if v >= 1e6 else f"{v / 1e3:.3g}K" if v >= 1e3 else f"{v:g}"
            _th.Thread(target=_post_giveaway, args=(name, vtxt, info.get("img", ""), "", info, gid), daemon=True).start()
        if not (CFG.get("fast_join", True) and CFG["auto_join"]) or self.paused:
            return
        for gid, creator in found:
            if gid in self.fast_tried:
                continue
            self.fast_tried[gid] = now
            if not self.join_tpl.get("url"):
                if not self._tpl_warned:
                    self._tpl_warned = True
                    self.log("Fast-join: waiting for one normal join to learn the site's join request")
                continue
            self.log(f"Fast-join: new giveaway {gid[:10]}… — joining on all accounts")
            for i, page in list(self.pages.items()):
                asyncio.ensure_future(self._fast_join(i, page, gid, creator))

    async def _fetch_list(self, i, page):
        try:
            txt = await page.evaluate(
                """async (h) => (await fetch('/api/giveaways', {credentials: 'include', headers: h})).text()""",
                self.join_hdrs.get(str(i), {}))
            self._fast_scan(json.loads(txt))
        except Exception:
            pass

    async def _fast_join(self, i, page, gid, creator=""):
        try:
            me = self.accounts.get(i, {}).get("user", "")
            if creator and me and me.lower() == creator.lower():
                return                                    # can't join your own giveaway
            await asyncio.sleep((i - 1) * CFG["join_stagger"])
            if page.is_closed() or self.paused:
                return
            t = {**self.join_tpl, "headers": self.join_hdrs.get(str(i), {"accept": "*/*"})}
            res = await page.evaluate(
                """async ([url, body, headers]) => {
                     const r = await fetch(url, {method: 'POST', credentials: 'include', headers,
                                                 body: body || undefined});
                     return r.status + ' ' + (await r.text()).slice(0, 100);
                   }""",
                [t["url"].replace("{id}", gid), t["body"].replace("{id}", gid), t["headers"]])
            self.log(f"Account {i}: fast-join {res}")
        except Exception as e:
            self.log(f"Account {i}: fast-join failed ({str(e).splitlines()[0][:60]})")

    async def _click(self, i, page, sel, force=False):
        """Real click; if it can't happen, log what's in the way (at most every 30 s) instead of spamming."""
        try:
            await page.click(sel, timeout=2000, force=force)
            return True
        except Exception as e:
            if "Timeout" not in str(e):
                raise
            why = await page.evaluate("""sel => {
              const el = document.querySelector(sel);
              if (!el) return 'button disappeared';
              const r = el.getBoundingClientRect();
              if (!r.width || !r.height) return 'button hidden';
              const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
              if (!top) return 'button is off-screen (window too small or scrolled?)';
              if (el.contains(top)) return 'button not ready yet';
              return 'covered by: ' + (top.textContent || top.className || top.tagName).trim().slice(0, 50);
            }""", sel)
            now = time.time()
            if now - self._click_warned.get(i, 0) > 30:
                self._click_warned[i] = now
                self.log(f"Account {i}: couldn't click ({why})")
            if why.startswith("covered"):
                await page.keyboard.press("Escape")   # closes most popups
            return False

    async def _open_and_watch(self, i, page, url):
        self._monitor(i, page)
        async def skip_feed(route):
            await route.abort()
        for feed in skip_feeds():
            await page.route(lambda u, f=feed: f in u, skip_feed)
        EXTRAS = ("/api/chat", "/api/deposits", "/api/withdrawals")

        async def skip_extra(route):
            if CFG.get("skip_extras", True) and self.market.get("buyer") != i:
                await route.abort()
            else:
                await route.continue_()
        await page.route(lambda u: any(x in u for x in EXTRAS), skip_extra)
        try:
            await page.goto(url)
        except Exception as e:
            self.log(f"Account {i}: couldn't load site ({str(e).splitlines()[0][:80]})")
        await self._watch(i, page)

    async def _watch(self, i, page):
        acc = self.accounts[i]
        direction, last_found, last_logged, last_sig = 1, None, None, None
        join_clicked_at, last_arrow = {}, 0.0
        bad_since, last_reload = None, 0.0
        target, back_tries, clicked = None, 0, set()
        while True:
            if self.paused:                       # window is parked on a blank page: do nothing
                acc["status"] = "paused"
                await asyncio.sleep(1)
                continue
            try:
                st = await page.evaluate(SCAN_JS, [acc["joins"], self.paused])
                g = st if st and st["found"] else None

                acc["logged"], acc["user"], acc["balance"] = st["logged"], st["user"], st["balance"]
                if st.get("avatar"):
                    acc["avatar"] = st["avatar"]
                if st["logged"] != last_logged:
                    self.log(f"Account {i}: " + (f"logged in as {st['user'] or '?'}" if st["logged"] else "NOT logged in"))
                    if last_logged and not st["logged"]:
                        self.alert("problems", "⚠ Account logged out",
                                   f"Account {i} ({acc['user'] or '?'}) is logged out", 0xEF4444)
                    last_logged = st["logged"]
                if bool(g) != last_found:
                    self.log(f"Account {i}: giveaway box {'found' if g else 'NOT found'}")
                    last_found = bool(g)
                acc["giveaway"] = g["name"] if g else ""
                acc["btn"] = ("" if not g else "already entered" if g["text"] == "Joined"
                              else "Join available" if g["isJoin"] else "ended")
                # --- watchdog: reload a window that stays broken for too long
                others_see_box = any(a["giveaway"] for k, a in self.accounts.items() if k != i)
                bad = ("no giveaway box" if not g and others_see_box else "logged out" if not st["logged"]
                       else "can't reach server" if st["offline"] and not CFG["skip_coinflips"] else "")
                wd = CFG["watchdog_min"] * 60
                if not bad or self.paused:
                    bad_since = None
                elif bad_since is None:
                    bad_since = time.time()
                elif wd > 0 and time.time() - bad_since > wd and time.time() - last_reload > wd:
                    self.log(f"Watchdog: account {i} — {bad} for {CFG['watchdog_min']:g} min, reloading")
                    self.alert("problems", "🔄 Watchdog", f"Account {i}: {bad}, reloaded the window", 0xFACC15)
                    last_reload, bad_since = time.time(), None
                    await page.reload()
                    await asyncio.sleep(3)
                    continue
                acc["status"] = ("paused" if self.paused else "rate limited" if st["limited"]
                                 else "logged out" if not st["logged"] else "watching" if g else "not found")

                if g:
                    if g["winner"] and g["name"] and g["name"] not in self.won:
                        a = self._is_us(g["winner"])
                        if a:
                            self.won[g["name"]] = {"name": g["name"], "img": g["itemImg"], "value": g["value"],
                                                   "i": a["i"], "user": a["user"], "avatar": a.get("avatar", ""),
                                                   "time": time.time()}
                            self.log(f"🏆 Account {a['i']} ({a['user']}) WON '{g['name']}' ({g['value']})")
                            self.alert("wins", "🏆 Giveaway won!",
                                       f"Account {a['i']} ({a['user']}) won {g['name']} ({g['value']})", 0xFCD34D)
                    if g["text"] == "Joined" and g["name"]:
                        gi = self.ginfo.get(g["name"]) or {}
                        key = gi.get("gid") or g["name"]      # one card per giveaway, even for repeat items
                        e = self.entered.setdefault(key, {"name": g["name"], "img": g["itemImg"],
                                                          "value": g["value"], "accounts": set(),
                                                          "time": time.time()})
                        if gi.get("host"):
                            e["host"] = gi["host"]
                        if i not in e["accounts"]:
                            e["accounts"].add(i)
                            e["time"] = time.time()
                        if g["itemImg"]:
                            e["img"] = g["itemImg"]
                    if g["name"] in clicked and g["text"] == "Joined":
                        clicked.discard(g["name"])
                        acc["joins"] += 1
                        self.log(f"Account {i}: ✓ joined '{g['name']}'")
                        if target == g["name"]:
                            target = None

                buyer_busy = self.market_buying and self.market["buyer"] == i
                if g and not self.paused and not buyer_busy and CFG["auto_join"]:
                    if g["isJoin"]:
                        target, back_tries = g["name"], 0
                        if st["limited"]:
                            self.log(f"Account {i}: rate limited, retrying '{target}' in {CFG['limit_wait']}s")
                            await asyncio.sleep(CFG["limit_wait"])
                            continue
                        if time.time() - join_clicked_at.get(target, 0) < CFG["join_retry"]:
                            await asyncio.sleep(0.25)   # already clicked recently: wait for the site, don't spam
                            continue
                        if target not in join_clicked_at and i > 1:
                            await asyncio.sleep((i - 1) * CFG["join_stagger"])
                        acc["status"] = "joining"
                        self.log(f"Account {i}: Join seen on '{target}', clicking")
                        await self._click(i, page, MAIN_SEL)
                        join_clicked_at[target] = time.time()
                        self.hunt.setdefault(target, time.time())
                        clicked.add(target)
                        await asyncio.sleep(0.5)
                    elif target and g["name"] != target:
                        back_tries += 1
                        if back_tries > 12:
                            self.log(f"Account {i}: lost '{target}', moving on")
                            target = None
                        else:
                            use_prev = back_tries <= 6
                            off = g["prevOff"] if use_prev else g["nextOff"]
                            if not off and time.time() - last_arrow >= 0.5:
                                await self._click(i, page, PREV_SEL if use_prev else NEXT_SEL, force=True)
                                last_arrow = time.time()
                    else:
                        if target and g["name"] == target:
                            target = None
                        if st["limited"]:
                            await asyncio.sleep(CFG["limit_wait"])
                            continue
                        off = g["nextOff"] if direction == 1 else g["prevOff"]
                        if off:
                            direction *= -1
                            off = g["nextOff"] if direction == 1 else g["prevOff"]
                        now = time.time()
                        hunting = any(now - t < 20 and n != g["name"] and n not in join_clicked_at
                                      for n, t in self.hunt.items())
                        gap = 0.35 if hunting else CFG["arrow_every"]
                        if not off and now - last_arrow >= gap:
                            await self._click(i, page, NEXT_SEL if direction == 1 else PREV_SEL, force=True)
                            last_arrow = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                msg = str(e).splitlines()[0][:120]
                if page.is_closed() or "closed" in msg.lower():
                    acc["status"] = "closed"
                    self.log(f"Account {i}: window closed, stopped watching")
                    break
                if "Execution context was destroyed" not in msg:
                    self.log(f"Account {i}: {msg}")
            await asyncio.sleep(CFG["tick"])


UI_ARGS = ["--app=data:text/html,<title>AstraFlip AutoJoin</title>", "--window-size=560,900"]


def _msg(title, text, error=False):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(title, text)
        r.destroy()
    except Exception:
        pass


def install_browser():
    """First run: download the Chromium build Playwright needs (one time, ~150 MB)."""
    _msg("AstraFlip AutoJoin", "First run: the app will now download the browser it needs (about 150 MB).\n"
                               "This takes a minute or two. Click OK to start.")
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    exe = compute_driver_executable()
    cmd = list(exe) if isinstance(exe, (tuple, list)) else [exe]
    subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True,
                   creationflags=0x08000000 if os.name == "nt" else 0)


async def launch_ui(p):
    for attempt in range(2):
        try:
            return await p.chromium.launch_persistent_context(
                os.path.join(PROFILES, "ui"), headless=False, no_viewport=True, args=UI_ARGS)
        except Exception as e:
            if attempt == 0 and ("Executable doesn't exist" in str(e) or "playwright install" in str(e)):
                await asyncio.to_thread(install_browser)
                continue
            raise


async def main():
    async with async_playwright() as p:
        ctl = Controller(p)
        ui_ctx = await launch_ui(p)
        ui = ui_ctx.pages[0] if ui_ctx.pages else await ui_ctx.new_page()
        await ui.expose_function("ajStart", lambda: asyncio.create_task(ctl.start()) and None)
        await ui.expose_function("ajSaveSettings", lambda s: asyncio.create_task(ctl.save_settings(s)) and None)
        await ui.expose_function("ajResetSettings", lambda: asyncio.create_task(ctl.reset_settings()) and None)
        await ui.expose_function("ajTestAlert", lambda: asyncio.create_task(ctl.test_alert()) and None)
        await ui.expose_function("ajClearData", lambda: asyncio.create_task(ctl.clear_data()) and None)
        await ui.expose_function("ajPause", lambda: asyncio.create_task(ctl.pause()) and None)
        await ui.expose_function("ajMarket", lambda cfg: asyncio.create_task(ctl.set_market(cfg)) and None)
        market_task = asyncio.create_task(ctl._market_loop())
        tips_task = asyncio.create_task(ctl._tips_loop())
        bg_tasks = [asyncio.create_task(ctl._icons_loop()), asyncio.create_task(ctl._discord_stats_loop())]
        await ui.expose_function("ajTipsRefresh", lambda: asyncio.create_task(ctl.refresh_tips()) and None)
        await ui.expose_function("ajClearLog", lambda: asyncio.create_task(ctl.clear_log()) and None)
        await ui.expose_function("ajStop", lambda: asyncio.create_task(ctl.stop()) and None)
        await ui.expose_function("ajCheckUpdate", lambda: asyncio.create_task(ctl.check_update()) and None)
        await ui.expose_function("ajInstallUpdate", lambda: asyncio.create_task(ctl.install_update()) and None)
        await ui.set_content(UI_HTML)
        prev = finish_update_if_pending()
        if prev:
            ctl.log(f"✅ Updated from v{prev} to v{APP_VERSION}")
        if CFG["auto_update_check"]:
            asyncio.create_task(ctl.check_update(quiet=True))
        if _NEW_INSTALL:
            save_cfg()
        if CFG["auto_start"]:
            asyncio.create_task(ctl.start())
        last_save = time.time()

        closed = asyncio.Event()
        ui.on("close", lambda _: closed.set())
        ui_ctx.on("close", lambda _: closed.set())
        while not closed.is_set() and not ctl.exit_requested:
            try:
                await ui.evaluate("s => window.render && window.render(s)", ctl.state())
            except Exception:
                pass
            if time.time() - last_save > 30:
                ctl.save_data()
                last_save = time.time()
            try:
                await asyncio.wait_for(closed.wait(), 0.4)
            except asyncio.TimeoutError:
                pass
        ctl.save_data()
        market_task.cancel()
        tips_task.cancel()
        for t in bg_tasks:
            t.cancel()
        await ctl.stop()
        try:
            await ui_ctx.close()
        except Exception:
            pass


def _kill_leftover_browsers():
    """Close Chromium windows left behind by a previous run that was force-closed (they lock the profiles)."""
    if os.name != "nt":
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like '*" + PROFILES.replace("'", "''") + "*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, creationflags=0x08000000)
    except Exception:
        pass


if __name__ == "__main__":
    os.chdir(BASE)
    if "--after-update" in sys.argv:
        time.sleep(3)   # let the old version close its browser windows and free the single-copy lock
    import socket
    _single = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    for _try in range(15 if "--after-update" in sys.argv else 1):
        try:
            _single.bind(("127.0.0.1", 47613))  # held for the app's lifetime: only one copy can run
            break
        except OSError:
            if _try < 14 and "--after-update" in sys.argv:
                time.sleep(1)
                continue
                _msg("AstraFlip AutoJoin", "AstraFlip AutoJoin is already running.\nCheck your taskbar for its window.")
            sys.exit(0)
    _kill_leftover_browsers()
    try:
        asyncio.run(main())
    except Exception:
        if rollback_update_if_pending():
            try:
                _single.close()
            except Exception:
                pass
            _msg("AstraFlip AutoJoin", "The new version failed to start, so the previous version was restored.", error=True)
            raise SystemExit
        import traceback
        err = traceback.format_exc()
        with open(os.path.join(BASE, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n") + err)
        _msg("AstraFlip AutoJoin crashed", err[-900:] + "\n\nDetails saved to error.log", error=True)
