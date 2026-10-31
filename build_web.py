"""
build_web.py — bygger Billig Protein-siden (én selvstændig HTML-fil).

Læser output/full_matrix_pdcaas.json, udvælger varer med kurator.py og
indsætter data i web/template.html. Al layout og JavaScript ligger i
skabelonen, så denne fil indeholder kun data-logik.

Brug:
  python build_web.py                    → web/index.html
  python build_web.py --out docs/index.html
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from kurator import KATEGORIER, kurater

ROD = Path(__file__).resolve().parent
INPUT_FILE = ROD / "output" / "full_matrix_pdcaas.json"
TEMPLATE_FILE = ROD / "web" / "template.html"
STANDARD_OUT = ROD / "web" / "index.html"
REPO_URL = "https://github.com/KielCode/billig-protein"

# Typiske værdier pr. 100 g rå vare (vises under "Næringsreference").
# PDCAAS-værdierne svarer til dem der bruges i pdcaas_lookup.py.
REFERENCE = [
    ("🍗 Fjerkræ", [
        ("Kyllingebrystfilet", 23.0, 1.5, 105, 0.91),
        ("Hel kylling (spiselig del)", 20.0, 9.0, 157, 0.91),
        ("Kyllingelår (spiselig del)", 18.0, 12.0, 180, 0.91),
        ("Kalkunbryst", 24.0, 1.5, 110, 0.91),
    ]),
    ("🥩 Okse", [
        ("Hakket oksekød 8–12 %", 20.0, 10.0, 170, 0.92),
        ("Oksebøf", 21.0, 6.0, 145, 0.92),
    ]),
    ("🥓 Svin", [
        ("Svinemørbrad", 21.0, 3.0, 115, 0.88),
        ("Hakket svinekød 8–12 %", 19.0, 10.0, 170, 0.88),
        ("Nakkefilet", 18.0, 15.0, 210, 0.88),
    ]),
    ("🐟 Fisk & skaldyr", [
        ("Tun i vand (dåse)", 26.0, 1.0, 115, 0.90),
        ("Laks", 20.0, 13.0, 200, 0.90),
        ("Torsk", 17.0, 0.7, 76, 0.90),
        ("Rejer", 16.0, 1.0, 72, 0.90),
    ]),
    ("🥛 Mejeri & æg", [
        ("Æg (1 stk. ≈ 60 g)", 12.5, 10.0, 140, 1.00),
        ("Skyr naturel", 11.0, 0.2, 63, 1.00),
        ("Skyr med smag", 8.0, 0.5, 75, 1.00),
        ("Hytteost 1,5 %", 13.0, 1.5, 75, 1.00),
        ("Skummetmælk", 3.5, 0.1, 35, 1.00),
        ("Skæreost 30+", 26.0, 16.0, 250, 1.00),
        ("Valleproteinpulver", 75.0, 6.0, 390, 1.00),
    ]),
    ("🌱 Plantebaseret", [
        ("Røde linser (tørre)", 24.0, 1.5, 340, 0.52),
        ("Kikærter (tørre)", 20.0, 6.0, 340, 0.71),
        ("Gule ærter (tørre)", 22.0, 1.5, 330, 0.69),
        ("Kidneybønner (dåse)", 8.0, 0.5, 100, 0.68),
        ("Tofu (fast)", 12.0, 7.0, 120, 0.91),
        ("Edamame", 11.0, 5.0, 120, 0.91),
    ]),
]


def byg(input_file: Path = INPUT_FILE, out: Path = STANDARD_OUT, meta_ekstra: dict | None = None) -> dict:
    """Bygger siden og returnerer statistik."""
    with open(input_file, encoding="utf-8") as f:
        rå = json.load(f)
    produkter, stat = kurater(rå.get("products", []))

    meta = {
        "genereret": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "antal": len(produkter),
        "repo": REPO_URL,
        **(meta_ekstra or {}),
    }
    data = {
        "meta": meta,
        "kategorier": KATEGORIER,
        "produkter": produkter,
        "reference": [
            {"gruppe": gruppe, "rows": [
                {"navn": n, "prot": p, "fedt": f, "kcal": k, "pdcaas": q} for n, p, f, k, q in rækker
            ]}
            for gruppe, rækker in REFERENCE
        ],
    }
    # "</" escapes, så JSON aldrig kan lukke <script>-tagget
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    skabelon = TEMPLATE_FILE.read_text(encoding="utf-8")
    assert skabelon.count("__DATA__") == 1, "Skabelonen skal indeholde __DATA__ præcis én gang"
    html = skabelon.replace("__DATA__", data_json)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    butikker = sorted({p["butik"] for p in produkter})
    print(f"✓ {out}  ({len(produkter)} varer, {len(butikker)} butikker, {len(html) // 1024} KB)")
    print(f"  Frasorteret: {stat}")
    return {**stat, "butikker": butikker}


def main() -> None:
    ap = argparse.ArgumentParser(description="Byg Billig Protein-siden")
    ap.add_argument("--out", type=Path, default=STANDARD_OUT, help="Hvor HTML-filen skal gemmes")
    ap.add_argument("--input", type=Path, default=INPUT_FILE)
    args = ap.parse_args()
    if not args.input.exists():
        print(f"FEJL: {args.input} ikke fundet — kør pdcaas_lookup.py først.")
        raise SystemExit(1)
    byg(args.input, args.out)


if __name__ == "__main__":
    main()
