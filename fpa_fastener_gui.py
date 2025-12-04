#!/usr/bin/env python3
"""
FPA Fastener Selector (GUI + Zone-by-Zone)
=========================================

Legal/Engineering Disclaimer
- Screening/selection aid only; not a sealed analysis. Confirm governing code/edition (FBC + ASCE 7), internal pressure, roof zone extents,
  topography (Kzt), and installation details. Values are pulled from the FPA PDFs; when parsing fails, use YAML overrides or verify manually.
  No warranties.

Dependencies
    pip install pdfplumber pyyaml requests tkinter

Run
    python fpa_fastener_gui.py --gui
    # Or CLI mode (explicit wind column; no interpolation):
    python fpa_fastener_gui.py --panel GulfSeam --address "Naples, FL" --exposure C --height 30 --wind-col 150
"""
import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pdfplumber
import requests
import yaml

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    HAVE_TK = True
except Exception:
    HAVE_TK = False

UA = {"User-Agent": "fpa-zone-fastener/2.1"}

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
DEFAULT_FPA_DIR = r"Q:\\Engineering\\All current FPAs"
HEIGHTS = [20, 25, 30, 40, 50, 60]
WIND_COLS = [120, 130, 140, 150, 160, 170, 180, 190, 200]
EXPOSURES = ["B", "C", "D"]
ZONES = ["Zone 1", "Zone 2", "Zone 3"]

# Friendly panel → nonHVHZ.pdf filename (under DEFAULT_FPA_DIR)
PANEL_TO_PDF = {
    "5V-Crimp": "5V-Crimp-nonHVHZ.pdf",
    "GulfLok": "GulfLok-nonHVHZ.pdf",
    "GulfPBR": "GulfPBR-nonHVHZ.pdf",
    "GulfRib": "GulfRib-nonHVHZ.pdf",
    "GulfSeam": "GulfSeam-nonHVHZ.pdf",
    "GulfSnap": "GulfSnap-nonHVHZ.pdf",
    "GulfWave": "GulfWave-nonHVHZ.pdf",
    "MegaLoc": "MegaLoc-nonHVHZ.pdf",
    "VersaLoc": "VersaLoc-nonHVHZ.pdf",
}
SUPPORTED_PANELS = list(PANEL_TO_PDF.keys())

# ------------------------------------------------------------------
# Geocode (address for context only)
# ------------------------------------------------------------------

def geocode(address: str) -> Tuple[Optional[float], Optional[float], str]:
    if not address:
        return None, None, ""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "addressdetails": 1, "countrycodes": "us", "limit": 1},
            headers=UA,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None, None, address
        lat = float(data[0]["lat"]) ; lon = float(data[0]["lon"]) ; disp = data[0].get("display_name", address)
        return lat, lon, disp
    except Exception:
        return None, None, address

# ------------------------------------------------------------------
# PDF parsing (conversion charts + allowables)
# ------------------------------------------------------------------
@dataclass
class Chart:
    exposure: str
    zone: str
    table: Dict[int, Dict[int, float]]  # height -> wind_col -> pressure (negative psf)

@dataclass
class Allowable:
    system: str
    description: str
    allowable_psf: float

@dataclass
class ParsedFPA:
    charts: List[Chart]
    allowables: List[Allowable]

EXPOSURE_PAGE_HINTS = {
    "B": re.compile(r"Exposure\s*B", re.I),
    "C": re.compile(r"Exposure\s*C", re.I),
    "D": re.compile(r"Exposure\s*D", re.I),
}
ROW_HEAD_RE = re.compile(r"^(20|25|30|40|50|60)\b")
WIND_ROW_RE = re.compile(r"1\s*20\b.*200\b")


def _extract_last_pages(pdf_path: str):
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        return [(i, pdf.pages[i]) for i in range(max(0, total - 3), total)]


def _parse_grid_from_text(page_text: str) -> Optional[Dict[int, Dict[int, float]]]:
    if not WIND_ROW_RE.search(page_text.replace("\n", " ")):
        return None
    rows = {}
    for line in page_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = ROW_HEAD_RE.match(line)
        if not m:
            continue
        parts = re.split(r"\s+", line)
        try:
            h = int(parts[0])
            nums = [float(x.replace(",", "")) for x in parts[1:1 + len(WIND_COLS)]]
            if len(nums) == len(WIND_COLS):
                rows[h] = {WIND_COLS[i]: nums[i] for i in range(len(WIND_COLS))}
        except Exception:
            continue
    return rows or None


def _parse_grid_from_tables(page) -> Optional[Dict[int, Dict[int, float]]]:
    grids = page.extract_tables() or []
    for g in grids:
        header_found = False
        norm = [[(c or "").strip() for c in row] for row in g]
        for row in norm:
            row_text = " ".join(row)
            if WIND_ROW_RE.search(row_text):
                header_found = True
                break
        if not header_found:
            continue
        data: Dict[int, Dict[int, float]] = {}
        for row in norm:
            if len(row) < len(WIND_COLS) + 1:
                continue
            if not ROW_HEAD_RE.match(row[0]):
                continue
            try:
                h = int(row[0])
                vals = []
                for cell in row[1:1 + len(WIND_COLS)]:
                    cell = (cell or "").replace(",", "")
                    m = re.search(r"-?\d+(?:\.\d+)?", cell)
                    if not m:
                        raise ValueError
                    vals.append(float(m.group()))
                if len(vals) == len(WIND_COLS):
                    data[h] = {WIND_COLS[i]: vals[i] for i in range(len(WIND_COLS))}
            except Exception:
                continue
        if len(data) >= 4:
            return data
    return None

# Systems/allowables: look on early pages (first ~6)
SYS_TAG = re.compile(r"^(GS|GL|VL|ML|GP|GR|5V)-\d+\b")

def _parse_allowables(pdf_path: str) -> List[Allowable]:
    out: List[Allowable] = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages[:6]:
            # table-first
            tables = p.extract_tables() or []
            for t in tables:
                norm = [[(c or "").strip() for c in row] for row in t]
                for row in norm:
                    if not row:
                        continue
                    first = row[0]
                    if first and SYS_TAG.match(first):
                        tail = row[-1]
                        m = re.search(r"-?\d+(?:\.\d+)?", tail or "")
                        if m:
                            out.append(Allowable(system=first, description=" ".join(row[1:-1])[:240], allowable_psf=float(m.group())))
            # text fallback
            txt = p.extract_text(x_tolerance=2, y_tolerance=2) or ""
            for line in txt.splitlines():
                if SYS_TAG.match(line):
                    m = re.search(r"(-?\d+(?:\.\d+)?)\s*psf", line, re.I)
                    if m:
                        out.append(Allowable(system=line.split()[0], description=line[:200], allowable_psf=float(m.group(1))))
    # de-dup
    uniq: Dict[Tuple[str, float], Allowable] = {}
    for a in out:
        uniq[(a.system, a.allowable_psf)] = a
    return sorted(uniq.values(), key=lambda a: a.allowable_psf)

def _parse_charts(pdf_path: str) -> List[Chart]:
    """
    Parse the conversion charts from the last ~3 pages of the FPA PDF.

    IMPORTANT:
    - We open the PDF here and keep it open while we work with `page` objects.
    - This avoids the 'seek of closed file' error you were seeing when pages were
      returned from a helper AFTER the file was already closed.
    """
    charts: List[Chart] = []

    import pdfplumber  # local import so module can be imported even if pdfplumber missing

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        last_indices = list(range(max(0, total - 3), total))  # e.g. last 3 pages

        for i, idx in enumerate(last_indices):
            page = pdf.pages[idx]
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""

            # 1) Detect exposure; fallback by position (B, C, D)
            exp = None
            for e, pat in EXPOSURE_PAGE_HINTS.items():
                if pat.search(text):
                    exp = e
                    break
            if not exp:
                exp = EXPOSURES[i % 3]

            # 2) Try to find separate tables per zone on this page
            zone_tables: List[Dict[int, Dict[int, float]]] = []

            tables = page.extract_tables() or []
            for t in tables:
                norm_txt = " ".join(" ".join((c or "").strip() for c in row) for row in t)
                if not WIND_ROW_RE.search(norm_txt):
                    continue

                data: Dict[int, Dict[int, float]] = {}
                for row in t:
                    row = [(c or "").strip() for c in row]
                    if not row or not ROW_HEAD_RE.match(row[0]):
                        continue
                    try:
                        h = int(row[0])
                        vals = []
                        for cell in row[1:1 + len(WIND_COLS)]:
                            cell = (cell or "").replace(",", "")
                            m = re.search(r"-?\d+(?:\.\d+)?", cell)
                            if not m:
                                raise ValueError
                            vals.append(float(m.group()))
                        if len(vals) == len(WIND_COLS):
                            data[h] = {WIND_COLS[i2]: vals[i2] for i2 in range(len(WIND_COLS))}
                    except Exception:
                        continue

                if len(data) >= 4:
                    zone_tables.append(data)

            # 3) If we didn't get zone-specific tables, fall back to generic grid parser
            if not zone_tables:
                g = _parse_grid_from_text(text) or _parse_grid_from_tables(page)
                if g:
                    # Use same grid for all 3 zones on that page
                    zone_tables = [g, g, g]

            # 4) Build Chart objects for Zone 1/2/3
            for zi, zname in enumerate(ZONES):
                if zone_tables:
                    table = zone_tables[zi] if zi < len(zone_tables) else zone_tables[-1]
                else:
                    table = {}
                charts.append(Chart(exposure=exp, zone=zname, table=table or {}))

    # 5) YAML overrides: <pdf>.yaml to supply exact grids per exposure/zone when parsing is tricky
    ypath = os.path.splitext(pdf_path)[0] + ".yaml"
    if os.path.exists(ypath):
        with open(ypath, "r", encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
        for ch in charts:
            ov = (((y.get("charts") or {}).get(ch.exposure) or {}).get(ch.zone))
            if ov:
                ch.table = {int(h): {int(w): float(p) for w, p in row.items()} for h, row in ov.items()}

    return charts



# ------------------------------------------------------------------
# Selection
# ------------------------------------------------------------------

def parse_fpa(pdf_path: str) -> ParsedFPA:
    charts = _parse_charts(pdf_path)
    allowables = _parse_allowables(pdf_path)
    return ParsedFPA(charts=charts, allowables=allowables)


def select_zone_patterns(fpa: ParsedFPA, exposure: str, height: int, wind_col: int) -> Dict[str, Dict]:
    if height not in HEIGHTS:
        return {"error": "Height must be one of 20/25/30/40/50/60 ft."}
    if wind_col not in WIND_COLS:
        return {"error": f"Wind column must be one of {WIND_COLS}."}

    allow = sorted(fpa.allowables, key=lambda a: a.allowable_psf)
    out: Dict[str, Dict] = {}

    for z in ZONES:
        grid = None
        for ch in fpa.charts:
            if ch.exposure == exposure and ch.zone == z and ch.table:
                grid = ch.table
                break
        if not grid:
            out[z] = {"status": "needs_mapping", "reason": "Chart not parsed; add YAML override."}
            continue
        req = grid.get(height, {}).get(wind_col)
        if req is None:
            out[z] = {"status": "needs_mapping", "reason": f"No value at height {height} and wind {wind_col} mph."}
            continue
        need = abs(float(req))
        chosen = None
        for a in allow:
            if a.allowable_psf >= need:
                chosen = a
                break
        if chosen:
            out[z] = {
                "status": "ok",
                "required_psf": round(need, 2),
                "wind_col_mph": wind_col,
                "pattern": chosen.system,
                "allowable_psf": chosen.allowable_psf,
                "desc": chosen.description,
            }
        else:
            out[z] = {
                "status": "engineering_required",
                "required_psf": round(need, 2),
                "wind_col_mph": wind_col,
                "max_allowable_psf": allow[-1].allowable_psf if allow else None,
            }
    return out

# ------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------

def run_once(panel: str, address: str, exposure: str, height: int, wind_col: int, fpa_dir: Optional[str] = None):
    if height > 60:
        return {"status": "engineering_required", "reason": "Height > 60 ft per policy"}

    pdf_dir = fpa_dir or DEFAULT_FPA_DIR
    pdf_name = PANEL_TO_PDF.get(panel)
    if not pdf_name:
        return {"status": "error", "reason": f"Unknown panel '{panel}'."}
    pdf_path = os.path.join(pdf_dir, pdf_name)
    if not os.path.exists(pdf_path):
        return {"status": "error", "reason": f"PDF not found: {pdf_path}"}

    lat, lon, disp = geocode(address)

    try:
        fpa = parse_fpa(pdf_path)
        zones = select_zone_patterns(fpa, exposure, height, wind_col)
    except Exception as e:
        # Never crash FastAPI; just surface an error blob
        return {
            "status": "error",
            "reason": f"FPA parse/selection failed: {e}",
            "panel": panel,
            "pdf": os.path.basename(pdf_path),
        }

    return {
        "status": "ok",
        "address": disp,
        "lat": lat,
        "lon": lon,
        "panel": panel,
        "pdf": os.path.basename(pdf_path),
        "exposure": exposure,
        "height_ft": height,
        "wind_col_mph": wind_col,
        "zone_results": zones,
        "legal": (
            "Screening/selection aid using values from the Florida Product Approval PDF. Confirm governing code/edition, "
            "internal pressure category, zone geometry, and installation details. No interpolation; wind column is chosen "
            "from the chart (conservative rounding up if needed). Not a sealed analysis."
        ),
    }

# ------------------------------------------------------------------
# CLI + GUI
# ------------------------------------------------------------------

def main_cli(args):
    res = run_once(args.panel, args.address, args.exposure, args.height, args.wind_col, args.fpa_dir)
    print(json.dumps(res, indent=2))


def launch_gui():
    if not HAVE_TK:
        print("Tkinter not available on this system.")
        return

    root = tk.Tk()
    root.title("FPA Fastener Selector — Zone-by-Zone")

    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky="nsew")

    # Directory picker (defaults to Q:\...)
    tk.Label(frm, text="FPA Folder:").grid(row=0, column=0, sticky="e")
    dir_var = tk.StringVar(value=DEFAULT_FPA_DIR)
    tk.Entry(frm, textvariable=dir_var, width=60).grid(row=0, column=1, columnspan=3, sticky="we")
    def browse_dir():
        d = filedialog.askdirectory()
        if d:
            dir_var.set(d)
    ttk.Button(frm, text="Browse", command=browse_dir).grid(row=0, column=4, padx=4)

    # Address
    tk.Label(frm, text="Address:").grid(row=1, column=0, sticky="e")
    addr_var = tk.StringVar()
    tk.Entry(frm, textvariable=addr_var, width=60).grid(row=1, column=1, columnspan=4, sticky="we")

    # Panel
    tk.Label(frm, text="Panel:").grid(row=2, column=0, sticky="e")
    panel_var = tk.StringVar(value=SUPPORTED_PANELS[0])
    ttk.Combobox(frm, values=SUPPORTED_PANELS, textvariable=panel_var, state="readonly", width=24).grid(row=2, column=1, sticky="w")

    # Exposure
    tk.Label(frm, text="Exposure:").grid(row=2, column=2, sticky="e")
    exp_var = tk.StringVar(value="C")
    ttk.Combobox(frm, values=EXPOSURES, textvariable=exp_var, state="readonly", width=6).grid(row=2, column=3, sticky="w")

    # Height
    tk.Label(frm, text="Height (ft):").grid(row=3, column=0, sticky="e")
    h_var = tk.IntVar(value=30)
    ttk.Combobox(frm, values=HEIGHTS, textvariable=h_var, state="readonly", width=8).grid(row=3, column=1, sticky="w")

    # Wind column
    tk.Label(frm, text="Wind (mph):").grid(row=3, column=2, sticky="e")
    w_var = tk.IntVar(value=150)
    ttk.Combobox(frm, values=WIND_COLS, textvariable=w_var, state="readonly", width=8).grid(row=3, column=3, sticky="w")

    # Output
    out = tk.Text(frm, width=100, height=28)
    out.grid(row=5, column=0, columnspan=5, pady=(8, 0))

    def run():
        out.delete("1.0", tk.END)
        try:
            res = run_once(panel_var.get(), addr_var.get(), exp_var.get(), int(h_var.get()), int(w_var.get()), dir_var.get())
            out.insert(tk.END, json.dumps(res, indent=2))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    ttk.Button(frm, text="Compute", command=run).grid(row=4, column=0, pady=6, sticky="w")

    root.mainloop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FPA-driven zone fastener selector (GUI + CLI)")
    ap.add_argument("--gui", action="store_true", help="Launch GUI")
    ap.add_argument("--panel", choices=SUPPORTED_PANELS)
    ap.add_argument("--address")
    ap.add_argument("--exposure", choices=EXPOSURES)
    ap.add_argument("--height", type=int, choices=HEIGHTS)
    ap.add_argument("--wind-col", type=int, choices=WIND_COLS)
    ap.add_argument("--fpa-dir", help="Override FPA directory (defaults to Q:\\Engineering\\All current FPAs)")

    args = ap.parse_args()

    if args.gui:
        launch_gui()
    else:
        if not all([args.panel, args.address, args.exposure, args.height, args.wind_col]):
            print("Missing required arguments. Use --gui or provide --panel --address --exposure --height --wind-col")
            raise SystemExit(2)
        main_cli(args)
