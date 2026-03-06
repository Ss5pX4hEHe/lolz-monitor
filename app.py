import requests, time, re, json, threading, os
from datetime import datetime
from flask import Flask, jsonify, request, Response

BOT_TOKEN = "8613935632:AAFd_xP-xQmYruNe0nnxIIOzfDsYQMTSDls"
TG_URL = "https://api.telegram.org/bot" + BOT_TOKEN
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
app = Flask(__name__)
lock = threading.Lock()
tg_ok = False

def db_load():
    try:
        if os.path.exists(DATA):
            with open(DATA, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("lots", [])
                d.setdefault("subscribers", [])
                d.setdefault("logs", [])
                return d
    except:
        pass
    return {"lots": [], "subscribers": [], "logs": []}

def db_save(d):
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def log(msg, t=""):
    print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg), flush=True)
    with lock:
        d = db_load()
        d["logs"].insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "type": t})
        d["logs"] = d["logs"][:100]
        db_save(d)

def tg_send(chat_id, text):
    try:
        r = requests.post(TG_URL + "/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=10)
        return r.status_code == 200
    except:
        return False

def broadcast(text):
    with lock:
        subs = db_load().get("subscribers", [])
    n = sum(1 for s in subs if tg_send(s, text))
    log("Отправлено {}/{} подписчикам".format(n, len(subs)), "ok")

def tg_poll():
    global tg_ok
    # Удаляем вебхук если установлен — иначе polling не работает
    try:
        requests.post(TG_URL + "/deleteWebhook", timeout=10)
    except:
        pass
    offset = None
    while True:
        try:
            p = {"timeout": 20}
            if offset:
                p["offset"] = offset
            r = requests.get(TG_URL + "/getUpdates", params=p, timeout=25)
            data = r.json()
            tg_ok = data.get("ok", False)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                cid = str(msg.get("chat", {}).get("id", ""))
                txt = msg.get("text", "")
                if cid and txt.startswith("/start"):
                    with lock:
                        d = db_load()
                        if cid not in d["subscribers"]:
                            d["subscribers"].append(cid)
                            db_save(d)
                            log("Новый подписчик: " + cid, "ok")
                    tg_send(cid, "Вы подписаны! Пришлю уведомление когда лот будет продан.")
        except Exception as e:
            tg_ok = False
        time.sleep(3)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

def check_sold(item_id):
    try:
        r = requests.get("https://api.lzt.market/" + str(item_id), headers=HEADERS, timeout=15)
        if r.status_code == 404:
            return True, "404/удалён"
        raw = r.text.strip()
        if not raw or raw[0] not in "{[":
            return None, "не JSON (код {})".format(r.status_code)
        data = json.loads(raw)
        if "error" in data:
            err = data["error"]
            if isinstance(err, dict) and "not found" in str(err.get("message", "")).lower():
                return True, "не найден"
            return None, "API: {}".format(err)
        item = data.get("item", {})
        state = item.get("item_state") or data.get("item_state") or "active"
        sold_flag = item.get("sold", False) or data.get("sold", False)
        SOLD = ["sold", "deleted", "closed", "awaiting", "paid", "purchased"]
        return bool(sold_flag) or state in SOLD, state
    except requests.exceptions.Timeout:
        return None, "timeout"
    except json.JSONDecodeError as e:
        return None, "JSON err: {}".format(e)
    except Exception as e:
        return None, str(e)

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

def monitor():
    log("Монитор запущен (интервал {}с)".format(CHECK_INTERVAL))
    while True:
        try:
            with lock:
                lots = db_load().get("lots", [])
            active = [l for l in lots if l.get("status") == "active"]
            if active:
                log("Проверяю {} лот(ов)...".format(len(active)))
            for lot in active:
                is_s, state = check_sold(lot["item_id"])
                if is_s is None:
                    log("Ошибка #{}: {}".format(lot["item_id"], state), "err")
                    continue
                log("#{}: {}".format(lot["item_id"], state))
                if is_s:
                    with lock:
                        d = db_load()
                        for l in d["lots"]:
                            if l["item_id"] == lot["item_id"]:
                                l["status"] = "sold"
                                l["sold_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
                        db_save(d)
                    note = ("\n" + lot["note"]) if lot.get("note") else ""
                    broadcast("Лот продан! #{}{}\n{}".format(
                        lot["item_id"], note, datetime.now().strftime("%d.%m.%Y %H:%M")))
                    log("ЛОТ #{} ПРОДАН!".format(lot["item_id"]), "ok")
        except Exception as e:
            log("Ошибка: " + str(e), "err")
        time.sleep(CHECK_INTERVAL)

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/lots", methods=["GET"])
def get_lots():
    with lock:
        return jsonify(db_load().get("lots", []))

@app.route("/api/lots", methods=["POST"])
def add_lot():
    b = request.get_json(force=True) or {}
    url = b.get("url", "").strip()
    note = b.get("note", "").strip()
    m = re.search(r"/(\d+)/?", url)
    if not m:
        return jsonify({"error": "Неверная ссылка"}), 400
    item_id = m.group(1)
    with lock:
        d = db_load()
        if any(l["item_id"] == item_id for l in d["lots"]):
            return jsonify({"error": "Уже добавлен"}), 400
        d["lots"].insert(0, {
            "item_id": item_id, "url": url, "note": note,
            "status": "active",
            "added_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "sold_at": None
        })
        db_save(d)
    log("Добавлен лот #" + item_id)
    return jsonify({"ok": True})

@app.route("/api/lots/<iid>", methods=["DELETE"])
def del_lot(iid):
    with lock:
        d = db_load()
        d["lots"] = [l for l in d["lots"] if l["item_id"] != iid]
        db_save(d)
    log("Удалён #" + iid)
    return jsonify({"ok": True})

@app.route("/api/lots/<iid>/note", methods=["PUT"])
def upd_note(iid):
    note = (request.get_json(force=True) or {}).get("note", "")
    with lock:
        d = db_load()
        for l in d["lots"]:
            if l["item_id"] == iid:
                l["note"] = note
        db_save(d)
    return jsonify({"ok": True})

@app.route("/api/check/<iid>")
def api_check(iid):
    sold, state = check_sold(iid)
    return jsonify({"sold": sold, "state": state})

@app.route("/api/test_tg", methods=["POST"])
def test_tg():
    with lock:
        subs = db_load().get("subscribers", [])
    if not subs:
        return jsonify({"ok": False, "error": "Нет подписчиков! Напиши /start боту"})
    n = sum(1 for s in subs if tg_send(s, "Тест: соединение работает!"))
    return jsonify({"ok": n > 0, "sent": n})

@app.route("/api/clear_subs", methods=["POST"])
def clear_subs():
    with lock:
        d = db_load()
        d["subscribers"] = []
        db_save(d)
    log("Подписчики сброшены", "warn")
    return jsonify({"ok": True})

@app.route("/api/stats")
def stats():
    with lock:
        d = db_load()
    lots = d.get("lots", [])
    return jsonify({
        "active": sum(1 for l in lots if l["status"] == "active"),
        "sold": sum(1 for l in lots if l["status"] == "sold"),
        "subscribers": len(d.get("subscribers", [])),
        "logs": d.get("logs", [])[:30],
        "tg_ok": tg_ok
    })

@app.route("/health")
def health():
    return "OK"

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lolz Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#07080d;--s1:#0e0f18;--s2:#141520;--bd:#1e2030;--acc:#e8ff47;--red:#ff4d6d;--grn:#39ff8f;--blu:#4d8fff;--org:#ff9f47;--txt:#dde1f5;--mut:#4a4e6a;--r:10px}
html,body{min-height:100%;background:var(--bg);color:var(--txt);font-family:'IBM Plex Mono',monospace}
body{background-image:radial-gradient(ellipse 60% 40% at 10% 0%,rgba(232,255,71,.05) 0%,transparent 70%),radial-gradient(ellipse 50% 50% at 90% 100%,rgba(255,77,109,.04) 0%,transparent 70%)}
header{display:flex;align-items:center;gap:14px;padding:16px 28px;border-bottom:1px solid var(--bd);background:rgba(7,8,13,.9);position:sticky;top:0;z-index:100}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:20px;letter-spacing:-1px;color:var(--acc)}
.logo span{color:var(--mut)}
.hl{margin-left:auto;display:flex;gap:8px}
.pill{display:flex;align-items:center;gap:6px;background:var(--s1);border:1px solid var(--bd);border-radius:20px;padding:5px 13px;font-size:11px}
.pill b{font-family:'Syne',sans-serif;font-size:13px}
.wrap{max-width:960px;margin:0 auto;padding:24px 28px}
.box{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:18px 20px;margin-bottom:14px}
.lbl{font-family:'Syne',sans-serif;font-size:10px;font-weight:700;color:var(--mut);letter-spacing:3px;text-transform:uppercase;margin-bottom:12px}
.status-box{background:rgba(57,255,143,.05);border:1px solid rgba(57,255,143,.2);border-radius:var(--r);padding:14px 18px;margin-bottom:14px}
.st-row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.st-item{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--mut)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut);flex-shrink:0;transition:.3s}
.dot.ok{background:var(--grn);box-shadow:0 0 6px var(--grn)}
.dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.dot.spin{background:var(--org);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
input,textarea,select{background:var(--s2);border:1px solid var(--bd);border-radius:8px;color:var(--txt);font-family:'IBM Plex Mono',monospace;font-size:12px;padding:10px 13px;outline:none;width:100%;transition:border-color .2s}
input::placeholder,textarea::placeholder{color:var(--mut)}
input:focus,textarea:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(232,255,71,.08)}
textarea{resize:none;height:60px}
select{cursor:pointer}
.row{display:flex;gap:10px}
.col{flex:1;display:flex;flex-direction:column;gap:8px}
.btn{border:none;border-radius:8px;font-family:'Syne',sans-serif;font-weight:800;font-size:11px;cursor:pointer;transition:.2s;letter-spacing:.5px;white-space:nowrap}
.btn-add{background:var(--acc);color:#07080d;padding:0 22px;height:44px;align-self:stretch}
.btn-add:hover{background:#f5ff70}
.btn-grn{background:rgba(57,255,143,.1);color:var(--grn);border:1px solid rgba(57,255,143,.3);padding:0 16px;height:34px}
.btn-grn:hover{background:rgba(57,255,143,.2)}
.btn-grn:disabled{opacity:.4;cursor:not-allowed}
.sm{background:none;border:1px solid var(--bd);border-radius:6px;color:var(--mut);font-size:10px;padding:5px 11px;cursor:pointer;font-family:'IBM Plex Mono',monospace;transition:.2s}
.sm:hover{border-color:var(--acc);color:var(--acc)}
.sm.blu{border-color:rgba(77,143,255,.3);color:var(--blu)}
.sm.blu:hover{border-color:var(--blu);background:rgba(77,143,255,.1)}
.sm.org{border-color:rgba(255,159,71,.3);color:var(--org)}
.sm.org:hover{border-color:var(--org);background:rgba(255,159,71,.1)}
.iv-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.iv-row>span{font-size:11px;color:var(--mut);white-space:nowrap}
.iv-row input{width:70px;text-align:center}
.iv-row select{width:100px}
#cd{color:var(--org)}
.sh{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.sh-title{font-family:'Syne',sans-serif;font-weight:700;font-size:11px;color:var(--mut);letter-spacing:3px;text-transform:uppercase}
.badge{background:var(--s2);border:1px solid var(--bd);border-radius:20px;padding:1px 10px;font-size:11px;color:var(--mut)}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tab{background:var(--s1);border:1px solid var(--bd);border-radius:20px;padding:5px 14px;font-size:10px;font-family:'Syne',sans-serif;font-weight:700;color:var(--mut);cursor:pointer;transition:.2s}
.tab.on{background:var(--acc);color:#07080d;border-color:var(--acc)}
.tab:hover:not(.on){border-color:var(--acc);color:var(--acc)}
.lots{display:flex;flex-direction:column;gap:8px}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:14px 18px;display:flex;align-items:flex-start;gap:14px;animation:fi .25s ease}
@keyframes fi{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.card:hover{border-color:rgba(232,255,71,.2)}
.card.sold{border-color:rgba(57,255,143,.2);background:rgba(57,255,143,.02)}
.tag{flex-shrink:0;font-family:'Syne',sans-serif;font-weight:700;font-size:9px;padding:4px 9px;border-radius:20px;margin-top:2px}
.tag.a{background:rgba(232,255,71,.1);color:var(--acc);border:1px solid rgba(232,255,71,.25)}
.tag.s{background:rgba(57,255,143,.1);color:var(--grn);border:1px solid rgba(57,255,143,.25)}
.cb{flex:1;min-width:0}
.cid{font-family:'Syne',sans-serif;font-size:14px;font-weight:700;margin-bottom:3px}
.curl{font-size:10px;color:var(--blu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:400px;margin-bottom:9px}
.curl a{color:inherit;text-decoration:none}
.curl a:hover{text-decoration:underline}
.nr{display:flex;gap:6px;align-items:center;margin-bottom:7px}
.ni{font-size:11px;height:29px;border:1px dashed var(--bd);color:var(--mut);border-radius:6px;flex:1;padding:0 10px;background:var(--s2)}
.ni:focus{color:var(--txt);border-style:solid}
.meta{font-size:10px;color:var(--mut);display:flex;gap:14px;flex-wrap:wrap}
.del{background:none;border:1px solid var(--bd);border-radius:6px;color:var(--mut);font-size:10px;padding:5px 10px;cursor:pointer;transition:.2s;font-family:'IBM Plex Mono',monospace;flex-shrink:0}
.del:hover{border-color:var(--red);color:var(--red)}
.empty{text-align:center;padding:48px 0;color:var(--mut);font-size:12px}
.empty-ico{font-size:30px;margin-bottom:10px;opacity:.4}
.logbox{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);margin-top:16px;overflow:hidden}
.lhd{padding:11px 18px;border-bottom:1px solid var(--bd);font-family:'Syne',sans-serif;font-weight:700;font-size:10px;color:var(--mut);letter-spacing:3px;text-transform:uppercase;display:flex;align-items:center;gap:8px}
.ldot{width:7px;height:7px;border-radius:50%;background:var(--grn);box-shadow:0 0 6px var(--grn);animation:pulse 2s infinite}
.le{padding:8px 18px;border-bottom:1px solid var(--bd);display:flex;gap:12px;font-size:10px}
.le:last-child{border:none}
.lt{color:var(--mut);flex-shrink:0}.lm{color:var(--txt);word-break:break-all}
.lm.ok{color:var(--grn)}.lm.err{color:var(--red)}.lm.warn{color:var(--org)}
.toast{position:fixed;bottom:20px;right:20px;z-index:999;background:var(--s2);border:1px solid var(--grn);color:var(--grn);border-radius:8px;padding:10px 18px;font-size:12px;transform:translateY(60px);opacity:0;transition:.25s;max-width:300px}
.toast.on{transform:none;opacity:1}
.toast.e{border-color:var(--red);color:var(--red)}
</style>
</head>
<body>
<header>
  <div class="ldot" style="width:9px;height:9px"></div>
  <div class="logo">LOLZ<span>.</span>MONITOR</div>
  <div class="hl">
    <div class="pill">🟡 <b id="ha">0</b>&nbsp;активных</div>
    <div class="pill">🟢 <b id="hs">0</b>&nbsp;продано</div>
    <div class="pill">👥 <b id="hu">0</b>&nbsp;подписчиков</div>
  </div>
</header>

<div class="wrap">
  <div class="status-box">
    <div class="lbl">Статус подключения</div>
    <div class="st-row">
      <div class="st-item"><div class="dot ok" id="d-srv"></div><span id="l-srv">Сервер: работает</span></div>
      <div class="st-item"><div class="dot spin" id="d-tg"></div><span id="l-tg">Telegram: проверяю...</span></div>
      <div class="st-item"><div class="dot spin" id="d-sub"></div><span id="l-sub">Подписчики: ...</span></div>
    </div>
    <div class="btn-row">
      <button class="sm blu" onclick="testTg()">📨 Тест Telegram</button>
      <button class="sm org" onclick="testApi()">🔍 Тест lzt.market</button>
      <button class="sm" onclick="clearSubs()">🗑 Сбросить подписчиков</button>
    </div>
  </div>

  <div class="box">
    <div class="lbl">Интервал проверки</div>
    <div class="iv-row">
      <span>Каждые</span>
      <input id="iv" type="number" min="5" value="30">
      <select id="iu">
        <option value="1">секунд</option>
        <option value="60" selected>минут</option>
        <option value="3600">часов</option>
      </select>
      <button class="sm" style="border-color:var(--acc);color:var(--acc);padding:6px 12px" onclick="saveIv()">✓ Сохранить</button>
      <span>След: <b id="cd">—</b></span>
      <button class="btn btn-grn" id="bn" onclick="checkNow()">⚡ Проверить сейчас</button>
    </div>
  </div>

  <div class="box">
    <div class="lbl">Добавить лот</div>
    <div class="row">
      <div class="col">
        <input id="url" placeholder="https://lzt.market/12345678">
        <textarea id="note" placeholder="Заметка: цена покупки, описание..."></textarea>
      </div>
      <button class="btn btn-add" onclick="addLot()">+ Добавить</button>
    </div>
  </div>

  <div class="sh">
    <span class="sh-title">Мои лоты</span>
    <span class="badge" id="tc">0</span>
  </div>
  <div class="tabs">
    <div class="tab on" onclick="setF('all',this)">Все</div>
    <div class="tab" onclick="setF('active',this)">Активные</div>
    <div class="tab" onclick="setF('sold',this)">Проданные</div>
  </div>
  <div class="lots" id="lots"></div>

  <div class="logbox">
    <div class="lhd"><div class="ldot"></div>Лог событий</div>
    <div id="logs"></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let ivMs = parseInt(localStorage.getItem("lolz_iv") || "30000");
let cdSec = 0, cdTmr = null, chkTmr = null, flt = "all";

function saveIv() {
  const v = parseInt(document.getElementById("iv").value) || 30;
  const u = parseInt(document.getElementById("iu").value);
  ivMs = Math.max(5000, v * u * 1000);
  localStorage.setItem("lolz_iv", ivMs);
  sched();
  toast("Сохранено: каждые " + v + " " + ({1:"сек",60:"мин",3600:"ч"}[u]||""));
}
function loadIv() {
  let v, u;
  if (ivMs % 3600000 === 0) { v = ivMs/3600000; u = "3600"; }
  else if (ivMs % 60000 === 0) { v = ivMs/60000; u = "60"; }
  else { v = ivMs/1000; u = "1"; }
  document.getElementById("iv").value = v;
  document.getElementById("iu").value = u;
}
function sched() {
  clearTimeout(chkTmr); clearInterval(cdTmr);
  cdSec = Math.round(ivMs / 1000); updCd();
  cdTmr = setInterval(() => { if (--cdSec <= 0) clearInterval(cdTmr); updCd(); }, 1000);
  chkTmr = setTimeout(() => { refresh(); sched(); }, ivMs);
}
function updCd() {
  const el = document.getElementById("cd");
  if (!el) return;
  if (cdSec <= 0) { el.textContent = "—"; return; }
  const m = Math.floor(cdSec/60), s = cdSec % 60;
  el.textContent = m > 0 ? m + "м " + s + "с" : s + "с";
}
async function checkNow() {
  const b = document.getElementById("bn");
  b.disabled = true; b.textContent = "⏳...";
  await refresh();
  sched();
  b.disabled = false; b.textContent = "⚡ Проверить сейчас";
}

function setDot(k, st, lbl) {
  const d = document.getElementById("d-" + k), e = document.getElementById("l-" + k);
  if (d) { d.className = "dot " + (st || ""); }
  if (e) e.textContent = lbl;
}
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "toast on" + (err ? " e" : "");
  setTimeout(() => t.classList.remove("on"), 2800);
}
function setF(f, el) {
  flt = f;
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("on"));
  el.classList.add("on"); refresh();
}
function exId(url) { const m = url.match(/\\/(\\d+)\\/?/); return m ? m[1] : null; }

async function addLot() {
  const url = document.getElementById("url").value.trim();
  const note = document.getElementById("note").value.trim();
  if (!url || !exId(url)) { toast("Неверная ссылка", 1); return; }
  const r = await fetch("/api/lots", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({url, note})
  });
  const d = await r.json();
  if (d.error) { toast(d.error, 1); return; }
  document.getElementById("url").value = "";
  document.getElementById("note").value = "";
  toast("Лот добавлен!");
  refresh();
}
async function delLot(id) {
  await fetch("/api/lots/" + id, {method: "DELETE"});
  toast("Удалено"); refresh();
}
async function saveNote(id) {
  const note = document.getElementById("n" + id).value;
  await fetch("/api/lots/" + id + "/note", {
    method: "PUT", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({note})
  });
  toast("Заметка сохранена");
}
async function testTg() {
  const r = await fetch("/api/test_tg", {method: "POST"});
  const d = await r.json();
  d.ok ? toast("Отправлено " + d.sent + " подписчику(ам)!") : toast(d.error, 1);
}
async function testApi() {
  const r = await fetch("/api/lots");
  const lots = await r.json();
  if (!lots.length) { toast("Сначала добавь лот"); return; }
  const c = await fetch("/api/check/" + lots[0].item_id);
  const d = await c.json();
  toast(d.sold ? "Лот продан (" + d.state + ")" : "Лот активен (" + d.state + ")");
}
async function clearSubs() {
  if (!confirm("Сбросить подписчиков?")) return;
  await fetch("/api/clear_subs", {method: "POST"});
  toast("Сброшено"); refresh();
}

async function refresh() {
  try {
    const [lr, sr] = await Promise.all([fetch("/api/lots"), fetch("/api/stats")]);
    const lots = await lr.json(), st = await sr.json();
    document.getElementById("ha").textContent = st.active;
    document.getElementById("hs").textContent = st.sold;
    document.getElementById("hu").textContent = st.subscribers;
    document.getElementById("tc").textContent = lots.length;
    setDot("tg", st.tg_ok ? "ok" : "err", st.tg_ok ? "Telegram: подключен" : "Telegram: ошибка бота");
    setDot("sub", st.subscribers > 0 ? "ok" : "err",
      st.subscribers > 0 ? "Подписчики: " + st.subscribers + " чел." : "Подписчики: напиши /start боту!");

    const fil = lots.filter(l => flt === "all" || l.status === flt);
    const el = document.getElementById("lots");
    if (!fil.length) {
      el.innerHTML = '<div class="empty"><div class="empty-ico">📭</div>' +
        (lots.length ? "Нет лотов в этой категории" : "Добавьте первый лот выше") + "</div>";
    } else {
      el.innerHTML = fil.map(l =>
        '<div class="card ' + (l.status === "sold" ? "sold" : "") + '">' +
        '<span class="tag ' + (l.status === "active" ? "a" : "s") + '">' +
        (l.status === "active" ? "АКТИВЕН" : "ПРОДАН") + "</span>" +
        '<div class="cb">' +
        '<div class="cid">Лот #' + l.item_id + "</div>" +
        '<div class="curl"><a href="' + l.url + '" target="_blank">' + l.url + "</a></div>" +
        '<div class="nr"><input class="ni" id="n' + l.item_id + '" value="' +
        (l.note || "").replace(/"/g, "&quot;") + '" placeholder="Заметка...">' +
        '<button class="sm" onclick="saveNote(\\'' + l.item_id + '\\')">💾</button></div>' +
        '<div class="meta"><span>Добавлен: ' + l.added_at + "</span>" +
        (l.sold_at ? '<span style="color:var(--grn)">Продан: ' + l.sold_at + "</span>" : "") +
        "</div></div>" +
        '<button class="del" onclick="delLot(\\'' + l.item_id + '\\')">✕</button></div>'
      ).join("");
    }

    const logs = st.logs || [];
    document.getElementById("logs").innerHTML = logs.length
      ? logs.map(l => '<div class="le"><span class="lt">' + l.time +
          '</span><span class="lm ' + (l.type || "") + '">' + l.msg + "</span></div>").join("")
      : '<div class="le"><span class="lm" style="color:var(--mut)">Ожидание событий...</span></div>';
  } catch (e) {
    setDot("srv", "err", "Сервер: не отвечает");
  }
}

document.getElementById("url").addEventListener("keydown", e => { if (e.key === "Enter") addLot(); });
loadIv(); refresh(); sched();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

# Запускаем потоки на уровне модуля — работает и с gunicorn и напрямую
threading.Thread(target=tg_poll, daemon=True).start()
threading.Thread(target=monitor, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if not os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("RENDER"):
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:{}".format(port))).start()
        print("\nLolz Monitor -> http://localhost:{}\n".format(port))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
