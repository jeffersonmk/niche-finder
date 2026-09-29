#!/usr/bin/env python3
"""Servidor local do Niche Finder.

    python3 server.py            -> abre http://127.0.0.1:8765
    python3 server.py --scan     -> faz uma varredura completa sem abrir a interface
"""
from __future__ import annotations

import argparse
import json
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
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "kind": kind, "status": "running", "message": "Iniciando…", "results": None, "error": None}
    JOBS[jid] = job

    def run():
        try:
            job["results"] = fn(lambda m: job.__setitem__("message", m))
            job["status"] = "done"
            job["message"] = f"{len(job['results'])} canais encontrados"
        except core.ApiError as e:
            job.update(status="error", error=str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            job.update(status="error", error=f"Erro inesperado: {e}")

    threading.Thread(target=run, daemon=True).start()
    return jid


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
        # bloqueia requisições de outros sites (CSRF) às rotas que gastam cota ou gravam a chave
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            self._json({"error": "origem não permitida"}, 403)
            return False
        return True

    # ---------------------------------------------------------------- GET
    def do_GET(self):
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
                    "days": cfg["max_channel_age_days"], "min_subs": cfg["min_subscribers"],
                    "max_subs": cfg["max_subscribers"], "min_views_per_sub": cfg["min_views_per_sub"],
                    "min_total_views": cfg["min_total_views"], "scan_languages": cfg["scan_languages"],
                    "languages": {k: v["label"] for k, v in cfg["languages"].items()},
                    "genres": {k: v["label"] for k, v in cfg["categories"].items()},
                },
            })
        if u.path == "/api/job":
            job = JOBS.get(q.get("id", ""))
            return self._json(job or {"status": "error", "error": "tarefa não encontrada"}, 200 if job else 404)
        if u.path == "/api/estimate":
            if q.get("kind") == "similar":
                ch = core.load_db()["channels"].get(q.get("channel", ""))
                langs = [l for l in cfg["languages"] if ch and l != ch.get("lang")]
                return self._json({"cost": core.similar_cost(cfg, ch, langs) if ch else 0, "langs": langs})
            langs = q.get("langs", ",".join(cfg["scan_languages"])).split(",")
            genres = q.get("genres", ",".join(cfg["categories"])).split(",")
            return self._json({"cost": core.scan_cost(cfg, langs, genres, q.get("mode", "channel"))})
        # arquivos estáticos
        path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        f = (WEB / path).resolve()
        if WEB not in f.parents or not f.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
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
        days = int(b.get("days") or cfg["max_channel_age_days"])
        try:
            if u.path == "/api/key":
                key = (b.get("key") or "").strip()
                if len(key) < 20:
                    return self._json({"error": "Chave parece inválida."}, 400)
                core.save_api_key(key)
                # validação barata (1 unidade)
                try:
                    core.YouTube(key, cache_hours=0, fetch=FETCH).get("videoCategories", part="snippet", regionCode="BR")
                except core.ApiError as e:
                    return self._json({"ok": False, "error": str(e)})
                return self._json({"ok": True})

            if u.path == "/api/scan":
                client = yt()
                langs = b.get("langs") or cfg["scan_languages"]
                genres = b.get("genres") or list(cfg["categories"])
                mode = b.get("mode", "channel")

                def fn(progress):
                    res = core.full_scan(client, cfg, langs, genres, days, mode, progress)
                    core.prune_db(days)
                    core.save_results(res)
                    return res
                return self._json({"job": start_job("scan", fn)})

            if u.path == "/api/search":
                query = (b.get("query") or "").strip()
                if not query:
                    return self._json({"error": "Digite algo para buscar."}, 400)
                client = yt()
                langs = b.get("langs") or cfg["scan_languages"]

                def fn(progress):
                    res = core.query_scan(client, cfg, query, langs, days, "both", progress)
                    core.save_results(res)
                    return res
                return self._json({"job": start_job("search", fn)})

            if u.path == "/api/similar":
                ch = core.load_db()["channels"].get(b.get("channel", ""))
                if not ch:
                    return self._json({"error": "Canal não encontrado."}, 404)
                client = yt()
                langs = [l for l in cfg["languages"] if l != ch.get("lang")]

                def fn(progress):
                    res = core.find_similar(client, cfg, ch, langs, days, progress)
                    core.save_results(res)
                    return res
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan", action="store_true", help="varredura completa pelo terminal, sem interface")
    a = ap.parse_args()
    if a.scan:
        cfg = core.load_config()
        client = yt()
        res = core.full_scan(client, cfg, cfg["scan_languages"], list(cfg["categories"]),
                             cfg["max_channel_age_days"], "channel", print)
        core.prune_db(cfg["max_channel_age_days"])
        core.save_results(res)
        print(f"{len(res)} canais aprovados · cota usada: {client.used}")
    else:
        serve(a.port, not a.no_browser)
