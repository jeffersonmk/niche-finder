"""Testes offline do Niche Finder — a API do YouTube e o tradutor são simulados: não precisa de chave nem internet.

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


LONG, SHORT = "PT58M10S", "PT45S"
# id: (nome, inscritos, views, criado_há_dias, 1º_vídeo_há_dias, categoria, idioma, títulos, tópicos, duração)
CHANNELS = {
    "UCa": ("Neon Jungle", 12000, 2_400_000, 22, 21, "10", "en",
            ["VIRTUAL MEMORY 2003 (atmospheric jungle dnb mix)", "MIDNIGHT OVERFLOW (atmospheric jungle dnb mix)",
             "STEREO DREAMS 1998 (atmospheric jungle dnb mix)"], ["Music", "Electronic_music"], LONG),
    "UCb": ("Selva Atmosférica", 4000, 900_000, 18, 17, "22", "pt",          # música publicada como "Pessoas e blogs"
            ["MEMÓRIA VIRTUAL | atmosférico jungle dnb mix", "SONHOS ESTÉREO | atmosférico jungle dnb mix",
             "MADRUGADA | atmosférico jungle dnb mix"], ["Music", "Electronic_music"], LONG),
    "UCc": ("Mistérios Perdidos", 8400, 1_300_000, 27, 26, "27", "pt",
            ["O mistério do voo 370", "A cidade que sumiu do mapa", "Casos sem solução"], ["Society"], "PT12M3S"),
    "UCd": ("Fatos Rápidos", 45000, 9_800_000, 12, 11, "24", "pt",
            ["Você sabia que o polvo tem 3 corações?", "Fatos do espaço", "Curiosidades do corpo"], ["Knowledge"], SHORT),
    "UCe": ("Math in 60s", 3200, 410_000, 9, 8, "27", "en",
            ["Multiply any number by 11", "Fractions trick", "Square roots fast"], ["Knowledge"], SHORT),
    "UCk": ("Lost Mysteries", 22000, 3_300_000, 19, 18, "27", "en",
            ["The unsolved mystery of flight 19", "The town that vanished", "Unsolved cases documentary"], ["Society"], "PT14M"),
    # devem ser REPROVADOS:
    "UCg": ("Canal Antigo Voltou", 20000, 4_000_000, 900, 300, "10", "en", ["atmospheric jungle dnb mix"], ["Music"], LONG),
    "UCh": ("Histórico Importado", 15000, 3_000_000, 20, 400, "10", "en", ["liquid dnb mix"], ["Music"], LONG),
    "UCi": ("Vlog Aleatório", 9000, 2_000_000, 10, 9, "22", "pt", ["Meu dia", "Rotina"], ["Lifestyle"], "PT8M"),
    "UCj": ("Pequenino", 600, 900_000, 10, 9, "10", "en", ["jungle mix"], ["Music"], LONG),            # poucos inscritos, mas performa
    "UCl": ("Média Baixa", 5000, 150_000, 15, 14, "10", "en", ["dnb mix"], ["Music"], LONG),           # 60 vídeos -> média 2.500
}
# canais que aparecem nas buscas de cada idioma
BY_LANG = {"en": ["UCa", "UCe", "UCk", "UCg", "UCh", "UCj", "UCl"], "pt": ["UCb", "UCc", "UCd", "UCi"]}
EXPECTED = {"Neon Jungle", "Selva Atmosférica", "Mistérios Perdidos", "Fatos Rápidos", "Math in 60s", "Lost Mysteries",
            "Pequenino"}


def n_videos(c: str) -> int:
    return 60 if c == "UCl" else len(CHANNELS[c][7]) * 6


def fake_translate(text: str, target: str) -> tuple[str, str]:
    """Tradutor de mentira: marca o idioma no texto (o texto de origem é sempre 'inglês')."""
    return f"{text} [{target}]", "en"


def fake_api(url: str) -> dict:
    """Imita as respostas da YouTube Data API v3 para os canais acima."""
    rnd = random.Random(url)
    p = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(p.query))
    ep = p.path.rsplit("/", 1)[1]
    if ep == "videoCategories":
        return {"items": []}
    if ep == "search":
        ids = BY_LANG.get(q.get("relevanceLanguage"), [])
        if q.get("videoDuration") == "long":
            ids = [c for c in ids if CHANNELS[c][9] == LONG]
        return {"items": [{"id": {"channelId": c} if q["type"] == "channel" else {"videoId": f"{c}_v0"},
                           "snippet": {"channelId": c, "title": CHANNELS[c][7][0]}} for c in ids]}
    if ep == "channels":
        if q.get("forHandle") or q.get("forUsername"):
            name = (q.get("forHandle") or q.get("forUsername")).lstrip("@").lower()
            return {"items": [{"id": c} for c in CHANNELS if c.lower() == name]}
        return {"items": [{
            "id": c,
            "snippet": {"title": CHANNELS[c][0], "publishedAt": iso(CHANNELS[c][3]), "customUrl": "@" + c.lower(),
                        "thumbnails": {"medium": {"url": f"https://picsum.photos/seed/{c}/88"}}},
            "statistics": {"subscriberCount": str(CHANNELS[c][1]), "viewCount": str(CHANNELS[c][2]),
                           "videoCount": str(n_videos(c))},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + c}},
            "topicDetails": {"topicCategories": [f"https://en.wikipedia.org/wiki/{t}" for t in CHANNELS[c][8]]},
        } for c in q["id"].split(",") if c in CHANNELS]}
    if ep == "playlistItems":
        c = q["playlistId"][2:]
        n = n_videos(c)
        start = 50 if q.get("pageToken") else 0
        items = [{"contentDetails": {"videoId": f"{c}_v{i}", "videoPublishedAt": iso(CHANNELS[c][4] * (n - i) / n)}}
                 for i in range(start, min(n, start + 50))]
        return {"items": items, **({"nextPageToken": "p2"} if start + 50 < n else {})}
    if ep == "videos":
        out = []
        for vid in q["id"].split(","):
            if "_v" not in vid or vid.split("_v")[0] not in CHANNELS:
                continue
            c, i = vid.split("_v")
            i, d = int(i), CHANNELS[c]
            out.append({
                "id": vid,
                "snippet": {"title": d[7][i % len(d[7])], "publishedAt": iso(d[4] * (1 - i / 60)), "channelId": c,
                            "categoryId": d[5], "defaultAudioLanguage": d[6],
                            "thumbnails": {"medium": {"url": f"https://picsum.photos/seed/{vid}/80/45"}}},
                "statistics": {"viewCount": str(int(d[2] / n_videos(c) * rnd.uniform(.2, 2.2) * (3 if i < 3 else 1)))},
                "contentDetails": {"duration": d[9]},
            })
        return {"items": out}
    raise ValueError(f"endpoint não simulado: {ep}")


def use_temp_storage() -> Path:
    """Redireciona cache/banco/cota para uma pasta temporária e usa o tradutor falso (não mexe nos seus dados reais)."""
    tmp = Path(tempfile.mkdtemp(prefix="nf-test-"))
    core.CACHE_DIR, core.DATA_DIR = tmp / "cache", tmp / "data"
    core.DB_FILE, core.QUOTA_FILE = core.DATA_DIR / "channels.json", core.DATA_DIR / "quota.json"
    core._translator = fake_translate
    return tmp


class NicheFinderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = use_temp_storage()
        self.cfg = core.load_config()
        self.crit = core.criteria(self.cfg)
        self.yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, langs=("en", "pt")):
        return core.full_scan(self.yt, self.cfg, list(langs), list(self.cfg["categories"]), self.crit, "video")

    def test_scan_keeps_only_new_channels_performing_well(self):
        res, funnel = self.scan()
        self.assertEqual({r["title"] for r in res}, EXPECTED)
        self.assertEqual(funnel["aprovados"], len(EXPECTED))

    def test_old_channels_are_rejected_by_first_video_date(self):
        titles = {r["title"] for r in self.scan()[0]}
        self.assertNotIn("Canal Antigo Voltou", titles)
        self.assertNotIn("Histórico Importado", titles)

    def test_funnel_explains_rejections(self):
        _, funnel = self.scan()
        self.assertEqual(funnel["média de views baixa"], 1)          # Média Baixa
        self.assertEqual(funnel["fora dos gêneros"], 1)              # Vlog
        self.assertEqual(funnel["vídeos anteriores ao período"], 2)  # Antigo Voltou + Histórico Importado

    def test_music_detected_even_when_published_as_people_blogs(self):
        res = {r["title"]: r for r in self.scan()[0]}
        self.assertEqual(res["Selva Atmosférica"]["genres"], ["musica"])
        self.assertEqual(res["Neon Jungle"]["genres"], ["musica"])
        self.assertEqual(res["Mistérios Perdidos"]["genres"], ["documentario"])
        self.assertEqual(res["Fatos Rápidos"]["genres"], ["curiosidades"])
        self.assertEqual(res["Math in 60s"]["genres"], ["ensino"])

    def test_music_styles_without_mix_word(self):
        """Jazz, bossa nova, phonk… publicados como 'Pessoas e blogs' sem 'mix'/'music' no título continuam sendo música."""
        topics = ["Music"]
        for titles in (["Relaxing Jazz Cafe ☕", "Rainy Night Jazz 🌧", "Coffee Shop Jazz"],
                       ["Bossa Nova para relaxar", "Bossa Nova ao entardecer"],
                       ["Sertanejo raiz 2026", "Funk das antigas"]):
            vids = [{"title": t, "cat": "22"} for t in titles] * 2
            self.assertIn("musica", core.classify(self.cfg, vids, "", topics), titles)
        # sem o tópico Music do YouTube, um vlog com 'house' no título não vira música
        vlog = [{"title": t, "cat": "22"} for t in ("My house tour", "Rock climbing day", "Morning routine")] * 2
        self.assertNotIn("musica", core.classify(self.cfg, vlog, "", ["Lifestyle"]))

    def test_style_names_are_not_translated(self):
        self.assertEqual(core.translate_query(self.cfg, "jazz", "pt"), "jazz")
        self.assertEqual(core.translate_query(self.cfg, "late night jazz", "pt"), "late night [pt] jazz")

    def test_subscribers_do_not_matter_by_default(self):
        res = {r["title"]: r for r in self.scan()[0]}
        self.assertIn("Pequenino", res)                               # 600 inscritos, mas média alta
        crit = core.criteria(self.cfg, {"min_subscribers": 1000})    # ainda dá para ligar pelo config
        res, funnel = core.full_scan(self.yt, self.cfg, ["en"], ["musica"], crit, "video")
        self.assertNotIn("Pequenino", {r["title"] for r in res})
        self.assertEqual(funnel["poucos inscritos"], 1)

    def test_avg_views_criterion(self):
        crit = core.criteria(self.cfg, {"min_avg_views": 10**9})
        res, funnel = core.full_scan(self.yt, self.cfg, ["en"], ["musica"], crit, "video")
        self.assertEqual(res, [])
        self.assertGreater(funnel["média de views baixa"], 0)

    def test_split_title(self):
        self.assertEqual(core.split_title(self.cfg, "VIRTUAL MEMORY 2003 (atmospheric jungle dnb mix)"),
                         ("virtual memory", "atmospheric jungle dnb mix"))
        self.assertEqual(core.split_title(self.cfg, "CIGARETTE | Atmospheric liquid drum and bass mix"),
                         ("cigarette", "atmospheric liquid drum and bass mix"))

    def test_translation_keeps_genre_terms(self):
        self.assertEqual(core.translate_query(self.cfg, "atmospheric jungle dnb mix", "pt"),
                         "atmospheric [pt] jungle dnb mix")
        self.assertEqual(core.translate_query(self.cfg, "dnb mix", "pt"), "dnb mix")   # nada a traduzir

    def test_keyword_queries_per_language(self):
        q = core.keyword_queries(self.cfg, ["late night focus"], ["en", "pt"], True)
        self.assertEqual(q, [("late night focus", "en"), ("late night focus [pt]", "pt")])
        q = core.keyword_queries(self.cfg, ["late night focus"], ["en"], False)
        self.assertEqual(q, [("late night focus", "en")])

    def test_keyword_search_with_genre_filter(self):
        queries = core.keyword_queries(self.cfg, ["jungle mix"], ["en", "pt"], True)
        res, _ = core.keyword_search(self.yt, self.cfg, queries, self.crit, ["musica"], "long")
        self.assertEqual({r["title"] for r in res}, {"Neon Jungle", "Selva Atmosférica", "Pequenino"})

    def test_similar_uses_translated_titles(self):
        core.save_results(self.scan(["en"])[0])
        ch = core.load_db()["channels"]["UCa"]                       # Neon Jungle (EN)
        res, _, plan = core.find_similar(self.yt, self.cfg, ch, ["pt"], self.crit)
        self.assertIn("atmospheric jungle dnb mix", plan["base"])     # estilo extraído dos títulos
        self.assertTrue(all(l == "pt" for _, l in plan["queries"]))
        self.assertEqual(plan["duration"], "long")                     # mixes longos -> busca vídeos longos
        self.assertEqual([r["title"] for r in res], ["Selva Atmosférica"])   # mesmo estilo, outro idioma

    def test_score_is_bounded(self):
        self.assertTrue(0 <= core.score(0, 0, 0) <= 100)
        self.assertTrue(0 <= core.score(10**8, 10**9, 10**6) <= 100)
        self.assertGreater(core.score(50_000, 20_000, 10), core.score(3_000, 1_000, 10))

    def test_quota_is_tracked(self):
        self.scan()
        self.assertGreater(core.quota_status()["used"], 0)


class ViralRadarTest(unittest.TestCase):
    """Radar viral: canais novos, pequenos e com views muito acima do esperado, ranqueados do mais viral ao menos."""

    def setUp(self):
        self.tmp = use_temp_storage()
        self.cfg = core.load_config()
        self.yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def m(self, **kw):
        base = dict(subs=1000, hidden=False, views=100_000, nvid=10, age_days=20,
                    uploads=[(iso(d), 10_000) for d in range(1, 20, 2)])
        base.update(kw)
        return core.viral_metrics(base["subs"], base["hidden"], base["views"], base["nvid"], base["age_days"],
                                  base["uploads"])

    def test_score_bounded_and_monotonic(self):
        lo, _ = core.viral_score(self.m(views=0, uploads=[]))
        hi, _ = core.viral_score(self.m(subs=100, views=10**9, nvid=1, age_days=1, uploads=[(iso(1), 10**9)]))
        self.assertTrue(0 <= lo < hi <= 100)

    def test_each_factor_pushes_score_the_right_way(self):
        s = lambda **kw: core.viral_score(self.m(**kw))[0]
        self.assertGreater(s(views=500_000), s(views=100_000))                 # mais views
        self.assertGreater(s(subs=500), s(subs=20_000))                         # menos inscritos (views/inscrito, fora da curva)
        self.assertGreater(s(nvid=5), s(nvid=80))                               # menos vídeos
        self.assertGreater(s(age_days=5), s(age_days=80))                       # canal mais novo
        recent = [(iso(1), 50_000), (iso(2), 40_000)] + [(iso(d), 5_000) for d in range(10, 30, 3)]
        old = [(iso(d), 5_000) for d in range(1, 8)] + [(iso(25), 50_000), (iso(28), 40_000)]
        self.assertGreater(s(uploads=recent), s(uploads=old))                   # views recentes valem mais

    def test_parts_and_weights(self):
        score, parts = core.viral_score(self.m())
        self.assertEqual(set(parts), set(core.VIRAL_FACTORS))
        self.assertTrue(all(0 <= v <= 1 for v in parts.values()))
        only_young, _ = core.viral_score(self.m(age_days=0), {k: 0 for k in core.VIRAL_FACTORS} | {"young": 1})
        self.assertEqual(only_young, 100.0)

    def test_measured_growth_replaces_estimate(self):
        h = [{"t": iso(2), "views": 1000}, {"t": iso(1), "views": 6000}, {"t": iso(0.9), "views": 6100}]
        self.assertAlmostEqual(core.growth_per_day(h), 5100 / 1.1, delta=1)      # ignora checagens < 6 h
        self.assertIsNone(core.growth_per_day(h[:1]))

    def test_radar_ranks_new_viral_channels(self):
        crit = core.criteria(self.cfg, None, "radar")
        plan = core.radar_plan(self.cfg, ["en", "pt"], list(self.cfg["categories"]), None, crit["days"])
        self.assertTrue(all(p["days"] == 30 for p in plan))
        res, funnel = core.radar(self.yt, self.cfg, plan, crit)
        titles = [r["title"] for r in res]
        self.assertNotIn("Canal Antigo Voltou", titles)
        self.assertNotIn("Histórico Importado", titles)
        self.assertNotIn("Média Baixa", titles)                                  # 60 vídeos > máx. 40
        self.assertEqual(funnel["aprovados"], len(res))
        self.assertEqual([r["viral"] for r in res], sorted((r["viral"] for r in res), reverse=True))
        for r in res:
            self.assertIn(r["viral_label"], {"explosivo", "viralizando", "promissor", "observacao"})
            self.assertGreaterEqual(r["viral"], crit["min_viral"])

    def test_min_viral_goes_to_funnel(self):
        crit = core.criteria(self.cfg, {"min_viral": 100}, "radar")
        plan = core.radar_plan(self.cfg, ["en"], ["musica"], None, crit["days"])
        res, funnel = core.radar(self.yt, self.cfg, plan, crit)
        self.assertEqual(res, [])
        self.assertGreater(funnel["pontuação viral baixa"], 0)

    def test_week_window_doubles_queries(self):
        a = core.radar_plan(self.cfg, ["en"], ["musica"], None, 30)
        b = core.radar_plan(self.cfg, ["en"], ["musica"], None, 30, week=True)
        self.assertEqual(len(b), 2 * len(a))
        self.assertEqual({p["days"] for p in b}, {30, 7})

    def test_keywords_and_any_genre(self):
        crit = core.criteria(self.cfg, None, "radar")
        plan = core.radar_plan(self.cfg, ["pt"], [], ["meu dia"], crit["days"])
        self.assertEqual([(p["q"], p["lang"]) for p in plan], [("meu dia [pt]", "pt")])
        crit["min_viral"] = 0
        strict, _ = core.radar(self.yt, self.cfg, plan, crit)
        loose, _ = core.radar(self.yt, self.cfg, plan, crit, any_genre=True)
        self.assertNotIn("Vlog Aleatório", {r["title"] for r in strict})
        self.assertIn("Vlog Aleatório", {r["title"] for r in loose})
        self.assertEqual(next(r for r in loose if r["title"] == "Vlog Aleatório")["genres"], ["outros"])

    def test_refresh_builds_history_and_old_records_get_score(self):
        crit = core.criteria(self.cfg, {"min_viral": 0}, "radar")
        res, _ = core.radar(self.yt, self.cfg, core.radar_plan(self.cfg, ["en"], ["musica"], None, 30), crit)
        core.save_results(res, self.cfg)
        db = core.load_db()
        cid = res[0]["id"]
        db["channels"][cid]["history"][0]["t"] = iso(1)                         # simula checagem de ontem
        db["channels"][cid]["history"][0]["views"] -= 5000
        legacy = dict(db["channels"][cid], id="UCold")                          # registro salvo por versão antiga
        for k in ("viral", "viral_parts", "uploads", "history", "current_vpd"):
            legacy.pop(k, None)
        db["channels"]["UCold"] = legacy
        core.DB_FILE.write_text(json.dumps(db))
        ref, _ = core.refresh_channels(self.yt, self.cfg, [cid])
        core.save_results(ref, self.cfg)
        ch = core.load_db()["channels"][cid]
        self.assertEqual(len(ch["history"]), 2)
        self.assertTrue(ch["vpd_measured"])
        self.assertAlmostEqual(ch["current_vpd"], 5000, delta=300)
        ui = {c["id"]: c for c in core.channels_for_ui(self.cfg)}
        self.assertIn("viral", ui["UCold"])                                      # pontuação recalculada na hora
        self.assertNotIn("uploads", ui[cid])                                     # campo interno não vai ao navegador


class SimilarFromLinkTest(unittest.TestCase):
    """Colar o link de um canal (ou vídeo) e achar canais novos e virais parecidos."""

    def setUp(self):
        self.tmp = use_temp_storage()
        self.cfg = core.load_config()
        self.yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_all_link_formats(self):
        uc = "UC" + "a" * 22
        cases = {
            f"https://www.youtube.com/channel/{uc}": ("id", uc),
            f"youtube.com/channel/{uc}/videos": ("id", uc),
            uc: ("id", uc),
            "https://www.youtube.com/@NeonJungle": ("handle", "@NeonJungle"),
            "https://m.youtube.com/@NeonJungle/videos?view=0": ("handle", "@NeonJungle"),
            "@NeonJungle": ("handle", "@NeonJungle"),
            "https://www.youtube.com/@Sel%C3%A7a": ("handle", "@Selça"),
            "https://youtube.com/user/oldname": ("user", "oldname"),
            "https://youtube.com/c/CustomName": ("custom", "CustomName"),
            "https://www.youtube.com/watch?v=abc123&t=10s": ("video", "abc123"),
            "https://youtu.be/abc123?si=x": ("video", "abc123"),
            "https://www.youtube.com/shorts/abc123": ("video", "abc123"),
            "https://music.youtube.com/watch?v=abc123": ("video", "abc123"),
        }
        for text, exp in cases.items():
            self.assertEqual(core.parse_channel_ref(text), exp, text)
        for bad in ("", "https://google.com/x", "https://www.youtube.com/results?search_query=x"):
            with self.assertRaises(core.ApiError):
                core.parse_channel_ref(bad)

    def test_resolve_by_handle_video_and_id(self):
        self.assertEqual(core.resolve_channel(self.yt, "https://www.youtube.com/@uca"), "UCa")
        self.assertEqual(core.resolve_channel(self.yt, "https://youtu.be/UCa_v3"), "UCa")
        with self.assertRaises(core.ApiError):
            core.resolve_channel(self.yt, "https://www.youtube.com/@naoexiste")

    def test_profile_works_for_old_big_channels(self):
        ref = core.profile_channel(self.yt, self.cfg, "UCg")          # antigo: o Radar reprovaria, aqui não importa
        self.assertEqual(ref["title"], "Canal Antigo Voltou")
        self.assertEqual(ref["genres"], ["musica"])
        self.assertTrue(ref["top_videos"])

    def test_suggested_queries_are_short_and_clean(self):
        s = lambda q: core._short_query(self.cfg, q)
        self.assertEqual(s("24 7 halloween ambience spooky lofi to get chills to lofi hip hop beats"), "lofi hip hop beats")
        self.assertEqual(s("shobhinaw satyaa, chandan anand, vihaan"), "shobhinaw satyaa")
        self.assertLessEqual(len(s("why german pows thought america was staged for them").split()), 5)
        self.assertEqual(s("liquid dnb mix"), "liquid dnb mix")

    def test_finds_new_viral_similar_channels_in_other_language(self):
        used0 = core.quota_status()["used"]
        ref = core.profile_channel(self.yt, self.cfg, core.resolve_channel(self.yt, "https://www.youtube.com/@uca"))
        self.assertLessEqual(core.quota_status()["used"] - used0, 4)    # analisar o canal é barato
        plan = core.link_plan(self.cfg, ref, ["pt"])
        self.assertIn("atmospheric jungle dnb mix", plan["base"])
        self.assertEqual(plan["duration"], "long")
        crit = core.criteria(self.cfg, None, "radar")
        res, funnel, extra = core.similar_from_link(self.yt, self.cfg, ref, plan["base"], ["pt"], crit,
                                                    True, plan["duration"])
        titles = [r["title"] for r in res]
        self.assertIn("Selva Atmosférica", titles)
        self.assertNotIn("Neon Jungle", titles)                          # o próprio canal não volta
        self.assertTrue(all(l == "pt" for _ in extra["queries"] for l in [_["lang"]]))
        self.assertTrue(all("musica" in r["genres"] for r in res))
        self.assertEqual(res[0]["similar_to"], ["Neon Jungle"])
        self.assertEqual([r["viral"] for r in res], sorted((r["viral"] for r in res), reverse=True))
        self.assertEqual(funnel["aprovados"], len(res))


class SecurityTest(unittest.TestCase):
    """A chave nunca pode sair do arquivo .env: nem para o navegador, nem para cache, nem para mensagens de erro."""

    FAKE_KEY = "AIzaSyTESTE_chave_falsa_1234567890abcd"

    def setUp(self):
        import os
        import threading
        import server
        self.tmp = use_temp_storage()
        self._env_file, self._env_var = core.ENV_FILE, os.environ.pop("YT_API_KEY", None)
        core.ENV_FILE = self.tmp / "cfg" / ".env"
        server.FETCH = fake_api
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        import os
        self.srv.shutdown()
        self.srv.server_close()
        core.ENV_FILE = self._env_file
        if self._env_var is not None:
            os.environ["YT_API_KEY"] = self._env_var
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path, body=None, headers=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_key_file_is_private(self):
        import os
        core.save_api_key(self.FAKE_KEY)
        if os.name != "nt":
            self.assertEqual(core.ENV_FILE.stat().st_mode & 0o777, 0o600)
            self.assertEqual(core.ENV_FILE.parent.stat().st_mode & 0o777, 0o700)

    def test_key_never_returned_to_browser_nor_cached(self):
        code, _ = self.req("/api/key", {"key": self.FAKE_KEY})
        self.assertEqual(code, 200)
        code, body = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertNotIn(self.FAKE_KEY, body)
        self.assertIn('"has_key": true', body)
        cfg = core.load_config()
        core.full_scan(core.YouTube(self.FAKE_KEY, fetch=fake_api), cfg, ["en"], ["musica"], core.criteria(cfg))
        for f in self.tmp.rglob("*"):
            if f.is_file() and f != core.ENV_FILE:
                self.assertNotIn(self.FAKE_KEY, f.read_text(errors="ignore"), f"chave vazou em {f}")

    def test_errors_are_redacted(self):
        core.save_api_key(self.FAKE_KEY)
        msg = core.redact(f"falhou em https://www.googleapis.com/youtube/v3/search?q=x&key={self.FAKE_KEY}")
        self.assertNotIn(self.FAKE_KEY, msg)

    def test_other_sites_are_blocked(self):
        code, _ = self.req("/api/key", {"key": self.FAKE_KEY}, {"Origin": "https://site-malicioso.com"})
        self.assertEqual(code, 403)                                    # CSRF
        code, _ = self.req("/api/state", headers={"Host": f"site-malicioso.com:{self.port}"})
        self.assertEqual(code, 403)                                    # DNS rebinding

    def test_plan_previews_queries_without_quota(self):
        code, body = self.req("/api/plan", {"keywords": "jungle mix, late night", "langs": ["en", "pt"], "translate": True})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual(len(plan["queries"]), 4)
        self.assertEqual(plan["cost"], 400)
        self.assertEqual(core.quota_status()["used"], 0)

    def test_radar_plan_and_job_never_expose_key(self):
        code, body = self.req("/api/plan", {"radar": True, "langs": ["en"], "genres": ["musica"], "week": True})
        self.assertEqual(code, 200)
        plan = json.loads(body)
        self.assertEqual(plan["cost"], len(plan["queries"]) * 100)
        self.assertEqual(core.quota_status()["used"], 0)                       # pré-visualização não gasta cota
        self.req("/api/key", {"key": self.FAKE_KEY})
        code, body = self.req("/api/radar", {"langs": ["en"], "genres": ["musica"], "criteria": {"min_viral": 0}})
        self.assertEqual(code, 200)
        jid = json.loads(body)["job"]
        import time
        for _ in range(50):
            code, body = self.req(f"/api/job?id={jid}")
            if json.loads(body)["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(body)["status"], "done", body)
        self.assertNotIn(self.FAKE_KEY, body)
        self.assertNotIn(self.FAKE_KEY, self.req("/api/state")[1])
        for f in self.tmp.rglob("*"):
            if f.is_file() and f != core.ENV_FILE:
                self.assertNotIn(self.FAKE_KEY, f.read_text(errors="ignore"), f"chave vazou em {f}")

    def test_link_endpoints(self):
        self.req("/api/key", {"key": self.FAKE_KEY})
        code, body = self.req("/api/channel_lookup", {"url": "https://www.youtube.com/@uca", "langs": ["pt"]})
        self.assertEqual(code, 200, body)
        r = json.loads(body)
        self.assertEqual(r["channel"]["id"], "UCa")
        self.assertTrue(r["queries"])
        self.assertNotIn(self.FAKE_KEY, body)
        code, body = self.req("/api/channel_lookup", {"url": "https://google.com"})
        self.assertEqual(code, 400)
        self.assertIn("link", json.loads(body)["error"])
        code, body = self.req("/api/similar_link", {"channel": "UCa", "queries": r["queries"], "langs": ["pt"],
                                                    "duration": r["duration"]})
        self.assertEqual(code, 200, body)
        import time
        jid = json.loads(body)["job"]
        for _ in range(50):
            body = self.req(f"/api/job?id={jid}")[1]
            if json.loads(body)["status"] != "running":
                break
            time.sleep(0.1)
        j = json.loads(body)
        self.assertEqual(j["status"], "done", body)
        self.assertIn("Selva Atmosférica", [x["title"] for x in j["results"]])
        self.assertNotIn(self.FAKE_KEY, body)

    def test_frontend_has_no_key_or_google_api_calls(self):
        web = Path(__file__).resolve().parent.parent / "web"
        for f in web.rglob("*"):
            if f.suffix in (".js", ".html", ".css"):
                src = f.read_text(encoding="utf-8")
                self.assertNotRegex(src, r"AIza[0-9A-Za-z_\-]{20,}", f)
                self.assertNotIn("googleapis.com", src, f"{f.name} não pode chamar a API do YouTube direto")


def serve_demo(port: int = 8799):
    """Sobe a interface com dados de demonstração."""
    import server
    use_temp_storage()
    cfg = core.load_config()
    yt = core.YouTube("fake", cache_hours=0, fetch=fake_api)
    res, _ = core.full_scan(yt, cfg, ["en", "pt"], list(cfg["categories"]), core.criteria(cfg), "video")
    core.save_results([r for r in res if r["id"] != "UCb"])  # UCb fica p/ o botão "parecidos" encontrar
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
