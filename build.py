#!/usr/bin/env python3
"""~/nieuws — haalt RSS/Atom-feeds op en bouwt een rustige nieuwssite van alleen tekst.

Gebruik:  python build.py            (schrijft naar ./site)
          python build.py --offline map/met/xml   (test met lokale feedbestanden)

Alleen de standaardbibliotheek van Python 3.11+, geen extra pakketten.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime.now(timezone.utc)
MAX_AGE = timedelta(hours=48)
UA = "Mozilla/5.0 (compatible; nieuws-tekst/1.0; +https://github.com/)"

DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]

STOP = set("""
de het een en of van in op te met voor door aan bij uit over naar als dat die dit
is zijn was waren wordt worden werd heeft hebben had niet nog ook maar dan om tot
er hij zij ze we wij je jij u ik zich meer na al wel geen kan moet zal gaat
the a an and or of in on to for with by at from as that this is are was were be
been has have had not but its it his her their they we you new says said after
over into about than more will can
""".split())


@dataclass
class Item:
    bron: str
    titel: str
    link: str
    tijd: datetime
    samenvatting: str = ""
    categorieen: tuple[str, ...] = ()

    @property
    def tokens(self) -> set[str]:
        woorden = re.findall(r"[a-z0-9à-ÿ]+", self.titel.lower())
        # eerste 6 letters als simpele 'stam': kabinet ≈ kabinetsplan, betaalbare ≈ betaalbaarder
        return {w[:6] for w in woorden if len(w) >= 3 and w not in STOP}


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)

    @property
    def hoofd(self) -> Item:
        return self.items[0]

    @property
    def bronnen(self) -> list[str]:
        gezien: list[str] = []
        for it in self.items:
            if it.bron not in gezien:
                gezien.append(it.bron)
        return gezien

    @property
    def tijd(self) -> datetime:
        return max(it.tijd for it in self.items)

    @property
    def pagina(self) -> str:
        """Bestandsnaam van de eigen artikelpagina, stabiel zolang het hoofdbericht gelijk blijft."""
        return "a-" + hashlib.sha1(self.hoofd.link.encode()).hexdigest()[:10] + ".html"


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
    if maxlen and len(s) > maxlen:
        s = s[: maxlen].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def veilige_link(url: str) -> str:
    url = url.strip()
    return url if url.startswith(("https://", "http://")) else ""


def parse_feed(data: bytes, bron: str) -> list[Item]:
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
        # Alleen de korte samenvatting uit de feed; sommige feeds zetten er het hele
        # artikel in, daarom altijd ingekort tot een paar zinnen.
        samenvatting = schoon(kind_tekst(el, "description", "summary"), 320)
        if samenvatting.lower().startswith(titel.lower()[:40]):
            samenvatting = ""
        cats = tuple(schoon(c.text or "") for c in el if lokaal(c.tag) == "category" and (c.text or "").strip())
        items.append(Item(bron, titel, link, tijd, samenvatting, cats))
    return items


# ── groeperen ───────────────────────────────────────────────────────────────

def lijkt_op(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    gedeeld = len(a & b)
    return (gedeeld >= 2 and gedeeld / min(len(a), len(b)) >= 0.5) or gedeeld / len(a | b) >= 0.45


def groepeer(items: list[Item]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for it in sorted(items, key=lambda i: i.tijd, reverse=True):
        tok = it.tokens
        for c in clusters:
            if lijkt_op(tok, c.tokens):
                c.items.append(it)
                c.tokens |= tok
                break
        else:
            clusters.append(Cluster([it], set(tok)))
    return clusters


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


def dag_kop(d: datetime) -> str:
    d = d.astimezone(TZ).date()
    vandaag = NOW.astimezone(TZ).date()
    if d == vandaag:
        return "vandaag"
    if d == vandaag - timedelta(days=1):
        return "gisteren"
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def meta(c: Cluster) -> str:
    n = len(c.bronnen)
    namen = ", ".join(c.bronnen[:3]) + (f" +{n - 3}" if n > 3 else "")
    return f"{e(namen)} · {n} {'bron' if n == 1 else 'bronnen'} · {leeftijd(c.tijd)}"


def cluster_html(c: Cluster, uitgebreid: bool = False) -> str:
    h = c.hoofd
    uit = []
    uit.append(f'<a class="kop" href="{c.pagina}">{e(h.titel)}</a>')
    if uitgebreid and h.samenvatting:
        uit.append(f'<p class="sam">{e(h.samenvatting)}</p>')
    uit.append(f'<div class="meta">{meta(c)}</div>')
    if len(c.items) > 1:
        rijen = "".join(
            f'<li><span class="src">{e(it.bron)}</span><a href="{e(it.link)}">{e(it.titel)}</a><span class="t">{klok(it.tijd)}</span></li>'
            for it in c.items[1:]
        )
        uit.append(f'<details><summary>zo brengen de anderen het ({len(c.items) - 1})</summary><ul class="dekking">{rijen}</ul></details>')
    return '<div class="item">' + "".join(uit) + "</div>"


CSS = """
:root{--bg:#1a1b26;--fg:#a9b1d6;--br:#c0caf5;--dim:#787fa6;--ln:#292e42;--ac:#7aa2f7;--ac2:#9ece6a;--sel:#283457}
[data-theme=catppuccin]{--bg:#1e1e2e;--fg:#bac2de;--br:#cdd6f4;--dim:#8a8fa8;--ln:#313244;--ac:#89b4fa;--ac2:#a6e3a1;--sel:#313244}
[data-theme=gruvbox]{--bg:#282828;--fg:#d5c4a1;--br:#ebdbb2;--dim:#a19480;--ln:#3c3836;--ac:#83a598;--ac2:#b8bb26;--sel:#3c3836}
[data-theme=rose-pine]{--bg:#191724;--fg:#c8c4d9;--br:#e0def4;--dim:#8b87a3;--ln:#26233a;--ac:#9ccfd8;--ac2:#f6c177;--sel:#26233a}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,summary:focus-visible,button:focus-visible{outline:1px solid var(--ac);outline-offset:2px}
.bar{border-bottom:1px solid var(--ln);padding:6px 24px;display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;font-size:12px;color:var(--dim)}
.bar nav{display:flex;flex-wrap:wrap;gap:4px}
.bar nav a{padding:3px 10px}.bar nav a.on{background:var(--sel);color:var(--br)}
.bar .info{display:flex;gap:16px;align-items:center}.bar .info b{color:var(--fg);font-weight:400}
.wrap{max-width:1200px;margin:0 auto;padding:28px 24px 64px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:4px 16px;padding-bottom:16px;border-bottom:1px solid var(--ln)}
.logo{font-size:22px;font-weight:700;letter-spacing:-.5px;color:var(--br)}.logo span{color:var(--ac)}
.sub{color:var(--dim)}
.label{font-size:12px;color:var(--ac2);margin-bottom:12px}
.nu{padding:32px 0 40px;border-bottom:1px solid var(--ln)}
.nu ol{list-style:none;margin:0;padding:0}
.nu ol.groot{display:flex;flex-direction:column;gap:14px;max-width:960px}
.nu ol.rest{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px 48px;margin-top:18px}
.nu li{display:flex;gap:16px}.nu .t{color:var(--dim);flex:none;width:44px}
.nu .kop{color:var(--br);font-weight:500}.nu li.top .kop{font-size:17px;font-weight:700}
.nu li.top{padding-bottom:14px;border-bottom:1px solid var(--ln)}
.nu .sam{margin:4px 0}
.rubrieken{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:40px 48px;padding-top:40px}
h2{margin:0 0 14px;font-size:14px;font-weight:500;color:var(--ac)}h2 span{color:var(--dim);font-weight:400}
.lijst{display:flex;flex-direction:column;gap:16px}
.item .kop{color:var(--br);font-weight:500}
.item .sam{margin:4px 0 0;max-width:72ch}
.meta,.meer{font-size:12px;color:var(--dim)}
.meer{display:inline-block;margin-top:16px}.meer:hover{color:var(--ac)}
details{font-size:12px;color:var(--dim);margin-top:2px}
summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
summary::before{content:"+ "}details[open] summary::before{content:"− "}
.dekking{list-style:none;margin:6px 0 0;padding:0;border-top:1px solid var(--ln)}
.dekking li{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--ln);color:var(--fg)}
.dekking .src{width:96px;flex:none;color:var(--ac)}.dekking a{flex:1;min-width:0}.dekking .t{color:var(--dim)}
.dag{margin:40px 0 14px;font-size:12px;color:var(--ac2)}.dag:first-child{margin-top:0}
.rubriek-pagina .lijst{max-width:820px}
.artikel{max-width:72ch;padding-top:40px}
.terug{display:inline-block;font-size:12px;color:var(--dim);margin-bottom:24px}.terug:hover{color:var(--ac)}
.artikel h1{margin:0 0 20px;font-size:26px;line-height:1.35;color:var(--br)}
.intro{margin:0 0 24px;font-size:15px;line-height:1.75}
.lees{font-size:12px;color:var(--ac)}.lees.groot{display:inline-block;font-size:14px;border:1px solid var(--ln);padding:8px 14px}
.lees.groot:hover{background:var(--sel);text-decoration:none}
.anderen{display:flex;flex-direction:column;border-top:1px solid var(--ln)}
.ander{padding:16px 0;border-bottom:1px solid var(--ln)}.ander .src{color:var(--ac)}
.ander .kop{color:var(--br);font-weight:500;margin:2px 0}.ander p{margin:4px 0 6px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--ln);font-size:12px;color:var(--dim);display:flex;flex-direction:column;gap:12px}
footer .rij{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px}
footer .stuk{color:#f7768e}
.thema button{font:inherit;background:none;border:0;color:var(--dim);cursor:pointer;padding:2px 6px}
.thema button[aria-pressed=true]{color:var(--br);background:var(--sel)}
@media (max-width:640px){.wrap{padding:32px 16px 48px}.bar{padding:6px 16px}.nu .t{width:40px}.dekking .src{width:80px}}
"""

JS = """
(function(){var k='nieuws-thema',r=document.documentElement;
function zet(t){if(t==='tokyo-night')r.removeAttribute('data-theme');else r.setAttribute('data-theme',t);
document.querySelectorAll('.thema button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.t===t)});}
var t='tokyo-night';try{t=localStorage.getItem(k)||t}catch(x){}zet(t);
document.querySelectorAll('.thema button').forEach(function(b){b.onclick=function(){zet(b.dataset.t);try{localStorage.setItem(k,b.dataset.t)}catch(x){}}});})();
"""


def pagina(titel: str, actief: str, rubrieken: list[dict], inhoud: str, stats: dict) -> str:
    tabs = [("voorpagina", "index.html")] + [(r["titel"], f'{r["id"]}.html') for r in rubrieken]
    nav = "".join(
        f'<a href="{href}"{" class=on aria-current=page" if naam == actief else ""}>{i + 1} {e(naam)}</a>'
        for i, (naam, href) in enumerate(tabs)
    )
    stuk = stats["stuk"]
    stuk_html = f' · <span class="stuk">niet bereikbaar: {e(", ".join(stuk))}</span>' if stuk else ""
    themas = "".join(f'<button type="button" data-t="{t}" aria-pressed="false">{t}</button>' for t in ("tokyo-night", "catppuccin", "gruvbox", "rose-pine"))
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(titel)}</title>
<meta name="description" content="Nieuws uit alle bronnen, alleen tekst.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="bar">
<nav aria-label="Rubrieken">{nav}</nav>
<div class="info"><span>{stats["artikelen"]} artikelen</span><span>{stats["bronnen"]} bronnen</span><b>{datum_lang(NOW)}</b></div>
</div>
<div class="wrap">
<header>
<div class="logo"><span>~/</span>nieuws</div>
<div class="sub">alle bronnen, één rustig overzicht · bijgewerkt {klok(NOW)}</div>
</header>
{inhoud}
<footer>
<div class="rij"><span>bronnen: {e(" · ".join(stats["namen"]))}{stuk_html}</span></div>
<div class="rij"><span>alleen tekst · geen tracking · elke 15 minuten bijgewerkt</span><span class="thema" aria-label="Kleurenschema">{themas}</span></div>
</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def voorpagina(rubrieken: list[dict], per: dict[str, list[Cluster]], stats: dict) -> str:
    alle = [(r, c) for r in rubrieken for c in per[r["id"]]]
    if not alle:
        return pagina("~/nieuws", "voorpagina", rubrieken, '<p class="sub" style="padding:40px 0">Nog geen berichten opgehaald.</p>', stats)

    # Laatste nieuws bovenaan, nieuwste eerst; de eerste paar met samenvatting.
    laatste_lijst = sorted(alle, key=lambda x: x[1].tijd, reverse=True)[:14]
    rijen = []
    for i, (r, c) in enumerate(laatste_lijst):
        sam = f'<p class="sam">{e(c.hoofd.samenvatting)}</p>' if i < 4 and c.hoofd.samenvatting else ""
        rijen.append(
            f'<li class="{"top" if i < 4 else ""}"><span class="t">{klok(c.tijd)}</span><div>'
            f'<a class="kop" href="{c.pagina}">{e(c.hoofd.titel)}</a>{sam}'
            f'<div class="meta">{e(r["titel"])} · {meta(c)}</div></div></li>'
        )
    getoond = {id(c) for _, c in laatste_lijst}

    blokken = []
    for r in rubrieken:
        cl = [c for c in per[r["id"]] if id(c) not in getoond]
        cl.sort(key=lambda c: (len(c.bronnen) >= 2, c.tijd), reverse=True)
        if not cl:
            continue
        items = "".join(cluster_html(c) for c in cl[:6])
        blokken.append(
            f'<section id="{r["id"]}"><h2>## {e(r["titel"])} <span>· {len(per[r["id"]])}</span></h2>'
            f'<div class="lijst">{items}</div><a class="meer" href="{r["id"]}.html">alles in {e(r["titel"])} →</a></section>'
        )

    inhoud = f"""<section class="nu"><div class="label">▍ laatste nieuws</div><ol class="groot">{"".join(rijen[:4])}</ol><ol class="rest">{"".join(rijen[4:])}</ol></section>
<div class="rubrieken">{"".join(blokken)}</div>"""
    return pagina("~/nieuws", "voorpagina", rubrieken, inhoud, stats)


def rubriekpagina(r: dict, rubrieken: list[dict], clusters: list[Cluster], stats: dict) -> str:
    delen, huidige = [], None
    for c in sorted(clusters, key=lambda c: c.tijd, reverse=True):
        kop = dag_kop(c.tijd)
        if kop != huidige:
            if huidige is not None:
                delen.append("</div>")
            delen.append(f'<div class="dag">▍ {kop}</div><div class="lijst">')
            huidige = kop
        delen.append(cluster_html(c, uitgebreid=True))
    if huidige is not None:
        delen.append("</div>")
    inhoud = f'<div class="rubriek-pagina" style="padding-top:40px"><h2>## {e(r["titel"])} <span>· {len(clusters)} onderwerpen</span></h2>{"".join(delen)}</div>'
    return pagina(f'{r["titel"]} — ~/nieuws', r["titel"], rubrieken, inhoud, stats)


def artikelpagina(c: Cluster, r: dict, rubrieken: list[dict], stats: dict) -> str:
    h = c.hoofd
    sam = h.samenvatting or "Deze bron geeft geen samenvatting mee in de feed."
    anderen = ""
    if len(c.items) > 1:
        blokken = "".join(
            f'<div class="ander"><div class="meta"><span class="src">{e(it.bron)}</span> · {klok(it.tijd)}</div>'
            f'<div class="kop">{e(it.titel)}</div>'
            + (f'<p>{e(it.samenvatting)}</p>' if it.samenvatting else "")
            + f'<a class="lees" href="{e(it.link)}" rel="noopener">hele artikel lezen bij {e(it.bron)} →</a></div>'
            for it in c.items[1:]
        )
        anderen = f'<div class="label" style="margin-top:48px">▍ zo brengen de anderen het · {len(c.items) - 1}</div><div class="anderen">{blokken}</div>'
    inhoud = f"""<article class="artikel">
<a class="terug" href="{r["id"]}.html">← {e(r["titel"])}</a>
<div class="label">▍ {e(r["titel"])} · {e(h.bron)} · {datum_lang(h.tijd)}</div>
<h1>{e(h.titel)}</h1>
<p class="intro">{e(sam)}</p>
<a class="lees groot" href="{e(h.link)}" rel="noopener">hele artikel lezen bij {e(h.bron)} →</a>
{anderen}
</article>"""
    return pagina(f"{h.titel} — ~/nieuws", r["titel"], rubrieken, inhoud, stats)


# ── hoofdprogramma ──────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="feeds.toml")
    ap.add_argument("--out", default="site")
    ap.add_argument("--offline", help="map met <rubriek>__<bron>.xml testbestanden")
    args = ap.parse_args()

    config = tomllib.loads(Path(args.config).read_text("utf-8"))
    rubrieken = config["rubriek"]
    taken = [(r["id"], b["naam"], b["url"]) for r in rubrieken for b in r["bronnen"]]
    # per bron (rubriek, naam): optionele lijst met toegestane categorieën
    alleen = {(r["id"], b["naam"]): {c.lower() for c in b.get("alleen_categorie", [])} for r in rubrieken for b in r["bronnen"]}

    def laad(taak):
        rid, naam, url = taak
        try:
            if args.offline:
                pad = Path(args.offline) / f"{rid}__{re.sub(r'[^a-z0-9]+', '-', naam.lower())}.xml"
                data = pad.read_bytes()
            else:
                data = ophalen(url)
            return taak, parse_feed(data, naam), None
        except Exception as fout:  # één kapotte feed mag de rest niet tegenhouden
            return taak, [], fout

    with ThreadPoolExecutor(max_workers=12) as pool:
        resultaten = list(pool.map(laad, taken))

    per_rubriek: dict[str, list[Item]] = {r["id"]: [] for r in rubrieken}
    flt = config.get("filter", {})
    url_weg = [u.lower() for u in flt.get("negeer_url", [])]
    titel_weg = [re.compile(t, re.I) for t in flt.get("negeer_titel", [])]

    cat_weg = {c.lower() for c in flt.get("negeer_categorie", [])}

    def is_nieuws(it: Item, toegestaan: set[str]) -> bool:
        link = it.link.lower()
        cats = {c.lower() for c in it.categorieen}
        if toegestaan and not (cats & toegestaan):
            return False
        if cats & cat_weg:
            return False
        return not any(u in link for u in url_weg) and not any(t.search(it.titel) for t in titel_weg)

    weggefilterd = 0
    stuk, werkend = [], []
    for (rid, naam, url), items, fout in resultaten:
        if fout is not None or not items:
            print(f"! {rid}/{naam}: {fout or 'geen berichten'}", file=sys.stderr)
            stuk.append(naam)
            continue
        werkend.append(naam)
        goed = [it for it in items if is_nieuws(it, alleen[(rid, naam)])]
        weggefilterd += len(items) - len(goed)
        per_rubriek[rid].extend(it for it in goed if NOW - it.tijd <= MAX_AGE)

    clusters: dict[str, list[Cluster]] = {}
    totaal = 0
    for rid, items in per_rubriek.items():
        uniek = list({it.link: it for it in items}.values())
        totaal += len(uniek)
        clusters[rid] = groepeer(uniek)

    namen = list(dict.fromkeys(werkend))
    stats = {
        "artikelen": totaal,
        "bronnen": len(namen),
        "namen": namen,
        "stuk": sorted(set(stuk) - set(werkend)),
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(voorpagina(rubrieken, clusters, stats), "utf-8")
    for r in rubrieken:
        (out / f'{r["id"]}.html').write_text(rubriekpagina(r, rubrieken, clusters[r["id"]], stats), "utf-8")
        for c in clusters[r["id"]]:
            (out / c.pagina).write_text(artikelpagina(c, r, rubrieken, stats), "utf-8")
    (out / ".nojekyll").write_text("")
    print(f"✓ {totaal} artikelen uit {len(namen)} bronnen → {out}/ ({weggefilterd} niet-nieuws weggefilterd)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
