# ~/nieuws

Een rustige nieuwssite, alleen tekst. Haalt berichten op uit RSS-feeds van Nederlandse en internationale media, groepeert berichten over hetzelfde onderwerp en toont ze per rubriek: **binnenland**, **buitenland** en **tech**.

Stijl geïnspireerd op Omarchy (Tokyo Night), met keuze uit vier kleurenschema's onderaan de pagina.

## Hoe het werkt

- `feeds.toml` is de bronnenlijst. Bron toevoegen of weghalen = één regel aanpassen.
- `build.py` haalt de feeds op en schrijft de site naar `site/`. Alleen standaard Python 3.11+, geen pakketten.
- GitHub Actions draait het script elke 15 minuten en zet het resultaat op GitHub Pages.

## Eenmalig instellen

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
Daarna staat de site op `https://<gebruikersnaam>.github.io/nieuws/`.

## Lokaal draaien

```sh
python3 build.py
open site/index.html   # of: xdg-open site/index.html
```

## Een rubriek toevoegen

Kopieer een `[[rubriek]]`-blok in `feeds.toml`, geef het een eigen `id` en `titel` en zet er feeds in. De nieuwe kop verschijnt vanzelf in het menu en op de voorpagina.
