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


def criteria(cfg: dict, overrides: dict | None = None, preset: str = "criteria") -> dict:
    """Critérios de aprovação: padrão do config.json (bloco `preset`), sobrescrito pelo que veio da interface."""
    c = {"min_viral": 0, **cfg["criteria"], **cfg.get(preset, {})}
    for k, v in (overrides or {}).items():
        if k in c and v not in (None, ""):
            c[k] = int(float(v))
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


# ------------------------------------------------------------------ pontuação viral
# Fatores (0–1 cada) e peso padrão. Os pesos podem ser trocados em config.json -> "viral_weights".
VIRAL_FACTORS = {
    "views":         (0.10, "Views totais"),
    "avg_views":     (0.20, "Views por vídeo"),
    "views_per_sub": (0.15, "Views por inscrito"),
    "few_videos":    (0.10, "Poucos vídeos"),
    "young":         (0.10, "Canal novo"),
    "recency":       (0.20, "Recência das views"),
    "outlier":       (0.15, "Fora da curva"),
}
VIRAL_LABELS = [(75, "explosivo"), (60, "viralizando"), (45, "promissor"), (0, "observacao")]


def _lg(x: float, top: float) -> float:
    """Escala logarítmica 0–1: 0 -> 0, `top` (ou mais) -> 1."""
    return max(0.0, min(math.log10(max(x, 0) + 1) / math.log10(top + 1), 1.0))


def _days_since(iso: str, now: datetime) -> float:
    return max((now - parse_dt(iso if "T" in iso else iso + "T00:00:00Z")).total_seconds() / 86400, 0.5)


def growth_per_day(history: list[dict]) -> float | None:
    """Views/dia medidas entre duas checagens (a mais recente e a última com ≥ 6 h de diferença)."""
    if len(history or []) < 2:
        return None
    last = history[-1]
    t1 = parse_dt(last["t"])
    for h in reversed(history[:-1]):
        dt = (t1 - parse_dt(h["t"])).total_seconds() / 86400
        if dt >= 0.25:
            return max(last["views"] - h["views"], 0) / dt
    return None


def viral_metrics(subs: int, hidden: bool, views: int, nvid: int, age_days: float,
                  uploads: list, now: datetime | None = None, growth: float | None = None) -> dict:
    """Métricas de viralização a partir das estatísticas do canal e da lista de vídeos [(publicado, views), …].

    • velocidade de cada vídeo = views ÷ dias desde a publicação;
    • views/dia agora = crescimento medido entre checagens (se houver) ou soma das velocidades dos vídeos
      dos últimos 7 dias (ou dos 3 mais recentes);
    • momento = velocidade mediana dos 3 vídeos mais novos ÷ velocidade mediana de todos (>1 = acelerando);
    • fora da curva = melhor vídeo ÷ inscritos e views/dia ÷ inscritos.
    """
    now = now or datetime.now(timezone.utc)
    ups = sorted(((p, int(v)) for p, v in uploads if p), key=lambda u: u[0])
    vel = [(p, v / _days_since(p, now)) for p, v in ups]
    recent = [x for x in vel if _days_since(x[0], now) <= 7] or vel[-3:]
    recent_vpd = sum(v for _, v in recent)
    if len(vel) >= 4:
        allm = statistics.median(v for _, v in vel)
        momentum = statistics.median(v for _, v in vel[-3:]) / allm if allm else 1.0
    else:
        momentum = 1.0
    views_7d = sum(v for p, v in ups if _days_since(p, now) <= 7)
    top = max((v for _, v in ups), default=0)
    base = max(subs, 100)
    current = growth if growth is not None else recent_vpd
    return {
        "subs": subs, "hidden": hidden, "views": views, "videos": nvid, "age_days": age_days,
        "avg_views": views / max(nvid, 1), "views_per_sub": 0 if hidden else views / max(subs, 1),
        "current_vpd": current, "measured": growth is not None, "momentum": momentum,
        "views_7d": views_7d, "top_views": top,
        "outlier_x": None if hidden else top / base, "daily_per_sub": None if hidden else current / base,
    }


def viral_score(m: dict, weights: dict | None = None) -> tuple[float, dict]:
    """Pontuação viral 0–100 e a contribuição de cada fator (0–1)."""
    parts = {
        "views": _lg(m["views"], 10_000_000),                        # 10 mi de views -> máximo
        "avg_views": _lg(m["avg_views"], 1_000_000),                 # 1 mi de média por vídeo
        "views_per_sub": 0.5 if m["hidden"] else _lg(m["views_per_sub"], 1000),
        "few_videos": max(0.0, min(1 - math.log10(max(m["videos"], 1)) / 2, 1.0)),   # 1 -> 1 · 10 -> .5 · 100 -> 0
        "young": math.exp(-max(m["age_days"], 0) / 45),              # 7 d -> .86 · 30 d -> .51 · 90 d -> .14
        "recency": 0.75 * _lg(m["current_vpd"], 200_000) + 0.25 * max(0.0, min(m["momentum"] / 2, 1.0)),
        "outlier": 0.5 if m["hidden"] else 0.5 * _lg(m["daily_per_sub"], 100) + 0.5 * _lg(m["outlier_x"], 1000),
    }
    w = {k: d[0] for k, d in VIRAL_FACTORS.items()}
    w.update({k: float(v) for k, v in (weights or {}).items() if k in w})
    total = sum(w.values()) or 1
    return round(100 * sum(w[k] * parts[k] for k in parts) / total, 1), {k: round(v, 3) for k, v in parts.items()}


def viral_label(s: float) -> str:
    return next(lbl for lim, lbl in VIRAL_LABELS if s >= lim)


def apply_viral(r: dict, cfg: dict, now: datetime | None = None) -> dict:
    """(Re)calcula os campos virais de um canal salvo — também serve para canais salvos por versões antigas."""
    now = now or datetime.now(timezone.utc)
    if r.get("first_video"):                        # idade sempre recalculada para hoje
        r["age_days"] = round(_days_since(r["first_video"], now), 1)
    uploads = r.get("uploads") or [(v["published"], v["views"]) for v in r.get("top_videos", [])]
    m = viral_metrics(r["subscribers"], r.get("hidden_subs", False), r["views"], r["videos"],
                      r.get("age_days", 30), uploads, now, growth_per_day(r.get("history", [])))
    s, parts = viral_score(m, cfg.get("viral_weights"))
    r.update(viral=s, viral_label=viral_label(s), viral_parts=parts,
             current_vpd=round(m["current_vpd"]), vpd_measured=m["measured"], momentum=round(m["momentum"], 2),
             views_7d=m["views_7d"], outlier_x=None if m["outlier_x"] is None else round(m["outlier_x"], 1))
    return r


# ------------------------------------------------------------------ tradução (sem cota da API do YouTube)
class TranslateError(Exception):
    pass


def _http_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (niche-finder)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


_MT_ERRORS = re.compile(r"PLEASE SELECT|MYMEMORY WARNING|INVALID|QUERY LENGTH|NO QUERY|LIMIT", re.I)


def _translate_http(text: str, target: str, source: str | None = None) -> tuple[str, str]:
    """Traduz `text` para `target`. Retorna (tradução, idioma detectado da origem).

    1º Google Tradutor (endpoint público, sem chave); se falhar (ex.: 429), MyMemory com detecção
    automática do idioma de origem. Mensagens de erro do MyMemory nunca viram "tradução".
    Só o texto da busca é enviado — nunca a chave da API.
    """
    try:
        r = _http_json("https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text}))
        return "".join(seg[0] for seg in r[0] if seg and seg[0]), str(r[2] or "")
    except Exception:  # noqa: BLE001
        pass
    try:        # mesmo Google por outra porta (limite de uso separado do endpoint acima)
        r = _http_json("https://clients5.google.com/translate_a/t?" + urllib.parse.urlencode(
            {"client": "dict-chrome-ex", "sl": source or "auto", "tl": target, "q": text}))
        first = r[0] if isinstance(r, list) and r else None
        if isinstance(first, list) and first and first[0]:
            return str(first[0]), str(first[1] if len(first) > 1 else source or "")
        if isinstance(first, str) and first:
            return first, source or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        r = _http_json("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
            {"q": text, "langpair": f"{source or 'Autodetect'}|{target}"}))
        out = (r.get("responseData") or {}).get("translatedText") or ""
        if (out and str(r.get("responseStatus", 200)) == "200" and not _MT_ERRORS.search(out)
                and float((r.get("responseData") or {}).get("match") or 0) >= 0.5):
            return out, source or str((r.get("responseData") or {}).get("detectedLanguage") or "")
    except Exception:  # noqa: BLE001
        pass
    raise TranslateError(text)


_translator = _translate_http  # substituível nos testes


def translate(text: str, target: str, source: str | None = None) -> tuple[str, str]:
    """Tradução com cache em disco (30 dias). `source` (opcional) fixa o idioma de origem: a detecção
    automática confunde português com espanhol."""
    ck = hashlib.sha1(json.dumps([text, target] + ([source] if source else [])).encode()).hexdigest()
    cf = CACHE_DIR / f"translate_{ck}.json"
    if cf.exists() and time.time() - cf.stat().st_mtime < 30 * 86400:
        cached = tuple(json.loads(cf.read_text()))
        if not _MT_ERRORS.search(cached[0]):          # ignora entradas envenenadas por erros antigos do MyMemory
            return cached
    out = _translator(text, target, source) if source else _translator(text, target)
    CACHE_DIR.mkdir(exist_ok=True)
    cf.write_text(json.dumps(list(out), ensure_ascii=False))
    return out


def _protected_rx(cfg: dict, keys: tuple[str, ...] = ("protected_terms",)) -> re.Pattern | None:
    terms = sorted({_norm(t) for k in keys for t in cfg.get(k, []) if t.strip()}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w])", re.IGNORECASE)


def translate_query(cfg: dict, text: str, target: str, source: str | None = None) -> str:
    """Traduz uma busca preservando nomes de gêneros musicais (dnb, jungle, breakcore, lofi, mix…).

    'atmospheric jungle dnb mix' -> PT: 'atmosférico jungle dnb mix' (e não 'mix de dnb da selva').
    Se a tradução falhar, devolve o texto original.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text or (source and source == target):
        return text
    rx = _protected_rx(cfg, ("protected_terms", "untranslated_names"))     # gêneros + nomes próprios
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
                tr, src = translate(s.strip(), target, source)
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
    if styles and not genre_terms(cfg, styles.most_common(1)[0][0]):
        styles = Counter()          # o trecho entre parênteses não é um estilo musical: vale o título inteiro
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
    # arredonda para o início do dia (UTC): buscas repetidas no mesmo dia reaproveitam o cache em vez de gastar 100
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
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
def _video_details(yt: YouTube, vid_ids: list[str]) -> list[dict]:
    """Detalhes de até 50 vídeos (1 unidade): título, views, data, categoria, duração, idioma do áudio."""
    if not vid_ids:
        return []
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
    return vids


NEAR_FACTOR = 3.0        # "quase aprovado" = passou raspando: até 3× o limite de um único filtro
FUNNEL_ORDER = ["encontrados", "indisponíveis", "poucos inscritos", "muitos inscritos",
                "sem vídeos", "muitos vídeos", "média de views baixa", "poucas views por inscrito",
                "canal criado antes do limite", "vídeos anteriores ao período", "títulos sem as palavras-chave",
                "fora dos gêneros",
                "pontuação viral baixa", "aprovados"]


def _term_regexes(terms: list[str]) -> list[tuple[str, list[re.Pattern]]]:
    """Cada palavra-chave vira uma lista de regex, uma por palavra (todas precisam aparecer no título, em qualquer
    ordem). Palavras curtas (≤4 letras) precisam ser palavra inteira (aceita plural: mix → mixes); as maiores casam por prefixo."""
    out, seen = [], set()
    for term in terms:
        words = [w for w in re.split(r"[^a-z0-9&]+", _norm(term)) if w]
        if not words or tuple(words) in seen:
            continue
        seen.add(tuple(words))
        out.append((term.strip(), [re.compile(r"(?<![a-z0-9])" + re.escape(w) + (r"(?:e?s)?(?![a-z0-9])" if len(w) <= 4 else ""))
                                   for w in words]))
    return out


def title_match(titles: list[str], terms: list[str]) -> dict:
    """Quantos títulos contêm pelo menos uma das palavras-chave (todas as palavras dela, em qualquer ordem)."""
    rx = _term_regexes(terms)
    hits, counts, example = 0, Counter(), ""
    for title in titles:
        n = _norm(title)
        found = [term for term, pats in rx if all(p.search(n) for p in pats)]
        if found:
            hits += 1
            counts.update(found)
            example = example or title
    return {"hits": hits, "of": len(titles), "term": counts.most_common(1)[0][0] if counts else "", "example": example}


def evaluate(yt: YouTube, cfg: dict, cands: dict[str, set], crit: dict,
             allowed_genres: list[str] | None = None, progress=None, any_genre: bool = False,
             title_terms: list[str] | None = None, near: list | None = None) -> tuple[list[dict], dict]:
    """Aplica os filtros e devolve (aprovados, funil com quantos canais caíram em cada filtro).

    near: se for uma lista, recebe os canais que ficaram a um passo de passar (ver `_near_miss`) — sem custo extra.

    Ordem (do mais barato ao mais caro em cota):
      1. inscritos, nº de vídeos, média de views por vídeo e views por inscrito   (1 unidade a cada 50 canais)
      2. 1º vídeo do canal publicado há no máximo `days` dias                      (1 unidade a cada 50 vídeos)
      3. gênero pelo conteúdo dos vídeos                                            (1 unidade)
      4. pontuação viral ≥ crit["min_viral"] (se definido)
    any_genre=True aceita canais fora dos gêneros do config (marcados como "outros").
    title_terms: se informado, o canal só passa se pelo menos `crit["min_title_pct"]`% dos títulos dos vídeos
    (mínimo 1 título) contiverem uma das palavras-chave.
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

    def miss(cid, ch, reason, ratio, age=None):
        """Registra um quase aprovado (ratio = quão longe do limite, 1 = no limite)."""
        if near is None or ratio > NEAR_FACTOR:
            return
        st, sn = ch.get("statistics", {}), ch["snippet"]
        nv = int(st.get("videoCount", 0))
        near.append({"id": cid, "title": sn["title"], "url": f"https://www.youtube.com/channel/{cid}",
                     "subscribers": int(st.get("subscriberCount", 0)), "videos": nv,
                     "avg_views": round(int(st.get("viewCount", 0)) / max(nv, 1)), "reason": reason,
                     "age_days": round(age) if age is not None else None, "hits": len(cands[cid]),
                     "found_by": [{"q": q, "lang": l} for q, l in sorted(cands[cid])]})

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
            miss(cid, ch, "muitos inscritos", subs / crit["max_subscribers"])
            continue
        if nvid == 0:
            funnel["sem vídeos"] += 1
            continue
        if nvid > crit["max_videos"]:
            funnel["muitos vídeos"] += 1
            miss(cid, ch, "muitos vídeos", nvid / max(crit["max_videos"], 1))
            continue
        avg = views / nvid
        if avg < crit["min_avg_views"]:
            funnel["média de views baixa"] += 1
            miss(cid, ch, "média de views baixa", crit["min_avg_views"] / max(avg, 1))
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
            if oldest is not None:
                miss(cid, ch, "1º vídeo fora do período", (now - oldest).days / max(days, 1), (now - oldest).days)
            continue
        vids = _video_details(yt, vid_ids)
        topics = [t.rsplit("/", 1)[-1] for t in ch.get("topicDetails", {}).get("topicCategories", [])]
        if not vids:
            funnel["sem vídeos"] += 1
            continue
        tm = None
        if title_terms:
            tm = title_match([v["title"] for v in vids], title_terms)
            need = max(1, math.ceil(len(vids) * crit.get("min_title_pct", 0) / 100))
            if tm["hits"] < need:
                funnel["títulos sem as palavras-chave"] += 1
                continue
        genres = [g for g in classify(cfg, vids, sn["title"] + " " + sn.get("description", ""), topics) if g in allowed]
        if not genres and any_genre:
            genres = ["outros"]
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
            "uploads": [[v["published"], v["views"]] for v in sorted(vids, key=lambda v: v["published"])],
            "latest_video": max(v["published"] for v in vids)[:10],
            "history": [{"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "views": views, "subs": subs, "videos": nvid}],
            "checked": now.strftime("%Y-%m-%d %H:%M"),
        })
        if tm:
            out[-1]["title_match"] = tm
        apply_viral(out[-1], cfg, now)
        if crit.get("min_viral") and out[-1]["viral"] < crit["min_viral"]:
            out.pop()
            funnel["aprovados"] -= 1
            funnel["pontuação viral baixa"] += 1
    out.sort(key=lambda r: r["viral"], reverse=True)
    return out, {k: funnel.get(k, 0) for k in FUNNEL_ORDER if funnel.get(k) or k in ("encontrados", "aprovados")}


# ------------------------------------------------------------------ banco local
_db_lock = threading.Lock()


def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return {"channels": {}, "last_scan": None}


def channels_for_ui(cfg: dict) -> list[dict]:
    """Canais salvos com a pontuação viral recalculada para agora (as views envelhecem a cada dia).

    Campos internos grandes (`uploads`) não vão para o navegador."""
    now = datetime.now(timezone.utc)
    out = []
    for r in load_db()["channels"].values():
        r = apply_viral(dict(r), cfg, now)
        r.pop("uploads", None)
        out.append(r)
    return sorted(out, key=lambda r: r["viral"], reverse=True)


def save_results(results: list[dict], cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    with _db_lock:
        db = load_db()
        for r in results:
            old = db["channels"].get(r["id"], {})
            r["genres"] = sorted(set(r["genres"]) | set(old.get("genres", [])))
            if len(r["genres"]) > 1 and "outros" in r["genres"]:
                r["genres"].remove("outros")
            fb = {(x["q"], x["lang"]) for x in r.get("found_by", []) + old.get("found_by", [])}
            r["found_by"] = [{"q": q, "lang": l} for q, l in sorted(fb)]
            # histórico de checagens: permite medir o crescimento real (views/dia) entre uma busca e outra
            hist = {h["t"]: h for h in old.get("history", []) + r.get("history", [])}
            r["history"] = [hist[t] for t in sorted(hist)][-30:]
            r["first_seen"] = old.get("first_seen") or r.get("checked")
            sim = list(dict.fromkeys(old.get("similar_to", []) + r.get("similar_to", [])))
            if sim:
                r["similar_to"] = sim[-5:]
            apply_viral(r, cfg)
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


def keyword_queries(cfg: dict, keywords: list[str], langs: list[str], translate_kw: bool,
                    source: str | None = None) -> list[tuple[str, str]]:
    """Lista (busca, idioma). Com tradução, cada palavra-chave vira uma busca por idioma, no idioma dele."""
    out, seen = [], set()
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        for l in langs:
            q = translate_query(cfg, kw, l, source) if translate_kw else kw
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


# ------------------------------------------------------------------ radar viral (canais novos explodindo)
def radar_plan(cfg: dict, langs: list[str], genres: list[str], keywords: list[str] | None = None,
               days: int = 30, week: bool = False) -> list[dict]:
    """Buscas do Radar: palavras-chave digitadas (traduzidas) ou os conceitos dos gêneros, em cada idioma.

    Cada busca procura VÍDEOS publicados no período, ordenados por views — é onde canais novos que estão
    viralizando aparecem. Com `week=True` repete a busca só com vídeos dos últimos 7 dias, o que traz outro
    lote de canais (os que estão subindo agora) e dobra o custo.
    """
    windows = [days] + ([7] if week and days > 7 else [])
    base: list[tuple[str, str, str | None, str | None]] = []
    if keywords:
        for q, l in keyword_queries(cfg, keywords, langs, True):
            base.append((q, l, None, None))
    else:
        for g, l, concept in scan_jobs(cfg, langs, genres):
            base.append((_concept_query(cfg, concept, l), l, g, cfg["categories"][g].get("video_duration")))
    out, seen = [], set()
    for q, l, g, dur in base:
        for w in windows:
            if q and (q.lower(), l, w, dur) not in seen:
                seen.add((q.lower(), l, w, dur))
                out.append({"q": q, "lang": l, "days": w, "duration": dur, "genre": g})
    return out


def radar(yt: YouTube, cfg: dict, plan: list[dict], crit: dict, genres: list[str] | None = None,
          duration: str | None = None, any_genre: bool = False, progress=None,
          title_terms: list[str] | None = None) -> tuple[list[dict], dict]:
    """Executa o plano do Radar e devolve os canais aprovados ordenados do mais viral para o menos viral.

    title_terms: palavras-chave que precisam aparecer nos títulos dos vídeos do canal (ver `evaluate`)."""
    cands: dict[str, set] = {}
    for i, p in enumerate(plan, 1):
        if progress:
            progress(f"Radar {i}/{len(plan)}: “{p['q']}” ({p['lang']}, últimos {p['days']} dias)")
        _merge(cands, search(yt, cfg, p["q"], p["lang"], p["days"], "video", duration or p.get("duration")))
    if progress:
        progress(f"{len(cands)} canais encontrados. Calculando pontuação viral…")
    return evaluate(yt, cfg, cands, crit, genres, progress, any_genre=any_genre, title_terms=title_terms)


def radar_title_terms(keywords: list[str], plan: list[dict]) -> list[str]:
    """Palavras-chave digitadas + as versões traduzidas usadas nas buscas (um canal em PT com 'jazz suave' também vale)."""
    return list(dict.fromkeys([k.strip() for k in keywords if k.strip()] + [p["q"] for p in plan]))


# ------------------------------------------------------------------ parecidos a partir de um link
_UC_ID = re.compile(r"^UC[\w-]{22}$")


def parse_channel_ref(text: str) -> tuple[str, str]:
    """Entende o que o usuário colou e devolve (tipo, valor).

    tipos: "id" (UC…), "handle" (@nome), "user" (/user/nome), "custom" (/c/nome ou youtube.com/nome),
           "video" (watch?v=, youtu.be/, /shorts/, /live/) — o canal é descoberto pelo vídeo.
    """
    t = (text or "").strip().strip("<>\"' ")
    if not t:
        raise ApiError("Cole o link de um canal ou de um vídeo do YouTube.")
    if _UC_ID.match(t):
        return "id", t
    if t.startswith("@") and len(t) > 1 and "/" not in t:
        return "handle", t
    if not re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I) and re.search(r"(youtube\.com|youtu\.be)", t, re.I):
        t = "https://" + t
    u = urllib.parse.urlparse(t)
    host = re.sub(r"^(www|m|music)\.", "", u.netloc.lower())
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    q = urllib.parse.parse_qs(u.query)
    if host == "youtu.be" and parts:
        return "video", parts[0]
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parts[:1] == ["watch"] and q.get("v"):
            return "video", q["v"][0]
        if len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v"):
            return "video", parts[1]
        if len(parts) > 1 and parts[0] == "channel" and _UC_ID.match(parts[1]):
            return "id", parts[1]
        if parts and parts[0].startswith("@"):
            return "handle", parts[0]
        if len(parts) > 1 and parts[0] == "user":
            return "user", parts[1]
        if len(parts) > 1 and parts[0] == "c":
            return "custom", parts[1]
        if len(parts) == 1 and parts[0] not in ("watch", "results", "feed", "playlist"):
            return "custom", parts[0]
    elif not u.netloc and re.fullmatch(r"[\w.\-]{3,40}", t):
        return "handle", "@" + t                       # digitou só o nome do @, sem a arroba
    raise ApiError("Não reconheci esse link. Use o link do canal (youtube.com/@nome ou /channel/UC…) "
                   "ou de um vídeo dele.")


def resolve_channel(yt: YouTube, text: str) -> str:
    """Converte o link no ID do canal (UC…). Custa 0–1 unidade (100 só no caso raro de /c/nome sem @handle)."""
    kind, val = parse_channel_ref(text)
    if kind == "id":
        return val
    if kind == "video":
        items = yt.get("videos", part="snippet", id=val).get("items", [])
        if items and items[0].get("snippet", {}).get("channelId"):
            return items[0]["snippet"]["channelId"]
        raise ApiError("Vídeo não encontrado (pode ser privado ou ter sido removido).")
    if kind == "user":
        items = yt.get("channels", part="id", forUsername=val).get("items", [])
        if items:
            return items[0]["id"]
        kind, val = "handle", "@" + val
    if kind in ("handle", "custom"):
        handle = val if val.startswith("@") else "@" + val
        items = yt.get("channels", part="id", forHandle=handle).get("items", [])
        if items:
            return items[0]["id"]
        if kind == "custom":                          # link antigo /c/nome que não é igual ao @handle
            items = yt.get("search", part="snippet", type="channel", q=val, maxResults=1).get("items", [])
            if items:
                return items[0]["snippet"].get("channelId") or items[0]["id"]["channelId"]
    raise ApiError("Canal não encontrado. Confira o link (ou cole o link de um vídeo do canal).")


def profile_channel(yt: YouTube, cfg: dict, cid: str) -> dict:
    """Retrato do canal de referência a partir dos 50 vídeos mais recentes (~3 unidades). Sem filtros:
    o canal de referência pode ser antigo e grande."""
    items = yt.get("channels", part="snippet,statistics,contentDetails,topicDetails", id=cid).get("items", [])
    if not items:
        raise ApiError("Canal não encontrado (pode ter sido removido).")
    ch = items[0]
    st, sn = ch.get("statistics", {}), ch["snippet"]
    uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    ids = []
    if uploads:
        try:
            pl = yt.get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50)
            ids = [it["contentDetails"]["videoId"] for it in pl.get("items", [])]
        except ApiError:
            ids = []
    vids = _video_details(yt, ids)
    if not vids:
        raise ApiError("Esse canal ainda não tem vídeos públicos para analisar.")
    topics = [t.rsplit("/", 1)[-1] for t in ch.get("topicDetails", {}).get("topicCategories", [])]
    genres = classify(cfg, vids, sn["title"] + " " + sn.get("description", ""), topics) or ["outros"]
    vlangs = Counter(v["lang"] for v in vids if v["lang"])
    lang = vlangs.most_common(1)[0][0] if vlangs else _lang_of(sn.get("defaultLanguage"))
    hidden = bool(st.get("hiddenSubscriberCount"))
    subs, views, nvid = int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0))
    durs = [_duration_s(v["duration"]) for v in vids]
    top = sorted(vids, key=lambda v: v["views"], reverse=True)
    th = sn.get("thumbnails", {})
    return {
        "id": ch["id"], "title": sn["title"], "handle": sn.get("customUrl", ""),
        "url": f"https://www.youtube.com/channel/{ch['id']}",
        "avatar": (th.get("medium") or th.get("default") or {}).get("url", ""),
        "country": sn.get("country", ""), "lang": lang, "genres": genres, "topics": topics,
        "subscribers": subs, "hidden_subs": hidden, "views": views, "videos": nvid,
        "avg_views": round(views / max(nvid, 1)),
        "recent_avg": round(statistics.mean(v["views"] for v in vids)), "analyzed": len(vids),
        "titles": [v["title"] for v in vids],
        "created": sn["publishedAt"][:10],
        "shorts_share": round(sum(1 for d in durs if 0 < d <= 180) / len(vids), 2),
        "avg_minutes": round(statistics.mean(durs) / 60, 1) if durs else 0,
        "top_videos": [{k: v[k] for k in ("id", "title", "views", "thumb", "published")} for v in top[:3]],
    }


_FILLER = {"to", "for", "the", "a", "an", "and", "of", "de", "da", "do", "para", "com", "e", "o", "a", "24", "7", "live",
           "radio", "official", "video", "ao", "vivo", "episode", "ep", "part", "parte"}


def _short_query(cfg: dict, q: str, max_words: int = 5) -> str:
    """Encurta buscas longas tiradas de títulos: buscas compridas demais quase não trazem resultado."""
    q = q.split(",")[0].strip()                      # 'ator a, ator b, ator c' -> só o 1º trecho
    words = q.split()
    if len(words) > max_words:
        terms = genre_terms(cfg, q, 4)
        if len(terms.split()) >= 2:
            words = terms.split()
        else:
            words = [w for w in words if w.lower() not in _FILLER][:max_words] or words[:max_words]
    return " ".join(dict.fromkeys(words))            # sem palavras repetidas ('lofi lofi hip hop')


_STOP = set("""
a o os as um uma uns umas de da do das dos em no na nos nas por para com sem sob sobre que se ao aos mais menos muito
mas como foi era ser tem teve seu sua seus suas ele ela eles elas isso esse essa este esta aquele quando onde porque
el la los las un una unos unas del al en con sin por para que se su sus lo le les mas como fue era ser tiene tuvo
este esta ese esa pero muy todo toda todos
the an and or of to in on at for with without by from as is was were be been has had have that this these those
it its his her their they he she we you your who whom what when where why how not but all one out up
video videos official full part parte episode episodio live mix
""".split())


def topic_terms(cfg: dict, ref: dict, n: int = 4) -> list[str]:
    """Palavras que definem o assunto do canal: as mais repetidas nos títulos dos vídeos (e siglas como FBI).
    Usadas para aprovar só canais cujos títulos falam do mesmo assunto. Vazio para música (o gênero já filtra)."""
    if "musica" in ref.get("genres", []):
        return []
    titles = ref.get("titles") or [v["title"] for v in ref.get("top_videos", [])]
    count: Counter = Counter()
    for t in titles:
        words = {_norm(w) for w in re.findall(r"[^\W\d_]+", clean_title(t)) if len(w) >= 4 or (len(w) == 3 and w.isupper())}
        count.update(w for w in words if w not in _STOP)       # siglas de 3 letras (FBI, CIA) também contam
    need = max(2, math.ceil(len(titles) * 0.2))
    out = [w for w, c in count.most_common() if c >= need]
    return out[:n]


def title_pattern(ref: dict) -> str:
    """Abertura que se repete nos títulos do canal (ex.: 'o hacker que' em 'O Hacker que…').
    Como frase de busca, acha vídeos com a mesma fórmula de título sem puxar canais gigantes do assunto."""
    titles = ref.get("titles") or []
    need = max(2, math.ceil(len(titles) * 0.3))
    for k in (3, 2):
        count = Counter(" ".join(w[:k]) for w in (clean_title(t).lower().split() for t in titles) if len(w) >= k)
        for pat, c in count.most_common():
            if c >= need and not all(x in _STOP for x in pat.split()):
                return pat
    return ""


def link_plan(cfg: dict, ref: dict, langs: list[str], n: int = 3) -> dict:
    """Buscas sugeridas para achar canais parecidos com `ref` (títulos dos vídeos mais vistos → tema + estilo),
    encurtadas para no máx. 8 palavras, e as palavras do assunto (`terms`) que os títulos dos candidatos devem ter."""
    plan = similar_plan({**cfg, "similar_queries": n}, ref, langs)
    base = list(dict.fromkeys(_short_query(cfg, q, 8) for q in plan["base"] if q.strip()))
    terms = topic_terms(cfg, ref)
    if terms:
        # buscas curtas de palavras-chave (ex.: "hacker nasa") trazem ~40 canais; frases longas de título quase
        # não trazem nada. Vão primeiro; a 1ª busca por título completa a lista.
        kw = [terms[0]] if len(terms) == 1 else [f"{terms[0]} {t}" for t in terms[1:3]]
        pat = title_pattern(ref)
        base = list(dict.fromkeys(([pat] if pat else []) + kw + base[:1]))[:n]
    return {**plan, "base": base, "terms": terms}


def _validate_near(yt: YouTube, cfg: dict, near: list[dict], terms: list[str], pct: float, limit: int = 6) -> list[dict]:
    """Confere se os quase aprovados falam do mesmo assunto (~3 unidades cada) e anexa os vídeos mais vistos."""
    out = []
    for n in near[:limit * 2]:
        if len(out) >= limit:
            break
        try:
            prof = profile_channel(yt, cfg, n["id"])
        except ApiError:
            continue
        if terms:
            tm = title_match(prof["titles"], terms)
            if tm["hits"] < max(1, math.ceil(len(prof["titles"]) * pct / 100)):
                continue
            n = {**n, "title_match": tm}
        out.append({**n, "lang": prof["lang"], "top_videos": prof["top_videos"]})
    return out


def _rank_near(near: list[dict], exclude: set[str], limit: int = 12) -> list[dict]:
    """Quase aprovados sem repetição, os achados por mais buscas primeiro (mais ligados ao nicho)."""
    best: dict[str, dict] = {}
    for n in near:
        if n["id"] in exclude:
            continue
        old = best.get(n["id"])
        if old is None or n["hits"] > old["hits"]:
            best[n["id"]] = n
    return sorted(best.values(), key=lambda n: (-n["hits"], -n["avg_views"]))[:limit]


def snowball_queries(cfg: dict, seeds: list[dict], used: set[tuple[str, str]], langs: list[str],
                     per_seed: int = 2, limit: int = 4) -> list[tuple[str, str]]:
    """"Seguir pistas": os títulos mais vistos de canais que já deram certo viram novas buscas, no idioma
    do próprio canal (sem traduzir) — acha canais que a busca original não alcançou."""
    out: list[tuple[str, str]] = []
    for sd in seeds:
        lang = sd.get("lang") if sd.get("lang") in cfg["languages"] else (langs[0] if langs else "en")
        if langs and lang not in langs:
            lang = langs[0]
        n = 0
        for v in sd.get("top_videos", []):
            q = _short_query(cfg, clean_title(v["title"]).lower(), 7)
            key = (q.lower(), lang)
            if len(q.split()) >= 2 and key not in used and key not in out:
                out.append(key)
                n += 1
            if n >= per_seed:
                break
        if len(out) >= limit:
            break
    return out[:limit]


def similar_from_link(yt: YouTube, cfg: dict, ref: dict, base_queries: list[str], langs: list[str], crit: dict,
                      same_genre: bool = True, duration: str | None = None,
                      progress=None, snowball: int = 0, terms: list[str] | None = None) -> tuple[list[dict], dict, dict]:
    """Busca canais parecidos com o canal de referência, aplicando os critérios do Radar (novos e virais).

    snowball: nº de buscas extras (100 unidades cada) feitas a partir dos títulos dos melhores canais achados
    (e dos quase aprovados) — "seguir pistas". Também devolve em extra["near"] os canais que quase passaram.
    terms: palavras do assunto; só passam canais cujos títulos (≥ crit["min_title_pct"]%) contêm alguma delas, já
    traduzidas para cada idioma buscado. Sem `terms`, vale só o gênero.
    """
    queries = keyword_queries(cfg, base_queries, langs, True, ref.get("lang") or None)
    plan = [{"q": q, "lang": l, "days": crit["days"], "duration": duration, "genre": None} for q, l in queries]
    real_genres = [g for g in ref.get("genres", []) if g in cfg["categories"]]
    genres = real_genres if same_genre and real_genres else None
    near: list[dict] = []
    cands: dict[str, set] = {}
    title_terms = None
    if terms:
        title_terms = list(dict.fromkeys(
            [t.strip() for t in terms if t.strip()]
            + [translate_query(cfg, t, l, ref.get("lang") or None) for t in terms if t.strip() for l in langs]))
    pct = crit.get("min_title_pct", 0)
    for i, p in enumerate(plan, 1):
        if progress:
            progress(f"Busca {i}/{len(plan)}: “{p['q']}” ({p['lang']}, últimos {p['days']} dias)")
        _merge(cands, search(yt, cfg, p["q"], p["lang"], p["days"], "video", duration))
    if progress:
        progress(f"{len(cands)} canais encontrados. Calculando pontuação viral…")
    res, funnel = evaluate(yt, cfg, cands, crit, genres, progress, any_genre=not genres, near=near,
                           title_terms=title_terms)
    if progress:
        progress("Conferindo os que quase passaram…")
    near_ok = _validate_near(yt, cfg, _rank_near(near, {ref["id"]} | {r["id"] for r in res}, 30), title_terms or [], pct)
    extra_q: list[tuple[str, str]] = []
    if snowball > 0:
        seeds = [r for r in res if r["id"] != ref["id"]][:3] + near_ok[:2]
        extra_q = snowball_queries(cfg, seeds, set(queries), langs, limit=snowball)
        if extra_q:
            cands2: dict[str, set] = {}
            for i, (q, l) in enumerate(extra_q, 1):
                if progress:
                    progress(f"Seguindo pistas {i}/{len(extra_q)}: “{q}” ({l})")
                _merge(cands2, search(yt, cfg, q, l, crit["days"], "video", duration))
            cands2 = {c: v for c, v in cands2.items() if c not in cands}
            if progress:
                progress(f"{len(cands2)} canais novos nas pistas. Avaliando…")
            res2, f2, near2 = [], None, []
            if cands2:
                res2, f2 = evaluate(yt, cfg, cands2, crit, genres, progress, any_genre=not genres, near=near2,
                                    title_terms=title_terms)
                near_ok += _validate_near(yt, cfg, _rank_near(near2, {ref["id"]} | {n["id"] for n in near_ok}, 20),
                                          title_terms or [], pct, 4)
            have = {r["id"] for r in res}
            res += [r for r in res2 if r["id"] not in have]
            res.sort(key=lambda r: r["viral"], reverse=True)
            if f2:
                for k, v in f2.items():
                    funnel[k] = funnel.get(k, 0) + v if k != "aprovados" else len(res)
            funnel["aprovados"] = len(res)
    if any(r["id"] == ref["id"] for r in res):        # o próprio canal de referência não conta
        res = [r for r in res if r["id"] != ref["id"]]
        funnel["aprovados"] = len(res)
    for r in res:
        r["similar_to"] = [ref["title"]]
    funnel = {k: funnel[k] for k in FUNNEL_ORDER if funnel.get(k) or k in ("encontrados", "aprovados")}
    return res, funnel, {"queries": [{"q": q, "lang": l} for q, l in queries],
                         "snowball": [{"q": q, "lang": l} for q, l in extra_q],
                         "terms": title_terms or [],
                         "near": [n for n in near_ok if n["id"] not in {r["id"] for r in res}]}


def refresh_channels(yt: YouTube, cfg: dict, ids: list[str], progress=None) -> tuple[list[dict], dict]:
    """Rechecagem dos canais salvos (~3 unidades por canal, sem buscas de 100).

    Cada rechecagem grava um ponto no histórico, e a partir da 2ª o card mostra o crescimento REAL
    em views/dia (em vez da estimativa pela idade dos vídeos).
    """
    db = load_db()["channels"]
    cands = {cid: {(f["q"], f["lang"]) for f in db.get(cid, {}).get("found_by", [])} for cid in ids}
    loose = {"days": 100_000, "max_created_days": 0, "min_subscribers": 0, "max_subscribers": 0,
             "min_avg_views": 0, "min_views_per_sub": 0, "max_videos": 200, "min_viral": 0}
    res, funnel = evaluate(yt, cfg, cands, loose, None, progress, any_genre=True)
    for r in res:                                   # mantém os gêneros de quando o canal foi encontrado
        old = db.get(r["id"], {})
        if old.get("genres") and r["genres"] == ["outros"]:
            r["genres"] = old["genres"]
    return res, funnel
