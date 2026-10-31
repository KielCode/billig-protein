"""
Rema 1000 protein hunter scraper.

Henter alle varer fra Remas webshop via deres offentlige produkt-API
(api.digital.rema1000.dk — det samme som shop.rema1000.dk selv bruger),
inkl. pris, kampagner og næringsindhold fra varedeklarationen.
Kræver ingen nøgle.
"""

import csv
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv er valgfri
    pass

# ── API ───────────────────────────────────────────────────────────────────────────
API_BASE = os.getenv("REMA_API_BASE", "https://api.digital.rema1000.dk/api")
PER_PAGE = 500
INCLUDE = "department,category,nutrition_info"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; BilligProtein/3.0; prissammenligning af protein)",
}

OUTPUT_DIR = Path("output")
OUTPUT_FILE   = OUTPUT_DIR / "top25_proteins.json"
MATRIX_FILE   = OUTPUT_DIR / "full_matrix.json"
CSV_FILE      = OUTPUT_DIR / "full_matrix.csv"
CSV_DK_FILE   = OUTPUT_DIR / "full_matrix_dk.csv"

CSV_FIELDS = [
    "rank", "product_id", "butik", "name", "underline", "category", "subcategory",
    "price_dkk", "tilbud_start", "tilbud_slut", "weight_g",
    "protein_per_100g", "fedt_per_100g", "kulhydrat_per_100g", "kcal_per_100g",
    "price_per_gram_protein", "kcal_per_gram_protein", "sanity_flags",
]
NUMERIC_FIELDS = {
    "price_dkk", "weight_g", "protein_per_100g", "fedt_per_100g",
    "kulhydrat_per_100g", "kcal_per_100g",
    "price_per_gram_protein", "kcal_per_gram_protein",
}

# ── Sanity-check thresholds ───────────────────────────────────────────────────
MAX_PROTEIN_PER_100G = 100.0      # physically impossible to exceed
WARN_PROTEIN_PER_100G = 50.0      # suspiciously high (flag, not discard)
MIN_KCAL_PER_100G = 1.0
MAX_KCAL_PER_100G = 1000.0
MIN_PRICE_DKK = 0.01
MAX_PRICE_DKK = 10_000.0
MIN_WEIGHT_G = 1.0
MAX_WEIGHT_G = 50_000.0


# ── Helpers ───────────────────────────────────────────────────────────────────────

def i_dag() -> str:
    """Dagens dato i Danmark som YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Copenhagen")).date().isoformat()
    except Exception:
        return date.today().isoformat()


def fetch_all_products() -> list[dict]:
    """Bladrer gennem alle varer i Remas produkt-API."""
    products: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{API_BASE}/v3/products",
            params={"per_page": PER_PAGE, "page": page, "include": INCLUDE},
            headers=HEADERS,
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("data", [])
        products.extend(hits)
        sidste = ((data.get("meta") or {}).get("pagination") or {}).get("last_page", page)
        print(f"  Side {page}/{sidste}: {len(hits)} varer (i alt {len(products)})")
        if not hits or page >= sidste:
            break
        page += 1
        time.sleep(0.3)
    return products


def vaelg_pris(priser: list[dict], dag: str) -> dict | None:
    """Vælger den pris der gælder i dag (kampagnepris foretrækkes)."""
    if not priser:
        return None

    def gyldig(p: dict) -> bool:
        start = (p.get("starting_at") or "0000")[:10]
        slut = (p.get("ending_at") or "9999")[:10]
        return start <= dag <= slut

    aktive = [p for p in priser if p.get("price") and gyldig(p)]
    if aktive:
        return sorted(aktive, key=lambda p: (not p.get("is_campaign"), p.get("price")))[0]
    kommende = sorted((p for p in priser if p.get("price") and (p.get("starting_at") or "")[:10] > dag),
                      key=lambda p: p.get("starting_at") or "")
    if kommende:
        return kommende[0]
    return next((p for p in priser if p.get("price")), None)


def parse_nutrition(nutrition_info: list[dict]) -> dict[str, float | None]:
    """
    Extract protein, fedt, kulhydrat (g/100g) and kcal (kcal/100g) from nutrition_info.

    nutrition_info entries look like:
      {"name": "Protein",    "value": "20"}
      {"name": "Fedt",       "value": "5,0"}
      {"name": "Kulhydrat",  "value": "20,0"}
      {"name": "Energi",     "value": "1.411 KJ / 337 kcal"}
    Values use Danish decimal comma and may have "< " prefix.
    """
    protein: float | None = None
    kcal: float | None = None
    fedt: float | None = None
    kulhydrat: float | None = None

    for entry in nutrition_info:
        name = (entry.get("name") or "").strip().lower()
        raw = (entry.get("value") or "").strip()

        # Strip "< " or "> " prefixes and replace Danish comma
        cleaned = re.sub(r"^[<>]\s*", "", raw).replace(",", ".").strip()

        def parse_float(s: str) -> float | None:
            try:
                return float(s.split()[0])  # tag første tal, ignorer enheder
            except (ValueError, IndexError):
                return None

        if name == "protein":
            protein = parse_float(cleaned)

        elif name in ("fedt", "fat"):
            fedt = parse_float(cleaned)

        elif name in ("kulhydrat", "kulhydrater", "carbohydrate", "carbohydrates"):
            kulhydrat = parse_float(cleaned)

        elif name == "energi":
            # Format: "403 KJ / 95 kcal"  OR  "95 kcal"  OR  "95"
            m = re.search(r"(\d[\d.,]*)\s*kcal", cleaned, re.IGNORECASE)
            if m:
                try:
                    kcal = float(m.group(1).replace(",", "."))
                except ValueError:
                    pass
            else:
                # Some entries only contain kJ – convert
                m_kj = re.search(r"(\d[\d.,]*)\s*kj", cleaned, re.IGNORECASE)
                if m_kj:
                    try:
                        kcal = round(float(m_kj.group(1).replace(".", "")) / 4.184, 1)
                    except ValueError:
                        pass

    return {
        "protein_per_100g": protein,
        "kcal_per_100g": kcal,
        "fedt_per_100g": fedt,
        "kulhydrat_per_100g": kulhydrat,
    }


def parse_weight_grams(underline: str, pris: dict | None) -> float | None:
    """
    Pakkens vægt i gram.
      1. Fra underline-teksten ("450 GR.", "1,2 KG.", "1 LTR.")
      2. Ellers pris ÷ sammenligningspris pr. kg/l
    """
    m = re.search(
        r"([\d]+(?:[.,]\d+)?)\s*(kg|kilo|g|gr|gram|ml|cl|ltr|l|liter)\b",
        underline or "",
        re.IGNORECASE,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit in ("kg", "kilo", "l", "ltr", "liter"):
            return qty * 1000.0
        if unit in ("g", "gr", "gram", "ml"):
            return qty
        if unit == "cl":
            return qty * 10.0

    if pris and pris.get("price") and pris.get("compare_unit_price") and \
            (pris.get("compare_unit") or "").lower() in ("kg", "l", "ltr", "liter"):
        return round(pris["price"] / pris["compare_unit_price"] * 1000.0, 1)
    return None


def sanity_check(product_name: str, protein: float, kcal: float | None,
                 weight_g: float | None, price: float) -> list[str]:
    """Return a list of warning strings (empty = clean)."""
    warnings: list[str] = []

    if protein > MAX_PROTEIN_PER_100G:
        warnings.append(f"protein {protein}g/100g exceeds physical maximum")
    elif protein > WARN_PROTEIN_PER_100G:
        warnings.append(f"protein {protein}g/100g is suspiciously high")

    if kcal is not None:
        if kcal < MIN_KCAL_PER_100G:
            warnings.append(f"kcal {kcal}/100g is suspiciously low")
        if kcal > MAX_KCAL_PER_100G:
            warnings.append(f"kcal {kcal}/100g is suspiciously high")
        if kcal < protein * 2:
            warnings.append(f"kcal {kcal} seems too low for protein content {protein}g")

    if weight_g is not None:
        if weight_g < MIN_WEIGHT_G:
            warnings.append(f"weight {weight_g}g is suspiciously low")
        if weight_g > MAX_WEIGHT_G:
            warnings.append(f"weight {weight_g}g is suspiciously high")

    if price < MIN_PRICE_DKK or price > MAX_PRICE_DKK:
        warnings.append(f"price {price} DKK looks unrealistic")

    return warnings


def find_billede(hit: dict) -> str | None:
    """Finder en billed-URL i et produkt (formatet varierer)."""
    imgs = hit.get("images") or hit.get("image") or []
    if isinstance(imgs, str):
        return imgs if imgs.startswith("http") else None
    if isinstance(imgs, dict):
        imgs = [imgs]
    if not isinstance(imgs, list):
        return None
    for im in imgs:
        if isinstance(im, str) and im.startswith("http"):
            return im
        if isinstance(im, dict):
            for k in ("medium", "small", "large", "url", "thumbnail"):
                v = im.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
    return None


def process_product(hit: dict, dag: str) -> dict | None:
    """
    Omsætter en vare fra API'et til vores format.
    Returnerer None hvis varen mangler protein, pris eller vægt.
    """
    nutrition = parse_nutrition(hit.get("nutrition_info") or [])
    protein = nutrition["protein_per_100g"]
    kcal = nutrition["kcal_per_100g"]
    if not protein or protein <= 0:
        return None

    pris = vaelg_pris(hit.get("prices") or [], dag)
    price = (pris or {}).get("price")
    if not price or price <= 0:
        return None

    underline = hit.get("underline") or ""
    weight_g = parse_weight_grams(underline, pris)
    if not weight_g or weight_g <= 0:
        return None

    sanity_flags = sanity_check(hit.get("name", ""), protein, kcal, weight_g, price)
    grams_of_protein = weight_g * protein / 100.0
    price_per_gram_protein = round(price / grams_of_protein, 4)
    kcal_per_gram_protein = round(kcal / protein, 2) if kcal else None

    er_kampagne = bool(pris.get("is_campaign"))
    pid = hit.get("id")

    return {
        "butik": "Rema 1000",
        "name": hit.get("name", ""),
        "underline": underline,
        "category": (hit.get("department") or {}).get("name", ""),
        "subcategory": (hit.get("category") or {}).get("name", ""),
        "price_dkk": price,
        "tilbud_start": (pris.get("starting_at") or "")[:10] or None if er_kampagne else None,
        "tilbud_slut": (pris.get("ending_at") or "")[:10] or None if er_kampagne else None,
        "weight_g": weight_g,
        "protein_per_100g": protein,
        "fedt_per_100g": nutrition.get("fedt_per_100g"),
        "kulhydrat_per_100g": nutrition.get("kulhydrat_per_100g"),
        "kcal_per_100g": kcal,
        "price_per_gram_protein": price_per_gram_protein,
        "kcal_per_gram_protein": kcal_per_gram_protein,
        "sanity_flags": sanity_flags,
        "product_id": pid,
        "image_url": find_billede(hit),
        "url": f"https://shop.rema1000.dk/varer/{pid}" if pid else None,
    }


# ── CSV helpers ──────────────────────────────────────────────────────────────────────

def write_csv(products: list[dict], path: Path, delimiter: str = ",",
              danish_decimals: bool = False) -> None:
    """Write products to CSV. Optionally use semicolon delimiter + comma decimals."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        for product in products:
            row = dict(product)
            row["sanity_flags"] = " | ".join(product.get("sanity_flags") or [])
            if danish_decimals:
                for field in NUMERIC_FIELDS:
                    if row.get(field) is not None:
                        row[field] = str(row[field]).replace(".", ",")
            writer.writerow(row)


# ── Main ───────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Henter alle varer fra Remas produkt-API …")
    raw_products = fetch_all_products()
    print(f"Varer hentet: {len(raw_products)}")

    dag = i_dag()
    results: list[dict] = []
    uden_protein = 0
    uden_pris_vaegt = 0
    for hit in raw_products:
        processed = process_product(hit, dag)
        if processed is None:
            if not parse_nutrition(hit.get("nutrition_info") or [])["protein_per_100g"]:
                uden_protein += 1
            else:
                uden_pris_vaegt += 1
        else:
            results.append(processed)

    print(f"  Med brugbare proteindata:  {len(results)}")
    print(f"  Uden proteindata:          {uden_protein}")
    print(f"  Uden pris/vægt:            {uden_pris_vaegt}")

    results.sort(key=lambda x: x["price_per_gram_protein"])
    for rank, row in enumerate(results, 1):
        row["rank"] = rank
    top25 = results[:25]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "kilde": f"{API_BASE}/v3/products",
        "total_products_fetched": len(raw_products),
        "products_with_protein": len(results),
        "sorted_by": "price_per_gram_protein (DKK/g, ascending)",
        "genereret": datetime.now().isoformat(timespec="seconds"),
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "top25": top25}, f, ensure_ascii=False, indent=2)
    with open(MATRIX_FILE, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "products": results}, f, ensure_ascii=False, indent=2)
    write_csv(results, CSV_FILE, delimiter=",", danish_decimals=False)
    write_csv(results, CSV_DK_FILE, delimiter=";", danish_decimals=True)

    print(f"\nGemt: {MATRIX_FILE}  ({len(results)} varer)")
    print("\n── Top 10 ───────────────────────────────────────────────────────────────────")
    for p in top25[:10]:
        print(
            f"{p['rank']:>4}. {(p['name'] or '')[:35]:<35} {(p['underline'] or '')[:22]:<22} "
            f"{p['price_dkk']:>7.2f} kr  protein {p['protein_per_100g']:>5.1f} g/100 g  "
            f"→ {p['price_per_gram_protein']:.4f} kr/g"
        )


if __name__ == "__main__":
    main()
