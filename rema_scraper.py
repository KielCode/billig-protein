"""
Rema 1000 protein hunter scraper.

Fetches all products from shop.rema1000.dk via the internal Algolia search API,
extracts nutrition data, and finds the top 25 cheapest protein sources by
price-per-gram-protein.
"""

import csv
import json
import os
import re
import sys
from pathlib import Path

from urllib.parse import urljoin

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv er valgfri
    pass

# ── Algolia config ────────────────────────────────────────────────────────────────────────
# shop.rema1000.dk bruger Algolia som søgemaskine. Den offentlige søgenøgle
# ligger i sidens JavaScript; vi finder den automatisk ved hver kørsel i stedet
# for at gemme den i koden. Kan overstyres med miljøvariablerne
# REMA_ALGOLIA_APP_ID / REMA_ALGOLIA_KEY / REMA_ALGOLIA_INDEX.
STANDARD_APP_ID = "FLWDN2189E"
ALGOLIA_INDEX = os.getenv("REMA_ALGOLIA_INDEX", "aws-prod-products")
REMA_SHOP = "https://shop.rema1000.dk/"
BROWSER_UA = "Mozilla/5.0 (compatible; BilligProtein/3.0; prissammenligning af protein)"


def _browse_url(app_id: str) -> str:
    return f"https://{app_id}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/browse"


def _algolia_headers(app_id: str, key: str) -> dict:
    return {
        "X-Algolia-API-Key": key,
        "X-Algolia-Application-Id": app_id,
        "Content-Type": "application/json",
    }


def _virker(app_id: str, key: str) -> bool:
    try:
        r = requests.post(_browse_url(app_id), headers=_algolia_headers(app_id, key),
                          json={"params": "hitsPerPage=1"}, timeout=15)
        return r.status_code == 200 and "hits" in r.json()
    except Exception:
        return False


def _kandidater(tekst: str):
    """Finder (app_id, nøgle)-par i en JavaScript-fil."""
    app_ids = list(dict.fromkeys(re.findall(r"([A-Z0-9]{10})-dsn\.algolia\.net", tekst)))
    for m in re.finditer(r"[\"']([a-f0-9]{32})[\"']", tekst):
        vindue = tekst[max(0, m.start() - 800): m.end() + 800]
        naer = re.findall(r"[\"']([A-Z0-9]{10})[\"']", vindue)
        for app_id in dict.fromkeys([STANDARD_APP_ID] + app_ids + naer):
            yield app_id, m.group(1)


def find_algolia_adgang() -> tuple[str, str]:
    """Returnerer (app_id, søgenøgle) — fra miljøvariabler eller Remas egen side."""
    env_key = os.getenv("REMA_ALGOLIA_KEY")
    if env_key:
        par = (os.getenv("REMA_ALGOLIA_APP_ID", STANDARD_APP_ID), env_key)
        if _virker(*par):
            return par
        print("  ⚠ REMA_ALGOLIA_KEY virker ikke længere — leder efter ny nøgle på Remas side")

    sess = requests.Session()
    sess.headers["User-Agent"] = BROWSER_UA
    kilder: list[str] = [urljoin(REMA_SHOP, "js/app.js")]
    forside = ""
    try:
        forside = sess.get(REMA_SHOP, timeout=20).text
        for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", forside):
            kilder.append(urljoin(REMA_SHOP, src))
    except Exception as e:
        print(f"  ⚠ Kunne ikke hente Remas forside: {e}")

    tekster = [forside]
    for url in list(dict.fromkeys(kilder))[:30]:
        try:
            r = sess.get(url, timeout=20)
            if r.ok and len(r.text) < 15_000_000:
                tekster.append(r.text)
        except Exception:
            continue

    afprøvet: set[tuple[str, str]] = set()
    for tekst in tekster:
        for par in _kandidater(tekst):
            if par in afprøvet:
                continue
            afprøvet.add(par)
            if _virker(*par):
                print(f"  ✓ Fandt Algolia-adgang (app {par[0]})")
                return par
            if len(afprøvet) > 60:
                break
    raise RuntimeError("Kunne ikke finde Remas Algolia-nøgle — sæt REMA_ALGOLIA_KEY som miljøvariabel")

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


# ── Helpers ─────────────────────────────────────────────────────────────────────────

def fetch_all_products() -> list[dict]:
    """Paginate through all Algolia records using the browse cursor API."""
    app_id, key = find_algolia_adgang()
    browse_url = _browse_url(app_id)
    headers = _algolia_headers(app_id, key)
    products: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        body: dict = {"params": "hitsPerPage=1000"}
        if cursor:
            body["cursor"] = cursor

        resp = requests.post(browse_url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", [])
        products.extend(hits)
        page += 1
        print(f"  Page {page}: fetched {len(hits)} products (total so far: {len(products)})")

        cursor = data.get("cursor")
        if not cursor:
            break

    return products


def parse_nutrition(nutrition_info: list[dict]) -> dict[str, float | None]:
    """
    Extract protein, fedt, kulhydrat (g/100g) and kcal (kcal/100g) from nutrition_info.

    nutrition_info entries look like:
      {"name": "Protein",    "value": "20",               "sort": "7"}
      {"name": "Fedt",       "value": "5.0",              "sort": "3"}
      {"name": "Kulhydrat",  "value": "20.0",             "sort": "5"}
      {"name": "Energi",     "value": "403 KJ / 95 kcal", "sort": "1"}
    Values use Danish decimal comma and may have "< " prefix.
    """
    protein: float | None = None
    kcal: float | None = None
    fedt: float | None = None
    kulhydrat: float | None = None

    for entry in nutrition_info:
        name = entry.get("name", "").strip().lower()
        raw = entry.get("value", "").strip()

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
                        kcal = round(float(m_kj.group(1).replace(",", ".")) / 4.184, 1)
                    except ValueError:
                        pass

    return {
        "protein_per_100g": protein,
        "kcal_per_100g": kcal,
        "fedt_per_100g": fedt,
        "kulhydrat_per_100g": kulhydrat,
    }


def parse_weight_grams(product: dict) -> float | None:
    """
    Derive package weight in grams from available fields.

    Strategy:
      1. Parse weight from the 'underline' text  (e.g. "450 GR.", "1,2 KG.")
      2. Fall back to price / price_per_kilogram  (works for price-tagged items)
      3. Use median_weight if non-zero
    """
    underline: str = product.get("underline") or ""
    # Match patterns like "450 GR", "1.2 KG", "500 ML", "1 LTR.", "0,75 L"
    m = re.search(
        r"([\d]+(?:[.,]\d+)?)\s*(kg|kilo|g|gr|gram|ml|cl|ltr|l|liter)\b",
        underline,
        re.IGNORECASE,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit in ("kg", "kilo"):
            return qty * 1000.0
        if unit in ("g", "gr", "gram"):
            return qty
        if unit in ("l", "ltr", "liter"):
            return qty * 1000.0
        if unit == "cl":
            return qty * 10.0
        if unit == "ml":
            return qty

    # Fallback: derive from price and price_per_kg
    pricing = product.get("pricing") or {}
    price = pricing.get("price")
    price_per_kg = pricing.get("price_per_kilogram")
    if price and price_per_kg and price_per_kg > 0:
        return round((price / price_per_kg) * 1000.0, 1)

    # Fallback: median_weight
    mw = product.get("median_weight")
    if mw and mw > 0:
        unit = (product.get("median_weight_unit") or "gram").lower()
        if unit in ("kg", "kilo"):
            return mw * 1000.0
        return float(mw)

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
        # Protein provides ~4 kcal/g; kcal < protein*2 or kcal > protein*9 is odd
        expected_min = protein * 2
        expected_max = protein * 9 + 200  # allow fat & carbs
        if kcal < expected_min:
            warnings.append(
                f"kcal {kcal} seems too low for protein content {protein}g"
            )

    if weight_g is not None:
        if weight_g < MIN_WEIGHT_G:
            warnings.append(f"weight {weight_g}g is suspiciously low")
        if weight_g > MAX_WEIGHT_G:
            warnings.append(f"weight {weight_g}g is suspiciously high")

    if price < MIN_PRICE_DKK or price > MAX_PRICE_DKK:
        warnings.append(f"price {price} DKK looks unrealistic")

    return warnings


def find_billede(hit: dict) -> str | None:
    """Finder en billed-URL i et Algolia-hit (formatet varierer)."""
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


def process_product(hit: dict) -> dict | None:
    """
    Convert a raw Algolia hit into a cleaned result dict.
    Returns None if the product should be excluded (no protein data).
    """
    nutrition = parse_nutrition(hit.get("nutrition_info") or [])
    protein = nutrition["protein_per_100g"]
    kcal = nutrition["kcal_per_100g"]

    # Must have a positive protein value
    if not protein or protein <= 0:
        return None

    pricing = hit.get("pricing") or {}
    price = pricing.get("price")
    if not price or price <= 0:
        return None

    weight_g = parse_weight_grams(hit)

    sanity_flags = sanity_check(hit.get("name", ""), protein, kcal, weight_g, price)

    # price_per_gram_protein: how much you pay per gram of protein in the package
    # = price_DKK / (weight_g * protein_per_100g / 100)
    if weight_g and weight_g > 0:
        grams_of_protein = weight_g * protein / 100.0
        price_per_gram_protein = round(price / grams_of_protein, 4) if grams_of_protein > 0 else None
    else:
        # fall back to per-kg price if we have it (price_per_kilogram is per 1000g)
        ppkg = pricing.get("price_per_kilogram")
        if ppkg and ppkg > 0:
            grams_of_protein_per_kg = 1000 * protein / 100.0
            price_per_gram_protein = round(ppkg / grams_of_protein_per_kg, 4)
        else:
            price_per_gram_protein = None

    if price_per_gram_protein is None:
        return None

    kcal_per_gram_protein = (
        round((kcal * weight_g / 100.0) / (weight_g * protein / 100.0), 2)
        if (kcal and weight_g and weight_g > 0 and protein > 0)
        else (round(kcal / protein, 2) if (kcal and protein > 0) else None)
    )

    # Prisperiode — hentes fra campaign-objekt hvis tilgængeligt
    campaign = pricing.get("campaign") or {}
    tilbud_start = campaign.get("start") or pricing.get("campaign_start") or None
    tilbud_slut  = campaign.get("end")   or pricing.get("campaign_end")   or None

    return {
        "butik": "Rema 1000",
        "name": hit.get("name", ""),
        "underline": hit.get("underline", ""),
        "category": hit.get("department_name", ""),
        "subcategory": hit.get("category_name", ""),
        "price_dkk": price,
        "tilbud_start": tilbud_start,
        "tilbud_slut": tilbud_slut,
        "weight_g": weight_g,
        "protein_per_100g": protein,
        "fedt_per_100g": nutrition.get("fedt_per_100g"),
        "kulhydrat_per_100g": nutrition.get("kulhydrat_per_100g"),
        "kcal_per_100g": kcal,
        "price_per_gram_protein": price_per_gram_protein,
        "kcal_per_gram_protein": kcal_per_gram_protein,
        "sanity_flags": sanity_flags,
        "product_id": hit.get("id"),
        "image_url": find_billede(hit),
        "url": f"https://shop.rema1000.dk/varer/{hit.get('id')}" if hit.get("id") else None,
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
    print("Fetching all products from shop.rema1000.dk …")
    raw_products = fetch_all_products()
    print(f"Total raw products fetched: {len(raw_products)}")

    print("Processing products …")
    results: list[dict] = []
    skipped_no_protein = 0
    skipped_no_price_or_weight = 0

    for hit in raw_products:
        processed = process_product(hit)
        if processed is None:
            if not (hit.get("nutrition_info") or []):
                skipped_no_protein += 1
            else:
                nutrition = parse_nutrition(hit.get("nutrition_info") or [])
                if not nutrition["protein_per_100g"]:
                    skipped_no_protein += 1
                else:
                    skipped_no_price_or_weight += 1
        else:
            results.append(processed)

    print(f"  Products with usable protein data:  {len(results)}")
    print(f"  Skipped (no protein info):          {skipped_no_protein}")
    print(f"  Skipped (no price/weight):          {skipped_no_price_or_weight}")

    # Sort by price_per_gram_protein ascending (cheapest protein first)
    results.sort(key=lambda x: x["price_per_gram_protein"])

    # Add rank to every row
    for rank, row in enumerate(results, 1):
        row["rank"] = rank

    top25 = results[:25]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── top25 ────────────────────────────────────────────────────────────────────
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "meta": {
                    "total_products_fetched": len(raw_products),
                    "products_with_protein": len(results),
                    "sorted_by": "price_per_gram_protein (DKK/g, ascending)",
                },
                "top25": top25,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # ── full matrix ────────────────────────────────────────────────────────────────────
    with open(MATRIX_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "meta": {
                    "total_products_fetched": len(raw_products),
                    "products_with_protein": len(results),
                    "sorted_by": "price_per_gram_protein (DKK/g, ascending)",
                    "columns": [
                        "rank", "product_id", "name", "underline", "category",
                        "subcategory", "price_dkk", "weight_g",
                        "protein_per_100g", "kcal_per_100g",
                        "price_per_gram_protein", "kcal_per_gram_protein",
                        "sanity_flags",
                    ],
                },
                "products": results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # ── CSV outputs ────────────────────────────────────────────────────────────────────
    write_csv(results, CSV_FILE, delimiter=",",  danish_decimals=False)
    write_csv(results, CSV_DK_FILE, delimiter=";", danish_decimals=True)

    print(f"\nTop 25 results saved to  {OUTPUT_FILE}")
    print(f"Full matrix saved to     {MATRIX_FILE}  ({len(results)} products)")
    print(f"CSV (English) saved to   {CSV_FILE}")
    print(f"CSV (Danish)  saved to   {CSV_DK_FILE}")
    print("\n── Top 10 preview ───────────────────────────────────────────────────")
    for p in top25[:10]:
        flags = " ⚠ " + "; ".join(p["sanity_flags"]) if p["sanity_flags"] else ""
        print(
            f"{p['rank']:>4}. {(p['name'] or ''):<35} {(p['underline'] or ''):<22} "
            f"{p['price_dkk']:>6.2f} DKK  "
            f"protein: {p['protein_per_100g']:>5.1f}g/100g  "
            f"→ {p['price_per_gram_protein']:.4f} DKK/g-protein"
            f"{flags}"
        )


if __name__ == "__main__":
    main()
