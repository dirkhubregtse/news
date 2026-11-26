#!/usr/bin/env python3
"""DIRGE — persoonlijke startpagina van alleen tekst: home, films en nieuws.

Haalt nieuws uit RSS/Atom-feeds en maakt er 'de tien van vandaag' van; de filmpagina
komt uit films.json (zie films.py).

Gebruik:  python build.py                         (schrijft naar ./site)
          python build.py --archief archief-data  (bewaart berichten voor het archief)
          python build.py --offline map/met/xml   (test met lokale feedbestanden)

Alleen de standaardbibliotheek van Python 3.11+, geen extra pakketten.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import tomllib
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime.now(timezone.utc)
MAX_AGE = timedelta(hours=48)
UA = "Mozilla/5.0 (compatible; nieuws-tekst/1.0; +https://github.com/)"

DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
DAGEN_LANG = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

STOP = set("""
de het een en of van in op te met voor door aan bij uit over naar als dat die dit
is zijn was waren wordt worden werd heeft hebben had niet nog ook maar dan om tot
er hij zij ze we wij je jij u ik zich meer na al wel geen kan moet zal gaat
the a an and or of in on to for with by at from as that this is are was were be
been has have had not but its it his her their they we you new says said after
over into about than more will can
""".split())

# Woorden met een hoofdletter die geen naam zijn (voor het herkennen van namen).
GEEN_NAAM = set("""
de het een en of in op dat deze die dit dan ook maar volgens na bij als wat wie waarom hoe
the a an and of in on at to for with as but this that these those what why how when after
i we he she it they his her its our their mr mrs ms dr
""".split())


@dataclass
class Item:
    bron: str
    titel: str
    link: str
    tijd: datetime
    samenvatting: str = ""
    categorieen: tuple[str, ...] = ()
    tekst: tuple[str, ...] = ()      # volledige tekst in alinea's (alleen bij open licentie)
    auteur: str = ""
    licentie: str = ""
    rubriek: str = ""
    zoekdata: str = ""               # feedbeschrijving, alleen in geheugen voor het samenvoegen

    @property
    def tokens(self) -> set[str]:
        woorden = re.findall(r"[a-z0-9à-ÿ]+", self.titel.lower())
        # eerste 6 letters als simpele 'stam': kabinet ≈ kabinetsplan, betaalbare ≈ betaalbaarder
        return {w[:6] for w in woorden if len(w) >= 3 and w not in STOP}

    @property
    def namen(self) -> set[str]:
        """Namen en getallen uit kop + beschrijving; werkt ook tussen Nederlands en Engels."""
        uit: set[str] = set()
        for zin in re.split(r"(?<=[.!?:;])\s+|\s+[–—-]\s+", self.titel + ". " + self.zoekdata[:500]):
            woorden = re.findall(r"[\wÀ-ÿ][\wÀ-ÿ'’.-]*", zin)
            for i, w in enumerate(woorden):
                w = re.sub(r"['’]s$", "", w).strip(".-'’")
                if re.fullmatch(r"\d[\d.,]*", w):
                    if len(w) >= 2:
                        uit.add(w)
                elif i > 0 and w[:1].isupper() and len(w) >= 3 and w.lower() not in GEEN_NAAM:
                    uit.add(w.lower())
        return uit


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    namen: set[str] = field(default_factory=set)

    @property
    def hoofd(self) -> Item:
        return self.items[0]

    @property
    def bronnen(self) -> list[str]:
        return list(dict.fromkeys(it.bron for it in self.items))

    @property
    def tijd(self) -> datetime:
        return max(it.tijd for it in self.items)

    @property
    def id(self) -> str:
        return "a-" + hashlib.sha1(self.hoofd.link.encode()).hexdigest()[:10]

    @property
    def pagina(self) -> str:
        return self.id + ".html"

    def gewicht(self, ref: datetime) -> float:
        """Hoe belangrijk: aantal bronnen, afnemend met de leeftijd (halveert per 14 uur)."""
        n = len(self.bronnen)
        uren = max((ref - self.tijd).total_seconds() / 3600, 0)
        return (n + 0.25 * (len(self.items) - n)) * 0.5 ** (uren / 14)


# ── ophalen & parsen ────────────────────────────────────────────────────────

def ophalen(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(5_000_000)


def lokaal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def kind_tekst(el: ET.Element, *namen: str) -> str:
    for kind in el:
        if lokaal(kind.tag) in namen:
            tekst = "".join(kind.itertext()).strip()
            if tekst:
                return tekst
    return ""


def kind_link(el: ET.Element) -> str:
    beste = ""
    for kind in el:
        if lokaal(kind.tag) != "link":
            continue
        href = kind.get("href")
        if href:  # Atom
            if kind.get("rel", "alternate") == "alternate":
                return href
            beste = beste or href
        elif (kind.text or "").strip():
            return kind.text.strip()
    return beste or kind_tekst(el, "guid")


def lees_datum(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def schoon(s: str, maxlen: int = 0) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s)))
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r" ([.,;:!?)])", r"\1", s)
    if maxlen and len(s) > maxlen:
        s = s[: maxlen].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def veilige_link(url: str) -> str:
    url = url.strip()
    return url if url.startswith(("https://", "http://")) else ""


def alineas(raw: str) -> tuple[str, ...]:
    if "&lt;" in raw:
        raw = html.unescape(raw)
    delen = re.split(r"(?i)</p>|<br\s*/?>|</h[1-6]>|</li>|\n\s*\n", raw)
    return tuple(a for a in (schoon(d) for d in delen) if len(a) > 1)


def parse_feed(data: bytes, bron: str, niveau: str = "kop", licentie: str = "") -> list[Item]:
    """niveau: 'kop' (alleen kop), 'samenvatting' (kop + korte samenvatting)
    of 'volledig' (hele tekst; alleen voor bronnen met een open licentie)."""
    root = ET.fromstring(data)
    items: list[Item] = []
    for el in root.iter():
        if lokaal(el.tag) not in ("item", "entry"):
            continue
        titel = schoon(kind_tekst(el, "title"))
        link = veilige_link(kind_link(el))
        if not titel or not link:
            continue
        tijd = lees_datum(kind_tekst(el, "pubdate", "published", "updated", "date", "issued")) or NOW
        if tijd > NOW + timedelta(hours=1):
            tijd = NOW
        beschrijving = schoon(kind_tekst(el, "description", "summary"))
        # Alleen een korte samenvatting; sommige feeds zetten er het hele artikel in.
        samenvatting = schoon(beschrijving, 320)
        if samenvatting.lower().startswith(titel.lower()[:40]):
            samenvatting = ""
        cats = tuple(schoon(c.text or "") for c in el if lokaal(c.tag) == "category" and (c.text or "").strip())
        tekst: tuple[str, ...] = ()
        if niveau == "kop":
            samenvatting = ""
        elif niveau == "volledig":
            for kind in el:
                if lokaal(kind.tag) in ("encoded", "content", "description", "summary") and "".join(kind.itertext()).strip():
                    kandidaat = alineas("".join(kind.itertext()))
                    if sum(map(len, kandidaat)) > sum(map(len, tekst)):
                        tekst = kandidaat
        auteur = schoon(kind_tekst(el, "creator", "author"))
        items.append(Item(bron, titel, link, tijd, samenvatting, cats, tekst, auteur, licentie, zoekdata=beschrijving))
    return items


# ── groeperen ───────────────────────────────────────────────────────────────

def lijkt_op(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    gedeeld = len(a & b)
    return (gedeeld >= 2 and gedeeld / min(len(a), len(b)) >= 0.5) or gedeeld / len(a | b) >= 0.45


def groepeer(items: list[Item]) -> list[Cluster]:
    """Voegt berichten over hetzelfde onderwerp samen: op woorden in de kop, of op
    gedeelde namen/getallen (zo vinden ook een Nederlandse en een Engelse kop elkaar)."""
    namen = {id(it): it.namen for it in items}
    df = Counter(n for it in items for n in namen[id(it)])
    te_vaak = max(4, int(len(items) * 0.06))  # 'Trump', 'kabinet': te algemeen om op te koppelen
    for it in items:
        namen[id(it)] = {n for n in namen[id(it)] if df[n] <= te_vaak}

    clusters: list[Cluster] = []
    for it in sorted(items, key=lambda i: i.tijd, reverse=True):
        tok, nm = it.tokens, namen[id(it)]
        gekozen = None
        for c in clusters:
            if it.bron in c.bronnen:
                # Zelfde bron: alleen samenvoegen als de koppen bijna gelijk zijn (bijgewerkt bericht).
                ct = c.hoofd.tokens
                if tok and ct and len(tok & ct) / len(tok | ct) >= 0.6:
                    gekozen = c
                    break
                continue
            if lijkt_op(tok, c.tokens):
                gekozen = c
                break
                continue  # op namen koppelen we alleen berichten van verschillende bronnen
            gedeeld = nm & c.namen
            uren = abs((c.tijd - it.tijd).total_seconds()) / 3600
            if len(gedeeld) >= 2 and uren <= 18:
                gekozen = c
                break
            if len(gedeeld) == 1 and uren <= 8:
                n = next(iter(gedeeld))
                if len(n) >= 5 and not n[0].isdigit() and df[n] <= 3:
                    gekozen = c
                    break
        if gekozen:
            gekozen.items.append(it)
            gekozen.tokens |= tok
            gekozen.namen |= nm
        else:
            clusters.append(Cluster([it], set(tok), set(nm)))
    return clusters


# ── edities ─────────────────────────────────────────────────────────────────

def editie_info(tijden: list[str]) -> tuple[datetime, str, str]:
    """Geeft (tijdstip editie, naam, tekst over de volgende). Zonder edities: live."""
    if not tijden:
        return NOW, "live", ""
    lokaal_nu = NOW.astimezone(TZ)
    momenten = []
    for dag in (lokaal_nu.date() - timedelta(days=1), lokaal_nu.date(), lokaal_nu.date() + timedelta(days=1)):
        for t in tijden:
            u, m = map(int, t.split(":"))
            momenten.append(datetime.combine(dag, time(u, m), TZ))
    momenten.sort()
    vorige = max(m for m in momenten if m <= lokaal_nu)
    volgende = min(m for m in momenten if m > lokaal_nu)
    naam = "ochtendeditie" if vorige.hour < 11 else "middageditie" if vorige.hour < 17 else "avondeditie"
    return vorige.astimezone(timezone.utc), naam, f"volgende om {volgende:%H:%M}"


# ── archief ─────────────────────────────────────────────────────────────────

def naar_dict(it: Item) -> dict:
    d = {"r": it.rubriek, "b": it.bron, "t": it.titel, "l": it.link, "ts": int(it.tijd.timestamp()),
         "s": it.samenvatting, "c": list(it.categorieen), "x": list(it.tekst), "a": it.auteur, "lic": it.licentie}
    return {k: v for k, v in d.items() if v not in ("", [], None)}


def van_dict(d: dict) -> Item:
    return Item(d["b"], d["t"], d["l"], datetime.fromtimestamp(d["ts"], timezone.utc), d.get("s", ""),
                tuple(d.get("c", ())), tuple(d.get("x", ())), d.get("a", ""), d.get("lic", ""), d["r"])


def lokale_dag(d: datetime) -> date:
    return d.astimezone(TZ).date()


# ── weergave ────────────────────────────────────────────────────────────────

e = lambda s: html.escape(s, quote=True)


def klok(d: datetime) -> str:
    return d.astimezone(TZ).strftime("%H:%M")


def leeftijd(d: datetime) -> str:
    m = int((NOW - d).total_seconds() // 60)
    if m < 60:
        return f"{max(m, 1)}m"
    if m < 60 * 24:
        return f"{m // 60}u"
    return f"{m // 1440}d"


def datum_lang(d: datetime) -> str:
    d = d.astimezone(TZ)
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} · {d:%H:%M}"


def dag_naam(d: date, lang: bool = False) -> str:
    vandaag = NOW.astimezone(TZ).date()
    if d == vandaag:
        return "vandaag"
    if d == vandaag - timedelta(days=1):
        return "gisteren"
    if lang:
        return f"{DAGEN_LANG[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d.year}"
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def meta(c: Cluster, met_tijd: bool = True) -> str:
    n = len(c.bronnen)
    namen = ", ".join(c.bronnen[:3]) + (f" +{n - 3}" if n > 3 else "")
    uit = f"{e(namen)} · {n} {'bron' if n == 1 else 'bronnen'}"
    return uit + (f" · {leeftijd(c.tijd)}" if met_tijd else "")


def kop_link(c: Cluster, href: str | None = None) -> str:
    """De kop als link, met data-id (gelezen) en data-t (nieuw sinds vorig bezoek)."""
    return (f'<a class="kop" href="{e(href or c.pagina)}" data-id="{c.id}" '
            f'data-t="{int(c.tijd.timestamp())}">{e(c.hoofd.titel)}</a>')


def dekking_html(c: Cluster) -> str:
    if len(c.items) < 2:
        return ""
    rijen = "".join(
        f'<li><span class="src">{e(it.bron)}</span><a href="{e(it.link)}" rel="noopener">{e(it.titel)}</a><span class="t">{klok(it.tijd)}</span></li>'
        for it in c.items[1:]
    )
    return f'<details><summary>zo brengen de anderen het ({len(c.items) - 1})</summary><ul class="dekking">{rijen}</ul></details>'


def cluster_html(c: Cluster, uitgebreid: bool = False, href: str | None = None, met_tijd: bool = True) -> str:
    uit = [kop_link(c, href)]
    if uitgebreid and c.hoofd.samenvatting:
        uit.append(f'<p class="sam">{e(c.hoofd.samenvatting)}</p>')
    uit.append(f'<div class="meta">{meta(c, met_tijd)}</div>')
    uit.append(dekking_html(c))
    return '<div class="item rij">' + "".join(uit) + "</div>"


KLAPBORD = [
    "..aabbaabbaabbaa",
    ".aabbaabbaabbaa.",
    "................",
    "aabbaabbaabbaabb",
    "abbaabbaabbaabba",
    "aaaaaaaaaaaaaaaa",
    "a..............a",
    "a.cccc.ccc.cc..a",
    "a..............a",
    "a.cc.cccc.ccc..a",
    "a..............a",
    "aaaaaaaaaaaaaaaa",
]


# Woordmerk DIRGE in 8-bit stijl; elke rij krijgt een eigen tint (verloop licht → donker).
DIRGE = [
    ".########....###...########......#########...#########",
    "##########...###..##########....##########..##########",
    "###....####..###..###....####..####.........###.......",
    "###.....###..###..###.....###..###..........###.......",
    "###.....###..###..###....####..###..........###.......",
    "###.....###..###..##########...###...#####..########..",
    "###.....###..###..#########....###...#####..########..",
    "###.....###..###..###..####....###.....###..###.......",
    "###.....###..###..###...####...###.....###..###.......",
    "###....####..###..###....###...####....###..###.......",
    "##########...###..###....####...##########..##########",
    ".########....###..###.....###....#########...#########",
    "###...........#...........###..........##...##........",
]


def pixel_logo(raster: list[str], rij_klasse: bool = False, label: str = "") -> str:
    """Pixelart als SVG; kleuren komen uit het thema (CSS-variabelen).
    rij_klasse=True: elke rij krijgt klasse d0, d1, … voor een kleurverloop."""
    rects = []
    for y, rij in enumerate(raster):
        x = 0
        while x < len(rij):
            k = rij[x]
            lengte = 1
            while x + lengte < len(rij) and rij[x + lengte] == k:
                lengte += 1
            if k != ".":
                klasse = f"d{y}" if rij_klasse else f"px-{k}"
                rects.append(f'<rect class="{klasse}" x="{x}" y="{y}" width="{lengte}" height="1"/>')
            x += lengte
    b, h = len(raster[0]), len(raster)
    toegankelijk = f'role="img" aria-label="{label}"' if label else 'aria-hidden="true"'
    return (f'<svg class="{"woordmerk" if rij_klasse else "pixellogo"}" viewBox="0 0 {b} {h}" width="{b * 3}" height="{h * 3}" '
            f'shape-rendering="crispEdges" {toegankelijk}>{"".join(rects)}</svg>')


THEMAS = ["tokyo-night", "catppuccin", "gruvbox", "rose-pine", "latte"]

CSS = """
:root{--bg:#1a1b26;--fg:#a9b1d6;--br:#c0caf5;--dim:#787fa6;--ln:#292e42;--ac:#7aa2f7;--ac2:#9ece6a;--sel:#283457;--rood:#f7768e;color-scheme:dark}
[data-theme=catppuccin]{--bg:#1e1e2e;--fg:#bac2de;--br:#cdd6f4;--dim:#8a8fa8;--ln:#313244;--ac:#89b4fa;--ac2:#a6e3a1;--sel:#313244;--rood:#f38ba8}
[data-theme=gruvbox]{--bg:#282828;--fg:#d5c4a1;--br:#ebdbb2;--dim:#a19480;--ln:#3c3836;--ac:#83a598;--ac2:#b8bb26;--sel:#3c3836;--rood:#fb4934}
[data-theme=rose-pine]{--bg:#191724;--fg:#c8c4d9;--br:#e0def4;--dim:#8b87a3;--ln:#26233a;--ac:#9ccfd8;--ac2:#f6c177;--sel:#26233a;--rood:#eb6f92}
[data-theme=latte]{--bg:#eff1f5;--fg:#4c4f69;--br:#303446;--dim:#5f6278;--ln:#ccd0da;--ac:#1e56d6;--ac2:#2f7a1f;--sel:#dce0e8;--rood:#b3122f;color-scheme:light}
*{box-sizing:border-box}
html{background:var(--bg);font-size:14px}
body{margin:0;background:var(--bg);color:var(--fg);font:1rem/1.6 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,summary:focus-visible,button:focus-visible,input:focus-visible{outline:1px solid var(--ac);outline-offset:2px}
button{font:inherit;background:none;border:0;color:inherit;cursor:pointer;padding:0}
.bar{border-bottom:1px solid var(--ln);padding:.4rem 1.7rem;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:.5rem 1.2rem;font-size:.86rem;color:var(--dim)}
.bar nav{display:flex;flex-wrap:wrap;gap:.3rem}
.bar nav a{padding:.2rem .7rem}.bar nav a.on{background:var(--sel);color:var(--br)}
.knoppen{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem}
.knoppen button,.knoppen a{padding:.2rem .6rem;min-height:2rem;color:var(--dim)}
.knoppen button:hover,.knoppen a:hover{color:var(--br);background:var(--sel);text-decoration:none}
#thema{color:var(--fg)}
.wrap{max-width:1200px;margin:0 auto;padding:2rem 1.7rem 4.5rem}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:.3rem 1.2rem;padding-bottom:1.15rem;border-bottom:1px solid var(--ln)}
.logo{font-size:1.57rem;font-weight:700;letter-spacing:-.5px;color:var(--br);display:flex;align-items:center;gap:.85rem}.logo span{color:var(--ac)}
.home{min-height:64vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2.8rem;padding:3rem 0}
.woordmerk{width:min(90vw,42rem);height:auto}
.d0,.d1{fill:var(--br)}.d2,.d3{fill:color-mix(in srgb,var(--br) 72%,var(--ac))}.d4{fill:color-mix(in srgb,var(--br) 45%,var(--ac))}
.d5{fill:color-mix(in srgb,var(--br) 20%,var(--ac))}.d6,.d7{fill:var(--ac)}.d8{fill:color-mix(in srgb,var(--ac) 82%,var(--bg))}
.d9{fill:color-mix(in srgb,var(--ac) 68%,var(--bg))}.d10,.d11{fill:color-mix(in srgb,var(--ac) 54%,var(--bg))}.d12{fill:color-mix(in srgb,var(--ac) 38%,var(--bg))}
.home-links{display:flex;flex-wrap:wrap;justify-content:center;gap:1rem 3.4rem;letter-spacing:.35em;text-transform:uppercase}
.home-links a.kop{color:var(--ac);font-weight:500;padding:.5rem .3rem .5rem .65rem}.home-links a.kop:hover{color:var(--br);text-decoration:none}
.pixellogo{height:2.1em;width:auto;flex:none}.px-a{fill:var(--ac)}.px-b{fill:var(--br)}.px-c{fill:var(--ac2)}
.sub{color:var(--dim)}.sub b{color:var(--fg);font-weight:500}
.label{font-size:.86rem;color:var(--ac2);margin-bottom:.85rem}
section.blok{padding:2.3rem 0 2.6rem;border-bottom:1px solid var(--ln)}
ol.groot,ol.rest,ol.top10{list-style:none;margin:0;padding:0}
ol.groot{display:flex;flex-direction:column;max-width:70rem}
ol.groot li{display:flex;gap:1.15rem;padding:1rem 0;border-bottom:1px solid var(--ln)}
ol.groot li:first-child{padding-top:0}ol.groot li:last-child{border-bottom:0}
ol.groot .kop{font-size:1.2rem;font-weight:700;color:var(--br)}
ol.groot .nr{color:var(--ac);flex:none;width:1.6rem;font-weight:700}
ol.rest{display:grid;grid-template-columns:repeat(auto-fill,minmax(24rem,1fr));gap:1.15rem 3.4rem}
ol.rest li{display:flex;gap:1.15rem}
.t{color:var(--dim);flex:none;width:3.1rem}
.kop{color:var(--br);font-weight:500}
.sam{margin:.3rem 0;max-width:72ch}
.rubrieken{display:grid;grid-template-columns:repeat(auto-fill,minmax(23rem,1fr));gap:2.8rem 3.4rem;padding-top:2.8rem}
h2{margin:0 0 1rem;font-size:1rem;font-weight:500;color:var(--ac)}h2 span{color:var(--dim);font-weight:400}
.lijst{display:flex;flex-direction:column;gap:1.15rem}
.meta,.meer{font-size:.86rem;color:var(--dim)}
.meer{display:inline-block;margin-top:1.15rem}.meer:hover{color:var(--ac)}
details{font-size:.86rem;color:var(--dim);margin-top:2px}
summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary::before{content:"+ "}details[open] summary::before{content:"− "}
.dekking{list-style:none;margin:.4rem 0 0;padding:0;border-top:1px solid var(--ln)}
.dekking li{display:flex;gap:.85rem;padding:.4rem 0;border-bottom:1px solid var(--ln);color:var(--fg)}
.dekking .src{width:7rem;flex:none;color:var(--ac)}.dekking a{flex:1;min-width:0}.dekking .t{width:auto}
.dag{margin:2.8rem 0 1rem;font-size:.86rem;color:var(--ac2)}.dag:first-child{margin-top:0}
.smal{max-width:58rem;padding-top:2.8rem}
.intro-tekst{color:var(--dim);margin:0 0 2rem;max-width:68ch}
.artikel{max-width:72ch;padding-top:2.8rem}
.terug{display:inline-block;font-size:.86rem;color:var(--dim);margin-bottom:1.7rem}.terug:hover{color:var(--ac)}
.artikel h1{margin:0 0 1.4rem;font-size:1.85rem;line-height:1.35;color:var(--br)}
.tekst p{margin:0 0 1.15rem;font-size:1.07rem;line-height:1.75}.tekst{margin-bottom:1.7rem}
.tekst .intro{font-size:1.07rem}
.tekst .bronregel{font-size:.86rem;color:var(--dim);border-left:2px solid var(--ln);padding-left:.85rem}
.lees{font-size:.86rem;color:var(--ac)}.lees.groot{display:inline-block;font-size:1rem;border:1px solid var(--ln);padding:.55rem 1rem}
.lees.groot:hover{background:var(--sel);text-decoration:none}
.anderen{display:flex;flex-direction:column;border-top:1px solid var(--ln)}
.ander{padding:1.15rem 0;border-bottom:1px solid var(--ln)}.ander .src{color:var(--ac)}
.ander .kop{margin:2px 0}.ander p{margin:.3rem 0 .4rem}
.gelezen .kop,.gelezen.kop{color:var(--dim)!important;font-weight:400!important}
.nieuw>.kop::before,.nieuw .kop:first-child::before{content:"▪ ";color:var(--ac2)}
a.kop.cursor{outline:1px solid var(--ac);outline-offset:3px}
footer{margin-top:4rem;padding-top:1.4rem;border-top:1px solid var(--ln);font-size:.86rem;color:var(--dim);display:flex;flex-direction:column;gap:.85rem}
footer .stuk{color:var(--rood)}
#hulp{position:fixed;right:1.2rem;bottom:1.2rem;background:var(--bg);border:1px solid var(--ln);padding:1rem 1.2rem;font-size:.86rem;z-index:10;max-width:calc(100vw - 2.4rem)}
#hulp table{border-collapse:collapse}#hulp td{padding:.15rem .7rem .15rem 0}#hulp td:first-child{color:var(--ac);white-space:nowrap}
#hulp .label{margin-bottom:.5rem}
kbd{font:inherit}
.filters{display:flex;flex-direction:column;gap:.6rem;margin:0 0 2.3rem}
.filterrij{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem}
.filterrij .naam{width:6.5rem;color:var(--dim);font-size:.86rem}
.chip{padding:.15rem .6rem;min-height:1.9rem;font-size:.86rem;color:var(--dim);border:1px solid var(--ln)}
.chip:hover{color:var(--br)}.chip[aria-pressed=true]{color:var(--br);background:var(--sel);border-color:var(--sel)}
.film .score{font-size:.86rem;color:var(--dim);margin-top:.15rem}.film .score b{color:var(--ac2);font-weight:700}
.film .jaar{color:var(--dim);font-weight:400}
.filmlijst{display:grid;grid-template-columns:repeat(auto-fill,minmax(30rem,1fr));gap:1.7rem 3.4rem}
.leeg{color:var(--dim)}
@media (max-width:640px){.wrap{padding:1.7rem 1.15rem 3.4rem}.bar{padding:.4rem 1.15rem}.dekking .src{width:5.7rem}ol.groot .kop{font-size:1.1rem}}
"""

HEAD_JS = """(function(){try{var r=document.documentElement,t=localStorage.getItem("nieuws-thema"),f=localStorage.getItem("nieuws-fs");if(t&&t!=="tokyo-night")r.setAttribute("data-theme",t);if(f)r.style.fontSize=f+"px"}catch(x){}})();"""

JS = """
(function(){
var d=document,r=d.documentElement;
var LS={get:function(k,v){try{var x=localStorage.getItem(k);return x===null?v:x}catch(e){return v}},set:function(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
var THEMAS=%THEMAS%;
function thema(t){if(THEMAS.indexOf(t)<0)t=THEMAS[0];if(t===THEMAS[0])r.removeAttribute("data-theme");else r.setAttribute("data-theme",t);var b=d.getElementById("thema");if(b)b.textContent="◐ "+t;LS.set("nieuws-thema",t)}
function volgendThema(){var t=LS.get("nieuws-thema",THEMAS[0]);thema(THEMAS[(THEMAS.indexOf(t)+1)%THEMAS.length])}
var FS=[12,13,14,15,16,18,20];
function stap(n){var i=FS.indexOf(+LS.get("nieuws-fs",14));if(i<0)i=2;i=Math.max(0,Math.min(FS.length-1,i+n));r.style.fontSize=FS[i]+"px";LS.set("nieuws-fs",FS[i])}
thema(LS.get("nieuws-thema",THEMAS[0]));
d.getElementById("thema").onclick=volgendThema;
d.getElementById("kleiner").onclick=function(){stap(-1)};
d.getElementById("groter").onclick=function(){stap(1)};
var hulp=d.getElementById("hulp");
d.getElementById("hulpknop").onclick=function(){hulp.hidden=!hulp.hidden};

/* gelezen en nieuw: alleen in deze browser, er gaat niets naar een server */
var gel={};try{JSON.parse(LS.get("nieuws-gelezen","[]")).forEach(function(i){gel[i]=1})}catch(e){}
function bewaar(){var k=Object.keys(gel);if(k.length>3000)k=k.slice(k.length-3000);LS.set("nieuws-gelezen",JSON.stringify(k))}
var eigen=d.body.getAttribute("data-id");if(eigen){gel[eigen]=1;bewaar()}
d.addEventListener("click",function(ev){var a=ev.target.closest&&ev.target.closest("a[data-id]");if(a){gel[a.getAttribute("data-id")]=1;bewaar()}});
var vorig=null;try{vorig=sessionStorage.getItem("nieuws-vorig")}catch(e){}
if(vorig===null){vorig=LS.get("nieuws-bezoek","");try{sessionStorage.setItem("nieuws-vorig",vorig)}catch(e){}}
LS.set("nieuws-bezoek",String(Math.floor(Date.now()/1000)));
d.querySelectorAll("a.kop[data-id]").forEach(function(a){var rij=a.closest(".rij")||a;
 if(gel[a.getAttribute("data-id")])rij.classList.add("gelezen");
 else if(vorig&&+a.getAttribute("data-t")>+vorig)rij.classList.add("nieuw")});

/* toetsenbord */
function ga(n){var lijst=[].filter.call(d.querySelectorAll("a.kop"),function(a){return a.offsetParent!==null});if(!lijst.length)return;
 var nu=d.querySelector("a.kop.cursor"),pos=lijst.indexOf(nu);pos=pos<0?(n>0?0:lijst.length-1):Math.max(0,Math.min(lijst.length-1,pos+n));
 if(nu)nu.classList.remove("cursor");var a=lijst[pos];a.classList.add("cursor");
 a.focus({preventScroll:true});a.scrollIntoView({block:"nearest"})}
d.addEventListener("keydown",function(ev){
 if(ev.ctrlKey||ev.metaKey||ev.altKey)return;
 var t=ev.target;if(t&&(t.tagName==="INPUT"||t.tagName==="TEXTAREA"||t.isContentEditable)){if(ev.key==="Escape")t.blur();return}
 var k=ev.key;
 if(k==="j")ga(1);else if(k==="k")ga(-1);
 else if(k==="?")hulp.hidden=!hulp.hidden;
 else if(k==="Escape")hulp.hidden=true;
 else if(k==="t")volgendThema();
 else if(k==="+"||k==="=")stap(1);else if(k==="-")stap(-1);
 else if(k==="h")history.back();
 else if(k==="o"){var b=d.querySelector(".lees.groot");if(b)location.href=b.href;else return}
 else if(/^[1-9]$/.test(k)){var n=d.querySelectorAll(".bar nav a")[+k-1];if(n)location.href=n.href;else return}
 else return;
 ev.preventDefault()});
})();
""".replace("%THEMAS%", json.dumps(THEMAS))

FILM_JS = """
(function(){var d=document;
var LS={get:function(k,v){try{var x=localStorage.getItem(k);return x===null?v:JSON.parse(x)}catch(e){return v}},set:function(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
var st={soort:LS.get("f-soort",[]),genre:LS.get("f-genre",[]),dienst:LS.get("f-dienst",[])},geenAnim=LS.get("f-geen-animatie",false);
var items=[].slice.call(d.querySelectorAll(".film"));
function heeft(lijst,w){return lijst.indexOf(w)>=0}
function pas(){
 items.forEach(function(el){var g=el.dataset.genres.split("|"),dn=el.dataset.diensten.split("|");
  var ok=!(geenAnim&&(heeft(g,"animatie")||heeft(g,"kinderen")))&&(!st.soort.length||heeft(st.soort,el.dataset.soort))&&(!st.genre.length||st.genre.some(function(x){return heeft(g,x)}))&&(!st.dienst.length||st.dienst.some(function(x){return heeft(dn,x)}));
  el.hidden=!ok});
 ["nieuw","alltime"].forEach(function(id){var box=d.getElementById(id);if(!box)return;var n=box.querySelectorAll(".film:not([hidden])").length;
  var t=d.querySelector('.telling[data-voor="'+id+'"]');if(t)t.textContent=n+" titels";var p=box.nextElementSibling;if(p)p.hidden=n>0||!box.querySelector(".film")});
 d.querySelectorAll(".chip[data-f]").forEach(function(b){var f=b.dataset.f,w=b.dataset.w;b.setAttribute("aria-pressed",w===""?!st[f].length:heeft(st[f],w))});
 var ga=d.getElementById("geen-animatie");if(ga)ga.setAttribute("aria-pressed",geenAnim);LS.set("f-geen-animatie",geenAnim);
 LS.set("f-soort",st.soort);LS.set("f-genre",st.genre);LS.set("f-dienst",st.dienst)}
var gab=d.getElementById("geen-animatie");if(gab)gab.onclick=function(){geenAnim=!geenAnim;pas()};
d.querySelectorAll(".chip[data-f]").forEach(function(b){b.onclick=function(){var f=b.dataset.f,w=b.dataset.w;
 if(w==="")st[f]=[];else{var i=st[f].indexOf(w);if(i>=0)st[f].splice(i,1);else st[f].push(w)}pas()}});
pas();
})();
"""

HULP = """<div id="hulp" hidden><div class="label">▍ toetsen</div><table>
<tr><td>j / k</td><td>volgende / vorige link</td></tr>
<tr><td>Enter</td><td>open de gekozen link</td></tr>
<tr><td>o</td><td>open het artikel bij de bron</td></tr>
<tr><td>h</td><td>terug</td></tr>
<tr><td>1 – %N%</td><td>pagina's in de balk</td></tr>
<tr><td>t</td><td>ander thema</td></tr>
<tr><td>+ / −</td><td>tekst groter / kleiner</td></tr>
<tr><td>?</td><td>dit venster · Esc sluit</td></tr>
</table></div>"""


class Site:
    def __init__(self, rubrieken: list[dict], stats: dict, editie: tuple[datetime, str, str], films: bool = False):
        self.films = films
        self.rubrieken = rubrieken
        self.stats = stats
        self.editie = editie
        self.tabs = ([("home", "index.html")]
                     + ([("films", "films.html")] if self.films else [])
                     + [("nieuws", "nieuws.html")])

    def editie_tekst(self) -> str:
        moment, naam, volgende = self.editie
        return f"bijgewerkt {klok(NOW)}" if naam == "live" else f"<b>{naam}</b> · {datum_lang(moment)} · {volgende}"

    def pagina(self, titel: str, actief: str, inhoud: str, pagina_id: str = "", extra_js: str = "", logo: str = "",
               icoon: str = "", voet: str = "", sub: str = "") -> str:
        """Algemeen sjabloon. Zonder logo (zoals op home) is er geen kopregel."""
        nav = "".join(
            f'<a href="{href}"{" class=on aria-current=page" if naam == actief else ""}>{i + 1} {e(naam)}</a>'
            for i, (naam, href) in enumerate(self.tabs)
        )
        kop = (f'<header>\n<div class="logo">{icoon}<div><span>~/</span>{e(logo)}</div></div>\n'
               f'<div class="sub">{sub}</div>\n</header>') if logo else ""
        body_id = f' data-id="{pagina_id}"' if pagina_id else ""
        return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titel)}</title>
<meta name="description" content="DIRGE — persoonlijke startpagina. Alleen tekst.">
<script>{HEAD_JS}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body{body_id}>
<div class="bar">
<nav aria-label="Pagina's">{nav}</nav>
<div class="knoppen">
<button type="button" id="thema" title="Ander thema (t)">◐ tokyo-night</button>
<button type="button" id="kleiner" aria-label="Tekst kleiner" title="Tekst kleiner (−)">A−</button>
<button type="button" id="groter" aria-label="Tekst groter" title="Tekst groter (+)">A+</button>
<button type="button" id="hulpknop" aria-label="Sneltoetsen" title="Sneltoetsen (?)">?</button>
</div>
</div>
<div class="wrap">
{kop}
{inhoud}
<footer>
{voet}
<div>dirge · alleen tekst · geen tracking · druk op ? voor sneltoetsen</div>
</footer>
</div>
{HULP.replace("%N%", str(len(self.tabs)))}
<script>{JS}</script>
{f"<script>{extra_js}</script>" if extra_js else ""}
</body>
</html>
"""

    # ── pagina's ──

    def home(self) -> str:
        links = [("films", "films.html")] if self.films else []
        links.append(("nieuws", "nieuws.html"))
        inhoud = (f'<main class="home">{pixel_logo(DIRGE, rij_klasse=True, label="DIRGE")}'
                  f'<nav class="home-links" aria-label="Onderdelen">'
                  + "".join(f'<a class="kop" href="{h}">{n}</a>' for n, h in links)
                  + "</nav></main>")
        return self.pagina("dirge", "home", inhoud)

    def nieuws_voet(self) -> str:
        st = self.stats
        stuk = f' · <span class="stuk">niet bereikbaar: {e(", ".join(st["stuk"]))}</span>' if st["stuk"] else ""
        return f'<div>nieuws uit {st["bronnen"]} bronnen: {e(" · ".join(st["namen"]))}{stuk}</div>'

    def top10(self, per: dict[str, list[Cluster]]) -> list[Cluster]:
        """De tien onderwerpen waar vandaag de meeste bronnen over schrijven."""
        ref = self.editie[0]
        alle = [c for r in self.rubrieken for c in per[r["id"]]]
        dag = lokale_dag(ref)
        kandidaten = [c for c in alle if lokale_dag(c.tijd) == dag]
        if len(kandidaten) < 10:
            kandidaten = [c for c in alle if ref - c.tijd <= timedelta(hours=24)]
        return sorted(kandidaten, key=lambda c: (len(c.bronnen), len(c.items), c.tijd), reverse=True)[:10]

    def nieuws(self, top: list[Cluster], rub_van: dict[int, dict]) -> str:
        dag = lokale_dag(self.editie[0])
        rijen = []
        for i, c in enumerate(top, 1):
            sam = f'<p class="sam">{e(c.hoofd.samenvatting)}</p>' if c.hoofd.samenvatting else ""
            rijen.append(f'<li class="rij"><span class="nr">{i}</span><div>{kop_link(c)}{sam}'
                         f'<div class="meta">{e(rub_van[id(c)]["titel"])} · {meta(c)}</div>{dekking_html(c)}</div></li>')
        datum = f"{DAGEN_LANG[dag.weekday()]} {dag.day} {MAANDEN[dag.month - 1]}"
        inhoud = f"""<div class="smal"><h2>## de tien van vandaag <span>· {datum}</span></h2>
<p class="intro-tekst">De tien onderwerpen waar vandaag de meeste bronnen over schrijven. Lees ze door, en je weet wat er speelt.</p>
<ol class="groot">{"".join(rijen) or '<li>Nog niets te melden.</li>'}</ol></div>"""
        return self.pagina("nieuws — dirge", "nieuws", inhoud, logo="nieuws", sub=self.editie_tekst(), voet=self.nieuws_voet())

    def artikelpagina(self, c: Cluster, r: dict) -> str:
        h = c.hoofd
        if h.tekst:
            door = f" door {e(h.auteur)}" if h.auteur else ""
            lic = f" · licentie {e(h.licentie)}" if h.licentie else ""
            intro = (f'<p class="bronregel">Dit artikel{door} verscheen oorspronkelijk bij '
                     f'<a href="{e(h.link)}" rel="noopener">{e(h.bron)}</a> op {datum_lang(h.tijd)}{lic}.</p>'
                     + "".join(f"<p>{e(a)}</p>" for a in h.tekst))
            knop = f"origineel bekijken bij {e(h.bron)} →"
        elif h.samenvatting:
            intro = f'<p class="intro">{e(h.samenvatting)}</p>'
            knop = f"hele artikel lezen bij {e(h.bron)} →"
        else:
            intro = ""
            knop = f"artikel lezen bij {e(h.bron)} →"
        anderen = ""
        if len(c.items) > 1:
            blokken = "".join(
                f'<div class="ander"><div class="meta"><span class="src">{e(it.bron)}</span> · {klok(it.tijd)}</div>'
                f'<div class="kop">{e(it.titel)}</div>'
                + (f'<p>{e(it.samenvatting)}</p>' if it.samenvatting else "")
                + f'<a class="lees" href="{e(it.link)}" rel="noopener">artikel lezen bij {e(it.bron)} →</a></div>'
                for it in c.items[1:]
            )
            anderen = f'<div class="label" style="margin-top:3.4rem">▍ zo brengen de anderen het · {len(c.items) - 1}</div><div class="anderen">{blokken}</div>'
        inhoud = f"""<article class="artikel">
<a class="terug" href="nieuws.html">← nieuws</a>
<div class="label">▍ {e(r["titel"])} · {e(h.bron)} · {datum_lang(h.tijd)}</div>
<h1>{e(h.titel)}</h1>
<div class="tekst">{intro}</div>
<a class="lees groot" href="{e(h.link)}" rel="noopener">{knop}</a>
{anderen}
</article>"""
        return self.pagina(f"{h.titel} — dirge", "nieuws", inhoud, pagina_id=c.id, logo="nieuws",
                           sub=self.editie_tekst(), voet=self.nieuws_voet())

    def filmpagina(self, data: dict, cfg: dict) -> str:
        bron = cfg.get("score", "imdb")
        def score(t):
            return (t.get("imdb"), t.get("stemmen", 0)) if bron == "imdb" else (t.get("tmdb_score"), t.get("tmdb_stemmen", 0))
        grens = data.get("grens_nieuw", "")
        nieuw, alltime = [], []
        for t in data.get("titels", []):
            sc, st = score(t)
            if sc is None:
                continue
            if t.get("datum", "") >= grens and sc >= cfg.get("min_score_nieuw", 7.0) and st >= cfg.get("min_stemmen_nieuw", 5000 if bron == "imdb" else 200):
                nieuw.append(t)
            elif sc >= cfg.get("min_score_alltime", 8.0) and st >= cfg.get("min_stemmen_alltime", 25000 if bron == "imdb" else 1500):
                alltime.append(t)
        nieuw.sort(key=lambda t: -score(t)[0]); alltime.sort(key=lambda t: -score(t)[0])
        nieuw, alltime = nieuw[:250], alltime[:300]

        def genres_van(t):
            uit = []
            for g in t.get("genres", []):
                uit += [x.strip() for x in g.split("&")] if "&" in g else [g]
            return list(dict.fromkeys(x for x in uit if x))

        diensten = Counter(dn for t in nieuw + alltime for dn in t.get("diensten", []))
        def stemmen(n):
            return f"{n / 1_000_000:.1f}M".replace(".", ",") if n >= 1_000_000 else f"{round(n / 1000)}k" if n >= 1000 else str(n)

        def titel_html(t):
            sc, st = score(t)
            g = genres_van(t)
            imdb_url = f"https://www.imdb.com/title/{t['imdb_id']}/"
            tmdb_url = f"https://www.themoviedb.org/{'movie' if t['soort'] == 'film' else 'tv'}/{t['tmdb']}"
            orig = f' <span class="jaar">({e(t["origineel"])})</span>' if t.get("origineel") and t["origineel"] != t["titel"] else ""
            kijk = ", ".join(e(x) for x in t.get("diensten", [])) or "niet bij een abonnement in Nederland"
            links = [f'<a href="{e(imdb_url)}" rel="noopener">IMDb</a>', f'<a href="{e(tmdb_url)}" rel="noopener">TMDB</a>']
            if t.get("kijklink"):
                links.insert(0, f'<a href="{e(t["kijklink"])}" rel="noopener">waar te zien</a>')
            return (f'<div class="item rij film" data-soort="{t["soort"]}" data-genres="{e("|".join(g))}" '
                    f'data-diensten="{e("|".join(x.lower() for x in t.get("diensten", [])))}">'
                    f'<a class="kop" href="{e(imdb_url)}" rel="noopener" data-id="f-{e(t["imdb_id"])}" data-t="0">{e(t["titel"])}</a>{orig}'
                    f' <span class="jaar">· {t.get("jaar") or "?"} · {t["soort"]}</span>'
                    f'<div class="score"><b>★ {sc:.1f}</b> {"IMDb" if bron == "imdb" else "TMDB"} · {stemmen(st)} stemmen · {e(", ".join(g))}</div>'
                    f'<p class="sam">{e(t.get("intro") or "Geen beschrijving beschikbaar.")}</p>'
                    f'<div class="meta">kijken: {kijk} · {" · ".join(links)}</div></div>')

        def chips(naam, sleutel, waarden, met_alles=True):
            knoppen = ([f'<button type="button" class="chip" data-f="{sleutel}" data-w="">alles</button>'] if met_alles else [])
            knoppen += [f'<button type="button" class="chip" data-f="{sleutel}" data-w="{e(w.lower())}">{e(w)}</button>' for w in waarden]
            return f'<div class="filterrij"><span class="naam">{naam}</span>{"".join(knoppen)}</div>'

        aanwezig = {g for t in nieuw + alltime for g in genres_van(t)}
        genres = [g for g in cfg.get("genres", []) if g in aanwezig] or sorted(aanwezig)
        bijgewerkt = data.get("bijgewerkt", "")
        try:
            bijgewerkt = datum_lang(datetime.fromisoformat(bijgewerkt))
        except ValueError:
            pass
        jaren = cfg.get("jaren_nieuw", 2)
        inhoud = f"""<div style="padding-top:2.8rem"><h2>## Films and series <span>· bijgewerkt {e(bijgewerkt)}</span></h2>
<p class="intro-tekst">Goed beoordeelde films en series, gesorteerd op {"IMDb" if bron == "imdb" else "TMDB"}-score. Kies genres (meerdere mag) en zet eventueel je eigen streamingdiensten aan. Je keuzes worden onthouden.</p>
<div class="filters">
{chips("soort", "soort", ["film", "serie"])}
{chips("genre", "genre", genres)}
{chips("kijken via", "dienst", [d for d, _ in diensten.most_common(12)])}
<div class="filterrij"><span class="naam">verbergen</span><button type="button" class="chip" id="geen-animatie" aria-pressed="false">animatie &amp; tekenfilms</button></div>
</div>
<section class="blok" style="padding-top:0"><div class="label">▍ nieuw · laatste {jaren} jaar · <span class="telling" data-voor="nieuw"></span></div>
<div class="filmlijst" id="nieuw">{"".join(titel_html(t) for t in nieuw) or '<p class="leeg">Nog geen titels.</p>'}</div>
<p class="leeg" hidden>Niets gevonden met deze filters.</p></section>
<section class="blok" style="border-bottom:0"><div class="label">▍ all-time · <span class="telling" data-voor="alltime"></span></div>
<div class="filmlijst" id="alltime">{"".join(titel_html(t) for t in alltime) or '<p class="leeg">Nog geen titels.</p>'}</div>
<p class="leeg" hidden>Niets gevonden met deze filters.</p></section>
</div>"""
        voet = (f'<div>{len(nieuw) + len(alltime)} films en series · bijgewerkt {e(bijgewerkt)}</div>'
                f'<div>introteksten, genres en streaminginfo: TMDB · streaminggegevens: JustWatch'
                f'{" · scores: IMDb (imdb.com)" if bron == "imdb" else ""}</div>'
                f'<div>Deze site gebruikt de TMDB API maar is niet door TMDB goedgekeurd of gecertificeerd.</div>')
        return self.pagina("Wat zullen we kijken? — dirge", "films", inhoud, extra_js=FILM_JS,
                           logo="Wat zullen we kijken?", icoon=pixel_logo(KLAPBORD), voet=voet)


# ── hoofdprogramma ──────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="feeds.toml")
    ap.add_argument("--out", default="site")
    ap.add_argument("--archief", help="map waarin berichten per dag bewaard worden")
    ap.add_argument("--offline", help="map met <rubriek>__<bron>.xml testbestanden")
    ap.add_argument("--films", help="films.json van films.py (optioneel)")
    args = ap.parse_args()

    config = tomllib.loads(Path(args.config).read_text("utf-8"))
    rubrieken = config["rubriek"]
    site_cfg = config.get("site", {})
    rub_per_id = {r["id"]: r for r in rubrieken}
    taken = [(r["id"], b["naam"], b["url"]) for r in rubrieken for b in r["bronnen"]]
    alleen = {(r["id"], b["naam"]): {c.lower() for c in b.get("alleen_categorie", [])} for r in rubrieken for b in r["bronnen"]}
    niveaus = {(r["id"], b["naam"]): (b.get("tekst", "kop"), b.get("licentie", "")) for r in rubrieken for b in r["bronnen"]}

    def laad(taak):
        rid, naam, url = taak
        try:
            if args.offline:
                pad = Path(args.offline) / f"{rid}__{re.sub(r'[^a-z0-9]+', '-', naam.lower())}.xml"
                data = pad.read_bytes()
            else:
                data = ophalen(url)
            niveau, licentie = niveaus[(rid, naam)]
            return taak, parse_feed(data, naam, niveau, licentie), None
        except Exception as fout:  # één kapotte feed mag de rest niet tegenhouden
            return taak, [], fout

    with ThreadPoolExecutor(max_workers=12) as pool:
        resultaten = list(pool.map(laad, taken))

    flt = config.get("filter", {})
    url_weg = [u.lower() for u in flt.get("negeer_url", [])]
    titel_weg = [re.compile(t, re.I) for t in flt.get("negeer_titel", [])]
    cat_weg = {c.lower() for c in flt.get("negeer_categorie", [])}

    def toegestaan(it: Item) -> bool:
        """Filter + regels per bron. Geldt ook voor berichten uit het archief."""
        sleutel = (it.rubriek, it.bron)
        if sleutel not in niveaus:
            return False  # bron staat niet (meer) in de lijst
        link = it.link.lower()
        cats = {c.lower() for c in it.categorieen}
        if alleen[sleutel] and not (cats & alleen[sleutel]):
            return False
        if cats & cat_weg:
            return False
        return not any(u in link for u in url_weg) and not any(t.search(it.titel) for t in titel_weg)

    def beperk(it: Item) -> Item:
        """Toon nooit meer tekst dan de bron toestaat, ook niet uit het archief."""
        niveau, licentie = niveaus[(it.rubriek, it.bron)]
        if niveau == "kop":
            it.samenvatting, it.tekst = "", ()
        elif niveau != "volledig":
            it.tekst = ()
        it.licentie = licentie if niveau == "volledig" else ""
        return it

    vers: list[Item] = []
    weggefilterd = 0
    stuk, werkend = [], []
    for (rid, naam, url), items, fout in resultaten:
        if fout is not None or not items:
            print(f"! {rid}/{naam}: {fout or 'geen berichten'}", file=sys.stderr)
            stuk.append(naam)
            continue
        werkend.append(naam)
        for it in items:
            it.rubriek = rid
        # Oude berichten (bijv. liveblogs met een datum van maanden terug) slaan we over.
        goed = [it for it in items if toegestaan(it) and NOW - it.tijd <= timedelta(days=7)]
        weggefilterd += len(items) - len(goed)
        vers.extend(goed)

    # ── archief: inlezen, aanvullen, wegschrijven ──
    alles: dict[tuple[str, str], Item] = {}
    archief_dagen = int(site_cfg.get("archief_dagen", 365))
    archief = Path(args.archief) if args.archief else None
    vandaag = lokale_dag(NOW)
    if archief:
        archief.mkdir(parents=True, exist_ok=True)
        for f in sorted(archief.glob("????-??-??.json")):
            dag = date.fromisoformat(f.stem)
            if (vandaag - dag).days > archief_dagen:
                f.unlink()
                continue
            for d in json.loads(f.read_text("utf-8")):
                it = van_dict(d)
                alles[(it.rubriek, it.link)] = it
    for it in vers:  # verse versie wint (kop kan zijn aangepast); zoekdata blijft in geheugen
        alles[(it.rubriek, it.link)] = it
    if archief:
        nieuwe_dagen = {lokale_dag(it.tijd) for it in vers}
        for dag in nieuwe_dagen:
            if (vandaag - dag).days > archief_dagen:
                continue
            van_dag = sorted((it for it in alles.values() if lokale_dag(it.tijd) == dag), key=lambda i: (i.rubriek, i.tijd))
            (archief / f"{dag.isoformat()}.json").write_text(
                json.dumps([naar_dict(it) for it in van_dag], ensure_ascii=False, separators=(",", ":")), "utf-8")

    alle_items = [beperk(it) for it in alles.values() if toegestaan(it)]

    # ── wat er nu getoond wordt (live, of tot de laatste editie) ──
    editie = editie_info(site_cfg.get("edities", []))
    grens = editie[0]
    live = [it for it in alle_items if it.tijd <= grens + timedelta(minutes=1) and grens - it.tijd <= MAX_AGE]

    clusters: dict[str, list[Cluster]] = {}
    for r in rubrieken:
        clusters[r["id"]] = groepeer([it for it in live if it.rubriek == r["id"]])
    rub_van = {id(c): rub_per_id[rid] for rid, cl in clusters.items() for c in cl}

    namen = list(dict.fromkeys(werkend))
    stats = {"artikelen": len(live), "bronnen": len(namen), "namen": namen, "stuk": sorted(set(stuk) - set(werkend))}
    films_data = None
    if args.films and Path(args.films).exists():
        try:
            films_data = json.loads(Path(args.films).read_text("utf-8"))
        except json.JSONDecodeError:
            print("! films.json is onleesbaar; filmpagina overgeslagen", file=sys.stderr)
    site = Site(rubrieken, stats, editie, films=films_data is not None)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for oud in out.glob("*.html"):
        oud.unlink()
    (out / "index.html").write_text(site.home(), "utf-8")
    if films_data is not None:
        (out / "films.html").write_text(site.filmpagina(films_data, config.get("films", {})), "utf-8")
    top = site.top10(clusters)
    (out / "nieuws.html").write_text(site.nieuws(top, rub_van), "utf-8")
    for c in top:
        (out / c.pagina).write_text(site.artikelpagina(c, rub_van[id(c)]), "utf-8")

    (out / ".nojekyll").write_text("")
    print(f"✓ home, {'films, ' if films_data is not None else ''}nieuws ({editie[1]}, {len(live)} berichten uit "
          f"{len(namen)} bronnen, {weggefilterd} niet-nieuws weggefilterd) → {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
