"""Núcleo do Niche Finder: cliente da API do YouTube, filtros, pontuação e buscas."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
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


def load_api_key() -> str:
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.strip().startswith("YT_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def save_api_key(key: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(f"YT_API_KEY={key.strip()}\n")
    os.chmod(ENV_FILE, 0o600)


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


# ------------------------------------------------------------------ cliente
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
                raise ApiError(f"Erro da API ({e.code}): {body[:300]}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"Sem conexão com a API: {e.reason}")
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


def score(views_per_day: float, views_per_sub: float, subs: int) -> float:
    """Nota 0–100: velocidade de views (55%), views por inscrito (30%), bônus p/ canal menor (15%)."""
    speed = min(math.log10(views_per_day + 1) / 6, 1)
    eff = min(math.log10(views_per_sub + 1) / 3, 1)
    small = 1 - min(math.log10(max(subs, 1)) / 5, 1)
    return round(100 * (0.55 * speed + 0.30 * eff + 0.15 * small), 1)


def _lang_of(code: str | None) -> str:
    return (code or "").split("-")[0].lower()


# ------------------------------------------------------------------ busca de candidatos
def search_channels(yt: YouTube, cfg: dict, query: str, lang: str, days: int,
                    category: str | None = None, mode: str = "both") -> dict[str, dict]:
    """Retorna {channelId: {"queries": set, "langs": set}} para uma consulta.

    mode "channel": busca canais criados no período (filtro direto de canal novo).
    mode "video":   busca vídeos publicados no período, ordenados por views.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    loc = cfg["languages"].get(lang, {})
    found: dict[str, dict] = {}
    modes = ["channel", "video"] if mode == "both" else [mode]
    for m in modes:
        params = dict(part="snippet", type=m, q=query, publishedAfter=since, order="viewCount",
                      maxResults=cfg.get("results_per_search", 50),
                      regionCode=loc.get("regionCode"), relevanceLanguage=loc.get("relevanceLanguage"))
        if m == "video" and category:
            params["videoCategoryId"] = cfg["categories"][category].get("search_category_id")
        data = yt.get("search", **params)
        for it in data.get("items", []):
            cid = it["snippet"].get("channelId") or it["id"].get("channelId")
            if cid:
                e = found.setdefault(cid, {"queries": set(), "langs": set()})
                e["queries"].add(query)
                e["langs"].add(lang)
    return found


# ------------------------------------------------------------------ avaliação dos canais
def evaluate(yt: YouTube, cfg: dict, cands: dict[str, dict], days: int,
             category_hint: dict[str, set] | None = None, progress=None) -> list[dict]:
    """Aplica todos os filtros e retorna os canais aprovados, com detalhes para o dashboard.

    Filtros (nesta ordem, do mais barato ao mais caro):
      1. inscritos entre min e max; views totais e views/inscrito mínimos
      2. canal CRIADO há no máximo `days` dias (data de criação do canal)
      3. vídeo mais antigo do canal também dentro do período (descarta canal com histórico importado)
      4. gênero: fração mínima dos vídeos em categorias do YouTube compatíveis com o nicho
    """
    now = datetime.now(timezone.utc)
    ids = list(cands)
    channels = {}
    for batch in chunks(ids, 50):
        data = yt.get("channels", part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50)
        for it in data.get("items", []):
            channels[it["id"]] = it

    out = []
    for n, (cid, ch) in enumerate(channels.items(), 1):
        if progress:
            progress(f"Avaliando canais {n}/{len(channels)}")
        st, sn = ch.get("statistics", {}), ch["snippet"]
        if st.get("hiddenSubscriberCount"):
            continue
        subs, views, nvid = int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0))
        if not (cfg["min_subscribers"] <= subs <= cfg["max_subscribers"]):
            continue
        if views < cfg["min_total_views"] or views / max(subs, 1) < cfg["min_views_per_sub"]:
            continue
        created = parse_dt(sn["publishedAt"])
        if (now - created).days > days:           # canal antigo -> fora, mesmo que tenha voltado a postar
            continue
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads or nvid == 0:
            continue
        # vídeos do canal (máx. 4 páginas = 200; canal de <30 dias raramente passa disso)
        vid_ids, oldest, token, complete = [], None, None, False
        for _ in range(4):
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
        if not complete or oldest is None or (now - oldest).days > days:
            continue                               # vídeos anteriores ao período -> não é canal novo de verdade
        # detalhes dos vídeos (até 50 mais recentes) p/ gênero, idioma e top vídeos
        vids = []
        for batch in chunks(vid_ids[:50], 50):
            vd = yt.get("videos", part="snippet,statistics,contentDetails", id=",".join(batch))
            for v in vd.get("items", []):
                vs, vsn = v.get("statistics", {}), v["snippet"]
                vids.append({
                    "id": v["id"], "title": vsn["title"],
                    "views": int(vs.get("viewCount", 0)),
                    "published": vsn["publishedAt"],
                    "thumb": (vsn.get("thumbnails", {}).get("medium") or vsn.get("thumbnails", {}).get("default") or {}).get("url", ""),
                    "cat": vsn.get("categoryId", ""),
                    "lang": _lang_of(vsn.get("defaultAudioLanguage") or vsn.get("defaultLanguage")),
                    "duration": v.get("contentDetails", {}).get("duration", ""),
                })
        if not vids:
            continue
        # gênero: decidido pelo conteúdo (categoria do YouTube + palavras-chave nos títulos)
        genres = classify(cfg, vids, sn["title"] + " " + sn.get("description", ""))
        if not genres:
            continue
        # idioma: maioria do áudio dos vídeos; senão o idioma da busca
        vlangs = Counter(v["lang"] for v in vids if v["lang"])
        lang = vlangs.most_common(1)[0][0] if vlangs else (sorted(cands[cid]["langs"])[0] if cands[cid]["langs"] else "")
        age = max((now - created).total_seconds() / 86400, 1)
        vpd, vps = views / age, views / max(subs, 1)
        shorts = sum(1 for v in vids if _is_short(v["duration"]))
        top = sorted(vids, key=lambda v: v["views"], reverse=True)
        timeline = sorted(vids, key=lambda v: v["published"])
        out.append({
            "id": cid,
            "title": sn["title"],
            "handle": sn.get("customUrl", ""),
            "url": f"https://www.youtube.com/channel/{cid}",
            "avatar": (sn.get("thumbnails", {}).get("medium") or sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
            "description": sn.get("description", "")[:300],
            "country": sn.get("country", ""),
            "lang": lang,
            "genres": genres,
            "queries": sorted(cands[cid]["queries"]),
            "subscribers": subs, "views": views, "videos": nvid,
            "created": created.date().isoformat(),
            "first_video": oldest.date().isoformat(),
            "age_days": round(age, 1),
            "views_per_day": round(vpd), "views_per_sub": round(vps, 1),
            "subs_per_day": round(subs / age, 1),
            "shorts_share": round(shorts / len(vids), 2),
            "score": score(vpd, vps, subs),
            "top_videos": [{k: v[k] for k in ("id", "title", "views", "thumb", "published")} for v in top[:3]],
            "video_titles": [v["title"] for v in vids],
            "timeline": [v["views"] for v in timeline][-40:],
            "checked": now.strftime("%Y-%m-%d %H:%M"),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def _kw_regex(words: list[str]) -> re.Pattern | None:
    """Palavras curtas (≤4 letras) precisam ser palavra inteira; as maiores casam por prefixo."""
    parts = []
    for w in words:
        w = _norm(w).strip()
        if w:
            parts.append(re.escape(w) + (r"(?![a-z0-9])" if len(w) <= 4 else ""))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")") if parts else None


def _norm(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def classify(cfg: dict, vids: list[dict], channel_text: str = "") -> list[str]:
    """Decide os gêneros pelo conteúdo real do canal.

    Música: maioria dos vídeos na categoria Música (10) do YouTube.
    Demais: maioria dos vídeos em categorias compatíveis E palavras do gênero em pelo menos
    `min_keyword_match` dos títulos (ou no nome/descrição do canal + algum título).
    """
    n = len(vids)
    cats = Counter(v["cat"] for v in vids)
    out = []
    for g, gcfg in cfg["categories"].items():
        allowed = set(gcfg.get("youtube_category_ids", []))
        if sum(c for k, c in cats.items() if k in allowed) / n < cfg["min_genre_match"]:
            continue
        rx = _kw_regex(gcfg.get("keywords", []))
        if rx is None:          # música: basta a categoria
            out.append(g)
            continue
        hits = sum(1 for v in vids if rx.search(_norm(v["title"])))
        if hits / n >= cfg.get("min_keyword_match", 0.3) or (hits and rx.search(_norm(channel_text))):
            out.append(g)
    return out


def _is_short(iso_dur: str) -> bool:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_dur or "")
    if not m:
        return False
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s <= 180


# ------------------------------------------------------------------ banco local
_db_lock = threading.Lock()


def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return {"channels": {}, "last_scan": None}


def save_results(results: list[dict], replace_ids: set | None = None) -> dict:
    with _db_lock:
        db = load_db()
        for r in results:
            old = db["channels"].get(r["id"], {})
            r["genres"] = sorted(set(r["genres"]) | set(old.get("genres", [])))
            r["queries"] = sorted(set(r["queries"]) | set(old.get("queries", [])))
            db["channels"][r["id"]] = r
        db["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        DATA_DIR.mkdir(exist_ok=True)
        DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
        return db


def prune_db(days: int) -> None:
    """Remove canais que já passaram da idade máxima."""
    with _db_lock:
        db = load_db()
        today = datetime.now(timezone.utc).date()
        db["channels"] = {k: v for k, v in db["channels"].items()
                          if (today - datetime.fromisoformat(v["created"]).date()).days <= days}
        if DB_FILE.exists():
            DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ operações de alto nível
def scan_cost(cfg: dict, langs: list[str], genres: list[str], mode: str) -> int:
    per = 2 if mode == "both" else 1
    return sum(len(cfg["categories"][g]["concepts"]) for g in genres) * len(langs) * per * 100


def full_scan(yt: YouTube, cfg: dict, langs: list[str], genres: list[str], days: int,
              mode: str = "channel", progress=None) -> list[dict]:
    cands: dict[str, dict] = {}
    hint: dict[str, set] = {}
    jobs = [(g, c, l) for g in genres for c in cfg["categories"][g]["concepts"] for l in langs]
    for i, (g, concept, lang) in enumerate(jobs, 1):
        q = cfg["categories"][g]["concepts"][concept].get(lang)
        if not q:
            continue
        if progress:
            progress(f"Buscando {i}/{len(jobs)}: “{q}”")
        for cid, e in search_channels(yt, cfg, q, lang, days, g, mode).items():
            c = cands.setdefault(cid, {"queries": set(), "langs": set()})
            c["queries"] |= e["queries"]
            c["langs"] |= e["langs"]
            hint.setdefault(cid, set()).add(g)
    if progress:
        progress(f"{len(cands)} canais candidatos. Avaliando…")
    return evaluate(yt, cfg, cands, days, hint, progress)


def query_scan(yt: YouTube, cfg: dict, query: str, langs: list[str], days: int,
               mode: str = "both", progress=None) -> list[dict]:
    """Busca livre (ex.: pelo nome de um vídeo). O gênero é deduzido pelas categorias dos vídeos."""
    cands: dict[str, dict] = {}
    for lang in langs:
        if progress:
            progress(f"Buscando “{query}” ({lang})")
        for cid, e in search_channels(yt, cfg, query, lang, days, None, mode).items():
            c = cands.setdefault(cid, {"queries": set(), "langs": set()})
            c["queries"] |= e["queries"]
            c["langs"] |= e["langs"]
    hint = {cid: set(cfg["categories"]) for cid in cands}  # aceita qualquer um dos 4 gêneros, validado por categoria
    return evaluate(yt, cfg, cands, days, hint, progress)


def concepts_of(cfg: dict, channel: dict) -> list[tuple[str, str]]:
    """Mapeia as consultas que acharam o canal de volta para (gênero, conceito)."""
    out = []
    for g, gcfg in cfg["categories"].items():
        for c, tr in gcfg["concepts"].items():
            if set(tr.values()) & set(channel.get("queries", [])):
                out.append((g, c))
    if not out:  # achado por busca livre: usa o 1º conceito de cada gênero do canal
        out = [(g, next(iter(cfg["categories"][g]["concepts"]))) for g in channel.get("genres", [])]
    return out


def similar_cost(cfg: dict, channel: dict, target_langs: list[str]) -> int:
    n = min(len(concepts_of(cfg, channel)), cfg.get("similar_max_concepts", 2))
    return n * len(target_langs) * 2 * 100


def find_similar(yt: YouTube, cfg: dict, channel: dict, target_langs: list[str], days: int,
                 progress=None) -> list[dict]:
    """Canais do mesmo nicho em outros idiomas: traduz os conceitos do canal e busca em cada idioma."""
    concepts = concepts_of(cfg, channel)[: cfg.get("similar_max_concepts", 2)]
    cands: dict[str, dict] = {}
    hint: dict[str, set] = {}
    for g, c in concepts:
        for lang in target_langs:
            q = cfg["categories"][g]["concepts"][c].get(lang)
            if not q:
                continue
            if progress:
                progress(f"Buscando “{q}” ({lang})")
            for cid, e in search_channels(yt, cfg, q, lang, days, g, "both").items():
                if cid == channel["id"]:
                    continue
                x = cands.setdefault(cid, {"queries": set(), "langs": set()})
                x["queries"] |= e["queries"]
                x["langs"] |= e["langs"]
                hint.setdefault(cid, set()).add(g)
    res = evaluate(yt, cfg, cands, days, hint, progress)
    same = set(channel.get("genres", []))
    return [r for r in res if (r["lang"] != channel.get("lang") or not r["lang"]) and same & set(r["genres"])]
