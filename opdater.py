"""
opdater.py — kører hele Billig Protein-opdateringen i ét hug.

Bruges af GitHub Actions hver morgen (se .github/workflows/opdater.yml),
men kan også køres lokalt:  python opdater.py --out docs

Trin:
  1. Hent Rema 1000 (rema_scraper) og tilbud fra eTilbudsavis (tilbud_scraper).
     Fejler en kilde, bruges sidste gode kopi fra data/cache (højst 7 dage gammel),
     så siden aldrig bliver tom pga. en midlertidig fejl.
  2. Tilknyt proteinkvalitet (pdcaas_lookup).
  3. Byg siden (build_web) og skriv status.json ved siden af.
Hvis der ikke er nok data, afbrydes uden at overskrive den eksisterende side.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROD = Path(__file__).resolve().parent
OUTPUT = ROD / "output"
CACHE = ROD / "data" / "cache"
MAX_CACHE_DAGE = 7
MIN_VARER = 50

KILDER = [
    # (navn, modul, outputfil, minimum antal produkter)
    ("rema", "rema_scraper", OUTPUT / "full_matrix.json", 200),
    ("etilbudsavis", "tilbud_scraper", OUTPUT / "tilbud_matrix.json", 20),
]


def antal_produkter(fil: Path) -> int:
    try:
        with open(fil, encoding="utf-8") as f:
            return len(json.load(f).get("products", []))
    except Exception:
        return 0


TIDSPUNKTER = CACHE / "tidspunkter.json"


def _tidspunkter() -> dict:
    try:
        return json.loads(TIDSPUNKTER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def gem_cache(fil: Path) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(fil, encoding="utf-8") as f:
        data = json.load(f)
    with open(CACHE / fil.name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    t = _tidspunkter()
    t[fil.name] = time.time()
    TIDSPUNKTER.write_text(json.dumps(t), encoding="utf-8")


def brug_cache(fil: Path) -> float | None:
    """Kopierer sidste gode kopi ind. Returnerer kopiens alder i dage (eller None)."""
    c = CACHE / fil.name
    if not c.exists():
        return None
    gemt = _tidspunkter().get(fil.name, c.stat().st_mtime)
    alder = (time.time() - gemt) / 86400
    if alder > MAX_CACHE_DAGE:
        return None
    fil.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(c, fil)
    return round(alder, 1)


def hent_kilde(navn: str, modul: str, fil: Path, minimum: int) -> dict:
    print(f"\n{'=' * 60}\n▶ {navn}\n{'=' * 60}", flush=True)
    if fil.exists():
        fil.unlink()
    start = time.time()
    fejl = None
    try:
        importlib.import_module(modul).main()
    except SystemExit as e:
        if e.code not in (0, None):
            fejl = f"afsluttede med kode {e.code}"
    except Exception as e:
        traceback.print_exc()
        fejl = f"{type(e).__name__}: {e}"[:300]

    n = antal_produkter(fil) if fil.exists() else 0
    if fejl is None and n >= minimum:
        gem_cache(fil)
        return {"ok": True, "antal": n, "sekunder": round(time.time() - start)}

    fejl = fejl or f"kun {n} produkter (forventede mindst {minimum})"
    alder = brug_cache(fil)
    print(f"  ⚠ {navn} fejlede: {fejl}" + (f" — bruger kopi fra for {alder} dage siden" if alder is not None else ""))
    return {"ok": False, "fejl": fejl, "cache_alder_dage": alder,
            "antal": antal_produkter(fil) if alder is not None else 0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROD / "docs", help="Mappe til index.html og status.json")
    args = ap.parse_args()
    out_dir = args.out if args.out.is_absolute() else ROD / args.out

    os.chdir(ROD)  # scraperne skriver til den relative mappe "output/"
    sys.path.insert(0, str(ROD))
    OUTPUT.mkdir(exist_ok=True)

    status = {"tidspunkt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kilder": {}}
    for navn, modul, fil, minimum in KILDER:
        status["kilder"][navn] = hent_kilde(navn, modul, fil, minimum)

    if not any(k.get("antal") for k in status["kilder"].values()):
        print("\n✗ Ingen data fra nogen kilde — siden opdateres ikke.")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    print(f"\n{'=' * 60}\n▶ PDCAAS\n{'=' * 60}", flush=True)
    importlib.import_module("pdcaas_lookup").main()

    print(f"\n{'=' * 60}\n▶ Byg side\n{'=' * 60}", flush=True)
    import build_web
    midlertidig = OUTPUT / "index.html"
    kilde_meta = {k: {"ok": v["ok"], "antal": v.get("antal", 0)} for k, v in status["kilder"].items()}
    stat = build_web.byg(out=midlertidig, meta_ekstra={"kilder": kilde_meta})
    status["side"] = stat

    if stat.get("ud", 0) < MIN_VARER:
        print(f"\n✗ Kun {stat.get('ud')} varer — den eksisterende side bevares.")
        raise SystemExit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(midlertidig, out_dir / "index.html")
    (out_dir / ".nojekyll").touch()
    with open(out_dir / "status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Færdig: {out_dir / 'index.html'}")
    print(json.dumps(status["kilder"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
