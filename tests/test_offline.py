"""Testes offline do Niche Finder — a API do YouTube é simulada, não precisa de chave nem internet.

    python3 -m unittest discover -s tests -v

Também serve a interface com dados de demonstração (útil para desenvolver o front-end):

    python3 tests/test_offline.py --serve      # http://127.0.0.1:8799
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nf_core as core  # noqa: E402

NOW = datetime.now(timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# id: (nome, inscritos, views, criado_há_dias, 1º_vídeo_há_dias, categoria YouTube, idioma, títulos)
CHANNELS = {
    "UCa": ("Lofi Noturno BR", 12000, 2_400_000, 22, 21, "10", "pt", ["Lofi para estudar à noite 🌙", "Chuva + lofi 3h", "Beats para focar"]),
    "UCb": ("Sleep Waves", 31000, 5_100_000, 18, 17, "10", "en", ["Deep sleep music 8 hours", "Rain sounds for sleeping", "Delta waves"]),
    "UCc": ("Mistérios Perdidos", 8400, 1_300_000, 27, 26, "27", "pt", ["O mistério do voo 370", "A cidade que sumiu do mapa", "Casos sem solução"]),
    "UCd": ("Fatos Rápidos", 45000, 9_800_000, 12, 11, "24", "pt", ["Você sabia que o polvo tem 3 corações?", "Fatos do espaço", "Curiosidades do corpo"]),
    "UCe": ("Math in 60s", 3200, 410_000, 9, 8, "27", "en", ["Multiply any number by 11", "Fractions trick", "Square roots fast"]),
    "UCf": ("Datos Curiosos MX", 6700, 950_000, 15, 14, "24", "es", ["¿Sabías que las abejas bailan?", "Datos del espacio", "El cuerpo humano"]),
    "UCk": ("Lost Mysteries", 22000, 3_300_000, 19, 18, "27", "en", ["The unsolved mystery of flight 19", "The town that vanished", "Unsolved cases documentary"]),
    # devem ser REPROVADOS:
    "UCg": ("Canal Antigo Voltou", 20000, 4_000_000, 900, 10, "27", "pt", ["Curiosidades incríveis", "Fatos"]),  # criado há 900 dias
    "UCh": ("Histórico Importado", 15000, 3_000_000, 20, 400, "27", "pt", ["Documentário completo"]),        # vídeos antigos
    "UCi": ("Vlog Aleatório", 9000, 2_000_000, 10, 9, "22", "pt", ["Meu dia", "Rotina"]),                   # fora dos gêneros
    "UCj": ("Pequenino", 600, 900_000, 10, 9, "10", "pt", ["Música"]),                                      # < 1.000 inscritos
}
MUSIC = {"UCa", "UCb", "UCj"}
EXPECTED = {"Lofi Noturno BR", "Sleep Waves", "Mistérios Perdidos", "Fatos Rápidos",
            "Math in 60s", "Datos Curiosos MX", "Lost Mysteries"}


def fake_api(url: str) -> dict:
    """Imita as respostas da YouTube Data API v3 para os canais acima."""
    rnd = random.Random(url)
    p = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    ep = p.path.rsplit("/", 1)[1]
    if ep == "videoCategories":
        return {"items": []}
    if ep == "search":
        lang = q.get("relevanceLanguage")
        ids = [c for c, v in CHANNELS.items() if v[6] == lang or c in ("UCg", "UCh", "UCi")]
        if q.get("videoCategoryId") == "10":
            ids = [c for c in ids if c in MUSIC]
        return {"items": [{"id": {"channelId": c} if q["type"] == "channel" else {"videoId": f"{c}_v0"},
                           "snippet": {"channelId": c, "title": CHANNELS[c][7][0]}} for c in ids]}
    if ep == "channels":
        return {"items": [{
            "id": c,
            "snippet": {"title": CHANNELS[c][0], "publishedAt": iso(CHANNELS[c][3]), "customUrl": "@" + c.lower(),
                        "thumbnails": {"medium": {"url": f"https://picsum.photos/seed/{c}/88"}}},
            "statistics": {"subscriberCount": str(CHANNELS[c][1]), "viewCount": str(CHANNELS[c][2]),
                           "videoCount": str(len(CHANNELS[c][7]) * 6)},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + c}},
        } for c in q["id"].split(",")]}
    if ep == "playlistItems":
        c = q["playlistId"][2:]
        n = len(CHANNELS[c][7]) * 6
        return {"items": [{"contentDetails": {"videoId": f"{c}_v{i}", "videoPublishedAt": iso(CHANNELS[c][4] * (n - i) / n)}}
                          for i in range(n)]}
    if ep == "videos":
        out = []
        for vid in q["id"].split(","):
            c, i = vid.split("_v")
            i, d = int(i), CHANNELS[c]
            out.append({
                "id": vid,
                "snippet": {"title": d[7][i % len(d[7])] + ("" if i < len(d[7]) else f" #{i}"),
                            "publishedAt": iso(d[4] * (1 - i / 20)), "categoryId": d[5], "defaultAudioLanguage": d[6],
                            "thumbnails": {"medium": {"url": f"https://picsum.photos/seed/{vid}/80/45"}}},
                "statistics": {"viewCount": str(int(d[2] / 18 * rnd.uniform(.2, 2.2)))},
                "contentDetails": {"duration": "PT45S" if c in ("UCd", "UCe", "UCf") else "PT12M3S"},
            })
        return {"items": out}
    raise ValueError(f"endpoint não simulado: {ep}")


def use_temp_storage() -> Path:
    """Redireciona cache/banco/cota para uma pasta temporária (não mexe nos seus dados reais)."""
    tmp = Path(tempfile.mkdtemp(prefix="nf-test-"))
    core.CACHE_DIR, core.DATA_DIR = tmp / "cache", tmp / "data"
    core.DB_FILE, core.QUOTA_FILE = core.DATA_DIR / "channels.json", core.DATA_DIR / "quota.json"
    return tmp


class NicheFinderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = use_temp_storage()
        self.cfg = core.load_config()
        self.yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        return core.full_scan(self.yt, self.cfg, ["pt", "en", "es"], list(self.cfg["categories"]), 30, "channel")

    def test_full_scan_keeps_only_new_small_channels_in_target_genres(self):
        res = {r["title"]: r for r in self.scan()}
        self.assertEqual(set(res), EXPECTED)

    def test_old_and_imported_channels_are_rejected(self):
        titles = {r["title"] for r in self.scan()}
        self.assertNotIn("Canal Antigo Voltou", titles)
        self.assertNotIn("Histórico Importado", titles)

    def test_genres_are_detected_from_content(self):
        res = {r["title"]: r["genres"] for r in self.scan()}
        self.assertEqual(res["Lofi Noturno BR"], ["musica"])
        self.assertEqual(res["Mistérios Perdidos"], ["documentario"])
        self.assertEqual(res["Fatos Rápidos"], ["curiosidades"])
        self.assertEqual(res["Math in 60s"], ["ensino"])

    def test_similar_finds_same_niche_in_other_languages(self):
        core.save_results(self.scan())
        ch = core.load_db()["channels"]["UCc"]  # Mistérios Perdidos (PT, documentário)
        sim = core.find_similar(self.yt, self.cfg, ch, ["en", "es"], 30)
        self.assertEqual([r["title"] for r in sim], ["Lost Mysteries"])

    def test_query_search(self):
        res = core.query_scan(self.yt, self.cfg, "você sabia", ["pt"], 30)
        self.assertIn("Fatos Rápidos", {r["title"] for r in res})

    def test_score_is_bounded(self):
        self.assertTrue(0 <= core.score(0, 0, 1) <= 100)
        self.assertTrue(0 <= core.score(10**9, 10**6, 1000) <= 100)

    def test_quota_is_tracked(self):
        self.scan()
        self.assertGreater(core.quota_status()["used"], 0)


def serve_demo(port: int = 8799):
    """Sobe a interface com dados de demonstração."""
    import server
    use_temp_storage()
    cfg = core.load_config()
    yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)
    res = core.full_scan(yt, cfg, ["pt", "en", "es"], list(cfg["categories"]), 30, "channel")
    core.save_results([r for r in res if r["id"] != "UCk"])  # UCk fica p/ o botão "similares" encontrar
    server.FETCH = fake_api
    srv = server.ThreadingHTTPServer(("127.0.0.1", port), server.Handler)
    print(f"Demo em http://127.0.0.1:{port}  (Ctrl+C para parar)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_demo()
    else:
        unittest.main(verbosity=2)
