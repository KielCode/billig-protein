"""
kurator.py — udvælger og beriger produkter til visning på Billig Protein.

Tager output/full_matrix_pdcaas.json (alle varer fra Rema 1000 + tilbud fra
eTilbudsavis) og laver den liste der vises på siden:

  1. Kun dagligvarebutikker og varer der faktisk er proteinkilder
     — EU's definition: "højt proteinindhold" kræver at mindst 20 % af
       energien kommer fra protein (forordning 1924/2006). Varer med 12–20 %
       ("kilde til protein") kan vises via en knap på siden.
  2. Udløbne tilbud fjernes.
  3. Hver vare får en hovedkategori (kylling, okse, fisk …) og flag for
     knogler, estimerede næringsdata, forarbejdning m.m.
  4. Prisen omregnes til kr pr. 100 g protein — rå og justeret for
     proteinkvalitet (PDCAAS).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime

# ── Grænser ───────────────────────────────────────────────────────────────────
EU_HOJT_PROTEIN = 0.20     # ≥ 20 % af energien fra protein → "højt proteinindhold"
EU_KILDE_PROTEIN = 0.12    # ≥ 12 % → "kilde til protein"
MIN_PROTEIN_G = 5.0        # g protein pr. 100 g (grøntsager, sodavand m.m. ryger ud)
MIN_PROTEIN_G_MEJERI = 3.0 # mælk og yoghurt
MIN_EFF_KR = 1.0           # under 1 kr pr. 100 g protein = næsten altid en datafejl

# ── Butikker ──────────────────────────────────────────────────────────────────
BUTIK_NAVN = {
    "rema 1000": "Rema 1000", "rema1000": "Rema 1000",
    "netto": "Netto", "føtex": "føtex", "bilka": "Bilka", "lidl": "Lidl",
    "superbrugsen": "SuperBrugsen", "kvickly": "Kvickly", "meny": "MENY",
    "365discount": "Coop 365", "coop 365": "Coop 365", "spar": "SPAR",
    "løvbjerg": "Løvbjerg", "min købmand": "Min Købmand", "let-køb": "LET-KØB",
    "abc lavpris": "ABC Lavpris", "brugsen": "Brugsen", "dagli'brugsen": "Dagli'Brugsen",
    "lokal brugsen": "Lokal Brugsen", "irma": "Irma", "fakta": "fakta", "nærkøb": "Nærkøb",
}

# ── Udelukkelser ──────────────────────────────────────────────────────────────
UDELUK_KATEGORIER = {
    "drikkevarer", "slik", "baby og småbørn", "husholdning", "personlig pleje",
    "dyr", "kiosk", "non food", "brød & bavinchi",
}
TILLAD_SUBKATEGORIER = {"plantedrikke", "kosttilskud"}
UDELUK_SUBKATEGORIER = {
    "gær", "krydderier", "kaffe", "te", "instant kaffe", "chips og snacks",
    "dressing", "ketchup, remoulade, mayonnaise m.v.", "olie, eddike & balsamico",
    "mel, sukker, bagning", "is og dessert", "saft m.v.", "energidrikke",
    "sodavand, vand, smoothies m.v.", "smør & fedtstoffer", "fløde m.v.",
    "marmelade & chokolade pålæg m.v.", "kiks, kager & knækbrød", "dej",
    "is & dessert", "margarine", "frys-selv-is", "kaffetilbehør",
}
UDELUK_NAVNEORD = [
    "gær", "sauce", "sovs", "fond", "bouillon", "kakao", "latte", "espresso",
    "cappuccino", "iskaffe", "milkshake", "smoothie", "juice", "saft", "chips",
    "tarteletfyld", "dressing", "pesto", "marinade", "krydderi", "dip",
    "hundemad", "kattemad", "foder",
]

# ── Kategorier (vises som kort øverst på siden) ──────────────────────────────
KATEGORIER = [
    {"key": "kylling", "navn": "Kylling & kalkun", "ikon": "🍗"},
    {"key": "okse",    "navn": "Okse, lam & vildt", "ikon": "🥩"},
    {"key": "svin",    "navn": "Svinekød",         "ikon": "🥓"},
    {"key": "fisk",    "navn": "Fisk & skaldyr",   "ikon": "🐟"},
    {"key": "aeg",     "navn": "Æg",               "ikon": "🥚"},
    {"key": "mejeri",  "navn": "Skyr & mejeri",    "ikon": "🥛"},
    {"key": "ost",     "navn": "Ost",              "ikon": "🧀"},
    {"key": "plante",  "navn": "Plantebaseret",    "ikon": "🌱"},
    {"key": "paalaeg", "navn": "Pålæg & pølser",   "ikon": "🥪"},
    {"key": "protein", "navn": "Proteinprodukter", "ikon": "💪"},
    {"key": "andet",   "navn": "Færdigretter & andet", "ikon": "🍽️"},
]

PAALAEG_SUB = {"pålæg", "pålægs salater", "pålægssalater", "postej/pate", "pølser", "delikatesser"}
AEG_ORD = {"æg", "skrabeæg", "frilandsæg", "buræg", "økoæg", "hønseæg",
           "æggehvide", "æggehvider", "skrabeæggehvide"}
NAVNEGRUPPER = [  # rækkefølgen betyder noget: første match vinder
    ("fisk", ["laks", "tun ", "tun,", "tunfisk", "makrel", "sild", "torsk", "rejer",
              "skaldyr", "fisk", "rødspætte", "sej", "kuller", "ørred", "salmon",
              "tuna", "shrimp", "hellefisk", "muslinger", "krabbe", "ansjos", "sardin"]),
    ("kylling", ["kylling", "chicken", "kalkun", "turkey", "fjerkræ", "andebryst",
                 "andelår", "hel and", "underlår", "overlår", "vinger", "drumstick", "wings"]),
    ("okse", ["okse", "beef", "bøf", "entrecote", "entrecôte", "ribeye", "kalv",
              "lam ", "lammek", "lammeb", "tyksteg", "culotte", "flanksteak",
              "roastbeef", "hakket kød", "vildt", "hjort", "vildsvin", "dådyr", "rådyr"]),
    ("svin", ["svin", "gris", "flæsk", "nakke", "mørbrad", "kotelet", "skinke",
              "bacon", "spareribs", "spare ribs", "pulled pork", "pork", "medister",
              "ribben", "hamburgerryg", "kamsteg", "prosciutto", "serrano",
              "parmaskinke", "salami", "chorizo"]),
    ("plante", ["linse", "bønne", "kikært", "ærter", "ærteprotein", "tofu", "tempeh",
                "edamame", "soja", "hummus", "falafel", "plantebaseret", "vegansk",
                "vegetar", "seitan", "quorn"]),
    ("mejeri", ["skyr", "kvark", "hytteost", "cottage", "yoghurt", "yogurt", "ymer",
                "mælk", "a38", "kefir", "fromage frais", "protein drik", "proteindrik"]),
    ("ost", ["mozzarella", "pizzatopping", "parmesan", "feta", "cheddar", "danbo",
             "havarti", "gouda", "brie", "halloumi", "mascarpone", "ricotta", "riven ost",
             "revet ost"]),
]
IKKE_OST = {"frost", "kost", "most", "post", "rost", "host", "toast", "hytteost"}
UDBENET_ORD = ["udben", "filet", "uden ben", "strimler", "inderfilet"]

# Varer med knogler og deres spiselige andel af vægten (ca.-værdier; første match vinder).
# Næringsdata på varedeklarationer gælder det spiselige kød — men pakkens vægt
# inkluderer knoglerne, så proteinet i pakken skal nedskrives.
SPISELIG_ANDEL = [
    (r"vinger|wings|chicken wing", 0.60),
    (r"spare ?ribs", 0.60),
    (r"underlår|drumstick|kyllingeklubbe|chicken drum", 0.70),
    (r"overlår|chicken thigh", 0.78),
    (r"kyllingelår|kalkunlår|\blår\b", 0.75),
    (r"\bhele?\s+(?:[a-zæøå.]+\s+){0,3}kylling|\bkylling,?\s+hel\b", 0.70),
    (r"\bhele?\s+(?:[a-zæøå.]+\s+){0,2}and\b", 0.65),
    (r"ribben", 0.80),
]


def spiselig_andel(navn: str) -> float:
    n = (navn or "").lower()
    if any(o in n for o in UDBENET_ORD):
        return 1.0
    return next((a for mønster, a in SPISELIG_ANDEL if re.search(mønster, n)), 1.0)


def i_dag() -> date:
    """Dagens dato i Danmark (kan overstyres med BILLIG_PROTEIN_DATO=YYYY-MM-DD ved test)."""
    override = os.getenv("BILLIG_PROTEIN_DATO")
    if override:
        return date.fromisoformat(override)
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Copenhagen")).date()
    except Exception:
        return date.today()


def pænt_navn(navn: str) -> str:
    """'1000G  HAKKET KYLLING 🐓' → 'Hakket kylling'."""
    n = re.sub(r"[^\w\s%,.+&/'()\-–½]", " ", navn or "")      # fjern emojis o.l.
    n = re.sub(r"^\s*\d+[.,]?\d*\s*(g|kg|gr)\b\.?\s*", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" *-,")
    if not n:
        return (navn or "").strip()
    n = n.lower()
    n = re.sub(r"\brema 1000\b", "Rema 1000", n)
    return n[0].upper() + n[1:]


def _ord(n: str) -> list[str]:
    return re.findall(r"[a-zæøåéü0-9]+", n)


def find_kategori(p: dict) -> str:
    n = f" {(p.get('name') or '').lower()} "
    sub = (p.get("subcategory") or "").lower()
    kat = (p.get("category") or "").lower()
    ordliste = _ord(n)

    if sub in PAALAEG_SUB:
        return "paalaeg"
    if "protein" in n and (sub in {"kosttilskud", "chokolade m.v.", "chips og snacks"}
                           or any(k in n for k in ("pulver", "bar", "whey", "chips", "shake"))):
        return "protein"
    if sub == "æg" or any(o in AEG_ORD for o in ordliste):
        return "aeg"
    if "hytteost" in n or "cottage" in n or sub == "hytteost":
        return "mejeri"
    for key, ord_ in NAVNEGRUPPER:
        if any(o in n for o in ord_):
            return key
    if "ost" in sub or kat == "ost m.v." or any(o.endswith("ost") and o not in IKKE_OST for o in ordliste):
        return "ost"
    # Fald tilbage på butikkens egne kategorier
    if sub == "and":
        return "kylling"
    if sub == "vildt":
        return "okse"
    for key, ord_ in [("okse", ["oksekød", "lam"]), ("svin", ["gris", "svinekød", "bacon"]),
                      ("fisk", ["fisk", "skaldyr"]), ("kylling", ["kylling", "fjerkræ"]),
                      ("mejeri", ["yoghurt", "skyr", "mælk", "syrnede", "koldskål"]),
                      ("plante", ["plantebase", "bælgfrugter", "planteprotein"]),
                      ("protein", ["kosttilskud", "proteinprodukter"])]:
        if any(o in sub for o in ord_):
            return key
    return "andet"


def er_ben_i(navn: str) -> bool:
    return spiselig_andel(navn) < 1.0


def er_udelukket(p: dict) -> bool:
    n = (p.get("name") or "").lower()
    sub = (p.get("subcategory") or "").lower()
    kat = (p.get("category") or "").lower()
    har_protein_ord = "protein" in n
    if kat in UDELUK_KATEGORIER and sub not in TILLAD_SUBKATEGORIER and not har_protein_ord:
        return True
    if sub in UDELUK_SUBKATEGORIER and not har_protein_ord:
        return True
    if any(o in n for o in UDELUK_NAVNEORD) and not (har_protein_ord and "chips" in n):
        return True
    return False


def _tal(v) -> float | None:
    try:
        return abs(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def kurater(produkter: list[dict]) -> tuple[list[dict], dict]:
    """Returnerer (liste til visning, statistik)."""
    dag = i_dag().isoformat()
    ud: list[dict] = []
    set_noegler: set[tuple] = set()
    stat = {"ind": len(produkter), "udloebet": 0, "ikke_butik": 0, "udelukket": 0,
            "lavt_protein": 0, "datafejl": 0, "dubletter": 0}

    for p in produkter:
        butik = BUTIK_NAVN.get((p.get("butik") or "Rema 1000").strip().lower())
        if not butik:
            stat["ikke_butik"] += 1
            continue

        slut = (p.get("tilbud_slut") or "")[:10] or None
        start = (p.get("tilbud_start") or "")[:10] or None
        if slut and slut < dag:
            stat["udloebet"] += 1
            continue

        pris, vaegt = _tal(p.get("price_dkk")), _tal(p.get("weight_g"))
        prot, kcal = _tal(p.get("protein_per_100g")), _tal(p.get("kcal_per_100g"))
        fedt, kulh = _tal(p.get("fedt_per_100g")), _tal(p.get("kulhydrat_per_100g"))
        pdcaas = _tal(p.get("pdcaas"))
        if not (pris and vaegt and prot and pdcaas) or prot > 95 or any(
                "physical maximum" in f for f in (p.get("sanity_flags") or [])):
            stat["datafejl"] += 1
            continue

        if er_udelukket(p):
            stat["udelukket"] += 1
            continue

        kat = find_kategori(p)
        if not kcal and fedt is not None and kulh is not None:
            kcal = 4 * prot + 4 * kulh + 9 * fedt
        andel = (prot * 4 / kcal) if kcal else None
        min_prot = MIN_PROTEIN_G_MEJERI if kat == "mejeri" else MIN_PROTEIN_G
        if prot < min_prot or (andel is not None and andel < EU_KILDE_PROTEIN) or (andel is None and prot < 10):
            stat["lavt_protein"] += 1
            continue
        andel = min(andel, 1.0) if andel is not None else None

        spiselig = spiselig_andel(p.get("name") or "")
        protein_i_pakken = vaegt * prot / 100 * spiselig
        raa = pris / protein_i_pakken * 100          # kr pr. 100 g protein
        eff = raa / pdcaas                            # kr pr. 100 g optageligt protein
        if eff < MIN_EFF_KR:
            stat["datafejl"] += 1
            continue

        navn = pænt_navn(p.get("name") or "")
        noegle = (butik, navn.lower(), round(pris, 2), round(vaegt))
        if noegle in set_noegler:
            stat["dubletter"] += 1
            continue
        set_noegler.add(noegle)

        metode = (p.get("pdcaas_method") or "").split(":", 1)[-1]
        if metode == "default":
            metode = "ukendt varetype (standardværdi)"
        ud.append({
            "id": str(p.get("product_id") or len(ud)),
            "navn": navn,
            "butik": butik,
            "kat": kat,
            "pris": round(pris, 2),
            "vaegt": round(vaegt),
            "vaegt_max": round(_tal(p.get("weight_g_max"))) if p.get("vaegt_variabel") and p.get("weight_g_max") else None,
            "prot": round(prot, 1),
            "fedt": round(fedt, 1) if fedt is not None else None,
            "kulh": round(kulh, 1) if kulh is not None else None,
            "kcal": round(kcal) if kcal else None,
            "andel": round(andel * 100) if andel is not None else None,
            "hoj": andel is None or andel >= EU_HOJT_PROTEIN,
            "pdcaas": round(pdcaas, 2),
            "pdcaas_grundlag": metode,
            "raa": round(raa, 2),
            "eff": round(eff, 2),
            "start": start,
            "slut": slut,
            "img": p.get("image_url"),
            "url": p.get("url"),
            "est": p.get("naering_kilde") == "estimat",
            "forarb": bool(p.get("is_processed")),
            "usikker": bool(p.get("naering_usikker")),
            "ben": spiselig < 1.0,
            "spiselig": spiselig if spiselig < 1.0 else None,
            "eller": pænt_navn(p.get("eller_original")) if p.get("eller_original") else None,
        })

    ud.sort(key=lambda x: x["eff"])
    stat["ud"] = len(ud)
    return ud, stat
