# DIRGE

Persoonlijke startpagina, alleen tekst, in Omarchy-stijl. Vijf kleurenschema's
(knop bovenaan of toets `t`), sneltoetsen (`?`), geen tracking.

- **home** (`index.html`): het DIRGE-logo met links naar de onderdelen.
- **films** (`films.html`): film- en seriesuggesties per genre met score, intro en
  streamingdiensten in Nederland.
- **nieuws** (`nieuws.html`): de tien onderwerpen waar vandaag de meeste bronnen over
  schrijven, met per onderwerp een eigen pagina en een link naar het hele artikel.

## Hoe het werkt

- `build.py` bouwt de site naar `site/`. Alleen standaard Python 3.11+.
- `feeds.toml` bevat de nieuwsbronnen, filters, edities en filminstellingen.
- `films.py` haalt één keer per dag de filmgegevens op (TMDB + IMDb-scores).
- GitHub Actions draait alles elke 15 minuten en zet het op GitHub Pages.
  Berichten en filmgegevens worden bewaard op de branch `archief`.

## Een onderdeel toevoegen

Een nieuwe pagina is een methode in de klasse `Site` in `build.py` plus een regel in
`self.tabs` en in `home()`; dan staat hij vanzelf in de balk en op de homepagina.

## Filmsuggesties (optioneel)

De pagina **films** toont goed beoordeelde films en series per genre, met introtekst,
score en waar je ze in Nederland kunt kijken. Instellen:

1. Maak een gratis account op themoviedb.org en vraag onder Settings → API een
   sleutel aan (kies *Developer*, niet-commercieel).
2. Zet die sleutel in je repo onder **Settings → Secrets and variables → Actions →
   New repository secret**, naam `TMDB_API_KEY`.
3. Start de workflow opnieuw. De films worden daarna één keer per dag bijgewerkt.

Instellingen (minimale score, genres, IMDb- of TMDB-score) staan onder `[films]` in `feeds.toml`.
