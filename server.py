#!/usr/bin/env python3
"""Servidor local do Niche Finder.

    python3 server.py                  -> abre http://127.0.0.1:8765
    python3 server.py --scan           -> varredura completa pelo terminal, sem interface
    python3 server.py --keywords "atmospheric jungle mix, liquid dnb"   -> busca por palavras-chave pelo terminal
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import nf_core as core

WEB = Path(__file__).resolve().parent / "web"
JOBS: dict[str, dict] = {}
FETCH = None  # substituível em testes


def yt():
    return core.YouTube(core.load_api_key(), fetch=FETCH)


def start_job(kind: str, fn) -> str:
    """Roda `fn(progress)` em segundo plano. `fn` devolve (resultados, funil[, extra])."""
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "kind": kind, "status": "running", "message": "Iniciando…",
           "results": None, "funnel": None, "extra": None, "error": None}
    JOBS[jid] = job

    def run():
        try:
            out = fn(lambda m: job.__setitem__("message", m))
            job["results"], job["funnel"] = out[0], out[1]
            job["extra"] = out[2] if len(out) > 2 else None
            job["status"] = "done"
            job["message"] = f"{len(job['results'])} canais aprovados"
        except core.ApiError as e:
            job.update(status="error", error=core.redact(e))
        except Exception as e:  # noqa: BLE001
            print(core.redact(traceback.format_exc()), file=sys.stderr)
            job.update(status="error", error=core.redact(f"Erro inesperado: {e}"))

    threading.Thread(target=run, daemon=True).start()
    return jid


def _keywords(raw) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").replace("\n", ",").split(",")
    return [k.strip() for k in items if k.strip()][:10]


def _similar_langs(cfg: dict, ch: dict, chosen) -> list[str]:
    """Idiomas marcados na tela, menos o do próprio canal (se sobrar nenhum, usa todos os outros)."""
    other = [l for l in cfg["languages"] if l != ch.get("lang")]
    picked = [l for l in (chosen or []) if l in other]
    return picked or other


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _local_only(self) -> bool:
        """Só aceita requisições feitas para 127.0.0.1/localhost e, se houver Origin, vindas da própria página.

        Checar o Host bloqueia "DNS rebinding" (um site malicioso apontando o próprio domínio para 127.0.0.1);
        checar o Origin bloqueia CSRF (outro site mandando POST para o servidor local).
        """
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        if host not in allowed or (origin and urlparse(origin).netloc not in allowed):
            self._json({"error": "origem não permitida"}, 403)
            return False
        return True

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if not self._local_only():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        cfg = core.load_config()
        if u.path == "/api/state":
            db = core.load_db()
            return self._json({
                "channels": list(db["channels"].values()),
                "last_scan": db["last_scan"],
                "has_key": bool(core.load_api_key()),
                "quota": core.quota_status(),
                "config": {
                    "criteria": cfg["criteria"], "scan_languages": cfg["scan_languages"],
                    "default_mode": cfg.get("default_mode", "video"),
                    "languages": {k: v["label"] for k, v in cfg["languages"].items()},
                    "genres": {k: v["label"] for k, v in cfg["categories"].items()},
                },
            })
        if u.path == "/api/job":
            job = JOBS.get(q.get("id", ""))
            return self._json(job or {"status": "error", "error": "tarefa não encontrada"}, 200 if job else 404)
        if u.path == "/api/estimate":
            langs = [l for l in q.get("langs", ",".join(cfg["scan_languages"])).split(",") if l in cfg["languages"]]
            genres = [g for g in q.get("genres", ",".join(cfg["categories"])).split(",") if g in cfg["categories"]]
            return self._json({"cost": core.scan_cost(cfg, langs, genres, q.get("mode", "video"))})
        # arquivos estáticos
        path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        f = (WEB / path).resolve()
        if WEB not in f.parents or not f.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        if not self._local_only():
            return
        u = urlparse(self.path)
        b = self._body()
        cfg = core.load_config()
        crit = core.criteria(cfg, b.get("criteria"))
        langs = [l for l in (b.get("langs") or cfg["scan_languages"]) if l in cfg["languages"]] or cfg["scan_languages"]
        genres = [g for g in (b.get("genres") or []) if g in cfg["categories"]] or None
        try:
            if u.path == "/api/key":
                key = (b.get("key") or "").strip()
                if len(key) < 20:
                    return self._json({"error": "Chave parece inválida."}, 400)
                core.save_api_key(key)
                try:  # validação barata (1 unidade)
                    core.YouTube(key, cache_hours=0, fetch=FETCH).get("videoCategories", part="snippet", regionCode="BR")
                except core.ApiError as e:
                    return self._json({"ok": False, "error": str(e)})
                return self._json({"ok": True})

            if u.path == "/api/plan":
                # pré-visualiza as buscas (e traduções) sem gastar cota do YouTube
                if b.get("channel"):
                    ch = core.load_db()["channels"].get(b["channel"])
                    if not ch:
                        return self._json({"error": "Canal não encontrado."}, 404)
                    target = _similar_langs(cfg, ch, b.get("langs"))
                    plan = core.similar_plan(cfg, ch, target)
                else:
                    kws = _keywords(b.get("keywords"))
                    if not kws:
                        return self._json({"error": "Digite ao menos uma palavra-chave."}, 400)
                    queries = core.keyword_queries(cfg, kws, langs, bool(b.get("translate", True)))
                    plan = {"queries": queries, "cost": len(queries) * 100 * (2 if b.get("mode") == "both" else 1)}
                plan["queries"] = [{"q": q, "lang": l} for q, l in plan["queries"]]
                plan["remaining"] = core.quota_status()["remaining"]
                return self._json(plan)

            if u.path == "/api/scan":
                client = yt()
                genres = genres or list(cfg["categories"])
                mode = b.get("mode") or cfg.get("default_mode", "video")

                def fn(progress):
                    res, funnel = core.full_scan(client, cfg, langs, genres, crit, mode, progress)
                    core.prune_db(crit["days"])
                    core.save_results(res)
                    return res, funnel
                return self._json({"job": start_job("scan", fn)})

            if u.path == "/api/search":
                kws = _keywords(b.get("keywords"))
                if not kws:
                    return self._json({"error": "Digite ao menos uma palavra-chave."}, 400)
                client = yt()
                mode = b.get("mode") or "video"
                duration = b.get("duration") or None

                def fn(progress):
                    progress("Traduzindo palavras-chave…")
                    queries = core.keyword_queries(cfg, kws, langs, bool(b.get("translate", True)))
                    res, funnel = core.keyword_search(client, cfg, queries, crit, genres, duration, mode, progress)
                    core.save_results(res)
                    return res, funnel, {"queries": [{"q": q, "lang": l} for q, l in queries]}
                return self._json({"job": start_job("search", fn)})

            if u.path == "/api/similar":
                ch = core.load_db()["channels"].get(b.get("channel", ""))
                if not ch:
                    return self._json({"error": "Canal não encontrado."}, 404)
                client = yt()
                target = _similar_langs(cfg, ch, b.get("langs"))

                def fn(progress):
                    progress("Traduzindo títulos…")
                    res, funnel, plan = core.find_similar(client, cfg, ch, target, crit, progress)
                    core.save_results(res)
                    plan["queries"] = [{"q": q, "lang": l} for q, l in plan["queries"]]
                    return res, funnel, plan
                return self._json({"job": start_job("similar", fn)})
        except core.ApiError as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "rota desconhecida"}, 404)


def serve(port=8765, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Niche Finder rodando em {url}  (Ctrl+C para parar)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


def _print_cli(res, funnel):
    print("\nFunil: " + " · ".join(f"{k}: {v}" for k, v in funnel.items()))
    for r in res:
        print(f"  {r['score']:5}  {r['title']}  [{r['lang']}]  {r['subscribers']:,} insc. · média {r['avg_views']:,} views "
              f"· {r['videos']} vídeos · 1º vídeo há {round(r['age_days'])} dias  {r['url']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan", action="store_true", help="varredura completa pelo terminal, sem interface")
    ap.add_argument("--keywords", help="busca por palavras-chave separadas por vírgula, pelo terminal")
    ap.add_argument("--langs", help="idiomas separados por vírgula (padrão: scan_languages do config)")
    ap.add_argument("--days", type=int, help="idade máxima do 1º vídeo, em dias")
    a = ap.parse_args()
    if a.scan or a.keywords:
        cfg = core.load_config()
        crit = core.criteria(cfg, {"days": a.days})
        langs = a.langs.split(",") if a.langs else cfg["scan_languages"]
        client = yt()
        if a.keywords:
            queries = core.keyword_queries(cfg, _keywords(a.keywords), langs, True)
            print("Buscas:", ", ".join(f"“{q}” ({l})" for q, l in queries))
            res, funnel = core.keyword_search(client, cfg, queries, crit, None, None, "video", print)
        else:
            res, funnel = core.full_scan(client, cfg, langs, list(cfg["categories"]), crit,
                                         cfg.get("default_mode", "video"), print)
            core.prune_db(crit["days"])
        core.save_results(res)
        _print_cli(res, funnel)
        print(f"Cota usada: {client.used}")
    else:
        serve(a.port, not a.no_browser)
