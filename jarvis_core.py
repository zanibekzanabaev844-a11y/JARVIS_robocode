# -*- coding: utf-8 -*-
"""
Jarvis / Шеф - core logic (runs inside the Android foreground service).

Pipeline:
  Vosk (offline, Java thread) -> wake word "Жарвис"/"Шеф"
  -> wake screen + home + TTS greeting
  -> Vosk command recognition
  -> parser -> [presentation: Gemini + python-pptx] / [send: history.json + Pyrogram]
"""
import asyncio
import datetime
import difflib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import traceback

# --------------------------------------------------------------------------- paths
BASE_DIR = "/sdcard/Jarvis"
PRES_DIR = BASE_DIR + "/Presentations"
MODELS_SRC = BASE_DIR + "/models"
HISTORY_PATH = BASE_DIR + "/history.json"
CONFIG_PATH = BASE_DIR + "/config.json"
LOG_PATH = BASE_DIR + "/jarvis.log"
BEAT_PATH = BASE_DIR + "/.heartbeat"

DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "use_google_genai_sdk": True,          # falls back to plain REST if the SDK is not packaged
    "tg_api_id": 0,
    "tg_api_hash": "",
    "tg_session_string": "",               # create with make_session.py on your PC
    "tts_gender": "male",                  # "male" | "female"  (pitch based, see README)
    "tts_pitch": 0,                        # 0 = automatic from gender, or e.g. 0.8 / 1.2
    "tts_rate": 1.0,
    "tts_voice_name": "",                  # optional: part of an installed voice name (see jarvis.log)
    "tts_language": "kk-KZ",
    "tts_fallback_language": "ru-RU",
    "wake_model": "ru",                    # folder in /sdcard/Jarvis/models/
    "command_model": "kz",                 # "kz" (Kazakh, closest to Karakalpak) or "ru"
    "wake_regex": r"(?<!\p{L})(?:ш[еэ]ф|(?:дж|ж|х|г)[аеэ]\s?р\s?[вб][иі][сз]|ярвис)",
    "default_slides": 6,
}

SAY = {
    "wake": "Иа шеф, тыңлап атырман!",
    "ask_topic": "Қандай тақырыпта, шеф?",
    "ask_recipient": "Кімге жіберемін, шеф?",
    "working": "Жақсы шеф, {n} беттік презентация жасап жатырмын.",
    "done": "Шеф, презентация дайын. {n} бет, файл сақталды.",
    "fail": "Шеф, жұмыс кезінде қате шықты.",
    "not_understood": "Түсінбедім, шеф. Қайталаңызшы.",
    "no_pres": "Шеф, ондай презентация табылмады.",
    "sending": "Жақсы шеф, жіберіп жатырмын.",
    "sent": "Жіберілді, шеф. Алушы: {name}.",
    "no_key": "Шеф, Gemini кілті көрсетілмеген. Config файлын толтырыңыз.",
    "tg_not_cfg": "Шеф, Telegram бапталмаған.",
    "tg_not_found": "Шеф, Telegram-да {name} табылмады.",
    "tg_auth": "Шеф, Telegram сессиясы жарамсыз. Жаңа сессия жасау керек.",
    "no_net_gen": "Шеф, қазір интернет жоқ. Презентация жасай алмадым, интернет келгенде өзім жасаймын.",
    "no_net_send": "Шеф, қазір интернет жоқ. Файлды жібере алмадым, интернет келгенде өзім жіберемін.",
    "net_back": "Шеф, интернет келді. Тоқтап қалған жұмысты жалғастырамын.",
    "no_model": "Шеф, Vosk моделі табылмады. Оны телефонға салыңыз.",
}


class NoInternet(Exception):
    pass


class UserError(Exception):
    def __init__(self, key, **fmt):
        Exception.__init__(self, key)
        self.key = key
        self.fmt = fmt


# --------------------------------------------------------------------------- helpers
_log_lock = threading.Lock()


def log(*args):
    line = time.strftime("%H:%M:%S ") + " ".join(str(a) for a in args)
    print(line)
    try:
        with _log_lock:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 300000:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def ensure_storage():
    os.makedirs(PRES_DIR, exist_ok=True)
    os.makedirs(MODELS_SRC, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    if not os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            f.write("[]")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception as e:
        log("config read error:", e)
    return cfg


def has_internet(timeout=2.5):
    for host in (("8.8.8.8", 53), ("1.1.1.1", 53), ("9.9.9.9", 53)):
        try:
            socket.create_connection(host, timeout).close()
            return True
        except Exception:
            continue
    return False


def heartbeat():
    try:
        with open(BEAT_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def get_context():
    from jnius import autoclass
    try:
        svc = autoclass("org.kivy.android.PythonService").mService
        if svc is not None:
            return svc
    except Exception:
        pass
    return autoclass("org.kivy.android.PythonActivity").mActivity


# --------------------------------------------------------------------------- history
_hist_lock = threading.Lock()


def load_history():
    with _hist_lock:
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_history(items):
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_PATH)


def add_history(entry):
    items = load_history()
    with _hist_lock:
        items.append(entry)
        _save_history(items)


def mark_sent(path, name):
    items = load_history()
    with _hist_lock:
        for h in items:
            if h.get("path") == path:
                h.setdefault("sent_to", []).append(
                    {"name": name, "time": datetime.datetime.now().isoformat(timespec="seconds")})
        _save_history(items)


# --------------------------------------------------------------------------- command parsing
NUM_WORDS = {
    "бір": 1, "бир": 1, "один": 1,
    "екі": 2, "еки": 2, "эки": 2, "два": 2,
    "үш": 3, "уш": 3, "три": 3,
    "төрт": 4, "торт": 4, "четыре": 4,
    "бес": 5, "пять": 5,
    "алты": 6, "шесть": 6,
    "жеті": 7, "жети": 7, "семь": 7,
    "сегіз": 8, "сегиз": 8, "восемь": 8,
    "тоғыз": 9, "тогыз": 9, "девять": 9,
    "он": 10, "десять": 10,
    "он бір": 11, "он екі": 12, "он үш": 13, "он төрт": 14, "он бес": 15,
    "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15, "двадцать": 20,
}
_NUM_ALT = "|".join(sorted((re.escape(k) for k in NUM_WORDS), key=len, reverse=True))
_COUNT_RE = re.compile(
    r"(?<!\w)(\d+|%s)\s*-?\s*(?:беттік|бетті|бетлік|бет\w*|слайд\w*|страниц\w*|лист\w*|парақ\w*)" % _NUM_ALT)

_SEND_VERB = r"(?:жібер\w*|жибер\w*|жолда\w*|отправ\w*|пришли\w*|скин\w*|кинь\w*)"
_SEND_RE = re.compile(_SEND_VERB)
_MAKE_RE = re.compile(r"презентаци|слайд|таныстырылым")
_WAKE = r"(?<!\w)(?:жарвис|джарвис|харвис|гарвис|ярвис|шеф)(?!\w)"
_DATE_WORDS = r"(?<!\w)(?:кеше\w*|вчера\w*|бүгін\w*|бугін\w*|сегодня\w*|соңғы\w*|последн\w*|прошл\w*|жуырда\w*)"
_YESTERDAY = r"кеше|вчера"
_TODAY = r"бүгін|бугін|сегодня"
_FILE_WORDS = r"презентаци\w*|слайд\w*|файл\w*"

_TOPIC_STRIP = [
    _WAKE,
    r"презентаци\w*|слайд\w*|таныстырылым\w*",
    r"(?<!\w)(?:жаса(?:шы|ші|ңыз|ңызшы|п)?|дайында(?:п|шы|ңыз)?|сделай\w*|создай\w*|подготов\w*|"
    r"сгенерируй\w*|құрып|құрастыр\w*)(?!\w)",
    r"(?<!\w)(?:мына|осы|бұл|эта|эту|этой|на|по|для|тему|тема|теме|тақырып\w*|тақырыб\w*|"
    r"керек|пожалуйста|өтінемін|маған|мне|туралы|жайлы|про)(?!\w)",
]


def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-]", " ", s)).strip()


def extract_count(t):
    m = _COUNT_RE.search(t)
    if not m:
        return None
    v = m.group(1)
    n = int(v) if v.isdigit() else NUM_WORDS.get(v)
    return max(1, min(n, 30)) if n else None


def extract_topic(t):
    t = _COUNT_RE.sub(" ", t.lower())
    for p in _TOPIC_STRIP:
        t = re.sub(p, " ", t)
    t = _clean(t)
    return t[:1].upper() + t[1:] if len(t) >= 3 else ""


def extract_recipient(t):
    m = (re.search(r"(?:%s)\s+(.+?)\s+%s" % (_FILE_WORDS, _SEND_VERB), t)
         or re.search(r"%s\s+(.+?)\s+(?:%s)" % (_SEND_VERB, _FILE_WORDS), t))
    rec = m.group(1) if m else t
    for p in (_WAKE, _DATE_WORDS, _FILE_WORDS, _SEND_VERB,
              r"(?<!\w)(?:кім\w*|кому|для|на|маған|мне)(?!\w)"):
        rec = re.sub(p, " ", rec)
    return _clean(rec)


def parse_command(text, cfg):
    t = " ".join(text.lower().split())
    if _SEND_RE.search(t):
        return {"kind": "send", "text": t, "recipient": extract_recipient(t)}
    if _MAKE_RE.search(t):
        return {"kind": "presentation", "text": t,
                "slides": extract_count(t) or int(cfg.get("default_slides", 6)),
                "topic": extract_topic(t)}
    return {"kind": "unknown", "text": t}


def find_presentation(t):
    """Find the right entry in history.json: 'кешегі' / 'бүгінгі' / topic words / newest."""
    hist = [h for h in load_history() if os.path.exists(h.get("path", ""))]
    if not hist:
        return None
    today = datetime.date.today()
    want = None
    if re.search(_YESTERDAY, t):
        want = today - datetime.timedelta(days=1)
    elif re.search(_TODAY, t):
        want = today
    cands = [h for h in hist if h.get("created", "")[:10] == want.isoformat()] if want else hist
    if not cands:
        return None
    words = {w[:5] for w in re.findall(r"\w+", t) if len(w) > 3}

    def score(h):
        return sum(1 for w in re.findall(r"\w+", h.get("topic", "").lower()) if w[:5] in words)

    cands.sort(key=lambda h: (score(h), h.get("created", "")), reverse=True)
    return cands[0]


# --------------------------------------------------------------------------- Gemini
def _gemini_prompt(topic, n):
    return (
        'You are an expert presentation writer. Create a presentation on the topic: "%s".\n'
        "Write in the same language as the topic (Kazakh, Karakalpak or Russian, Cyrillic script).\n"
        "Return ONLY valid JSON with this exact shape:\n"
        '{"title": str, "subtitle": str, "slides": [{"title": str, "bullets": [str, ...]}]}\n'
        'Rules: "slides" has EXACTLY %d content slides (the title slide is added separately). '
        "Each slide has 3-5 concise bullets, max 14 words each. "
        "If there are 4 or more slides, the last one is a conclusion. No markdown, no comments."
        % (topic, max(0, n - 1)))


def _gemini_sdk(prompt, key, cfg):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=cfg.get("gemini_model", "gemini-2.5-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    return resp.text


def _gemini_rest(prompt, key, cfg):
    import requests
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % \
          cfg.get("gemini_model", "gemini-2.5-flash")
    try:
        r = requests.post(
            url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=90)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        raise NoInternet()
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def generate_slides(topic, n, cfg):
    key = (cfg.get("gemini_api_key") or "").strip()
    if not key:
        raise UserError("no_key")
    if not has_internet():
        raise NoInternet()
    prompt = _gemini_prompt(topic, n)
    try:
        use_sdk = False
        if cfg.get("use_google_genai_sdk", True):
            try:
                import google.genai  # noqa: F401
                use_sdk = True
            except Exception:
                use_sdk = False
        raw = (_gemini_sdk if use_sdk else _gemini_rest)(prompt, key, cfg)
    except NoInternet:
        raise
    except Exception:
        if not has_internet():
            raise NoInternet()
        raise
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(raw)
    slides = []
    for s in data.get("slides", [])[:max(0, n - 1)]:
        bullets = [str(b) for b in s.get("bullets", []) if str(b).strip()]
        slides.append({"title": str(s.get("title", "")), "bullets": bullets})
    if n > 1 and not slides:
        raise ValueError("Gemini returned no slides")
    return {"title": str(data.get("title") or topic), "subtitle": str(data.get("subtitle", "")),
            "slides": slides}


# --------------------------------------------------------------------------- PPTX
def build_pptx(data, topic):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    BG = RGBColor(0x0F, 0x17, 0x2A)
    ACC = RGBColor(0x38, 0xBD, 0xF8)
    FG = RGBColor(0xF1, 0xF5, 0xF9)
    MUTED = RGBColor(0x94, 0xA3, 0xB8)

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def new_slide():
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = BG
        return s

    def bar(s, x, y, w, h):
        sh = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
        sh.fill.solid()
        sh.fill.fore_color.rgb = ACC
        sh.line.fill.background()

    def text(s, x, y, w, h, txt, size, color, bold=False, anchor=MSO_ANCHOR.TOP,
             align=PP_ALIGN.LEFT):
        tf = s.shapes.add_textbox(x, y, w, h).text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        p = tf.paragraphs[0]
        p.alignment = align
        r = p.add_run()
        r.text = txt
        r.font.size, r.font.bold, r.font.color.rgb = Pt(size), bold, color

    # title slide
    s = new_slide()
    bar(s, Inches(0.8), Inches(2.4), Inches(0.12), Inches(2.4))
    text(s, Inches(1.2), Inches(2.3), Inches(11), Inches(1.6), data["title"], 44, FG, True,
         MSO_ANCHOR.MIDDLE)
    text(s, Inches(1.2), Inches(3.9), Inches(11), Inches(1.0), data.get("subtitle", ""), 22, MUTED)
    text(s, Inches(1.2), Inches(6.5), Inches(6), Inches(0.5),
         datetime.date.today().strftime("%d.%m.%Y"), 14, MUTED)

    # content slides
    for i, sl in enumerate(data["slides"], start=2):
        s = new_slide()
        bar(s, Inches(0), Inches(0), Inches(13.333), Inches(0.12))
        text(s, Inches(0.9), Inches(0.5), Inches(11.5), Inches(1.0), sl["title"], 34, ACC, True,
             MSO_ANCHOR.MIDDLE)
        tf = s.shapes.add_textbox(Inches(0.9), Inches(1.8), Inches(11.5), Inches(4.8)).text_frame
        tf.word_wrap = True
        for k, b in enumerate(sl["bullets"]):
            p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            p.space_after = Pt(14)
            r = p.add_run()
            r.text = u"\u2022  " + b
            r.font.size, r.font.color.rgb = Pt(24), FG
        text(s, Inches(11.8), Inches(6.9), Inches(1.2), Inches(0.4), str(i), 14, MUTED,
             align=PP_ALIGN.RIGHT)

    os.makedirs(PRES_DIR, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", topic).strip("_")[:40] or "presentation"
    path = "%s/%s_%s.pptx" % (PRES_DIR, time.strftime("%Y%m%d_%H%M%S"), safe)
    prs.save(path)
    return path


# --------------------------------------------------------------------------- Telegram (Pyrogram userbot)
def _tok(s):
    return re.findall(r"\w+", s.lower())


def _stem(w):
    return w[:-2] if len(w) > 5 else w


def best_match(query, cands, threshold=0.62):
    q = [_stem(w) for w in _tok(query)]
    if not q:
        return None
    best, best_score = None, 0.0
    for cid, name in cands:
        dt = _tok(name)
        if not dt:
            continue
        total = 0.0
        for w in q:
            total += max(1.0 if d.startswith(w) else
                         difflib.SequenceMatcher(None, w, d[:len(w) + 2]).ratio() for d in dt)
        score = total / len(q) - 0.03 * max(0, len(dt) - len(q))
        if score > best_score:
            best, best_score = (cid, name), score
    return best if best_score >= threshold else None


async def _tg_send(cfg, query, path, caption):
    from pyrogram import Client
    app = Client("jarvis", api_id=int(cfg["tg_api_id"]), api_hash=cfg["tg_api_hash"],
                 session_string=cfg["tg_session_string"], in_memory=True, no_updates=True)
    await app.start()
    try:
        cands = []
        async for d in app.get_dialogs(limit=400):
            c = d.chat
            name = c.title or " ".join(x for x in (c.first_name, c.last_name) if x)
            if name:
                cands.append((c.id, name))
        try:
            for u in await app.get_contacts():
                name = " ".join(x for x in (u.first_name, u.last_name) if x)
                if name:
                    cands.append((u.id, name))
        except Exception:
            pass
        best = best_match(query, cands)
        if not best:
            raise UserError("tg_not_found", name=query)
        await app.send_document(best[0], path, caption=caption)
        return best[1]
    finally:
        try:
            await app.stop()
        except Exception:
            pass


def telegram_send(cfg, query, path, caption=""):
    if not (cfg.get("tg_api_id") and cfg.get("tg_api_hash") and cfg.get("tg_session_string")):
        raise UserError("tg_not_cfg")
    if not has_internet():
        raise NoInternet()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)           # must exist BEFORE pyrogram is imported in this thread
    try:
        return loop.run_until_complete(
            asyncio.wait_for(_tg_send(cfg, query, path, caption), timeout=180))
    except UserError:
        raise
    except (ConnectionError, OSError, asyncio.TimeoutError):
        raise NoInternet()
    except Exception as e:
        if type(e).__name__ in ("AuthKeyUnregistered", "Unauthorized", "AuthKeyInvalid",
                                "UserDeactivated", "SessionRevoked", "AuthKeyDuplicated"):
            raise UserError("tg_auth")
        if not has_internet():
            raise NoInternet()
        raise
    finally:
        try:
            loop.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- Android: screen / home
_root_ok = None


def _root(cmd):
    global _root_ok
    if _root_ok is False:
        return False
    try:
        r = subprocess.run(["su", "-c", cmd], timeout=3, capture_output=True)
        ok = r.returncode == 0
        if _root_ok is None:
            _root_ok = ok
        return ok
    except Exception:
        _root_ok = False
        return False


def wake_device_and_home():
    """keyevent 26 + keyevent 3 when root exists; otherwise WakeLock + HOME intent."""
    try:
        from jnius import autoclass, cast
        ctx = get_context()
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        pm = cast("android.os.PowerManager", ctx.getSystemService(Context.POWER_SERVICE))
        if not pm.isInteractive():                       # keyevent 26 would TURN OFF a lit screen
            if not _root("input keyevent 26"):
                wl = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                                    | PowerManager.ACQUIRE_CAUSES_WAKEUP
                                    | PowerManager.ON_AFTER_RELEASE, "jarvis:wake")
                wl.acquire(8000)
            time.sleep(0.4)
        if not _root("input keyevent 3"):
            Intent = autoclass("android.content.Intent")
            i = Intent(Intent.ACTION_MAIN)
            i.addCategory(Intent.CATEGORY_HOME)
            i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ctx.startActivity(i)
    except Exception:
        log("wake_device error:", traceback.format_exc())


# --------------------------------------------------------------------------- Android: TTS
class TTS(object):
    def __init__(self, cfg):
        from jnius import PythonJavaClass, autoclass, java_method
        self.cfg = cfg
        self.ready = False
        outer = self

        class InitListener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
            __javacontext__ = "app"

            @java_method("(I)V")
            def onInit(self, status):
                outer._on_init(status)

        self._listener = InitListener()          # keep a reference!
        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        self.tts = TextToSpeech(get_context(), self._listener)

    def _on_init(self, status):
        try:
            from jnius import autoclass
            if status != 0:
                log("TTS init failed", status)
                return
            Locale = autoclass("java.util.Locale")
            lang_active = None
            for code in (self.cfg["tts_language"], self.cfg["tts_fallback_language"]):
                lang, _, country = code.replace("_", "-").partition("-")
                if self.tts.setLanguage(Locale(lang, country)) >= 0:
                    lang_active = lang
                    log("TTS language:", code)
                    break
            pitch = float(self.cfg.get("tts_pitch") or 0) or \
                (0.82 if str(self.cfg.get("tts_gender", "male")).lower() == "male" else 1.12)
            self.tts.setPitch(pitch)
            self.tts.setSpeechRate(float(self.cfg.get("tts_rate", 1.0)))
            self._pick_voice(lang_active)
            self.ready = True
        except Exception:
            log("TTS setup error:", traceback.format_exc())

    def _pick_voice(self, lang):
        try:
            voices = self.tts.getVoices()
            if voices is None:
                return
            it = voices.iterator()
            names, want = [], (self.cfg.get("tts_voice_name") or "").lower()
            while it.hasNext():
                v = it.next()
                if lang and v.getLocale().getLanguage() != lang:
                    continue
                names.append(v.getName())
                if want and want in v.getName().lower():
                    self.tts.setVoice(v)
            log("TTS voices for '%s': %s" % (lang, ", ".join(sorted(names))))
        except Exception:
            log("voice list error:", traceback.format_exc())

    def say(self, text, wait=True):
        t0 = time.time()
        while not self.ready and time.time() - t0 < 6:
            time.sleep(0.1)
        log("SAY:", text)
        try:
            self.tts.speak(text, 0, None, "jv%d" % int(time.time() * 1000))
            if wait:
                time.sleep(0.4)
                t0 = time.time()
                while self.tts.isSpeaking() and time.time() - t0 < 30:
                    time.sleep(0.1)
                time.sleep(0.3)               # let the echo die before the mic opens again
        except Exception:
            log("speak error:", traceback.format_exc())


# --------------------------------------------------------------------------- Vosk models
def prepare_model(name):
    """Copy /sdcard/Jarvis/models/<name> to private storage once; return path usable by native code."""
    ctx = get_context()
    dest = os.path.join(str(ctx.getFilesDir().getAbsolutePath()), "models", name)
    if os.path.isdir(os.path.join(dest, "am")):
        return dest
    src = None
    base = os.path.join(MODELS_SRC, name)
    for root, dirs, _ in os.walk(base):
        if "am" in dirs and "conf" in dirs:
            src = root
            break
        if root[len(base):].count(os.sep) >= 2:
            dirs[:] = []
    if not src:
        raise RuntimeError("Vosk model not found in %s" % base)
    log("copying model", name, "(first run only)...")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    return dest


# --------------------------------------------------------------------------- the assistant
class Jarvis(object):
    def __init__(self):
        ensure_storage()
        self.cfg = load_config()
        self.say_q = queue.Queue()
        self.retry = []
        self.tts = None
        self.engine = None
        self.wake_model = self.cmd_model = None

    # -- infrastructure -------------------------------------------------------
    def say(self, key_or_text, **fmt):
        text = SAY.get(key_or_text, key_or_text)
        self.say_q.put(text.format(**fmt) if fmt else text)

    def setup(self):
        from jnius import autoclass
        log("Jarvis service starting")
        self.tts = TTS(self.cfg)
        self.engine = autoclass("org.jarvis.voice.VoskEngine")()
        warned = False
        while True:
            try:
                self.cfg = load_config()
                self.wake_model = prepare_model(self.cfg["wake_model"])
                self.cmd_model = prepare_model(self.cfg["command_model"])
                break
            except Exception as e:
                log("model error:", e)
                if not warned:
                    self.tts.say(SAY["no_model"])
                    warned = True
                heartbeat()
                time.sleep(10)
        log("models ready")

    def start_wake(self):
        self.engine.startWake(self.wake_model, self.cfg["wake_regex"])

    def listen(self, first_ms=7000, max_ms=18000):
        self.engine.startListen(self.cmd_model, first_ms, max_ms)
        t0 = time.time()
        while time.time() - t0 < (max_ms / 1000.0 + 5):
            r = self.engine.poll()
            if r is not None:
                if r.startswith("ERR:"):
                    log(r)
                    return ""
                return r.strip()
            time.sleep(0.05)
        self.engine.cancel()
        return ""

    # -- main loop --------------------------------------------------------------
    def run_forever(self):
        self.setup()
        self.start_wake()
        last_beat = last_retry = 0
        while True:
            try:
                now = time.time()
                if now - last_beat > 2:
                    heartbeat()
                    last_beat = now
                if not self.say_q.empty():                 # speak queued job results
                    self.engine.cancel()
                    while not self.say_q.empty():
                        self.tts.say(self.say_q.get())
                    self.start_wake()
                    continue
                if now - last_retry > 20:
                    last_retry = now
                    self.try_retry()
                r = self.engine.poll()
                if r is not None:
                    if r.startswith("ERR:"):
                        log("wake engine:", r)
                        time.sleep(3)
                    elif r:
                        self.on_wake(r)
                    self.start_wake()
                    continue
                time.sleep(0.04)
            except Exception:
                log("loop error:", traceback.format_exc())
                time.sleep(1)
                try:
                    self.start_wake()
                except Exception:
                    pass

    # -- wake + dispatch -------------------------------------------------------
    def on_wake(self, heard):
        log("WAKE:", heard)
        threading.Thread(target=wake_device_and_home, daemon=True).start()
        self.tts.say(SAY["wake"])
        text = self.listen()
        log("HEARD:", text)
        if not text:
            return
        self.cfg = load_config()                    # pick up config edits without restart
        intent = parse_command(text, self.cfg)
        log("INTENT:", intent)
        if intent["kind"] == "presentation":
            self.cmd_presentation(intent)
        elif intent["kind"] == "send":
            self.cmd_send(intent)
        else:
            self.tts.say(SAY["not_understood"])

    def cmd_presentation(self, it):
        topic, n = it["topic"], it["slides"]
        if not topic:
            self.tts.say(SAY["ask_topic"])
            topic = extract_topic(self.listen()) or ""
            if not topic:
                self.tts.say(SAY["not_understood"])
                return
        self.tts.say(SAY["working"].format(n=n))
        threading.Thread(target=self.job_presentation, args=(topic, n, 1), daemon=True).start()

    def cmd_send(self, it):
        entry = find_presentation(it["text"])
        if not entry:
            self.tts.say(SAY["no_pres"])
            return
        recipient = it.get("recipient", "")
        if len(recipient) < 2:
            self.tts.say(SAY["ask_recipient"])
            recipient = extract_recipient(self.listen().lower())
            if len(recipient) < 2:
                self.tts.say(SAY["not_understood"])
                return
        self.tts.say(SAY["sending"])
        threading.Thread(target=self.job_send, args=(recipient, entry, 1), daemon=True).start()

    # -- jobs (worker threads; results are spoken through say_q) ---------------
    def job_presentation(self, topic, n, attempt):
        try:
            data = generate_slides(topic, n, self.cfg)
            path = build_pptx(data, topic)
            add_history({"id": time.strftime("%Y%m%d%H%M%S"), "topic": topic, "title": data["title"],
                         "slides": n, "path": path,
                         "created": datetime.datetime.now().isoformat(timespec="seconds"),
                         "sent_to": []})
            log("PPTX saved:", path)
            self.say("done", n=n)
        except NoInternet:
            log("no internet: presentation")
            if attempt == 1:
                self.say("no_net_gen")
            if attempt < 4:
                self.retry.append(lambda: self.job_presentation(topic, n, attempt + 1))
        except UserError as e:
            self.say(e.key, **e.fmt)
        except Exception:
            log("presentation error:", traceback.format_exc())
            self.say("fail")

    def job_send(self, recipient, entry, attempt):
        try:
            name = telegram_send(self.cfg, recipient, entry["path"], caption=entry.get("title", ""))
            mark_sent(entry["path"], name)
            self.say("sent", name=name)
        except NoInternet:
            log("no internet: telegram")
            if attempt == 1:
                self.say("no_net_send")
            if attempt < 4:
                self.retry.append(lambda: self.job_send(recipient, entry, attempt + 1))
        except UserError as e:
            self.say(e.key, **e.fmt)
        except Exception:
            log("send error:", traceback.format_exc())
            self.say("fail")

    def try_retry(self):
        if self.retry and has_internet():
            jobs, self.retry = self.retry, []
            self.say("net_back")
            for j in jobs:
                threading.Thread(target=j, daemon=True).start()
