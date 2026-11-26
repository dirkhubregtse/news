#!/usr/bin/env python3
"""Film- en seriesuggesties voor ~/nieuws.

Haalt per genre goed beoordeelde films en series op via TMDB (Nederlandse intro,
genres, streamingdiensten in Nederland) en koppelt daar de IMDb-score aan uit het
openbare bestand title.ratings.tsv.gz van IMDb. Schrijft alles naar één JSON-bestand
dat build.py omzet in de pagina films.html.

Draait hooguit één keer per dag (films veranderen niet zo snel als nieuws).

Gebruik:  TMDB_API_KEY=... python films.py --uit archief-data/films.json
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TMDB = os.environ.get("TMDB_BASE", "https://api.themoviedb.org/3")
IMDB_RATINGS = os.environ.get("IMDB_RATINGS_URL", "https://datasets.imdbws.com/title.ratings.tsv.gz")
UA = "nieuws-tekst/1.0 (persoonlijke, niet-commerciële site)"

# Nederlandse genrenamen → TMDB-genre-id's voor films en voor series (None = bestaat niet voor series).
GENRES = {
    "actie": (28, 10759), "avontuur": (12, 10759), "animatie": (16, 16), "komedie": (35, 35),
    "misdaad": (80, 80), "documentaire": (99, 99), "drama": (18, 18), "familie": (10751, 10751),
    "fantasy": (14, 10765), "geschiedenis": (36, None), "horror": (27, None), "mysterie": (9648, 9648),
    "romantiek": (10749, None), "sciencefiction": (878, 10765), "thriller": (53, None),
    "oorlog": (10752, 10768), "western": (37, 37),
}
# TMDB-id → Nederlandse naam (voor de genres van een titel)
NAAM = {
    28: "actie", 12: "avontuur", 16: "animatie", 35: "komedie", 80: "misdaad", 99: "documentaire",
    18: "drama", 10751: "familie", 14: "fantasy", 36: "geschiedenis", 27: "horror", 10402: "muziek",
    9648: "mysterie", 10749: "romantiek", 878: "sciencefiction", 53: "thriller", 10752: "oorlog",
    37: "western", 10759: "actie & avontuur", 10765: "sciencefiction & fantasy", 10768: "oorlog & politiek",
    10762: "kinderen", 10763: "nieuws", 10764: "reality", 10766: "soap", 10767: "talkshow", 10770: "tv-film",
}


class Tmdb:
    def __init__(self, sleutel: str):
        self.sleutel = sleutel
        self.bearer = len(sleutel) > 40  # v4 'read access token' i.p.v. v3 api_key

    def get(self, pad: str, **params) -> dict:
        if not self.bearer:
            params["api_key"] = self.sleutel
        url = f"{TMDB}{pad}?{urllib.parse.urlencode(params)}"
        kop = {"User-Agent": UA, "Accept": "application/json"}
        if self.bearer:
            kop["Authorization"] = f"Bearer {self.sleutel}"
        for poging in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=kop), timeout=20) as r:
                    return json.load(r)
            except urllib.error.HTTPError as fout:
                if fout.code == 429 and poging < 3:  # te veel verzoeken: even wachten
                    time.sleep(2 * (poging + 1))
                    continue
                if fout.code == 404:
                    return {}
                raise
            except (urllib.error.URLError, TimeoutError):
                if poging < 3:
                    time.sleep(1 + poging)
                    continue
                raise
        return {}


def imdb_scores(nodig: set[str]) -> dict[str, tuple[float, int]]:
    """Leest IMDb's dagelijkse ratingsbestand en houdt alleen de titels over die we nodig hebben."""
    req = urllib.request.Request(IMDB_RATINGS, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    uit: dict[str, tuple[float, int]] = {}
    with gzip.open(io.BytesIO(data), "rt", encoding="utf-8") as f:
        next(f, None)  # kopregel
        for regel in f:
            tconst, score, stemmen = regel.rstrip("\n").split("\t")
            if tconst in nodig:
                uit[tconst] = (float(score), int(stemmen))
    return uit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="feeds.toml")
    ap.add_argument("--uit", default="archief-data/films.json")
    ap.add_argument("--forceer", action="store_true", help="ook bijwerken als het bestand nog vers is")
    args = ap.parse_args()

    uit = Path(args.uit)
    nu = datetime.now(timezone.utc)
    if uit.exists() and not args.forceer:
        try:
            vorig = datetime.fromisoformat(json.loads(uit.read_text("utf-8"))["bijgewerkt"])
            if nu - vorig < timedelta(hours=20):
                print("Films zijn nog vers; niets te doen.")
                return 0
        except (KeyError, ValueError, json.JSONDecodeError):
            pass

    sleutel = os.environ.get("TMDB_API_KEY", "").strip()
    if not sleutel:
        print("Geen TMDB_API_KEY ingesteld; filmsuggesties worden overgeslagen.")
        return 0

    cfg = tomllib.loads(Path(args.config).read_text("utf-8")).get("films", {})
    genres = [g for g in cfg.get("genres", list(GENRES)) if g in GENRES]
    jaren = int(cfg.get("jaren_nieuw", 2))
    land = cfg.get("land", "NL")
    grens_nieuw = (date.today() - timedelta(days=365 * jaren)).isoformat()
    tmdb = Tmdb(sleutel)

    # 1. Kandidaten per genre, voor films en series, recent en all-time.
    zoekopdrachten = []
    for g in genres:
        film_id, serie_id = GENRES[g]
        for soort, gid in (("movie", film_id), ("tv", serie_id)):
            if gid is None:
                continue
            datumveld = "primary_release_date.gte" if soort == "movie" else "first_air_date.gte"
            for pagina in (1, 2):
                zoekopdrachten.append((soort, {"with_genres": gid, "sort_by": "vote_count.desc",
                                               datumveld: grens_nieuw, "vote_count.gte": 50, "page": pagina}))
            for pagina in (1, 2, 3):
                zoekopdrachten.append((soort, {"with_genres": gid, "sort_by": "vote_average.desc",
                                               "vote_count.gte": 1500 if soort == "movie" else 400, "page": pagina}))

    def ontdek(z):
        soort, params = z
        return soort, tmdb.get(f"/discover/{soort}", include_adult="false", language="nl-NL", **params).get("results", [])

    kandidaten: set[tuple[str, int]] = set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for soort, res in pool.map(ontdek, zoekopdrachten):
            kandidaten |= {(soort, r["id"]) for r in res}
    print(f"{len(kandidaten)} kandidaten via TMDB")

    # 2. Details: Nederlandse intro, IMDb-id, streamingdiensten.
    def details(k):
        soort, tid = k
        d = tmdb.get(f"/{soort}/{tid}", language="nl-NL", append_to_response="external_ids,watch/providers")
        if not d:
            return None
        imdb_id = d.get("imdb_id") or d.get("external_ids", {}).get("imdb_id")
        if not imdb_id:
            return None
        intro = (d.get("overview") or "").strip()
        if not intro:  # geen Nederlandse tekst: val terug op Engels
            intro = (tmdb.get(f"/{soort}/{tid}", language="en-US").get("overview") or "").strip()
        aanbod = d.get("watch/providers", {}).get("results", {}).get(land, {})
        diensten = [p["provider_name"] for p in aanbod.get("flatrate", []) + aanbod.get("free", []) + aanbod.get("ads", [])]
        datum = d.get("release_date") or d.get("first_air_date") or ""
        return {
            "imdb_id": imdb_id, "tmdb": tid, "soort": "film" if soort == "movie" else "serie",
            "titel": d.get("title") or d.get("name") or "",
            "origineel": d.get("original_title") or d.get("original_name") or "",
            "datum": datum, "jaar": int(datum[:4]) if datum[:4].isdigit() else None,
            "genres": [NAAM.get(g["id"], g.get("name", "").lower()) for g in d.get("genres", [])],
            "intro": intro, "diensten": list(dict.fromkeys(diensten)),
            "kijklink": aanbod.get("link", ""), "tmdb_score": d.get("vote_average"), "tmdb_stemmen": d.get("vote_count", 0),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        titels = [t for t in pool.map(details, sorted(kandidaten)) if t and t["titel"]]
    print(f"{len(titels)} titels met details")

    # 3. IMDb-scores koppelen.
    scores = imdb_scores({t["imdb_id"] for t in titels})
    for t in titels:
        t["imdb"], t["stemmen"] = scores.get(t["imdb_id"], (None, 0))
    titels = [t for t in titels if t["imdb"] is not None]

    uit.parent.mkdir(parents=True, exist_ok=True)
    uit.write_text(json.dumps({"bijgewerkt": nu.isoformat(timespec="seconds"), "land": land, "grens_nieuw": grens_nieuw,
                               "titels": sorted(titels, key=lambda t: -t["imdb"])}, ensure_ascii=False, indent=0), "utf-8")
    print(f"✓ {len(titels)} films en series met IMDb-score → {uit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
