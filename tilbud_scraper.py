"""
tilbud_scraper.py — Protein-Jæger multi-butik scraper

Trækker aktuelle tilbud fra alle danske supermarkeder via eTilbudsavis
public API (ingen API-nøgle påkrævet).

Dækker: Netto, Meny, Lidl, REMA 1000, Foetex, Bilka, Spar,
        Kvickly, 365discount, SuperBrugsen og flere.

Da eTilbudsavis ikke leverer næringsdata, estimeres protein, fedt,
kulhydrat og kcal fra produktnavn via et keyword-lookup.
Estimaterne er grove — PDCAAS-scoring efterfølgende kompenserer
for biologisk kvalitet.

Output:
  output/tilbud_matrix.json  (flettes med Rema-data af pdcaas_lookup.py)

Koordinater: København centrum (55.6761° N, 12.5683° Ø)
Radius: 100 km — dækker hele Danmark + tilbud uden geografisk binding
"""

import json
import re
import time
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# ── Stier ─────────────────────────────────────────────────────────────────────────
OUTPUT_FILE = Path("output/tilbud_matrix.json")

# ── API-konfiguration ──────────────────────────────────────────────────────────
API_BASE    = "https://api.etilbudsavis.dk/v2/offers/search"
LAT         = "56.0"     # Danmarks centrum (Midtjylland)
LNG         = "10.0"
RADIUS      = "700000"   # 700 km — dækker hele Danmark
LOCALE      = "da_DK"
API_VERSION = "0.3.0"
LIMIT       = 100        # max per forespørgsel

# Forsinkelse mellem API-kald (sekunder) — vær pæn mod serveren
SLEEP_BETWEEN = 0.4

# ── Protein-søgeord ────────────────────────────────────────────────────────────
# Hvert søgeord repræsenterer en proteinkategori.
# Rækkefølgen afgør ikke noget — alle køres.
PROTEIN_QUERIES = [
    "kylling",
    "kyllingebryst",
    "kyllingefilet",
    "kalkun",
    "oksekød",
    "hakket oksekød",
    "bøf",
    "ribeye",
    "entrecôte",
    "svinekød",
    "nakkefilet",
    "svinemørbrad",
    "koteletter",
    "laks",
    "laksefilet",
    "tun",
    "makrel",
    "sild",
    "torsk",
    "rødspætte",
    "rejer",
    "skyr",
    "hytteost",
    "cottage cheese",
    "græsk yoghurt",
    "kvark",
    "æg",
    "ost",
    "mozzarella",
    "edamame",
    "linser",
    "kikærter",
    "tofu",
    "tempeh",
]

# ── Næringsestimat-tabel (g/100g og kcal/100g) ────────────────────────────────
# Bruges når butik ikke oplyser næringsdata.
# Kilde: Fødevarestyrelsen / DTU Frida gennemsnitsværdier.
NAERING_LOOKUP: list[tuple[str, dict]] = [
    # keyword → {'protein', 'fedt', 'kulhydrat', 'kcal'}
    # Prioriteret rækkefølge — første match vinder
    # ── Engelske produktnavne (internationale mærker via eTilbudsavis) ────────────
    ("chicken wings",   {"protein": 18.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 190}),
    ("chicken thigh",   {"protein": 17.0, "fedt": 14.0, "kulhydrat": 0.0, "kcal": 190}),
    ("chicken breast",  {"protein": 23.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 105}),
    ("chicken fillet",  {"protein": 23.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 105}),
    ("chicken",         {"protein": 20.0, "fedt":  7.0, "kulhydrat": 0.0, "kcal": 145}),
    ("beef",            {"protein": 22.0, "fedt": 10.0, "kulhydrat": 0.0, "kcal": 190}),
    ("salmon",          {"protein": 20.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 206}),
    ("tuna",            {"protein": 25.0, "fedt":  1.0, "kulhydrat": 0.0, "kcal": 116}),
    ("turkey",          {"protein": 22.0, "fedt":  2.0, "kulhydrat": 0.0, "kcal": 105}),
    ("pork",            {"protein": 20.0, "fedt": 12.0, "kulhydrat": 0.0, "kcal": 195}),
    ("shrimp",          {"protein": 18.0, "fedt":  1.0, "kulhydrat": 0.0, "kcal":  85}),
    # ── Danske produktnavne ───────────────────────────────────────────────────────
    ("kyllingebryst",   {"protein": 23.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 105}),
    ("kyllingefilet",   {"protein": 23.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 105}),
    # Kalkun-udskæringer (DTU Frida, rå). Lår/underlår er inkl. ben og skind.
    ("kalkunbryst",     {"protein": 24.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 110}),
    ("kalkunfilet",     {"protein": 24.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 110}),
    ("kalkunschnitzel", {"protein": 24.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal": 110}),
    ("hakket kalkun",   {"protein": 19.0, "fedt":  7.0, "kulhydrat": 0.0, "kcal": 140}),
    ("kalkunkød",       {"protein": 19.0, "fedt":  7.0, "kulhydrat": 0.0, "kcal": 140}),
    ("kalkununderlår",  {"protein": 16.0, "fedt":  8.0, "kulhydrat": 0.0, "kcal": 140}),
    ("kalkunoverlår",   {"protein": 16.0, "fedt":  9.0, "kulhydrat": 0.0, "kcal": 150}),
    ("kalkunlår",       {"protein": 16.0, "fedt":  8.0, "kulhydrat": 0.0, "kcal": 140}),
    # Generisk kalkun (ukendt udskæring) — kun hvis ingen mere specifik match
    ("kalkun",          {"protein": 22.0, "fedt":  5.0, "kulhydrat": 0.0, "kcal": 135}),
    # Kylling med ben — værdier pr. 100 g spiselig del; knoglerne trækkes fra i kurator.py
    # Udbenede lårstykker (uden ben) — skal matche før bone-in-værdierne nedenfor
    ("udbenede kyllingeoverlår", {"protein": 19.0, "fedt": 9.0, "kulhydrat": 0.0, "kcal": 160}),
    ("udbenet kyllingeoverlår",  {"protein": 19.0, "fedt": 9.0, "kulhydrat": 0.0, "kcal": 160}),
    ("kyllingelårfilet",         {"protein": 19.0, "fedt": 9.0, "kulhydrat": 0.0, "kcal": 160}),
    ("kyllingeoverlårfilet",     {"protein": 19.0, "fedt": 9.0, "kulhydrat": 0.0, "kcal": 160}),
    ("overlårsfilet",            {"protein": 19.0, "fedt": 9.0, "kulhydrat": 0.0, "kcal": 160}),
    ("kyllingevinger",  {"protein": 18.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 190}),
    ("kyllingelår",     {"protein": 18.0, "fedt": 12.0, "kulhydrat": 0.0, "kcal": 180}),
    ("kyllingeunderlår",{"protein": 19.0, "fedt":  9.0, "kulhydrat": 0.0, "kcal": 155}),
    ("kyllingeoverlår", {"protein": 17.0, "fedt": 14.0, "kulhydrat": 0.0, "kcal": 190}),
    ("hel kylling",     {"protein": 20.0, "fedt":  9.0, "kulhydrat": 0.0, "kcal": 157}),
    ("hele kylling",    {"protein": 20.0, "fedt":  9.0, "kulhydrat": 0.0, "kcal": 157}),
    # Generisk kylling (filet/bryst uden ben) — matcher kun hvis intet mere specifikt
    ("kylling",         {"protein": 20.0, "fedt":  7.0, "kulhydrat": 0.0, "kcal": 145}),
    ("ribeye",          {"protein": 22.0, "fedt": 18.0, "kulhydrat": 0.0, "kcal": 250}),
    ("entrecôte",       {"protein": 22.0, "fedt": 15.0, "kulhydrat": 0.0, "kcal": 230}),
    ("bøf",             {"protein": 21.0, "fedt":  6.0, "kulhydrat": 0.0, "kcal": 145}),
    ("hakket oksekød",  {"protein": 20.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 200}),
    ("oksekød",         {"protein": 22.0, "fedt": 10.0, "kulhydrat": 0.0, "kcal": 190}),
    ("svinemørbrad",    {"protein": 22.0, "fedt":  3.5, "kulhydrat": 0.0, "kcal": 120}),
    ("spareribs",       {"protein": 17.0, "fedt": 21.0, "kulhydrat": 0.0, "kcal": 260}),  # spiselig del; knogler trækkes fra i kurator.py
    ("nakkefilet",      {"protein": 18.0, "fedt": 15.0, "kulhydrat": 0.0, "kcal": 220}),
    ("koteletter",      {"protein": 20.0, "fedt": 10.0, "kulhydrat": 0.0, "kcal": 180}),
    ("svinekød",        {"protein": 20.0, "fedt": 12.0, "kulhydrat": 0.0, "kcal": 195}),
    ("laksefilet",      {"protein": 20.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 206}),
    ("laks",            {"protein": 20.0, "fedt": 13.0, "kulhydrat": 0.0, "kcal": 206}),
    ("tun",             {"protein": 25.0, "fedt":  1.0, "kulhydrat": 0.0, "kcal": 116}),
    ("makrel",          {"protein": 19.0, "fedt": 14.0, "kulhydrat": 0.0, "kcal": 205}),
    ("sild",            {"protein": 17.0, "fedt": 10.0, "kulhydrat": 0.0, "kcal": 160}),
    ("torsk",           {"protein": 18.0, "fedt":  0.7, "kulhydrat": 0.0, "kcal":  82}),
    ("rødspætte",       {"protein": 18.0, "fedt":  1.5, "kulhydrat": 0.0, "kcal":  90}),
    ("rejer",           {"protein": 18.0, "fedt":  1.0, "kulhydrat": 0.0, "kcal":  85}),
    # Creamy/flavored skyr har lavere protein end naturel pga. fløde + tilsætninger
    ("creamy skyr",     {"protein":  8.0, "fedt":  3.5, "kulhydrat": 5.5, "kcal":  82}),
    ("skyr vanilje",    {"protein":  8.5, "fedt":  2.0, "kulhydrat": 7.0, "kcal":  80}),
    ("skyr vanilla",    {"protein":  8.5, "fedt":  2.0, "kulhydrat": 7.0, "kcal":  80}),
    ("skyr jordbær",    {"protein":  8.5, "fedt":  0.5, "kulhydrat": 9.0, "kcal":  75}),
    ("skyr hindbær",    {"protein":  8.5, "fedt":  0.5, "kulhydrat": 9.0, "kcal":  75}),
    ("skyr mango",      {"protein":  8.5, "fedt":  0.5, "kulhydrat": 9.0, "kcal":  75}),
    ("skyr blåbær",     {"protein":  8.5, "fedt":  0.5, "kulhydrat": 9.0, "kcal":  75}),
    # Naturel skyr — eksplicit match sikrer korrekt score selv hvis nye entries tilføjes
    ("skyr naturel",    {"protein": 11.0, "fedt":  0.2, "kulhydrat": 4.0, "kcal":  63}),
    ("skyr natural",    {"protein": 11.0, "fedt":  0.2, "kulhydrat": 4.0, "kcal":  63}),
    ("naturel skyr",    {"protein": 11.0, "fedt":  0.2, "kulhydrat": 4.0, "kcal":  63}),
    # Generisk skyr (ukendt variant) — kun hvis ingen mere specifik match
    ("skyr",            {"protein": 11.0, "fedt":  0.2, "kulhydrat": 4.0, "kcal":  63}),
    ("hytteost",        {"protein": 12.0, "fedt":  4.0, "kulhydrat": 3.5, "kcal":  98}),
    ("cottage cheese",  {"protein": 12.0, "fedt":  4.0, "kulhydrat": 3.5, "kcal":  98}),
    ("kvark",           {"protein": 13.0, "fedt":  0.3, "kulhydrat": 3.8, "kcal":  70}),
    ("græsk yoghurt",   {"protein": 10.0, "fedt":  0.4, "kulhydrat": 3.8, "kcal":  59}),
    ("yoghurt",         {"protein":  5.0, "fedt":  3.0, "kulhydrat": 5.0, "kcal":  65}),
    ("æg",              {"protein": 13.0, "fedt": 11.0, "kulhydrat": 1.0, "kcal": 155}),
    # Smøre-/smelteost har langt mindre protein end fast ost
    ("hvid ost",        {"protein": 16.0, "fedt": 24.0, "kulhydrat": 1.0, "kcal": 285}),
    ("salatost",        {"protein": 16.0, "fedt": 24.0, "kulhydrat": 1.0, "kcal": 285}),
    ("feta",            {"protein": 16.0, "fedt": 21.0, "kulhydrat": 1.5, "kcal": 265}),
    ("smøreost",        {"protein": 10.0, "fedt": 23.0, "kulhydrat": 2.5, "kcal": 255}),
    ("smelteost",       {"protein": 11.0, "fedt": 17.0, "kulhydrat": 5.0, "kcal": 215}),
    ("flødeost",        {"protein":  9.0, "fedt": 28.0, "kulhydrat": 3.0, "kcal": 300}),
    ("friskost",        {"protein":  8.0, "fedt": 25.0, "kulhydrat": 3.0, "kcal": 270}),
    ("pizzatopping",    {"protein": 22.0, "fedt": 21.0, "kulhydrat": 1.5, "kcal": 285}),
    ("mozzarella",      {"protein": 18.0, "fedt": 17.0, "kulhydrat": 0.7, "kcal": 280}),
    ("ost",             {"protein": 25.0, "fedt": 30.0, "kulhydrat": 0.5, "kcal": 370}),
    ("edamame",         {"protein": 11.0, "fedt":  5.0, "kulhydrat": 8.0, "kcal": 121}),
    ("tempeh",          {"protein": 19.0, "fedt": 11.0, "kulhydrat": 9.0, "kcal": 193}),
    ("tofu",            {"protein": 12.0, "fedt":  7.0, "kulhydrat": 2.0, "kcal": 120}),
    ("linser",          {"protein":  9.0, "fedt":  0.4, "kulhydrat":20.0, "kcal": 116}),
    ("kikærter",        {"protein":  9.0, "fedt":  2.6, "kulhydrat":27.0, "kcal": 164}),
]

# ── Brand/produkt-specifikke næringsdata ──────────────────────────────────────
# Overstyrer NAERING_LOOKUP når produktnavnet matcher præcist (delstreng, lowercase).
# Kilde: faktiske produktemballager / Open Food Facts.
# Tilføj nye produkter her efterhånden som fejl opdages.
BRAND_OVERRIDES: list[tuple[str, dict]] = [
    # Stay Strong Skyr (creamy, højt fedt, lavt protein vs. naturel skyr)
    ("stay strong skyr", {"protein": 8.0, "fedt": 2.5, "kulhydrat": 6.0, "kcal": 78}),
    ("stay strong creamy", {"protein": 8.0, "fedt": 3.5, "kulhydrat": 5.5, "kcal": 82}),
    # Tilføj fremtidige her:
    # ("brand produkt", {"protein": X, "fedt": X, "kulhydrat": X, "kcal": X}),
]


def brand_override(tekst: str) -> dict | None:
    """
    Returnerer næringsdict hvis produktnavnet matcher en brand-override.
    Prioriteres over NAERING_LOOKUP.
    """
    tekst_l = tekst.lower()
    for keyword, naering in BRAND_OVERRIDES:
        if keyword in tekst_l:
            return naering
    return None


# ── Ekskluderede butikker ─────────────────────────────────────────────────────
# Butikker der ikke sælger dagligvarer — filtreres fra uanset produkt.
# Kun dagligvarekæder medtages (whitelist) — sammenlignes uden store/små bogstaver
DAGLIGVAREKAEDER = {
    "netto", "føtex", "bilka", "lidl", "rema 1000", "superbrugsen", "kvickly",
    "meny", "365discount", "coop 365", "spar", "løvbjerg", "min købmand",
    "let-køb", "abc lavpris", "brugsen", "dagli'brugsen", "lokal brugsen",
    "irma", "fakta", "nærkøb", "salling",
}


def er_dagligvarebutik(navn: str) -> bool:
    return (navn or "").strip().lower() in DAGLIGVAREKAEDER


EKSKLUDER_BUTIKKER = {
    # Non-dagligvare (hardware, elektronik, dyrehandel)
    "Biltema", "Maxi Zoo", "POWER", "Boligcenter.dk", "Hjem.dk",
    "BAUHAUS", "Harald Nyborg", "HOME-TEX.dk", "jem & fix", "Silvan",
    # Tyske butikker / grænsehandel
    "Kaufland", "CITTI", "Fleggaard", "fakta Tyskland",
    "Nielsen Scan-Shop", "Scandinavian Park",
    # Øvrige ikke-relevante
    "Calle",
}

# ── Udelukkelses-keywords ────────────────────────────────────────────────────
# Produkter hvis navn indeholder disse ord springes over.
# Bruges til at filtrere false positives: mel, grøntsager, bageri, saucer, osv.
UDELUK_KEYWORDS = [
    # Mel og bageri
    "mel", "hvedemel", "rugmel", "majsmel", "mandelmel",
    "bagemarked", "bageri", "brød", "boller", "kage", "wienerbrød",
    "croissant", "toast", "knækbrød", "crisp",
    "bagværk", "butterdej", "indbagt",
    # Grøntsager der matcher "ost" eller andre keywords fejlagtigt
    "grøntsager", "grønsager", "grønt", "salat", "spinat", "broccoli",
    "blomkål", "gulerod", "tomat", "agurk", "peberfrugt", "løg", "hvidløg",
    "kartofler", "kartoffel",
    # Saucer, dressinger og tilbehør
    "ketchup", "mayonnaise", "dressing", "sovs", "marinade",
    "sennep", "remoulade",
    # Drikkevarer
    "juice", "smoothie", "sodavand", "øl", "vin", "kaffe", "te",
    # Snacks og slik
    "chips", "slik", "chokolade", "is ", "iscreme", "sorbet",
    # Dyrefoder og dyreartikler
    "foder", "hundefoder", "kattefoder", "hvalpe", "kattegrus",
    "fuglemad", "fuglefoder",
    # Andet ikke-protein
    "rengøring", "vaskemiddel", "shampoo", "tandpasta",
]

# Kategorimapping fra keyword → category/subcategory
KATEGORI_MAP: dict[str, tuple[str, str]] = {
    "kylling":        ("Kød & Fjerkræ", "Fjerkræ"),
    "kyllingebryst":  ("Kød & Fjerkræ", "Fjerkræ"),
    "kyllingefilet":  ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkun":         ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunbryst":    ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunfilet":    ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunschnitzel":("Kød & Fjerkræ", "Fjerkræ"),
    "hakket kalkun":  ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunkød":      ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkununderlår": ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunoverlår":  ("Kød & Fjerkræ", "Fjerkræ"),
    "kalkunlår":      ("Kød & Fjerkræ", "Fjerkræ"),
    "oksekød":        ("Kød & Fjerkræ", "Oksekød"),
    "hakket oksekød": ("Kød & Fjerkræ", "Oksekød"),
    "bøf":            ("Kød & Fjerkræ", "Oksekød"),
    "ribeye":         ("Kød & Fjerkræ", "Oksekød"),
    "entrecôte":      ("Kød & Fjerkræ", "Oksekød"),
    "svinekød":       ("Kød & Fjerkræ", "Svinekød"),
    "nakkefilet":     ("Kød & Fjerkræ", "Svinekød"),
    "svinemørbrad":   ("Kød & Fjerkræ", "Svinekød"),
    "koteletter":     ("Kød & Fjerkræ", "Svinekød"),
    "laks":           ("Fisk & Skaldyr", "Fisk"),
    "laksefilet":     ("Fisk & Skaldyr", "Fisk"),
    "tun":            ("Fisk & Skaldyr", "Fisk"),
    "makrel":         ("Fisk & Skaldyr", "Fisk"),
    "sild":           ("Fisk & Skaldyr", "Fisk"),
    "torsk":          ("Fisk & Skaldyr", "Fisk"),
    "rødspætte":      ("Fisk & Skaldyr", "Fisk"),
    "rejer":          ("Fisk & Skaldyr", "Skaldyr"),
    "skyr":           ("Mejeri", "Skyr & Yoghurt"),
    "hytteost":       ("Mejeri", "Ost & Quark"),
    "cottage cheese": ("Mejeri", "Ost & Quark"),
    "kvark":          ("Mejeri", "Ost & Quark"),
    "græsk yoghurt":  ("Mejeri", "Skyr & Yoghurt"),
    "yoghurt":        ("Mejeri", "Skyr & Yoghurt"),
    "æg":             ("Mejeri", "Æg"),
    "hvid ost":       ("Mejeri", "Ost & Quark"),
    "salatost":       ("Mejeri", "Ost & Quark"),
    "feta":           ("Mejeri", "Ost & Quark"),
    "smøreost":       ("Mejeri", "Ost & Quark"),
    "smelteost":      ("Mejeri", "Ost & Quark"),
    "flødeost":       ("Mejeri", "Ost & Quark"),
    "friskost":       ("Mejeri", "Ost & Quark"),
    "pizzatopping":   ("Mejeri", "Ost & Quark"),
    "ost":            ("Mejeri", "Ost & Quark"),
    "mozzarella":     ("Mejeri", "Ost & Quark"),
    "edamame":        ("Grønt", "Bælgfrugter"),
    "tempeh":         ("Vegetar & Vegansk", "Planteprotein"),
    "tofu":           ("Vegetar & Vegansk", "Planteprotein"),
    "linser":         ("Kolonial", "Bælgfrugter"),
    "kikærter":       ("Kolonial", "Bælgfrugter"),
    "protein":        ("Sport & Sundhed", "Proteinprodukter"),
}

# ── Hjælpefunktioner ──────────────────────────────────────────────────────────

def slå_naering_op(tekst: str) -> tuple[dict | None, str | None]:
    """
    Returnerer (næringsdict, matched_keyword) for første match.
    Tjekker BRAND_OVERRIDES (præcise data) før NAERING_LOOKUP (estimater).
    """
    # 1. Brand-specifikke overrides (faktiske emballagedata)
    override = brand_override(tekst)
    if override is not None:
        return override, "brand-override"
    # 2. Kategori-estimater
    tekst_l = tekst.lower()
    for keyword, naering in NAERING_LOOKUP:
        if keyword in tekst_l:
            return naering, keyword
    return None, None


def _normalisér_tekst(tekst: str) -> str:
    """Fælles forbehandling til vægt-parsing."""
    t = tekst.lower().replace(",", ".")
    return t.replace("½", "0.5").replace("¼", "0.25").replace("¾", "0.75")


def ekstraher_vaegt_interval(tekst: str) -> tuple[float | None, float | None]:
    """
    Returnerer (min_g, max_g) fra tekst som "800-1600 g" eller "0.8-1.6 kg".
    Hvis intet interval: (enkelt-vægt-eller-None, None).
    """
    t = _normalisér_tekst(tekst)
    m = re.search(r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*(kg|g)\b", t)
    if m:
        v1    = float(m.group(1))
        v2    = float(m.group(2))
        enhed = m.group(3)
        f     = 1000 if enhed == "kg" else 1
        return min(v1, v2) * f, max(v1, v2) * f
    single = ekstraher_vaegt(tekst)
    return single, None


def ekstraher_vaegt(tekst: str) -> float | None:
    """
    Forsøger at udtrække vægt i gram fra produkttekst.
    Håndterer: '500g', '500 g', '1 kg', '1,5 kg', '2x500g', '6 stk', '12 stk' (æg),
               '1/2 kg' (brøker), '½ kg' (unicode-brøk),
               '800-1600 g' (intervaller → bruger minimum, konservativt estimat).

    Returnerer gram eller None.
    """
    t = _normalisér_tekst(tekst)

    # "6 stk" eller "12 stk" → æg ≈ 60g/stk
    m = re.search(r"(\d+)\s*stk", t)
    if m:
        stk = int(m.group(1))
        if "æg" in t or stk in (4, 6, 10, 12, 15, 18, 20, 24, 30):
            return stk * 60.0

    # BRØKER: "1/2 kg" → 500g, "3/4 kg" → 750g
    m = re.search(r"(\d+)/(\d+)\s*(kg|g)\b", t)
    if m:
        taeller = float(m.group(1))
        naevner = float(m.group(2))
        enhed   = m.group(3)
        if naevner > 0:
            return (taeller / naevner) * (1000 if enhed == "kg" else 1)

    # NYT: Interval "800-1600 g" → brug minimum (konservativt)
    m = re.search(r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*(kg|g)\b", t)
    if m:
        v1    = float(m.group(1))
        v2    = float(m.group(2))
        enhed = m.group(3)
        f     = 1000 if enhed == "kg" else 1
        return min(v1, v2) * f  # konservativt: brug mindste vægt

    # "2 x 500 g" eller "2x500g"
    m = re.search(r"(\d+)\s*x\s*(\d+\.?\d*)\s*(kg|g)", t)
    if m:
        antal = float(m.group(1))
        vaegt = float(m.group(2))
        enhed = m.group(3)
        return antal * vaegt * (1000 if enhed == "kg" else 1)

    # "500 g" eller "500g"
    m = re.search(r"(?<![/\d])(\d+\.?\d*)\s*(kg|g)\b", t)
    if m:
        vaegt = float(m.group(1))
        enhed = m.group(2)
        return vaegt * (1000 if enhed == "kg" else 1)

    return None


def produkt_id(offer_id: str, butik: str) -> int:
    """
    Genererer et unikt integer produkt-ID fra offer-ID og butiknavn.
    Bruger 9-cifret hash med 9-prefix for at undgå kollision med Remas ID-rum.
    """
    h = hashlib.md5(f"{butik}:{offer_id}".encode()).hexdigest()
    return 900_000_000 + (int(h[:8], 16) % 99_999_999)


HENT_FEJL: list[str] = []  # fejl fra API-kald (bruges af opdater.py til status)


def hent_tilbud(query: str, offset: int = 0) -> list[dict]:
    """Kalder eTilbudsavis API og returnerer rå tilbudsliste."""
    params = urllib.parse.urlencode({
        "r_lat":     LAT,
        "r_lng":     LNG,
        "r_radius":  RADIUS,
        "r_locale":  LOCALE,
        "api_av":    API_VERSION,
        "query":     query,
        "offset":    offset,
        "limit":     LIMIT,
    })
    url = f"{API_BASE}?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BilligProtein/3.0 (hobbyprojekt; prissammenligning af protein)",
            "Accept":     "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"  ⚠ Fejl ved hentning af '{query}': {e}")
        HENT_FEJL.append(f"{query}: {e}")
        return []


def parse_tilbud(offer: dict, matched_query: str) -> dict | None:
    """
    Konverterer et eTilbudsavis-tilbud til Protein-Jæger produktformat.
    Returnerer None hvis produktet ikke har nok data.
    """
    heading     = offer.get("heading", "").strip()
    description = offer.get("description", "") or ""
    price       = (offer.get("pricing") or {}).get("price")
    butik_navn  = (offer.get("branding") or {}).get("name", "Ukendt")
    run_from    = (offer.get("run_from") or "")[:10]
    run_till    = (offer.get("run_till") or "")[:10]
    offer_id    = offer.get("id", "")
    images      = offer.get("images") or {}
    image_url   = images.get("view") or images.get("zoom") or None

    if not heading or price is None:
        return None

    # Ekskluder non-dagligvare-butikker
    if butik_navn in EKSKLUDER_BUTIKKER or not er_dagligvarebutik(butik_navn):
        return None

    # Udelukkelsesfilter — spring over hvis produktnavn matcher et non-protein keyword
    heading_l = heading.lower()
    for udeluk in UDELUK_KEYWORDS:
        if udeluk in heading_l:
            return None

    # Vægt — krævet for at beregne pris/g protein
    fuld_tekst = f"{heading} {description}"
    min_g, max_g = ekstraher_vaegt_interval(fuld_tekst)
    vaegt_g = min_g  # brug minimum (konservativt ved intervaller)
    vaegt_variabel = max_g is not None and max_g != min_g
    if vaegt_g is None or vaegt_g <= 0:
        return None  # ingen vægt → kan ikke beregne pris/g → spring over

    # Næringsestimat
    naering, matched_kw = slå_naering_op(fuld_tekst)
    if naering is None:
        # Fallback: brug søgeordet der fandt produktet
        naering, matched_kw = slå_naering_op(matched_query)
    if naering is None:
        return None  # kan ikke estimere protein → spring over

    protein_per_100g   = naering["protein"]
    fedt_per_100g      = naering["fedt"]
    kulhydrat_per_100g = naering["kulhydrat"]
    kcal_per_100g      = naering["kcal"]

    # Pris per gram protein
    if vaegt_g and vaegt_g > 0 and protein_per_100g > 0:
        protein_gram_total = vaegt_g * protein_per_100g / 100
        price_per_gram_protein = round(price / protein_gram_total, 4) if protein_gram_total > 0 else None
        kcal_per_gram_protein  = round(kcal_per_100g / protein_per_100g, 2) if protein_per_100g > 0 else None
    else:
        price_per_gram_protein = None
        kcal_per_gram_protein  = None

    # Kategori
    kat, subkat = KATEGORI_MAP.get(matched_kw or "", ("Andet", "Andet"))

    # underline (samme format som Rema: "500 G / BUTIK")
    if vaegt_variabel and max_g:
        underline_vaegt = f"{vaegt_g:.0f}–{max_g:.0f} G (min.)"
    else:
        underline_vaegt = f"{vaegt_g:.0f} G" if vaegt_g else description[:40].upper()
    underline = f"{underline_vaegt} / {butik_navn.upper()}"

    return {
        "product_id":           produkt_id(offer_id, butik_navn),
        "name":                 heading.upper(),
        "underline":            underline,
        "category":             kat,
        "subcategory":          subkat,
        "butik":                butik_navn,
        "price_dkk":            round(float(price), 2),
        "weight_g":             vaegt_g,
        "weight_g_max":         max_g if vaegt_variabel else None,
        "vaegt_variabel":       vaegt_variabel,
        "protein_per_100g":     protein_per_100g,
        "fedt_per_100g":        fedt_per_100g,
        "kulhydrat_per_100g":   kulhydrat_per_100g,
        "kcal_per_100g":        kcal_per_100g,
        "price_per_gram_protein":  price_per_gram_protein,
        "kcal_per_gram_protein":   kcal_per_gram_protein,
        "tilbud_start":         run_from or None,
        "tilbud_slut":          run_till or None,
        "image_url":            image_url,
        "sanity_flags":         [],
        "naering_kilde":        "estimat",  # markerer at data er estimeret
    }



def parse_eller_tilbud(offer: dict, matched_query: str) -> list[dict]:
    """
    Splitter "A eller B"-tilbud i separate rækker — ét per del-produkt.
    Begge arver pris; vægt = minimum fra intervallet (konservativt estimat).
    Returnerer tom liste hvis splittet fejler eller data mangler.
    """
    heading    = offer.get("heading", "").strip()
    desc       = offer.get("description", "") or ""
    price      = (offer.get("pricing") or {}).get("price")
    butik_navn = (offer.get("branding") or {}).get("name", "Ukendt")
    run_from   = (offer.get("run_from") or "")[:10]
    run_till   = (offer.get("run_till") or "")[:10]
    offer_id   = offer.get("id", "")
    images     = offer.get("images") or {}
    image_url  = images.get("view") or images.get("zoom") or None

    if not heading or price is None or butik_navn in EKSKLUDER_BUTIKKER or not er_dagligvarebutik(butik_navn):
        return []

    # Udeluk non-protein keywords
    heading_l = heading.lower()
    for udeluk in UDELUK_KEYWORDS:
        if udeluk in heading_l:
            return []

    # Minimum-vægt fra interval
    fuld_tekst = f"{heading} {desc}"
    min_g, max_g = ekstraher_vaegt_interval(fuld_tekst)
    if min_g is None or min_g <= 0:
        return []

    vaegt_variabel = max_g is not None and max_g != min_g

    # Split på "eller" og kommaer ("Kyllingelår, -spyd eller -bryst")
    dele = [d.strip() for d in re.split(r"\s+eller\s+|,\s*", heading, flags=re.IGNORECASE) if d.strip()]
    if len(dele) < 2:
        return []

    produkter = []
    # Første del bruges som kontekst for varianter uden fødevareord (fx "vanilje")
    foerste_del = dele[0]
    prefix_ord = foerste_del.split()
    kontekst_prefix = " ".join(prefix_ord[:min(3, max(1, len(prefix_ord)-1))])
    STAMMER = ("kyllinge", "kylling", "kalkun", "svine", "okse", "kalve", "lakse", "fiske", "torske")

    # "Kyllingeover- eller underlår" → "Kyllingeoverlår" + "underlår"
    SUFFIKSER = ("lår", "filet", "bryst", "kød", "steg", "pølser", "fars", "vinger", "ost")
    for i in range(len(dele) - 1):
        if dele[i].endswith("-"):
            naeste = dele[i + 1].split()[-1].lower()
            suffiks = next((sf for sf in SUFFIKSER if naeste.endswith(sf)), "")
            dele[i] = dele[i][:-1] + suffiks

    set_navne: set[str] = set()
    for idx, del_navn in enumerate(dele):
        # "-spyd" efter "Kyllingelår" → "Kyllingespyd"
        if del_navn.startswith("-"):
            rod = foerste_del.split()[0].lower() if foerste_del else ""
            stamme = next((st for st in STAMMER if rod.startswith(st)), rod)
            del_navn = (stamme + del_navn[1:]).strip()
        # Variant uden fødevareord (fx "vanilje") → tilføj kontekst fra første del
        elif idx > 0 and slå_naering_op(del_navn)[0] is None:
            del_navn = f"{kontekst_prefix} {del_navn}"
        if del_navn.lower() in set_navne:
            continue
        set_navne.add(del_navn.lower())

        # Næringsdata: søg i del-produktets navn, derefter fallback til matched_query
        naering, matched_kw = slå_naering_op(del_navn)
        if naering is None:
            naering, matched_kw = slå_naering_op(matched_query)
        if naering is None:
            continue

        prot  = naering["protein"]
        fedt  = naering["fedt"]
        kulhy = naering["kulhydrat"]
        kcal  = naering["kcal"]

        prot_total = min_g * prot / 100
        pgp = round(price / prot_total, 4) if prot_total > 0 else None
        kgp = round(kcal / prot, 2)        if prot > 0        else None

        kat, subkat = KATEGORI_MAP.get(matched_kw or "", ("Andet", "Andet"))

        if vaegt_variabel and max_g:
            underline_vaegt = f"{min_g:.0f}–{max_g:.0f} G (min.)"
        else:
            underline_vaegt = f"{min_g:.0f} G"
        underline = f"{underline_vaegt} / {butik_navn.upper()}"

        # Unikt ID: hash af original offer-id + del-index
        uid = hashlib.md5(f"{butik_navn}:{offer_id}:eller:{idx}".encode()).hexdigest()
        pid = 900_000_000 + (int(uid[:8], 16) % 99_999_999)

        produkter.append({
            "product_id":              pid,
            "name":                    del_navn.upper(),
            "underline":               underline,
            "category":                kat,
            "subcategory":             subkat,
            "butik":                   butik_navn,
            "price_dkk":               round(float(price), 2),
            "weight_g":                min_g,
            "weight_g_max":            max_g if vaegt_variabel else None,
            "vaegt_variabel":          vaegt_variabel,
            "protein_per_100g":        prot,
            "fedt_per_100g":           fedt,
            "kulhydrat_per_100g":      kulhy,
            "kcal_per_100g":           kcal,
            "price_per_gram_protein":  pgp,
            "kcal_per_gram_protein":   kgp,
            "tilbud_start":            run_from or None,
            "tilbud_slut":             run_till or None,
            "image_url":               image_url,
            "sanity_flags":            [],
            "naering_kilde":           "estimat",
            "eller_original":          heading.upper(),
        })

    return produkter

# ── Hoved ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("Protein-Jæger — eTilbudsavis multi-butik scraper")
    print("=" * 60)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    alle_produkter: dict[int, dict] = {}
    set_offer_ids: set[str] = set()  # deduplikering på offer-ID

    for i, query in enumerate(PROTEIN_QUERIES, 1):
        print(f"[{i:02d}/{len(PROTEIN_QUERIES)}] Søger: '{query}' ...", end=" ", flush=True)

        tilbud = hent_tilbud(query)
        antal_nye = 0

        for offer in tilbud:
            oid = offer.get("id", "")
            if oid in set_offer_ids:
                continue  # allerede set via andet søgeord
            set_offer_ids.add(oid)

            # "A eller B"-tilbud → split i separate rækker
            heading_l = (offer.get("heading") or "").lower()
            if " eller " in heading_l:
                del_produkter = parse_eller_tilbud(offer, query)
                for produkt in del_produkter:
                    pid = produkt["product_id"]
                    if pid not in alle_produkter:
                        alle_produkter[pid] = produkt
                        antal_nye += 1
                if del_produkter:
                    continue  # ikke fall-through til parse_tilbud

            produkt = parse_tilbud(offer, query)
            if produkt is None:
                continue

            pid = produkt["product_id"]
            # Hold kun det med pris_per_gram_protein — lavere vinder ved kollision
            if pid not in alle_produkter:
                alle_produkter[pid] = produkt
                antal_nye += 1
            else:
                eksist = alle_produkter[pid]
                if (produkt.get("price_per_gram_protein") or 9999) < (eksist.get("price_per_gram_protein") or 9999):
                    alle_produkter[pid] = produkt
                    antal_nye += 1

        print(f"{len(tilbud)} tilbud hentet, {antal_nye} nye produkter")
        time.sleep(SLEEP_BETWEEN)

    produkter = list(alle_produkter.values())
    med_protein = [p for p in produkter if p.get("price_per_gram_protein") is not None]

    if not set_offer_ids and HENT_FEJL:
        raise RuntimeError(f"eTilbudsavis svarede ikke ({len(HENT_FEJL)} fejl) — fx {HENT_FEJL[0]}")

    print(f"\nI alt: {len(produkter)} unikke produkter, {len(med_protein)} med beregnet pris/g protein")

    # Sorter
    produkter.sort(key=lambda p: p.get("price_per_gram_protein") or 9999)

    butikker = sorted(set(p["butik"] for p in produkter))
    print(f"Butikker: {', '.join(butikker)}")

    output = {
        "meta": {
            "kilde":                 "eTilbudsavis API",
            "koordinater":           f"{LAT}°N {LNG}°Ø",
            "radius_km":             int(RADIUS) // 1000,
            "total_tilbud_fundet":   len(set_offer_ids),
            "produkter_med_protein": len(med_protein),
            "sorted_by":             "price_per_gram_protein",
            "naering_kilde":         "estimat (keyword-baseret, DTU Frida gennemsnit)",
            "genereret":             datetime.now().isoformat(timespec="seconds"),
            "butikker":              butikker,
        },
        "products": produkter,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Gemt: {OUTPUT_FILE}")
    print("\n── Top 10 preview ──────────────────────────────────────────────────────────")
    for j, p in enumerate(med_protein[:10], 1):
        print(
            f"  {j:2}. {p['name'][:40]:<40} "
            f"{p['butik']:<15} "
            f"{p.get('price_dkk', 0):.2f} kr  "
            f"{p.get('price_per_gram_protein', 0):.4f} kr/g protein"
        )


if __name__ == "__main__":
    main()
