"""
pdcaas_lookup.py — Session 2 af Protein-Jæger v2

Læser full_matrix.json (+ evt. tilbud_matrix.json fra eTilbudsavis),
tilknytter en PDCAAS-score til hvert produkt og beregner
effective_price_per_gram_protein = price_per_gram_protein / pdcaas.

Det giver et mål for hvad du *reelt* betaler per gram optagelig protein —
et produkt med PDCAAS 0.50 er dobbelt så dyrt for den biologisk tilgængelige
protein som dets rå pris/g-protein antyder.

Output:
  output/full_matrix_pdcaas.json  — alle produkter, re-rangeret
  output/top25_pdcaas.json        — de 25 bedste efter effektiv pris
"""

import json
import re
from pathlib import Path

# ── Stier ───────────────────────────────────────────────────────────────────────
INPUT_FILE        = Path("output/full_matrix.json")
TILBUD_FILE       = Path("output/tilbud_matrix.json")   # eTilbudsavis data (valgfri)
OUTPUT_FULL       = Path("output/full_matrix_pdcaas.json")
OUTPUT_TOP        = Path("output/top25_pdcaas.json")

# ── PDCAAS-referencetabel ────────────────────────────────────────────────────────
# Kilde: FAO/WHO anbefalinger + PRD v2
PDCAAS_REFERENCE = {
    "whey":       1.00,
    "valle":      1.00,
    "æg":         1.00,
    "skyr":       1.00,
    "mælk":       1.00,
    "ost":        1.00,
    "hytteost":   1.00,
    "yoghurt":    1.00,
    "kvark":      1.00,
    "okse":       0.92,
    "bøf":        0.92,
    "kalv":       0.92,
    "kylling":    0.91,
    "kalkun":     0.91,
    "fjerkræ":    0.91,
    "fisk":       0.90,
    "laks":       0.90,
    "tun":        0.90,
    "torsk":      0.90,
    "sild":       0.90,
    "makrel":     0.90,
    "rejer":      0.90,
    "skaldyr":    0.90,
    "tilapia":    0.90,
    "ørred":      0.90,
    "gris":       0.88,
    "svin":       0.88,
    "flæsk":      0.88,
    "bacon":      0.88,
    "skinke":     0.88,
    "pølse":      0.88,
    "kotelet":    0.88,
    "nakke":      0.88,
    "bov":        0.88,
    "spareribs":  0.88,
    "ribs":       0.88,
    # Bælgfrugter — omtrentlige værdier fra litteraturen (kogte/konserverede)
    "bønner":     0.68,
    "linser":     0.52,
    "ærter":      0.69,
    "kikærter":   0.71,
    "hummus":     0.65,
    # Soja er næsten komplet protein
    "tofu":       0.91,
    "edamame":    0.91,
    "soja":       0.91,
    "tempeh":     0.91,
    # Proteinprodukter — konservativt sat (blandinger af mælke-/planteprotein)
    "proteinbar": 0.80,
    "protein bar": 0.80,
    "protein chips": 0.80,
    "havre":      0.57,
    "oat":        0.57,
    "korn":       0.57,
    "müsli":      0.57,
    "gryn":       0.57,
    "mel":        0.40,
    "pasta":      0.40,
    "nudler":     0.40,
    "ris":        0.40,
    "brød":       0.40,
    "rugbrød":    0.40,
    "knækbrød":   0.40,
    "flæskesvær": 0.08,
    "collagen":   0.08,
    "gelatine":   0.08,   # slik-gelatine har samme lave PDCAAS som collagen
    "gelatin":    0.08,
    # ── Kød-bundle/sortiment — ukendt sammensætning, konservativt sat ─────────
    "marked":     0.75,   # fx "kyllingemarked" = blandet forarbejdet kød
    "sortiment":  0.75,
    "party box":  0.70,
    "grillboks":  0.72,
    "kødkasse":   0.75,
    # ── Slik/candy — gelatineprotein lav kvalitet ────────────────────────────
    "vingummi":   0.08,   # gelatinebaseret protein = som collagen
    "gummibamse": 0.08,
    # ── Forarbejdede/panerede produkter ─────────────────────────────────────────
    # Lavere PDCAAS pga. blanding: kød + fyld (mel, rasp, stivelse).
    # Logikken tager laveste score ved multiple matches, så
    # "kyllingnugget" matcher kylling(0.91) OG nugget(0.68) → 0.68 ✓
    "rasp":        0.70,   # panering (mel+æg+rasp) blander med primæringrediensen
    "paneret":     0.70,
    "nugget":      0.68,
    "nuggets":     0.68,
    "frikadell":   0.75,   # hakket kød + mel/løg/æg-fyld
    "leverpost":   0.62,   # lever + stivelse + fedt-fyld (lavt PDCAAS for lever)
    "medister":    0.68,   # svinekød + kartoffel/mel-fyld
    "hotdog":      0.65,   # pølse + fyld + stivelse
    "schnitzel":   0.70,   # paneret kød
    "burger":      0.75,   # hakket kød + bindemiddel, men relativt rent
}

# Nøgleord der markerer et produkt som forarbejdet (sættes som is_processed-flag)
FORARBEJDNINGS_NØGLEORD = {
    "rasp", "paneret", "nugget", "nuggets", "frikadell",
    "leverpost", "medister", "hotdog", "schnitzel", "burger",
    "fars", "rulle", "sylte",
    # Kød-bundter med ukendt sammensætning
    "marked", "sortiment", "party box", "grillboks", "kødkasse",
}

# Subkategori → PDCAAS (bruges kun hvis ingen navne-match)
SUBCATEGORY_PDCAAS: dict[str, float] = {
    "Æg":                          1.00,
    "Syrnede produkter":           1.00,
    "Yoghurt m.v.":                1.00,
    "Mælk m.v.":                   1.00,
    "Koldskål":                    1.00,
    "Hytteost":                    1.00,
    "Skæreost":                    1.00,
    "Hård ost":                    1.00,
    "Børneost":                    1.00,
    "Smøreost":                    1.00,
    "Specialost":                  1.00,
    "Skiveskåret":                 1.00,   # typisk pålægsost
    "Madlavningsost m.v.":         1.00,
    "Oksekød":                     0.92,
    "Kylling":                     0.91,
    "Lam":                         0.91,
    "Fisk & Skaldyr":              0.90,
    "Fisk, sild & skaldyr":        0.90,
    "Fiskekonserves":              0.90,
    "Gris":                        0.88,
    "Hakket kød":                  0.88,   # konservativt — ofte svinekød
    "Bacon & toppings":            0.88,
    "Pølser":                      0.88,
    "Pålæg":                       0.88,
    "Kosttilskud":                 0.90,   # proteinpulver — typisk valle/mælkeprotein
    "Plantebaserede alternativer": 0.70,
    "Plantebaserede produkter":    0.70,
    "Plantebaseret":               0.70,
    "Morgenmad":                   0.57,
    "Mel, sukker, bagning":        0.40,
    "Ris & pasta, mv":             0.40,
    "Frisk pasta":                 0.40,
    "Brød":                        0.40,
    "Rugbrød":                     0.40,
    "Knækbrød":                    0.40,
    "Boller":                      0.40,
    "Baguette/flutes":             0.40,
    "Kager":                       0.40,
    "Nødder & tørret frugt":       0.60,  # nødder er middel — blandet
    "Chips og snacks":             0.50,
    "Konserves & survarer":        0.70,  # bælgfrugter dominerer
}

# Kategori → PDCAAS (sidst-resort fallback)
CATEGORY_PDCAAS: dict[str, float] = {
    "Kød, fisk & fjerkræ": 0.90,
    "Mejeri":              1.00,
    "Ost m.v.":            1.00,
    "Kolonial":            0.50,
    "Brød & Bavinchi":     0.40,
    "Frost":               0.75,   # blandet
    "Køl":                 0.80,   # blandet
}

DEFAULT_PDCAAS = 0.50  # bruges hvis intet matcher

# Produkter med PDCAAS under denne grænse er primært kulhydratkilder (mel, pasta,
# ris, brød, gelatine) og ekskluderes fra protein-sammenligningen.
MIN_PDCAAS_THRESHOLD = 0.50


# ── Matching-logik ──────────────────────────────────────────────────────────────────────

def lookup_pdcaas(name: str, subcategory: str, category: str) -> tuple[float, str, bool]:
    """
    Returnerer (pdcaas_score, match_metode).
    match_metode bruges til at debugge/auditere scoringen.
    is_processed = True hvis produktet indeholder forarbejdnings-nøgleord.

    Prioritet:
      1. Navne-nøgleord (mest specifikt)
      2. Subkategori
      3. Kategori
      4. Default
    """
    name_lower = name.lower()

    # 1. Navne-match — søg efter alle nøgleord, brug det med højest score
    best_score: float | None = None
    best_keyword: str = ""
    # Undtagelser: delstrenge der ikke er det vi leder efter
    FALSK_POSITIV_UDELADELSER = {
        "rasp":   ["raspberry", "hindbærrasp"],   # frugt, ikke panering
        "ris":    ["ristet", "krispe", "pris"],   # "ris" som kornet
        "marked": ["supermarked", "dagligvare"],  # "marked" som butik, ikke sortiment
        "ærter":  ["kikærter"],                   # kikærter har egen score
    }
    for keyword, score in PDCAAS_REFERENCE.items():
        # Hel-ords-match for korte nøgleord (undgår "mel" i "karamel")
        if len(keyword) <= 3:
            pattern = rf"\b{re.escape(keyword)}\b"
            if re.search(pattern, name_lower):
                if best_score is None or score < best_score:
                    best_score = score
                    best_keyword = keyword
        else:
            if keyword in name_lower:
                # Tjek om det er et falsk positiv
                udelad = any(fp in name_lower for fp in FALSK_POSITIV_UDELADELSER.get(keyword, []))
                if udelad:
                    continue
                if best_score is None or score < best_score:
                    best_score = score
                    best_keyword = keyword

    # Afgør om produktet er forarbejdet
    is_processed = any(kw in name_lower for kw in FORARBEJDNINGS_NØGLEORD)

    # Særregler: undgå falske positiver
    if "raspberry" in name_lower or "hindbær" in name_lower:
        # "rasp" i "raspberry/hindbærrasp" er ikke panering
        is_processed = False
    if "boller" in name_lower or "rugbrød" in name_lower or "sandwichbrød" in name_lower:
        # "burgerboller" er brød, ikke burgerkød
        is_processed = False

    # Særregel: soja-produkter er næsten komplette proteiner — "bønner" må ikke trække ned
    if any(k in name_lower for k in ("soja", "edamame", "tofu", "tempeh")) and not is_processed:
        return 0.91, "navn:soja", is_processed

    # Særregel: "flæskesvær" skal altid vinde uanset andre matches
    if "flæskesvær" in name_lower or "collagen" in name_lower:
        return 0.08, "navn:flæskesvær/collagen", is_processed

    if best_score is not None:
        return best_score, f"navn:{best_keyword}", is_processed

    # 2. Subkategori-match
    if subcategory in SUBCATEGORY_PDCAAS:
        return SUBCATEGORY_PDCAAS[subcategory], f"subkategori:{subcategory}", is_processed

    # 3. Kategori-match
    if category in CATEGORY_PDCAAS:
        return CATEGORY_PDCAAS[category], f"kategori:{category}", is_processed

    # 4. Default
    return DEFAULT_PDCAAS, "default", is_processed


# ── Hovedfunktion ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if INPUT_FILE.exists():
        print(f"Læser {INPUT_FILE} …")
        with open(INPUT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        products: list[dict] = raw.get("products", [])
        for p in products:
            p.setdefault("butik", "Rema 1000")
        print(f"  {len(products)} Rema 1000-produkter indlæst")
    else:
        print(f"  ({INPUT_FILE} ikke fundet — kører uden Rema 1000-data)")
        raw = {"meta": {}}
        products = []

    # ── Flet eTilbudsavis-data ind (hvis tilgængeligt) ───────────────────────
    if TILBUD_FILE.exists():
        print(f"Læser {TILBUD_FILE} …")
        with open(TILBUD_FILE, encoding="utf-8") as f:
            try:
                tilbud_raw = json.load(f)
            except json.JSONDecodeError:
                f.seek(0)
                tilbud_raw, _ = json.JSONDecoder().raw_decode(f.read())
        tilbud_products = tilbud_raw.get("products", [])
        # Kun produkter med beregnet pris/g protein
        tilbud_med_data = [p for p in tilbud_products if p.get("price_per_gram_protein")]
        print(f"  {len(tilbud_med_data)} eTilbudsavis-produkter flettes ind")
        products = products + tilbud_med_data
    else:
        print(f"  (tilbud_matrix.json ikke fundet — kører kun med Rema 1000-data)")

    # Normaliser butiksnavne (fjern duplikater pga. store/blandede bogstaver)
    BUTIK_ALIAS = {
        "REMA 1000": "Rema 1000",
        "rema 1000": "Rema 1000",
        "Fakta": "fakta",
        "FAKTA": "fakta",
    }
    for p in products:
        if p.get("butik") in BUTIK_ALIAS:
            p["butik"] = BUTIK_ALIAS[p["butik"]]

    # Tilknyt PDCAAS og beregn effektiv pris
    for p in products:
        pdcaas, method, is_proc = lookup_pdcaas(
            p.get("name", ""),
            p.get("subcategory", ""),
            p.get("category", ""),
        )
        p["pdcaas"] = pdcaas
        p["pdcaas_method"] = method
        p["is_processed"] = is_proc
        # Marker produkter med sandsynlig mangelfuld kulhydrat/kaloriedata
        kulhy = p.get("kulhydrat_per_100g")
        p["naering_usikker"] = bool(
            is_proc and (kulhy is None or abs(kulhy) < 3)
        )

        ppg = p.get("price_per_gram_protein")
        if ppg and ppg > 0:
            p["effective_price_per_gram_protein"] = round(ppg / pdcaas, 4)
        else:
            p["effective_price_per_gram_protein"] = None

    # Filtrer produkter uden effektiv pris og under PDCAAS-grænsen
    # (mel 0.40, pasta 0.40, ris 0.40, brød 0.40, gelatine 0.08 → ikke proteinkilder)
    valid = [
        p for p in products
        if p["effective_price_per_gram_protein"] is not None
        and p.get("pdcaas", 0) >= MIN_PDCAAS_THRESHOLD
    ]

    # Re-ranger efter effektiv pris (lavest = bedst)
    valid.sort(key=lambda x: x["effective_price_per_gram_protein"])
    for i, p in enumerate(valid, 1):
        p["pdcaas_rank"] = i

    top25 = valid[:25]

    # ── Gem output ──────────────────────────────────────────────────────────────────────
    OUTPUT_FULL.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        **raw.get("meta", {}),
        "pdcaas_added": True,
        "sorted_by": "effective_price_per_gram_protein (price_per_gram_protein / pdcaas)",
        "note": (
            "effective_price_per_gram_protein angiver hvad du reelt betaler "
            "per gram biologisk optagelig protein (DKK/g). Lavere = bedre."
        ),
    }

    with open(OUTPUT_FULL, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "products": valid}, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_TOP, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "top25": top25}, f, ensure_ascii=False, indent=2)

    print(f"\nOutput gemt:")
    print(f"  {OUTPUT_FULL}  ({len(valid)} produkter)")
    print(f"  {OUTPUT_TOP}   (top 25)")

    # ── Preview ───────────────────────────────────────────────────────────────────────
    print("\n── Top 15 efter effektiv pris/g optagelig protein ──────────────────")
    print(f"{'Rank':>4}  {'Navn':<38} {'PDCAAS':>6}  {'kr/g rå':>8}  {'kr/g eff':>9}  Metode")
    print("─" * 90)
    for p in top25[:15]:
        flags = " ⚠" if p.get("sanity_flags") else ""
        print(
            f"{p['pdcaas_rank']:>4}. {p['name']:<38} "
            f"{p['pdcaas']:>6.2f}  "
            f"{p['price_per_gram_protein']:>8.4f}  "
            f"{p['effective_price_per_gram_protein']:>9.4f}  "
            f"{p['pdcaas_method']}{flags}"
        )

    # ── PDCAAS-fordeling ──────────────────────────────────────────────────────
    print("\n── PDCAAS-score fordeling ───────────────────────────────────────────")
    from collections import Counter
    dist = Counter(p["pdcaas"] for p in valid)
    for score in sorted(dist.keys()):
        label = {
            1.00: "Valle/æg/mejeri",
            0.92: "Oksekød",
            0.91: "Kylling/kalkun",
            0.90: "Fisk/skaldyr",
            0.88: "Svinekød/pølse",
            0.70: "Bælgfrugter/tofu",
            0.60: "Nødder",
            0.57: "Havre/korn",
            0.50: "Ukendt/blandet",
            0.40: "Mel/pasta/brød",
            0.08: "Collagen/flæskesvær",
        }.get(score, "")
        print(f"  {score:.2f}  {dist[score]:>4} produkter  {label}")

    # ── Match-metode statistik ────────────────────────────────────────────────
    print("\n── Matching-metode ─────────────────────────────────────────────────")
    methods = Counter(
        p["pdcaas_method"].split(":")[0] for p in valid
    )
    total = sum(methods.values())
    for m, count in methods.most_common():
        print(f"  {m:<15} {count:>4} produkter  ({count/total*100:.1f}%)")


if __name__ == "__main__":
    main()
