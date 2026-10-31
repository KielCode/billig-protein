# Billig Protein

Hvor får du mest protein for pengene i danske supermarkeder?

**Siden:** https://kielcode.github.io/billig-protein/

Siden sammenligner proteinrige varer fra Rema 1000 og ugens tilbud fra eTilbudsavis
(Netto, Lidl, føtex, Bilka, Coop m.fl.) og regner ud, hvad 100 g protein koster —
justeret for proteinkvalitet (PDCAAS) og for knogler i fx hel kylling.

## Sådan virker det

GitHub Actions kører `opdater.py` hver morgen (`.github/workflows/opdater.yml`):

1. `rema_scraper.py` — priser og varedeklarationer fra Remas webshop
2. `tilbud_scraper.py` — ugens tilbud fra eTilbudsavis (næringsindhold estimeres ud fra varetypen)
3. `pdcaas_lookup.py` — proteinkvalitet pr. vare
4. `kurator.py` — kun varer med højt proteinindhold (≥ 20 % af energien, EU's definition), udløbne tilbud fjernes
5. `build_web.py` — indsætter data i `web/template.html` og gemmer `docs/index.html`

Fejler en kilde, bruges sidste gode kopi (højst 7 dage gammel). `docs/status.json` viser hvordan seneste kørsel gik.

## Kør lokalt

```
pip install -r requirements.txt
python opdater.py --out docs
```

Start en kørsel manuelt: fanen **Actions** → *Opdater Billig Protein* → *Run workflow*.

Ingen butikker betaler for placering. Priser og næringsindhold er vejledende.
