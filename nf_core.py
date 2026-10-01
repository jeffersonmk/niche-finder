"""Núcleo do Niche Finder: cliente da API do YouTube, tradução, filtros, pontuação e buscas."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
API = "https://www.googleapis.com/youtube/v3/"
ENV_FILE = Path.home() / ".config" / "niche-finder" / ".env"
CACHE_DIR = ROOT / "cache"
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "channels.json"
QUOTA_FILE = DATA_DIR / "quota.json"
QUOTA_COST = {"search": 100}  # demais endpoints custam 1
DAILY_QUOTA = 10_000


class ApiError(Exception):
    pass


# ------------------------------------------------------------------ config / chave
def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def criteria(cfg: dict, overrides: dict | None = None) -> dict:
    """Critérios de aprovação: padrão do config.json, sobrescrito pelo que veio da interface."""
    c = dict(cfg["criteria"])
    for k, v in (overrides or {}).items():
        if k in c and v not in (None, ""):
            c[k] = int(v)
    return c


def load_api_key() -> str:
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key and ENV_FILE.exists():
        _restrict(ENV_FILE)
        _restrict(ENV_FILE.parent, 0o700)
        for line in ENV_FILE.read_text().splitlines():
            if line.strip().startswith("YT_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def _restrict(path: Path, mode: int = 0o600) -> None:
    """Garante que só o dono consiga ler o arquivo/pasta (no Windows, chmod é ignorado)."""
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            os.chmod(path, mode)
    except OSError:
        pass


def save_api_key(key: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(ENV_FILE.parent, 0o700)
    # cria o arquivo já com permissão 600 (sem janela em que outros usuários poderiam lê-lo)
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"YT_API_KEY={key.strip()}\n")
    _restrict(ENV_FILE)


def redact(text: str) -> str:
    """Remove a chave de qualquer mensagem antes de exibi-la ou registrá-la."""
    key = load_api_key()
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(key=)[A-Za-z0-9_\-]{20,}", r"\1***", text)


# ------------------------------------------------------------------ cota diária (reseta meia-noite do Pacífico)
_quota_lock = threading.Lock()


def _pacific_day() -> str:
    try:
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # Windows sem o pacote tzdata: aproxima com UTC-8
        tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).strftime("%Y-%m-%d")


def quota_status() -> dict:
    used = 0
    if QUOTA_FILE.exists():
        q = json.loads(QUOTA_FILE.read_text())
        if q.get("day") == _pacific_day():
            used = q.get("used", 0)
    return {"used": used, "limit": DAILY_QUOTA, "remaining": max(DAILY_QUOTA - used, 0)}


def _add_quota(n: int) -> None:
    with _quota_lock:
        DATA_DIR.mkdir(exist_ok=True)
        used = quota_status()["used"] + n
        QUOTA_FILE.write_text(json.dumps({"day": _pacific_day(), "used": used}))


# ------------------------------------------------------------------ cliente da API
class YouTube:
    def __init__(self, key: str, cache_hours: float = 12, fetch=None):
        if not key and fetch is None:
            raise ApiError("Chave da API não configurada. Clique em ⚙ Configurações e cole sua chave.")
        self.key, self.cache_hours = key, cache_hours
        self._fetch = fetch or self._http_get
        self.used = 0

    @staticmethod
    def _http_get(url: str) -> dict:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if "quotaExceeded" in body:
                    raise ApiError("Cota diária da API esgotada. Ela reseta à meia-noite do horário do Pacífico (~04h/05h em Brasília).")
                if "API_KEY_INVALID" in body or "keyInvalid" in body:
                    raise ApiError("Chave da API inválida. Confira em ⚙ Configurações.")
                if "accessNotConfigured" in body or "SERVICE_DISABLED" in body:
                    raise ApiError("A YouTube Data API v3 não está ativada no seu projeto do Google Cloud.")
                if e.code >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(redact(f"Erro da API ({e.code}): {body[:300]}"))
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(redact(f"Sem conexão com a API: {e.reason}"))
        raise ApiError("falha inesperada")

    def get(self, endpoint: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        ck = hashlib.sha1(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        cf = CACHE_DIR / f"{endpoint}_{ck}.json"
        if self.cache_hours and cf.exists() and time.time() - cf.stat().st_mtime < self.cache_hours * 3600:
            return json.loads(cf.read_text())
        url = API + endpoint + "?" + urllib.parse.urlencode({**params, "key": self.key})
        data = self._fetch(url)
        cost = QUOTA_COST.get(endpoint, 1)
        self.used += cost
        _add_quota(cost)
        CACHE_DIR.mkdir(exist_ok=True)
        cf.write_text(json.dumps(data))
        return data


# ------------------------------------------------------------------ utilidades
def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def _lang_of(code: str | None) -> str:
    return (code or "").split("-")[0].lower()


def _duration_s(iso_dur: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_dur or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _is_short(iso_dur: str) -> bool:
    return 0 < _duration_s(iso_dur) <= 180


def score(avg_views: float, views_per_day: float, views_per_sub: float) -> float:
    """Nota 0–100 só de performance: média de views por vídeo (50%), views/dia (40%), views/inscrito (10%)."""
    avg = min(math.log10(avg_views + 1) / 5, 1)          # 100 mil de média => máximo
    speed = min(math.log10(views_per_day + 1) / 6, 1)    # 1 milhão/dia => máximo
    eff = min(math.log10(views_per_sub + 1) / 3, 1)      # 1.000 views/inscrito => máximo
    return round(100 * (0.50 * avg + 0.40 * speed + 0.10 * eff), 1)


# ------------------------------------------------------------------ tradução (sem cota da API do YouTube)
class TranslateError(Exception):
    pass


def _http_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (niche-finder)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _translate_http(text: str, target: str) -> tuple[str, str]:
    """Traduz `text` para `target`. Retorna (tradução, idioma detectado da origem).

    1º Google Tradutor (endpoint público, sem chave); se falhar, MyMemory (assume origem em inglês).
    Só o texto da busca é enviado — nunca a chave da API.
    """
    try:
        r = _http_json("https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}))
        return "".join(seg[0] for seg in r[0] if seg and seg[0]), str(r[2] or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        r = _http_json("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": f"en|{target}"}))
        out = r["responseData"]["translatedText"]
        if out and "MYMEMORY WARNING" not in out:
            return out, "en"
    except Exception:  # noqa: BLE001
        pass
    raise TranslateError(text)


_translator = _translate_http  # substituível nos testes


def translate(text: str, target: str) -> tuple[str, str]:
    """Tradução com cache em disco (30 dias)."""
    ck = hashlib.sha1(json.dumps([text, target]).encode()).hexdigest()
    cf = CACHE_DIR / f"translate_{ck}.json"
    if cf.exists() and time.time() - cf.stat().st_mtime < 30 * 86400:
        return tuple(json.loads(cf.read_text()))
    out = _translator(text, target)
    CACHE_DIR.mkdir(exist_ok=True)
    cf.write_text(json.dumps(list(out), ensure_ascii=False))
    return out


def _protected_rx(cfg: dict) -> re.Pattern | None:
    terms = sorted({_norm(t) for t in cfg.get("protected_terms", []) if t.strip()}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w])", re.IGNORECASE)


def translate_query(cfg: dict, text: str, target: str) -> str:
    """Traduz uma busca preservando nomes de gêneros musicais (dnb, jungle, breakcore, lofi, mix…).

    'atmospheric jungle dnb mix' -> PT: 'atmosférico jungle dnb mix' (e não 'mix de dnb da selva').
    Se a tradução falhar, devolve o texto original.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    rx = _protected_rx(cfg)
    pieces, last = [], 0
    for m in (rx.finditer(_norm(text)) if rx else []):
        pieces.append(("t", text[last:m.start()]))
        pieces.append(("k", text[m.start():m.end()]))
        last = m.end()
    pieces.append(("t", text[last:]))
    out = []
    for kind, s in pieces:
        if kind == "t" and re.search(r"[^\W\d_]", s):
            try:
                tr, src = translate(s.strip(), target)
                out.append(s.strip() if _lang_of(src) == target else tr)
            except TranslateError:
                out.append(s.strip())
        else:
            out.append(s.strip())
    return re.sub(r"\s+", " ", " ".join(p for p in out if p)).strip()


# ------------------------------------------------------------------ títulos -> buscas
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u2700-\u27BF]")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]+")


def clean_title(t: str) -> str:
    t = _EMOJI.sub(" ", t or "")
    t = re.sub(r"#\S+", " ", t)
    t = _CJK.sub(" ", t)                                   # parte em japonês/chinês/coreano é decorativa
    t = re.sub(r"(?<!\w)(?:19|20)\d{2}(?!\w)", " ", t)     # anos (1998, 2003…)
    t = re.sub(r"\.(exe|mp3|wav)\b", " ", t, flags=re.I)
    t = re.sub(r"[\"“”'’!?:;~*_=+<>\[\]{}•✦]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def split_title(cfg: dict, title: str) -> tuple[str, str]:
    """Separa um título em (tema, estilo).

    'VIRTUAL MEMORY 2003 (atmospheric jungle dnb mix)' -> ('virtual memory', 'atmospheric jungle dnb mix')
    'CIGARETTE | Atmospheric liquid drum and bass mix' -> ('cigarette', 'atmospheric liquid drum and bass mix')
    """
    t = clean_title(title)
    rx = _protected_rx(cfg)
    style = ""
    m = re.search(r"\(([^()]{3,})\)", t)
    if m:
        style, t = m.group(1), t[:m.start()] + " " + t[m.end():]
    elif "|" in t:
        parts = [p.strip(" -/&") for p in t.split("|") if p.strip(" -/&")]
        if len(parts) > 1:
            # o estilo é o trecho com mais termos de gênero
            hits = [len(rx.findall(_norm(p))) if rx else 0 for p in parts]
            i = max(range(len(parts)), key=lambda k: hits[k])
            style = parts[i] if hits[i] else parts[-1]
            t = " ".join(p for k, p in enumerate(parts) if k != i)
        else:
            t = parts[0] if parts else ""
    t = re.sub(r"[()|/&]", " ", t)
    style = re.sub(r"[()|/&]", " ", style)
    t, style = re.sub(r"\s+", " ", t).strip().lower(), re.sub(r"\s+", " ", style).strip().lower()
    if not style and rx and len(rx.findall(_norm(t))) >= 2:   # título inteiro já é o estilo
        style, t = t, ""
    return t, style


def genre_terms(cfg: dict, text: str, n: int = 4) -> str:
    rx = _protected_rx(cfg)
    if not rx:
        return ""
    seen = []
    for m in rx.findall(_norm(text)):
        if m not in seen:
            seen.append(m)
    return " ".join(seen[:n])


def title_queries(cfg: dict, channel: dict) -> list[str]:
    """Buscas que descrevem o canal, extraídas dos títulos dos vídeos mais vistos."""
    styles, themes = Counter(), []
    for v in channel.get("top_videos", [])[:3]:
        th, st = split_title(cfg, v["title"])
        if st:
            styles[st] += 1
        if th and len(th) >= 3:
            themes.append(th)
    out = []
    if styles:
        style = styles.most_common(1)[0][0]
        out.append(style)
        if themes:                                  # tema do vídeo mais visto + termos de gênero
            out.append(f"{themes[0]} {genre_terms(cfg, style, 3)}".strip())
    else:
        out += themes[:2]
    if not out:
        out.append(clean_title(channel.get("title", "")).lower())
    seen, uniq = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[: cfg.get("similar_queries", 2)]


# ------------------------------------------------------------------ busca de candidatos
def search(yt: YouTube, cfg: dict, query: str, lang: str, days: int, mode: str = "video",
           duration: str | None = None) -> dict[str, set]:
    """Busca no YouTube. Retorna {channelId: {(busca, idioma), …}}.

    mode "video":   vídeos publicados no período, ordenados por views (acha quem está performando agora).
    mode "channel": canais criados no período.
    mode "both":    as duas (custa o dobro).
    duration:       "long" (>20 min), "medium" (4–20 min) ou "short" (<4 min) — só no modo vídeo.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    loc = cfg["languages"].get(lang, {})
    found: dict[str, set] = {}
    for m in (["video", "channel"] if mode == "both" else [mode]):
        params = dict(part="snippet", type=m, q=query, publishedAfter=since, order="viewCount",
                      maxResults=cfg.get("results_per_search", 50),
                      regionCode=loc.get("regionCode"), relevanceLanguage=loc.get("relevanceLanguage"))
        if m == "video" and duration in ("long", "medium", "short"):
            params["videoDuration"] = duration
        for it in yt.get("search", **params).get("items", []):
            cid = it["snippet"].get("channelId") or it["id"].get("channelId")
            if cid:
                found.setdefault(cid, set()).add((query, lang))
    return found


def _merge(into: dict[str, set], found: dict[str, set]) -> None:
    for cid, fb in found.items():
        into.setdefault(cid, set()).update(fb)


# ------------------------------------------------------------------ classificação de gênero
def _kw_regex(words: list[str]) -> re.Pattern | None:
    """Palavras curtas (≤4 letras) precisam ser palavra inteira; as maiores casam por prefixo."""
    parts = []
    for w in words:
        w = _norm(w).strip()
        if w:
            parts.append(re.escape(w) + (r"(?![a-z0-9])" if len(w) <= 4 else ""))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")") if parts else None


def classify(cfg: dict, vids: list[dict], channel_text: str = "", topics: list[str] | None = None) -> list[str]:
    """Decide os gêneros pelo conteúdo real do canal.

    Um gênero é aceito quando:
      • a maioria dos vídeos está nas categorias do YouTube do gênero E os títulos têm as palavras do gênero; ou
      • (gêneros com `category_is_enough`, ex.: Música) a maioria dos vídeos está na categoria do gênero; ou
      • (gêneros com `topics`, ex.: Música) o YouTube marcou o canal com o tópico do gênero E os títulos têm as
        palavras do gênero — pega canais de música publicados como "Pessoas e blogs".
    """
    n = len(vids)
    if not n:
        return []
    cats = Counter(v["cat"] for v in vids)
    topics_n = [_norm(t) for t in (topics or [])]
    out = []
    for g, gcfg in cfg["categories"].items():
        allowed = set(gcfg.get("youtube_category_ids", []))
        cat_ok = sum(c for k, c in cats.items() if k in allowed) / n >= cfg["min_genre_match"]
        rx = _kw_regex(gcfg.get("keywords", []))
        hits = sum(1 for v in vids if rx.search(_norm(v["title"]))) if rx else 0
        kw_ok = rx is None or hits / n >= cfg.get("min_keyword_match", 0.3) or (hits and rx.search(_norm(channel_text)))
        topic_ok = any(t in tp for t in gcfg.get("topics", []) for tp in topics_n)
        if (cat_ok and (kw_ok or gcfg.get("category_is_enough"))) or (topic_ok and rx and kw_ok):
            out.append(g)
    return out


# ------------------------------------------------------------------ avaliação dos canais
FUNNEL_ORDER = ["encontrados", "indisponíveis", "poucos inscritos", "muitos inscritos",
                "sem vídeos", "muitos vídeos", "média de views baixa", "poucas views por inscrito",
                "canal criado antes do limite", "vídeos anteriores ao período", "fora dos gêneros", "aprovados"]


def evaluate(yt: YouTube, cfg: dict, cands: dict[str, set], crit: dict,
             allowed_genres: list[str] | None = None, progress=None) -> tuple[list[dict], dict]:
    """Aplica os filtros e devolve (aprovados, funil com quantos canais caíram em cada filtro).

    Ordem (do mais barato ao mais caro em cota):
      1. inscritos, nº de vídeos, média de views por vídeo e views por inscrito   (1 unidade a cada 50 canais)
      2. 1º vídeo do canal publicado há no máximo `days` dias                      (1 unidade a cada 50 vídeos)
      3. gênero pelo conteúdo dos vídeos                                            (1 unidade)
    """
    now = datetime.now(timezone.utc)
    days = crit["days"]
    funnel = Counter(encontrados=len(cands))
    channels = {}
    for batch in chunks(list(cands), 50):
        data = yt.get("channels", part="snippet,statistics,contentDetails,topicDetails", id=",".join(batch), maxResults=50)
        for it in data.get("items", []):
            channels[it["id"]] = it
    funnel["indisponíveis"] = len(cands) - len(channels)
    allowed = set(allowed_genres or cfg["categories"])

    out = []
    for n, (cid, ch) in enumerate(channels.items(), 1):
        if progress:
            progress(f"Avaliando canais {n}/{len(channels)}")
        st, sn = ch.get("statistics", {}), ch["snippet"]
        hidden = bool(st.get("hiddenSubscriberCount"))
        subs, views, nvid = int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0))
        # filtros de inscritos são opcionais (0 = desligado); canais com inscritos ocultos passam direto
        if not hidden and crit.get("min_subscribers") and subs < crit["min_subscribers"]:
            funnel["poucos inscritos"] += 1
            continue
        if not hidden and crit.get("max_subscribers") and subs > crit["max_subscribers"]:
            funnel["muitos inscritos"] += 1
            continue
        if nvid == 0:
            funnel["sem vídeos"] += 1
            continue
        if nvid > crit["max_videos"]:
            funnel["muitos vídeos"] += 1
            continue
        avg = views / nvid
        if avg < crit["min_avg_views"]:
            funnel["média de views baixa"] += 1
            continue
        if not hidden and views / max(subs, 1) < crit["min_views_per_sub"]:
            funnel["poucas views por inscrito"] += 1
            continue
        created = parse_dt(sn["publishedAt"])
        if crit.get("max_created_days") and (now - created).days > crit["max_created_days"]:
            funnel["canal criado antes do limite"] += 1
            continue
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        vid_ids, oldest, token, complete = [], None, None, False
        for _ in range(min(4, nvid // 50 + 1)):
            pl = yt.get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token)
            for it in pl.get("items", []):
                cd = it["contentDetails"]
                vid_ids.append(cd["videoId"])
                if cd.get("videoPublishedAt"):
                    d = parse_dt(cd["videoPublishedAt"])
                    oldest = d if oldest is None or d < oldest else oldest
            token = pl.get("nextPageToken")
            if not token:
                complete = True
                break
        if not complete:
            funnel["muitos vídeos"] += 1
            continue
        if oldest is None or (now - oldest).days > days:
            funnel["vídeos anteriores ao período"] += 1     # inclui canais antigos que voltaram a postar
            continue
        vids = []
        for v in yt.get("videos", part="snippet,statistics,contentDetails", id=",".join(vid_ids[:50])).get("items", []):
            vs, vsn = v.get("statistics", {}), v["snippet"]
            th = vsn.get("thumbnails", {})
            vids.append({
                "id": v["id"], "title": vsn["title"], "views": int(vs.get("viewCount", 0)),
                "published": vsn["publishedAt"], "thumb": (th.get("medium") or th.get("default") or {}).get("url", ""),
                "cat": vsn.get("categoryId", ""), "duration": v.get("contentDetails", {}).get("duration", ""),
                "lang": _lang_of(vsn.get("defaultAudioLanguage") or vsn.get("defaultLanguage")),
            })
        topics = [t.rsplit("/", 1)[-1] for t in ch.get("topicDetails", {}).get("topicCategories", [])]
        genres = [g for g in classify(cfg, vids, sn["title"] + " " + sn.get("description", ""), topics) if g in allowed]
        if not genres:
            funnel["fora dos gêneros"] += 1
            continue
        funnel["aprovados"] += 1
        vlangs = Counter(v["lang"] for v in vids if v["lang"])
        found_langs = Counter(l for _, l in cands[cid])
        lang = vlangs.most_common(1)[0][0] if vlangs else (found_langs.most_common(1)[0][0] if found_langs else "")
        age = max((now - oldest).total_seconds() / 86400, 1)
        vpd, vps = views / age, (0 if hidden else views / max(subs, 1))
        durs = [_duration_s(v["duration"]) for v in vids]
        shorts = sum(1 for d in durs if 0 < d <= 180) / len(vids)
        top = sorted(vids, key=lambda v: v["views"], reverse=True)
        th = sn.get("thumbnails", {})
        out.append({
            "id": cid, "title": sn["title"], "handle": sn.get("customUrl", ""),
            "url": f"https://www.youtube.com/channel/{cid}",
            "avatar": (th.get("medium") or th.get("default") or {}).get("url", ""),
            "description": sn.get("description", "")[:300], "country": sn.get("country", ""),
            "lang": lang, "genres": genres, "topics": topics,
            "found_by": [{"q": q, "lang": l} for q, l in sorted(cands[cid])],
            "subscribers": subs, "hidden_subs": hidden, "views": views, "videos": nvid,
            "avg_views": round(avg), "median_views": round(statistics.median(v["views"] for v in vids)),
            "created": created.date().isoformat(), "first_video": oldest.date().isoformat(),
            "age_days": round(age, 1), "views_per_day": round(vpd), "views_per_sub": round(vps, 1),
            "subs_per_day": round(subs / age, 1),
            "shorts_share": round(shorts, 2), "avg_minutes": round(statistics.mean(durs) / 60, 1) if durs else 0,
            "score": score(avg, vpd, vps),
            "top_videos": [{k: v[k] for k in ("id", "title", "views", "thumb", "published")} for v in top[:3]],
            "video_titles": [v["title"] for v in vids],
            "timeline": [v["views"] for v in sorted(vids, key=lambda v: v["published"])][-40:],
            "checked": now.strftime("%Y-%m-%d %H:%M"),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out, {k: funnel.get(k, 0) for k in FUNNEL_ORDER if funnel.get(k) or k in ("encontrados", "aprovados")}


# ------------------------------------------------------------------ banco local
_db_lock = threading.Lock()


def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return {"channels": {}, "last_scan": None}


def save_results(results: list[dict]) -> dict:
    with _db_lock:
        db = load_db()
        for r in results:
            old = db["channels"].get(r["id"], {})
            r["genres"] = sorted(set(r["genres"]) | set(old.get("genres", [])))
            fb = {(x["q"], x["lang"]) for x in r.get("found_by", []) + old.get("found_by", [])}
            r["found_by"] = [{"q": q, "lang": l} for q, l in sorted(fb)]
            db["channels"][r["id"]] = r
        db["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        DATA_DIR.mkdir(exist_ok=True)
        DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
        return db


def prune_db(days: int) -> None:
    """Remove canais cujo 1º vídeo já passou da idade máxima."""
    with _db_lock:
        db = load_db()
        today = datetime.now(timezone.utc).date()
        db["channels"] = {k: v for k, v in db["channels"].items()
                          if (today - datetime.fromisoformat(v.get("first_video") or v["created"]).date()).days <= days}
        if DB_FILE.exists():
            DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ operações de alto nível
def _concept_query(cfg: dict, concept, lang: str) -> str:
    """Conceito pode ser um texto (mesma busca em todos os idiomas) ou {idioma: busca}.
    Idioma sem tradução no config é traduzido automaticamente a partir do inglês."""
    if isinstance(concept, str):
        return concept
    if concept.get(lang):
        return concept[lang]
    base = concept.get("en") or next(iter(concept.values()))
    return translate_query(cfg, base, lang)


def scan_jobs(cfg: dict, langs: list[str], genres: list[str]) -> list[tuple[str, str, str]]:
    jobs = []
    for g in genres:
        for concept in cfg["categories"][g]["concepts"].values():
            for l in langs:
                jobs.append((g, l, concept))
    return jobs


def scan_cost(cfg: dict, langs: list[str], genres: list[str], mode: str) -> int:
    return len(scan_jobs(cfg, langs, genres)) * (2 if mode == "both" else 1) * 100


def full_scan(yt: YouTube, cfg: dict, langs: list[str], genres: list[str], crit: dict,
              mode: str = "video", progress=None) -> tuple[list[dict], dict]:
    """Varredura: busca os conceitos de cada gênero em cada idioma."""
    cands: dict[str, set] = {}
    jobs = scan_jobs(cfg, langs, genres)
    seen = set()
    for i, (g, lang, concept) in enumerate(jobs, 1):
        q = _concept_query(cfg, concept, lang)
        if not q or (q, lang) in seen:
            continue
        seen.add((q, lang))
        if progress:
            progress(f"Buscando {i}/{len(jobs)}: “{q}” ({lang})")
        _merge(cands, search(yt, cfg, q, lang, crit["days"], mode, cfg["categories"][g].get("video_duration")))
    if progress:
        progress(f"{len(cands)} canais encontrados. Avaliando…")
    return evaluate(yt, cfg, cands, crit, genres, progress)


def keyword_queries(cfg: dict, keywords: list[str], langs: list[str], translate_kw: bool) -> list[tuple[str, str]]:
    """Lista (busca, idioma). Com tradução, cada palavra-chave vira uma busca por idioma, no idioma dele."""
    out, seen = [], set()
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        for l in langs:
            q = translate_query(cfg, kw, l) if translate_kw else kw
            if (q.lower(), l) not in seen:
                seen.add((q.lower(), l))
                out.append((q, l))
    return out


def keyword_search(yt: YouTube, cfg: dict, queries: list[tuple[str, str]], crit: dict,
                   genres: list[str] | None = None, duration: str | None = None, mode: str = "video",
                   progress=None) -> tuple[list[dict], dict]:
    """Busca por palavras-chave (ou nome de um vídeo) já traduzidas para cada idioma."""
    cands: dict[str, set] = {}
    for i, (q, lang) in enumerate(queries, 1):
        if progress:
            progress(f"Buscando {i}/{len(queries)}: “{q}” ({lang})")
        _merge(cands, search(yt, cfg, q, lang, crit["days"], mode, duration))
    if progress:
        progress(f"{len(cands)} canais encontrados. Avaliando…")
    return evaluate(yt, cfg, cands, crit, genres, progress)


def similar_plan(cfg: dict, channel: dict, langs: list[str]) -> dict:
    """Monta as buscas de 'canais parecidos': títulos dos vídeos mais vistos traduzidos para cada idioma."""
    base = title_queries(cfg, channel)
    queries = keyword_queries(cfg, base, langs, translate_kw=True)
    if channel.get("shorts_share", 0) >= 0.8:
        duration = "short"
    elif "musica" in channel.get("genres", []) and channel.get("avg_minutes", 0) >= 20:
        duration = "long"
    else:
        duration = None
    return {"base": base, "queries": queries, "duration": duration, "cost": len(queries) * 100}


def find_similar(yt: YouTube, cfg: dict, channel: dict, langs: list[str], crit: dict,
                 progress=None) -> tuple[list[dict], dict, dict]:
    plan = similar_plan(cfg, channel, langs)
    res, funnel = keyword_search(yt, cfg, plan["queries"], crit, channel.get("genres") or None,
                                 plan["duration"], "video", progress)
    res = [r for r in res if r["id"] != channel["id"]]
    return res, funnel, plan
