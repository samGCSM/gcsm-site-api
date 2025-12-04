# site_specific_desktop_app.py
# ===========================

# Deps:
#   pip install reportlab pymupdf
#
# Run:
#   python site_specific_desktop_app.py

import os
import sys
import json
import math
from datetime import datetime

# Optional import for FPA helper; app works if unavailable
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# PDF generation
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader

APP_TITLE   = "GCSM Site-Specific Uplift — Pat Style"
APP_VERSION = "1.0.0"

# Your network path; used as initial browse dir if available
DEFAULT_FPA_DIR = r"Q:\Engineering\All current FPAs"

# Place a PNG here to show in UI header and PDF header
LOGO_PATH = "gcsm_logo.png"

# Persisted small settings between runs
STATE_FILE = "gcsm_app_state.json"

# ---------- Utilities ----------

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_state(data: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def round_up(value, precision=1):
    """Round upward in magnitude to nearest 'precision' (default 1)."""
    if precision <= 0:
        precision = 1
    return math.ceil(float(value) / precision) * precision

# Nominal↔Ultimate quick relation (informational only)
# Baseline design reports ultimate (ASCE 7). Nominal is optional.
ULTIMATE_TO_NOMINAL_FACTOR = 0.6  # V_nom ≈ 0.6 * V_ult (rule-of-thumb)

def ult_to_nominal(v_ult):
    try:
        return float(v_ult) * ULTIMATE_TO_NOMINAL_FACTOR
    except Exception:
        return None

def nominal_to_ult(v_nom):
    try:
        v_nom = float(v_nom)
        if ULTIMATE_TO_NOMINAL_FACTOR == 0:
            return None
        return v_nom / ULTIMATE_TO_NOMINAL_FACTOR
    except Exception:
        return None

#--------------------------------------------------------------

# site_specific_desktop_app.py

def build_project_dict(address, prepared_by, panel_type, gauge, substrate,
                       risk_category, exposure, mean_roof_height_ft,
                       height_band, wind_speed_ult, wind_speed_nominal=None,
                       use_nominal=False):
    return {
        "name": address,  # or custom name
        "address": address,
        "prepared_by": prepared_by,
        "panel_type": panel_type,
        "gauge": gauge,
        "substrate": substrate,
        "risk_category": risk_category,
        "exposure": exposure,
        "mean_roof_height_ft": str(mean_roof_height_ft),
        "height_band": height_band,
        "wind_speed_ult": str(wind_speed_ult),
        "wind_speed_nominal": str(wind_speed_nominal) if wind_speed_nominal else "",
        "use_nominal": use_nominal,
        "fpa_path": "",
    }

# ---------- Model options (mirror typical FPA menus) ----------

RISK_CATEGORIES = ["I", "II", "III", "IV"]
EXPOSURES       = ["B", "C", "D"]

HEIGHT_BANDS = [
    "<=15 ft",
    "16-20 ft",
    "21-25 ft",
    "26-30 ft",
    "31-35 ft",
    "36-40 ft",
    ">40 ft",
]

ULT_WIND_SPEEDS = ["130", "140", "150", "160", "165", "169", "170", "180"]

PANEL_TYPES = [
    "GulfSeam",
    "VersaLoc",
    "MegaLoc",
    "GulfLok",
    "5V",
]

MATERIAL_GAUGES = ["24 ga", "26 ga", ".032", ".040"]

SUBSTRATES = [
    "Plywood 5/8\"",
    "Plywood 3/4\"",
    "Open Framing",
    "16ga Purlins",
    "18ga Purlins",
]

# ---------- Optional FPA helper (best-effort text scan of last 3 pages) ----------

class FPAHelper:
    """
    Tries to scan last 3 pages of an FPA PDF and surface possible Z1/Z2/Z3 triples and
    any height/wind strings seen. Layouts vary; this is a helper only—manual entry rules.
    """
    @staticmethod
    def try_extract(path):
        if fitz is None:
            raise RuntimeError("PyMuPDF (fitz) is not installed. Run: pip install pymupdf")
        data = {
            "heights": set(),
            "winds": set(),
            "samples": []  # dicts like {"Z1": ..., "Z2": ..., "Z3": ..., "src": page_index}
        }
        doc = fitz.open(path)
        try:
            pages_to_scan = list(range(max(0, len(doc)-3), len(doc)))
            for pno in pages_to_scan:
                page = doc[pno]
                text = page.get_text("text")
                for raw in text.splitlines():
                    s = raw.strip()

                    # collect height bands seen
                    for hb in HEIGHT_BANDS:
                        if hb in s:
                            data["heights"].add(hb)

                    # collect ult wind speeds seen
                    for ws in ULT_WIND_SPEEDS:
                        if (f"{ws} ") in s or s.endswith(ws) or (f" {ws} ") in s:
                            data["winds"].add(ws)

                    # look for Z1/Z2/Z3-ish lines
                    if ("Z1" in s) or ("Z2" in s) or ("Z3" in s):
                        tokens = s.replace("PSF", "psf").replace("psf", "").split()
                        nums = [t for t in tokens if t.replace(".", "", 1).replace("-", "", 1).isdigit()]
                        if len(nums) >= 3:
                            try:
                                z1, z2, z3 = [float(n) for n in nums[:3]]
                                data["samples"].append({"Z1": z1, "Z2": z2, "Z3": z3, "src": pno})
                            except Exception:
                                pass
        finally:
            doc.close()
        return data

# ---------- PDF bits ----------

def draw_header(pc, title, w, h):
    margin = 0.5 * inch
    y = h - margin
    pc.setFont("Helvetica-Bold", 14)
    pc.drawString(margin, y, title)
    pc.setFont("Helvetica", 9)
    pc.drawRightString(w - margin, y, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    y -= 0.15 * inch
    pc.setLineWidth(0.8)
    pc.line(margin, y, w - margin, y)
    y -= 0.1 * inch

    # Logo in header if present
    if os.path.exists(LOGO_PATH):
        try:
            img = ImageReader(LOGO_PATH)
            pc.drawImage(img, w - margin - 1.2*inch, h - 0.85*inch,
                         width=1.1*inch, height=0.55*inch, mask='auto')
        except Exception:
            pass
    return y
def make_report_pdf(out_path, project, calc):
    pc = pdfcanvas.Canvas(out_path, pagesize=LETTER)
    w, h = LETTER

    # Page 1 — Pat-style Summary
    y = draw_header(pc, "Site-Specific Uplift — Summary (Pat Style)", w, h)
    margin = 0.5 * inch
    line_h = 0.22 * inch

    def row(label, value):
        nonlocal y
        pc.setFont("Helvetica-Bold", 10)
        pc.drawString(margin, y, label)
        pc.setFont("Helvetica", 10)
        pc.drawString(margin + 2.0*inch, y, str(value))
        y -= line_h

    # Project block
    y -= 0.05*inch
    pc.setFont("Helvetica-Bold", 11)
    pc.drawString(margin, y, "Project Information")
    y -= 0.12*inch
    pc.setLineWidth(0.4)
    pc.line(margin, y, w - margin, y)
    y -= 0.12*inch

    row("Project Name:", project.get("name", ""))
    row("Address:", project.get("address", ""))
    row("Prepared By:", project.get("prepared_by", "GCSM Technical Services"))
    row("Date:", datetime.now().strftime("%Y-%m-%d"))

    # Inputs block
    y -= 0.12*inch
    pc.setFont("Helvetica-Bold", 11)
    pc.drawString(margin, y, "Inputs")
    y -= 0.12*inch
    pc.setLineWidth(0.4)
    pc.line(margin, y, w - margin, y)
    y -= 0.12*inch

    row("Panel:", project.get("panel_type"))
    row("Gauge:", project.get("gauge"))
    row("Substrate:", project.get("substrate"))
    row("Risk Category:", project.get("risk_category"))
    row("Exposure:", project.get("exposure"))
    row("Mean Roof Height:", f"{project.get('mean_roof_height_ft', '')} ft")
    if project.get("use_nominal"):
        row("Wind Speed (Nominal):", f"{project.get('wind_speed_nominal', '')} mph (user)")
        row("Wind Speed (Ultimate):", f"{project.get('wind_speed_ult','')} mph (converted/verified)")
    else:
        row("Wind Speed (Ultimate):", f"{project.get('wind_speed_ult','')} mph")

    # Zones
    y -= 0.12*inch
    pc.setFont("Helvetica-Bold", 11)
    pc.drawString(margin, y, "C&C Pressures (from FPA charts; NO interpolation; rounded up)")
    y -= 0.12*inch
    pc.setLineWidth(0.4)
    pc.line(margin, y, w - margin, y)
    y -= 0.12*inch

    row("Zone 1 (psf):", calc.get("Z1_psf", ""))
    row("Zone 2 (psf):", calc.get("Z2_psf", ""))
    row("Zone 3 (psf):", calc.get("Z3_psf", ""))

    # Notes
    y -= 0.08*inch
    pc.setFont("Helvetica", 9)
    notes = [
        "Ultimate wind speeds per ASCE 7 are used as the basis for design.",
        "If nominal speeds are provided, they are converted/verified against ultimate.",
        "Non-HVHZ FPA is assumed all-inclusive per manufacturer guidance.",
        "Zone pressures entered are taken directly from FPA tables (no interpolation).",
        "Conservative rounding up applied where applicable.",
    ]
    for n in notes:
        pc.drawString(margin, y, f"• {n}")
        y -= 0.18*inch

    pc.showPage()

    # Page 2 — Compact Calc Summary
    y = draw_header(pc, "Compact Calculation Summary", w, h)
    margin = 0.5 * inch
    pc.setFont("Helvetica-Bold", 10)
    headers = ["Item", "Value"]
    col_x = [margin, margin + 3.2*inch]
    y -= 0.15 * inch
    pc.drawString(col_x[0], y, headers[0])
    pc.drawString(col_x[1], y, headers[1])
    y -= 0.1 * inch
    pc.setLineWidth(0.8)
    pc.line(margin, y, w - margin, y)
    y -= 0.14 * inch
    pc.setFont("Helvetica", 10)

    rows = [
        ("Panel", project.get("panel_type")),
        ("Gauge", project.get("gauge")),
        ("Substrate", project.get("substrate")),
        ("Risk Category", project.get("risk_category")),
        ("Exposure", project.get("exposure")),
        ("Mean Roof Height", f"{project.get('mean_roof_height_ft','')} ft"),
        ("Wind Speed (ult)", f"{project.get('wind_speed_ult','')} mph"),
        ("Zone 1", f"{calc.get('Z1_psf','')} psf"),
        ("Zone 2", f"{calc.get('Z2_psf','')} psf"),
        ("Zone 3", f"{calc.get('Z3_psf','')} psf"),
    ]
    for label, value in rows:
        pc.drawString(col_x[0], y, str(label))
        pc.drawString(col_x[1], y, str(value))
        y -= 0.22 * inch

    y -= 0.1*inch
    pc.setFont("Helvetica-Oblique", 9)
    pc.drawString(margin, y, "This summary accompanies the Pat-style page and is not a substitute for code compliance.")
    pc.showPage()
    pc.save()

# ---------- UI ----------

class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.interior = ttk.Frame(self.canvas)
        self.interior.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.interior, anchor="nw")
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.bind_events()

    def bind_events(self):
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.geometry("1100x760")

        self.state_data = load_state()
        self.project = {
            "name":              self.state_data.get("project_name", ""),
            "address":           self.state_data.get("project_address", ""),
            "prepared_by":       self.state_data.get("prepared_by", "Jared Pearce, E.I.T."),
            "panel_type":        self.state_data.get("panel_type", PANEL_TYPES[0]),
            "gauge":             self.state_data.get("gauge", MATERIAL_GAUGES[1]),
            "substrate":         self.state_data.get("substrate", SUBSTRATES[0]),
            "risk_category":     self.state_data.get("risk_category", "II"),
            "exposure":          self.state_data.get("exposure", "B"),
            "mean_roof_height_ft": self.state_data.get("mean_roof_height_ft", "25"),
            "height_band":       self.state_data.get("height_band", HEIGHT_BANDS[2]),
            "wind_speed_ult":    self.state_data.get("wind_speed_ult", "169"),
            "wind_speed_nominal": self.state_data.get("wind_speed_nominal", ""),
            "use_nominal":       self.state_data.get("use_nominal", False),
            "fpa_path":          self.state_data.get("fpa_path", DEFAULT_FPA_DIR if os.path.exists(DEFAULT_FPA_DIR) else ""),
        }
        self.calc = {"Z1_psf": "", "Z2_psf": "", "Z3_psf": ""}
        self.fpa_data = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- UI build ---
    def _build_ui(self):
        # Header
        header = ttk.Frame(self); header.pack(fill="x", padx=8, pady=6)
        title_lbl = ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold"))
        subtitle_lbl = ttk.Label(header, text=f"Version {APP_VERSION} — GCSM Technical Services", font=("Segoe UI", 9))
        title_lbl.pack(side="left")
        subtitle_lbl.pack(side="left", padx=(12, 0))

        # Logo (tk.PhotoImage supports PNG; no Pillow needed)
        try:
            if os.path.exists(LOGO_PATH):
                self.logo_tk = tk.PhotoImage(file=LOGO_PATH)
                logo_lbl = ttk.Label(header, image=self.logo_tk)
                logo_lbl.pack(side="right")
        except Exception:
            pass

        # Toolbar
        toolbar = ttk.Frame(self); toolbar.pack(fill="x", padx=8)
        ttk.Button(toolbar, text="Open FPA…", command=self.on_open_fpa).pack(side="left")
        ttk.Button(toolbar, text="Save Project…", command=self.on_save_project).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Load Project…", command=self.on_load_project).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Generate PDF Report…", command=self.on_generate_pdf).pack(side="left", padx=(6, 0))

        # Scroll area
        scroller = ScrollableFrame(self); scroller.pack(fill="both", expand=True, padx=8, pady=8)
        root = scroller.interior

        # Project Info
        box_proj = ttk.LabelFrame(root, text="Project Info"); box_proj.pack(fill="x", padx=6, pady=6)
        self.var_name = tk.StringVar(value=self.project["name"])
        self.var_addr = tk.StringVar(value=self.project["address"])
        self.var_prep = tk.StringVar(value=self.project["prepared_by"])

        row = ttk.Frame(box_proj); row.pack(fill="x", pady=3)
        ttk.Label(row, text="Project Name:", width=20).pack(side="left")
        ttk.Entry(row, textvariable=self.var_name).pack(side="left", fill="x", expand=True)

        row = ttk.Frame(box_proj); row.pack(fill="x", pady=3)
        ttk.Label(row, text="Address:", width=20).pack(side="left")
        ttk.Entry(row, textvariable=self.var_addr).pack(side="left", fill="x", expand=True)

        row = ttk.Frame(box_proj); row.pack(fill="x", pady=3)
        ttk.Label(row, text="Prepared By:", width=20).pack(side="left")
        ttk.Entry(row, textvariable=self.var_prep).pack(side="left", fill="x", expand=True)

        # Inputs
        box_in = ttk.LabelFrame(root, text="Inputs (Match FPA Charts — NO Interpolation; Round Up)")
        box_in.pack(fill="x", padx=6, pady=6)

        self.var_panel   = tk.StringVar(value=self.project["panel_type"])
        self.var_gauge   = tk.StringVar(value=self.project["gauge"])
        self.var_sub     = tk.StringVar(value=self.project["substrate"])
        self.var_risk    = tk.StringVar(value=self.project["risk_category"])
        self.var_exp     = tk.StringVar(value=self.project["exposure"])
        self.var_mrh     = tk.StringVar(value=self.project["mean_roof_height_ft"])
        self.var_hband   = tk.StringVar(value=self.project["height_band"])
        self.var_ws_ult  = tk.StringVar(value=self.project["wind_speed_ult"])
        self.var_use_nom = tk.BooleanVar(value=self.project["use_nominal"])
        self.var_ws_nom  = tk.StringVar(value=self.project["wind_speed_nominal"])

        grid = ttk.Frame(box_in); grid.pack(fill="x", pady=4)
        def add_row(r, label, widget):
            ttk.Label(grid, text=label, width=20).grid(row=r, column=0, sticky="w", padx=(0,8), pady=3)
            widget.grid(row=r, column=1, sticky="ew", pady=3)
        grid.columnconfigure(1, weight=1)

        add_row(0, "Panel:",      ttk.Combobox(grid, textvariable=self.var_panel, values=PANEL_TYPES, state="readonly"))
        add_row(1, "Gauge:",      ttk.Combobox(grid, textvariable=self.var_gauge, values=MATERIAL_GAUGES, state="readonly"))
        add_row(2, "Substrate:",  ttk.Combobox(grid, textvariable=self.var_sub, values=SUBSTRATES, state="readonly"))
        add_row(3, "Risk Category:", ttk.Combobox(grid, textvariable=self.var_risk, values=RISK_CATEGORIES, state="readonly"))
        add_row(4, "Exposure:",   ttk.Combobox(grid, textvariable=self.var_exp, values=EXPOSURES, state="readonly"))
        add_row(5, "Mean Roof Height (ft):", ttk.Entry(grid, textvariable=self.var_mrh))
        add_row(6, "Height Band:", ttk.Combobox(grid, textvariable=self.var_hband, values=HEIGHT_BANDS, state="readonly"))
        add_row(7, "Wind Speed (ult, mph):", ttk.Combobox(grid, textvariable=self.var_ws_ult, values=ULT_WIND_SPEEDS, state="readonly"))

        row_n = ttk.Frame(box_in); row_n.pack(fill="x")
        ttk.Checkbutton(row_n,
                        text="Specify a NOMINAL wind speed (app will convert/verify ultimate for reporting)",
                        variable=self.var_use_nom, command=self.on_toggle_nominal).pack(side="left", pady=2)
        self.entry_nominal = ttk.Entry(row_n, textvariable=self.var_ws_nom, width=12)
        if self.var_use_nom.get():
            self.entry_nominal.pack(side="left", padx=6)

        expl = ttk.Label(box_in, foreground="#555", wraplength=1000, justify="left",
            text=("Design is based on ULTIMATE wind speeds per ASCE 7. If you enter a nominal speed, "
                  "the app will convert/verify against ultimate for reporting. Non-HVHZ FPA is assumed "
                  "all-inclusive. NO interpolation — pick exact FPA rows/columns; round UP conservatively "
                  "for in-between conditions."))
        expl.pack(fill="x", pady=(4, 2))

        # Zones (authoritative)
        box_z = ttk.LabelFrame(root, text="C&C Zone Pressures (psf) — Authoritative")
        box_z.pack(fill="x", padx=6, pady=6)

        self.var_z1 = tk.StringVar(value=str(self.calc.get("Z1_psf", "")))
        self.var_z2 = tk.StringVar(value=str(self.calc.get("Z2_psf", "")))
        self.var_z3 = tk.StringVar(value=str(self.calc.get("Z3_psf", "")))

        grid2 = ttk.Frame(box_z); grid2.pack(fill="x", pady=4)
        ttk.Label(grid2, text="Zone 1 (psf):", width=20).grid(row=0, column=0, sticky="w", padx=(0,8), pady=3)
        ttk.Entry(grid2, textvariable=self.var_z1).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(grid2, text="Zone 2 (psf):", width=20).grid(row=1, column=0, sticky="w", padx=(0,8), pady=3)
        ttk.Entry(grid2, textvariable=self.var_z2).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(grid2, text="Zone 3 (psf):", width=20).grid(row=2, column=0, sticky="w", padx=(0,8), pady=3)
        ttk.Entry(grid2, textvariable=self.var_z3).grid(row=2, column=1, sticky="w", pady=3)

        # FPA helper
        box_fpa = ttk.LabelFrame(root, text="FPA Helper (optional)")
        box_fpa.pack(fill="x", padx=6, pady=6)

        row = ttk.Frame(box_fpa); row.pack(fill="x", pady=3)
        self.var_fpa = tk.StringVar(value=self.project.get("fpa_path", ""))
        ttk.Label(row, text="FPA PDF:", width=20).pack(side="left")
        ttk.Entry(row, textvariable=self.var_fpa).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.on_open_fpa).pack(side="left", padx=6)
        ttk.Button(row, text="Try Extract", command=self.on_try_extract).pack(side="left")

        self.suggest_box = tk.Text(box_fpa, height=6, wrap="word")
        self.suggest_box.pack(fill="x", pady=4)
        self.suggest_box.insert("1.0",
            "No FPA suggestions yet. Click Try Extract to scan the last three pages; then copy any "
            "matching Z1/Z2/Z3 values into the authoritative fields above.")
        self.suggest_box.configure(state="disabled")

        # Actions + Status
        action = ttk.Frame(root); action.pack(fill="x", pady=8)
        ttk.Button(action, text="Compute & Update (Apply rules)", command=self.on_compute).pack(side="left")
        ttk.Button(action, text="Generate PDF Report…", command=self.on_generate_pdf).pack(side="left", padx=6)

        self.status = ttk.Label(self, anchor="w", relief="sunken")
        self.status.pack(fill="x")
        self.set_status("Ready. Ultimate speeds are authoritative; nominal optional.")
    # --- helpers / callbacks ---
    def set_status(self, msg):
        self.status.config(text=msg)
        self.update_idletasks()

    def on_toggle_nominal(self):
        if self.var_use_nom.get():
            self.entry_nominal.pack(side="left", padx=6)
        else:
            try:
                self.entry_nominal.pack_forget()
            except Exception:
                pass

    def on_open_fpa(self):
        # Decide initial directory cleverly
        current = self.var_fpa.get()
        initialdir = current if os.path.isdir(current) else os.path.dirname(current)
        if not initialdir:
            initialdir = DEFAULT_FPA_DIR if os.path.exists(DEFAULT_FPA_DIR) else os.getcwd()
        path = filedialog.askopenfilename(
            title="Open FPA PDF",
            initialdir=initialdir,
            filetypes=[("PDF", "*.pdf")]
        )
        if path:
            self.var_fpa.set(path)
            self.set_status(f"Loaded FPA path: {path}")

    def on_try_extract(self):
        path = self.var_fpa.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("FPA Helper", "Please choose a valid FPA PDF file.")
            return
        try:
            self.set_status("Parsing FPA (last 3 pages)…")
            data = FPAHelper.try_extract(path)
            self.fpa_data = data

            self.suggest_box.configure(state="normal")
            self.suggest_box.delete("1.0", "end")
            lines = []
            if data.get("heights"):
                lines.append("Heights found: " + ", ".join(sorted(data["heights"])))
            if data.get("winds"):
                lines.append("Wind speeds (ult) found: " + ", ".join(sorted(data["winds"], key=lambda x: float(x))))
            if data.get("samples"):
                lines.append("\nSample Z1/Z2/Z3 triples (copy into authoritative fields if they match your chart):")
                for s in data["samples"][:12]:
                    lines.append(f"  p.{s['src']+1}: Z1={s['Z1']}  Z2={s['Z2']}  Z3={s['Z3']}")
            if not lines:
                lines.append("No recognizable patterns found. Manual entry above still works.")
            self.suggest_box.insert("1.0", "\n".join(lines))
            self.suggest_box.configure(state="disabled")
            self.set_status("FPA parse complete (helper only).")
        except Exception as e:
            messagebox.showwarning("FPA Helper", f"FPA extraction failed (that's ok — manual entry still works).\n\n{e}")
            self.set_status("FPA parse failed.")

    def _update_project_from_ui(self):
        self.project.update({
            "name":                self.var_name.get().strip(),
            "address":             self.var_addr.get().strip(),
            "prepared_by":         self.var_prep.get().strip(),
            "panel_type":          self.var_panel.get(),
            "gauge":               self.var_gauge.get(),
            "substrate":           self.var_sub.get(),
            "risk_category":       self.var_risk.get(),
            "exposure":            self.var_exp.get(),
            "mean_roof_height_ft": self.var_mrh.get().strip(),
            "height_band":         self.var_hband.get(),
            "wind_speed_ult":      self.var_ws_ult.get().strip(),
            "use_nominal":         self.var_use_nom.get(),
            "wind_speed_nominal":  self.var_ws_nom.get().strip(),
            "fpa_path":            self.var_fpa.get().strip(),
        })

    def on_compute(self):
        self._update_project_from_ui()

        # Parse/round Z1/Z2/Z3 conservatively (magnitude up, retain sign)
        def get_psf(var):
            raw = var.get().strip()
            if raw == "":
                return ""
            try:
                val = float(raw)
                sgn = -1 if val < 0 else 1
                r = round_up(abs(val), precision=1)
                return sgn * r
            except Exception:
                return raw

        self.calc["Z1_psf"] = get_psf(self.var_z1)
        self.calc["Z2_psf"] = get_psf(self.var_z2)
        self.calc["Z3_psf"] = get_psf(self.var_z3)

        # Nominal handling: always keep ultimate as report authority.
        if self.project.get("use_nominal"):
            v_nom = self.project.get("wind_speed_nominal")
            v_ult_from_nom = nominal_to_ult(v_nom) if v_nom else None
            # If ultimate field is empty but nominal given, fill ultimate by conversion.
            if (not self.project.get("wind_speed_ult")) and v_ult_from_nom:
                self.project["wind_speed_ult"] = f"{round(v_ult_from_nom, 1)}"
                self.var_ws_ult.set(self.project["wind_speed_ult"])
        # else: do nothing; ultimate is already set

        # Persist small state eagerly
        self._save_small_state()
        self.set_status("Computed & updated.")

    def on_save_project(self):
        self._update_project_from_ui()
        out = filedialog.asksaveasfilename(
            title="Save Project JSON",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=(self.project.get("name") or "project").replace(" ", "_")
        )
        if not out:
            return
        data = {"project": self.project, "calc": self.calc}
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.set_status(f"Saved project: {out}")
        except Exception as e:
            messagebox.showerror("Save Project", f"Failed to save.\n\n{e}")

    def on_load_project(self):
        path = filedialog.askopenfilename(
            title="Load Project JSON",
            filetypes=[("JSON", "*.json")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            proj = data.get("project", {})
            calc = data.get("calc", {})
            if proj:
                self.project = proj
            if calc:
                self.calc = calc

            # Re-sync UI
            self.var_name.set(self.project.get("name", ""))
            self.var_addr.set(self.project.get("address", ""))
            self.var_prep.set(self.project.get("prepared_by", "Jared Pearce, E.I.T."))
            self.var_panel.set(self.project.get("panel_type", PANEL_TYPES[0]))
            self.var_gauge.set(self.project.get("gauge", MATERIAL_GAUGES[1]))
            self.var_sub.set(self.project.get("substrate", SUBSTRATES[0]))
            self.var_risk.set(self.project.get("risk_category", "II"))
            self.var_exp.set(self.project.get("exposure", "B"))
            self.var_mrh.set(self.project.get("mean_roof_height_ft", "25"))
            self.var_hband.set(self.project.get("height_band", HEIGHT_BANDS[2]))
            self.var_ws_ult.set(self.project.get("wind_speed_ult", "169"))
            self.var_ws_nom.set(self.project.get("wind_speed_nominal", ""))
            self.var_use_nom.set(self.project.get("use_nominal", False))
            if self.var_use_nom.get():
                if str(self.entry_nominal) not in [str(w) for w in self.entry_nominal.master.children.values()]:
                    self.entry_nominal.pack(side="left", padx=6)
            else:
                try:
                    self.entry_nominal.pack_forget()
                except Exception:
                    pass

            self.var_z1.set(str(self.calc.get("Z1_psf", "")))
            self.var_z2.set(str(self.calc.get("Z2_psf", "")))
            self.var_z3.set(str(self.calc.get("Z3_psf", "")))

            self.var_fpa.set(self.project.get("fpa_path", ""))

            self._save_small_state()
            self.set_status(f"Loaded project: {path}")
        except Exception as e:
            messagebox.showerror("Load Project", f"Failed to load.\n\n{e}")
    def on_generate_pdf(self):
        self.on_compute()  # ensure latest values/state
        out = filedialog.asksaveasfilename(
            title="Export PDF Report",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile=(self.project.get("name") or "site_specific_report").replace(" ", "_")
        )
        if not out:
            return
        try:
            make_report_pdf(out, self.project, self.calc)
            self.set_status(f"PDF saved: {out}")
            messagebox.showinfo("Report", f"PDF saved:\n{out}")
        except Exception as e:
            messagebox.showerror("Report", f"Failed to generate PDF.\n\n{e}")

    def _save_small_state(self):
        # light persistence for convenience
        s = {
            "project_name":        self.project.get("name", ""),
            "project_address":     self.project.get("address", ""),
            "prepared_by":         self.project.get("prepared_by", "Jared Pearce, E.I.T."),
            "panel_type":          self.project.get("panel_type", PANEL_TYPES[0]),
            "gauge":               self.project.get("gauge", MATERIAL_GAUGES[1]),
            "substrate":           self.project.get("substrate", SUBSTRATES[0]),
            "risk_category":       self.project.get("risk_category", "II"),
            "exposure":            self.project.get("exposure", "B"),
            "mean_roof_height_ft": self.project.get("mean_roof_height_ft", "25"),
            "height_band":         self.project.get("height_band", HEIGHT_BANDS[2]),
            "wind_speed_ult":      self.project.get("wind_speed_ult", "169"),
            "wind_speed_nominal":  self.project.get("wind_speed_nominal", ""),
            "use_nominal":         self.project.get("use_nominal", False),
            "fpa_path":            self.project.get("fpa_path", ""),
        }
        save_state(s)

    def on_close(self):
        # save small state and exit
        try:
            self._update_project_from_ui()
            self._save_small_state()
        except Exception:
            pass
        self.destroy()

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
