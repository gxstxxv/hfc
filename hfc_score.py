#!/usr/bin/env python3
"""
Health Safety Corridor (HSC) Score
=====================================
Berechnet einen räumlichen Score aus:
  - Unfallorte 2024        → Accident Severity Index (ASI)
  - Verkehrsbelastung 2025 → Freight Load Index (FLI)
  - Lärmbelastung 2022     → Traffic Intensity Index (TII, als Lärmproxy)

Ausgabe:
  hsc_score_map.png   – statische Karte (Pitch)
  hsc_score_map.html  – interaktive Karte (Demo)
"""

import csv
import math
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib.collections import LineCollection
import folium
import contextily as ctx
from pyproj import Transformer

# ---------------------------------------------------------------------------
# Grid-Definition: Deutschland, 0.25° Auflösung
# ---------------------------------------------------------------------------
LON_MIN, LON_MAX, LON_STEP = 5.5, 15.5, 0.25
LAT_MIN, LAT_MAX, LAT_STEP = 47.0, 55.5, 0.25

lon_edges = np.arange(LON_MIN, LON_MAX + LON_STEP, LON_STEP)
lat_edges = np.arange(LAT_MIN, LAT_MAX + LAT_STEP, LAT_STEP)
N_LON = len(lon_edges) - 1  # 40 Spalten
N_LAT = len(lat_edges) - 1  # 34 Zeilen

lon_centers = (lon_edges[:-1] + lon_edges[1:]) / 2
lat_centers = (lat_edges[:-1] + lat_edges[1:]) / 2


def lon_bin(lon):
    """Gibt den Grid-Spaltenindex zurück (-1 wenn außerhalb)."""
    idx = int((lon - LON_MIN) / LON_STEP)
    if 0 <= idx < N_LON:
        return idx
    return -1


def lat_bin(lat):
    """Gibt den Grid-Zeilenindex zurück (-1 wenn außerhalb)."""
    idx = int((lat - LAT_MIN) / LAT_STEP)
    if 0 <= idx < N_LAT:
        return idx
    return -1


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def haversine_km(lon1, lat1, lon2, lat2):
    """Haversine-Distanz in km zwischen zwei WGS84-Punkten."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def parse_linestring_coords(wkt):
    """
    Parst ein WKT LINESTRING und gibt Liste von (lon, lat) zurück.
    Unterstützt WGS84 (kleine Werte) – ETRS89 wird NICHT hier transformiert.
    """
    wkt = wkt.strip()
    if wkt.upper().startswith("LINESTRING"):
        wkt = wkt[wkt.index("(") + 1: wkt.rindex(")")]
    coords = []
    for pair in wkt.split(","):
        parts = pair.strip().split()
        if len(parts) >= 2:
            try:
                coords.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return coords


def linestring_centroid(coords):
    """Mittelpunkt einer Linie (arithmetisches Mittel aller Stützpunkte)."""
    if not coords:
        return None, None
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def linestring_length_km(coords):
    """Gesamtlänge einer Linie in km (Summe der Segment-Haversine-Distanzen)."""
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total


def minmax_norm(arr, eps=1e-9):
    """Min-Max-Normalisierung auf [0, 1]."""
    mn, mx = arr.min(), arr.max()
    return (arr - mn) / (mx - mn + eps)


# ---------------------------------------------------------------------------
# 1. Unfallorte laden und ASI berechnen
# ---------------------------------------------------------------------------

W_KAT   = {1.0: 10.0, 2.0: 3.0, 3.0: 1.0}   # Unfallkategorie
W_LIGHT = {0.0: 1.0,  1.0: 1.3, 2.0: 1.6}    # Lichtverhältnisse
W_ROAD  = {0.0: 1.0,  1.0: 1.2, 2.0: 1.5}    # Straßenzustand


def load_accidents(path="data/unfallorte_2024.xlsx"):
    print(f"[ASI] Lade {path} …", flush=True)
    t0 = time.time()
    asi_grid = np.zeros((N_LAT, N_LON), dtype=np.float64)

    z = zipfile.ZipFile(path)
    ns = {"ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    # Shared Strings
    ss_xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    ss_root = ET.fromstring(ss_xml)
    shared = [
        (si.find(".//ns:t", ns).text if si.find(".//ns:t", ns) is not None else "")
        for si in ss_root.findall("ns:si", ns)
    ]

    sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    s_root = ET.fromstring(sheet_xml)
    rows = s_root.findall(".//ns:row", ns)

    def cell_val(cell):
        t = cell.get("t", "")
        v = cell.find("ns:v", ns)
        if v is None:
            return None
        if t == "s":
            return shared[int(v.text)]
        return v.text

    n_ok = n_skip = 0
    for row in rows[1:]:  # Zeile 0 = Header
        cells = row.findall("ns:c", ns)
        if len(cells) < 6:
            n_skip += 1
            continue
        vals = [cell_val(c) for c in cells]
        try:
            kat   = float(vals[1])
            light = float(vals[2])
            road  = float(vals[3])
            # Komma als Dezimaltrenner
            x = float(str(vals[4]).replace(",", "."))
            y = float(str(vals[5]).replace(",", "."))
        except (TypeError, ValueError):
            n_skip += 1
            continue

        wi = W_KAT.get(kat, 1.0) * W_LIGHT.get(light, 1.0) * W_ROAD.get(road, 1.0)
        ci, ri = lon_bin(x), lat_bin(y)
        if ci >= 0 and ri >= 0:
            asi_grid[ri, ci] += wi
            n_ok += 1
        else:
            n_skip += 1

    print(f"[ASI] {n_ok:,} Unfälle eingeordnet, {n_skip:,} übersprungen — {time.time()-t0:.1f}s", flush=True)
    return asi_grid


# ---------------------------------------------------------------------------
# 2. Verkehrsbelastung (Toll Collect) laden und FLI berechnen
# ---------------------------------------------------------------------------

def load_traffic(path="data/verkehrsbelastung_2025-04-12.xlsx"):
    print(f"[FLI] Lade {path} …", flush=True)
    t0 = time.time()
    fli_grid = np.zeros((N_LAT, N_LON), dtype=np.float64)

    z = zipfile.ZipFile(path)
    ns = {"ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    ss_xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    ss_root = ET.fromstring(ss_xml)
    shared = [
        (si.find(".//ns:t", ns).text if si.find(".//ns:t", ns) is not None else "")
        for si in ss_root.findall("ns:si", ns)
    ]

    sheet_xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    s_root = ET.fromstring(sheet_xml)
    rows = s_root.findall(".//ns:row", ns)

    # Header-Indizes ermitteln
    header_cells = rows[0].findall("ns:c", ns)
    headers = [cell_val_simple(c, shared, ns) for c in header_cells]
    idx = {h: i for i, h in enumerate(headers)}

    befahrungen_i = idx.get("anzahl_befahrungen", 6)
    wkt_i         = idx.get("wkt", 8)
    typ_i         = idx.get("strassen_typ", 7)
    name_i        = idx.get("bundesfernstrasse", 5)
    von_i         = idx.get("mautknoten_id_von", 1)
    nach_i        = idx.get("mautknoten_id_nach", 3)

    def cell_val_s(cells, i):
        if i >= len(cells):
            return None
        c = cells[i]
        t = c.get("t", "")
        v = c.find("ns:v", ns)
        if v is None:
            return None
        if t == "s":
            return shared[int(v.text)]
        return v.text

    n_ok = n_skip = 0
    from collections import defaultdict
    # {road_name: [(von_id, nach_id, coords), ...]}  — für Merge
    auto_segs = defaultdict(list)
    bund_segs = defaultdict(list)

    for row in rows[1:]:
        cells = row.findall("ns:c", ns)
        try:
            befahrungen = float(cell_val_s(cells, befahrungen_i) or 0)
            wkt = cell_val_s(cells, wkt_i)
            if not wkt:
                n_skip += 1
                continue
            coords = parse_linestring_coords(wkt)
            if len(coords) < 2:
                n_skip += 1
                continue
            length_km = linestring_length_km(coords)
            clon, clat = linestring_centroid(coords)
            ci, ri = lon_bin(clon), lat_bin(clat)
            if ci >= 0 and ri >= 0:
                fli_grid[ri, ci] += befahrungen * length_km
                n_ok += 1
            else:
                n_skip += 1
            # Geometrie für Straßennetz (nach Straßenname gruppiert für späteres Mergen)
            try:
                typ = float(cell_val_s(cells, typ_i) or 1)
            except (TypeError, ValueError):
                typ = 1.0
            road_name = cell_val_s(cells, name_i) or "unbekannt"
            von_id    = cell_val_s(cells, von_i)  or ""
            nach_id   = cell_val_s(cells, nach_i) or ""
            if typ == 0.0:
                auto_segs[road_name].append((von_id, nach_id, coords))
            else:
                bund_segs[road_name].append((von_id, nach_id, coords))
        except Exception:
            n_skip += 1

    def _merge_segments(segs_by_road):
        """Verkette 2-Punkt-Segmente derselben Straße zu langen Polylinien.

        Algorithmus: Aufbau einer Adjazenz-Map (von_id → (nach_id, coords)).
        Dann von jedem noch nicht besuchten Startknoten aus die Kette ablaufen.
        """
        chains = []
        for _, segs in segs_by_road.items():
            from_map = defaultdict(list)
            for von, nach, coords in segs:
                from_map[von].append((nach, coords))
            visited: set = set()
            for start in list(from_map.keys()):
                if start in visited:
                    continue
                chain: list | None = None
                cur = start
                while cur in from_map and cur not in visited:
                    visited.add(cur)
                    nach, coords = from_map[cur][0]
                    chain = list(coords) if chain is None else chain + list(coords[1:])
                    cur = nach
                if chain and len(chain) >= 2:
                    chains.append(chain)
        return chains

    roads_autobahn = _merge_segments(auto_segs)
    roads_bundesstr = _merge_segments(bund_segs)

    print(f"[FLI] {n_ok:,} Segmente eingeordnet, {n_skip:,} übersprungen — {time.time()-t0:.1f}s", flush=True)
    print(f"[FLI] Straßennetz: {len(roads_autobahn):,} Autobahn-Ketten, {len(roads_bundesstr):,} Bundesstraßen-Ketten", flush=True)
    return fli_grid, roads_autobahn, roads_bundesstr


def cell_val_simple(cell, shared, ns):
    t = cell.get("t", "")
    v = cell.find("ns:v", ns)
    if v is None:
        return ""
    if t == "s":
        return shared[int(v.text)]
    return v.text or ""


# ---------------------------------------------------------------------------
# 3. Lärmbelastung (EU END / INSPIRE) laden und TII berechnen
# ---------------------------------------------------------------------------

def load_noise(path="data/laermbelastung_2022.csv"):
    """
    WICHTIG: Geometrie liegt in ETRS89-LAEA (EPSG:3035) vor!
    Transformation → WGS84 (EPSG:4326) via pyproj.
    """
    print(f"[TII] Lade {path} …", flush=True)
    t0 = time.time()
    tii_grid = np.zeros((N_LAT, N_LON), dtype=np.float64)

    transformer = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)

    def etrs_centroid_to_wgs84(wkt_3035):
        """Extrahiert Centroid aus ETRS89-Linestring und transformiert nach WGS84."""
        try:
            coords_3035 = parse_linestring_coords(wkt_3035)
            if not coords_3035:
                return None, None
            cx = sum(c[0] for c in coords_3035) / len(coords_3035)
            cy = sum(c[1] for c in coords_3035) / len(coords_3035)
            lon_wgs, lat_wgs = transformer.transform(cx, cy)
            return lon_wgs, lat_wgs
        except Exception:
            return None, None

    n_ok = n_skip = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i % 50000 == 0 and i > 0:
                print(f"  [TII] {i:,} Zeilen verarbeitet …", flush=True)
            try:
                flow = float(row["annualTrafficFlow"])
                length_m = float(row["length"])
                length_km = length_m / 1000.0
                wkt = row["centrelineGeometry"]
                lon, lat = etrs_centroid_to_wgs84(wkt)
                if lon is None:
                    n_skip += 1
                    continue
                ci, ri = lon_bin(lon), lat_bin(lat)
                if ci >= 0 and ri >= 0:
                    # Tagesäquivalent (annualTrafficFlow / 365)
                    tii_grid[ri, ci] += (flow / 365.0) * length_km
                    n_ok += 1
                else:
                    n_skip += 1
            except (ValueError, KeyError):
                n_skip += 1

    print(f"[TII] {n_ok:,} Segmente eingeordnet, {n_skip:,} übersprungen — {time.time()-t0:.1f}s", flush=True)
    return tii_grid


# ---------------------------------------------------------------------------
# 4. Score berechnen
# ---------------------------------------------------------------------------

def compute_hfc(asi, fli, tii, alpha=0.5, beta=0.3, gamma=0.2):
    print("[HSC] Normalisiere und berechne Score …", flush=True)
    asi_n = minmax_norm(asi)
    fli_n = minmax_norm(fli)
    tii_n = minmax_norm(tii)
    hfc = alpha * asi_n + beta * fli_n + gamma * tii_n
    return hfc, asi_n, fli_n, tii_n


# ---------------------------------------------------------------------------
# 5. Statische 4-Panel-Karte (matplotlib)
# ---------------------------------------------------------------------------

def plot_panels(hfc, asi_n, fli_n, tii_n, roads_autobahn=None, roads_bundesstr=None):
    """Erstellt eine 4-Panel PNG mit je einem Score pro Panel, überlappungsfrei."""
    print("[VIZ] Erstelle statische 4-Panel-Karte → hsc_score_map.png …", flush=True)

    # Straßennetz als LineCollection vorbereiten (schneller als viele plot()-Aufrufe)
    def _make_lc(road_list, color, lw, alpha, zorder):
        """Baut eine LineCollection aus einer Liste von Koordinaten-Listen."""
        segments = [np.array(coords) for coords in road_list if len(coords) >= 2]
        lc = LineCollection(segments, colors=color, linewidths=lw,
                            alpha=alpha, zorder=zorder)
        return lc

    lc_bundesstr_kwargs = dict(color="#888888", lw=0.35, alpha=0.55, zorder=3)
    lc_autobahn_kwargs  = dict(color="#003399", lw=0.8,  alpha=0.75, zorder=4)

    datasets = [
        (hfc,   "HSC Score (Gesamt)",          "RdYlGn_r", True),
        (asi_n, "ASI — Unfallindex",           "Reds",     False),
        (fli_n, "FLI — LKW-Befahrungen",      "Blues",    False),
        (tii_n, "TII — Lärmbelastung/Verkehr", "Purples",  False),
    ]

    # GridSpec mit explizitem vertikalem Abstand (hspace) verhindert Überlappung
    fig = plt.figure(figsize=(18, 16), facecolor="#f5f5f5")
    gs = fig.add_gridspec(
        2, 2,
        hspace=0.35,   # vertikaler Abstand zwischen den Zeilen
        wspace=0.22,   # horizontaler Abstand zwischen den Spalten
        left=0.06, right=0.96,
        top=0.91, bottom=0.06,
    )

    for idx, (data, title, cmap, is_main) in enumerate(datasets):
        ax = fig.add_subplot(gs[idx // 2, idx % 2])

        masked = np.ma.masked_where(data == 0, data)

        pcm = ax.pcolormesh(
            lon_edges, lat_edges, masked,
            cmap=cmap,
            vmin=0, vmax=1,
            shading="flat",
            alpha=0.85,
        )

        ax.set_xlim(5.8, 15.1)
        ax.set_ylim(47.2, 55.1)
        ax.set_aspect("equal")
        ax.set_facecolor("#d0e8f0")

        # Straßennetz einzeichnen (LineCollection — sehr performant)
        if roads_bundesstr:
            ax.add_collection(_make_lc(roads_bundesstr, **lc_bundesstr_kwargs))
        if roads_autobahn:
            ax.add_collection(_make_lc(roads_autobahn, **lc_autobahn_kwargs))

        cbar = plt.colorbar(pcm, ax=ax, shrink=0.8, pad=0.02)
        cbar.set_label("Score [0–1]", fontsize=9)
        cbar.ax.tick_params(labelsize=8)

        # Titel mit genug pad, damit er nicht mit Colorbar/Nachbar-Achse kollidiert
        ax.set_title(title,
                     fontsize=12, fontweight="bold" if is_main else "normal",
                     pad=10)
        ax.set_xlabel("Längengrad", fontsize=8, labelpad=6)
        ax.set_ylabel("Breitengrad", fontsize=8, labelpad=6)
        ax.tick_params(labelsize=7)
        ax.grid(True, linewidth=0.3, alpha=0.4, color="white")

        # Top-5 Sterne (referenziert auf HFC-Gesamt-Rang in allen Panels)
        flat_idx = np.argsort(hfc.ravel())[::-1]
        for rank, fi in enumerate(flat_idx[:5]):
            ri, ci = np.unravel_index(fi, hfc.shape)
            cx, cy = lon_centers[ci], lat_centers[ri]
            ax.plot(cx, cy, "k*", markersize=9 if rank == 0 else 6, zorder=6)
            ax.annotate(
                f"#{rank + 1}", (cx, cy),
                textcoords="offset points", xytext=(5, 5),
                fontsize=7, fontweight="bold", color="black", zorder=7,
                bbox=dict(boxstyle="round,pad=0.15", fc="white", alpha=0.6, ec="none"),
            )

        # Straßennetz-Legende (nur wenn Daten vorhanden)
        if roads_autobahn or roads_bundesstr:
            legend_lines = []
            legend_labels = []
            if roads_bundesstr:
                legend_lines.append(plt.Line2D([0], [0], color="#888888", lw=1.2, alpha=0.7))
                legend_labels.append("Bundesstraße")
            if roads_autobahn:
                legend_lines.append(plt.Line2D([0], [0], color="#003399", lw=1.8, alpha=0.85))
                legend_labels.append("Autobahn")
            ax.legend(legend_lines, legend_labels,
                      loc="lower left", fontsize=6.5,
                      framealpha=0.75, edgecolor="#aaa",
                      handlelength=1.8, handletextpad=0.5)

    fig.suptitle(
        "Health Safety Corridor (HSC) Score — Deutschland\n"
        "Priorisierungskarte für Straßeninfrastruktur-Investitionen",
        fontsize=15, fontweight="bold", y=0.97,
    )

    fig.text(
        0.5, 0.015,
        "Quellen: Unfallorte 2024 (BASt/Destatis) | Mautdaten 2025 (Toll Collect/BALM) | "
        "EU-END Verkehrsfluss 2021 (INSPIRE)\n"
        "HSC = 0.5×ASI + 0.3×FLI + 0.2×TII  ·  Zeitlicher Versatz bewusst vernachlässigt (Proof of Concept)",
        ha="center", fontsize=7.5, color="#555", style="italic",
    )

    plt.savefig("hsc_score_map.png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close()
    print("[VIZ] Gespeichert: hsc_score_map.png", flush=True)


# ---------------------------------------------------------------------------
# 6. Interaktive Folium-Karte
# ---------------------------------------------------------------------------

def plot_interactive(hfc, asi_n, fli_n, tii_n, roads_autobahn=None, roads_bundesstr=None, out="hsc_score_map.html"):
    print(f"[VIZ] Erstelle interaktive Karte → {out} …", flush=True)

    m = folium.Map(
        location=[51.2, 10.4],
        zoom_start=6,
        tiles=None,          # Basemap manuell hinzufügen, damit control=False greift
        control_scale=True,
    )
    # Basemap ohne Eintrag im LayerControl (control=False)
    folium.TileLayer("CartoDB positron", control=False).add_to(m)

    # ------------------------------------------------------------------
    # Straßennetz-Layer (GeoJSON FeatureCollection je Typ)
    # ------------------------------------------------------------------
    def _roads_to_geojson(road_list):
        """Wandelt eine Liste von Koordinaten-Ketten in eine GeoJSON FeatureCollection."""
        features = []
        for coords in road_list:
            if len(coords) < 2:
                continue
            # 3 Dezimalstellen ≈ 69 m Genauigkeit — für Kartenansicht ausreichend
            line_coords = [[round(lon, 3), round(lat, 3)] for lon, lat in coords]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line_coords},
                "properties": {},
            })
        return {"type": "FeatureCollection", "features": features}

    if roads_autobahn or roads_bundesstr:
        roads_fg = folium.FeatureGroup(name="Straßennetz", show=False)
        if roads_autobahn:
            print(f"  [VIZ] Baue Autobahn-GeoJSON ({len(roads_autobahn):,} Ketten) …", flush=True)
            folium.GeoJson(
                _roads_to_geojson(roads_autobahn),
                style_function=lambda _: {
                    "color": "#003399",
                    "weight": 1.8,
                    "opacity": 0.8,
                },
            ).add_to(roads_fg)
        if roads_bundesstr:
            print(f"  [VIZ] Baue Bundesstraßen-GeoJSON ({len(roads_bundesstr):,} Ketten) …", flush=True)
            folium.GeoJson(
                _roads_to_geojson(roads_bundesstr),
                style_function=lambda _: {
                    "color": "#888888",
                    "weight": 0.8,
                    "opacity": 0.6,
                },
            ).add_to(roads_fg)
        roads_fg.add_to(m)

    # Farbpalette
    def score_color(v):
        """RdYlGn_r: 0=grün, 1=rot."""
        cmap = plt.get_cmap("RdYlGn_r")
        r, g, b, _ = cmap(float(v))
        return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"

    # HSC-Layer
    hfc_layer = folium.FeatureGroup(name="HSC Score", show=True)
    asi_layer = folium.FeatureGroup(name="ASI (Unfallindex)", show=False)
    fli_layer = folium.FeatureGroup(name="FLI (LKW-Befahrungen)", show=False)
    tii_layer = folium.FeatureGroup(name="<i>TII (Lärmbelastung/Verkehrsintensität)</i>", show=False)

    layer_map = [
        (hfc_layer, hfc,   "HSC", plt.get_cmap("RdYlGn_r")),
        (asi_layer, asi_n, "ASI", plt.get_cmap("Reds")),
        (fli_layer, fli_n, "FLI", plt.get_cmap("Blues")),
        (tii_layer, tii_n, "TII", plt.get_cmap("Purples")),
    ]

    for layer, data, label, cmap in layer_map:
        # Eine einzige GeoJSON-FeatureCollection pro Score-Layer ist deutlich
        # kompakter als ein folium.Rectangle()-Objekt pro Zelle (~9× kleiner).
        features = []
        for ri in range(N_LAT):
            for ci in range(N_LON):
                v = data[ri, ci]
                if v < 0.01:
                    continue
                r, g, b, _ = cmap(float(v))
                color = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
                lon0 = lon_edges[ci]
                lat0 = lat_edges[ri]
                lon1 = lon_edges[ci + 1]
                lat1 = lat_edges[ri + 1]
                popup_html = (
                    f'<div class="hcp">'
                    f'<b class="hcp-title">{label} Score: {v:.3f}</b>'
                    f'<span class="hcp-loc">Lon {lon0:.2f}–{lon1:.2f} | Lat {lat0:.2f}–{lat1:.2f}</span>'
                    f'<hr>'
                    f'Accident Severity Index (<b>ASI</b>) = {asi_n[ri,ci]:.3f}<br>'
                    f'Freight Load Index (<b>FLI</b>) = {fli_n[ri,ci]:.3f}<br>'
                    f'Traffic Intensity Index (<b>TII</b>) = {tii_n[ri,ci]:.3f}'
                    f'</div>'
                )
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[lon0, lat0], [lon1, lat0],
                                         [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
                    },
                    "properties": {
                        "c":  color,
                        "fo": round(min(0.75, v * 0.9 + 0.1), 4),
                        "ph": popup_html,
                        "tt": f"{label}: {v:.3f}",
                    },
                })
        folium.GeoJson(
            {"type": "FeatureCollection", "features": features},
            style_function=lambda f: {
                "color":       f["properties"]["c"],
                "fillColor":   f["properties"]["c"],
                "fillOpacity": f["properties"]["fo"],
                "weight":      0.5,
                "opacity":     0.3,
            },
            tooltip=folium.GeoJsonTooltip(fields=["tt"], aliases=[""], labels=False),
            popup=folium.GeoJsonPopup(fields=["ph"], aliases=[""], labels=False,
                                      max_width=300),
        ).add_to(layer)

    for layer, _, _, _ in layer_map:
        layer.add_to(m)

    # Top-10 Marker
    top_layer = folium.FeatureGroup(name="Top-10 Hotspots", show=True)
    flat_sorted = np.argsort(hfc.ravel())[::-1]
    for rank, idx in enumerate(flat_sorted[:10]):
        ri, ci = np.unravel_index(idx, hfc.shape)
        cx, cy = lon_centers[ci], lat_centers[ri]
        popup_html = (
            f"<b>Rang #{rank+1}</b><br>"
            f"HSC Score: {hfc[ri,ci]:.3f}<br>"
            f"ASI: {asi_n[ri,ci]:.3f} | FLI: {fli_n[ri,ci]:.3f} | TII: {tii_n[ri,ci]:.3f}<br>"
            f"Lon {lon_edges[ci]:.2f}–{lon_edges[ci+1]:.2f}, "
            f"Lat {lat_edges[ri]:.2f}–{lat_edges[ri+1]:.2f}"
        )
        folium.Marker(
            location=[cy, cx],
            popup=folium.Popup(popup_html, max_width=220),
            tooltip=f"#{rank+1} HSC={hfc[ri,ci]:.3f}",
            icon=folium.Icon(color="red" if rank == 0 else "orange", icon="star", prefix="fa"),
        ).add_to(top_layer)
    top_layer.add_to(m)

    # ------------------------------------------------------------------
    # Korridor-Filter (Safety / Health-Noise / Multi-Stress)
    # Schwellenwert: normalisierter Index >= 0.3 (erhöhte Belastung)
    # ------------------------------------------------------------------
    CORRIDOR_THRESHOLD = 0.3

    def _build_corridor_layer(name, color, condition_fn, show=False):
        """Erzeugt einen FeatureGroup-Layer mit Zellen, die der Bedingung entsprechen."""
        layer = folium.FeatureGroup(name=name, show=show)
        features = []
        for ri in range(N_LAT):
            for ci in range(N_LON):
                if not condition_fn(ri, ci):
                    continue
                lon0, lat0 = lon_edges[ci], lat_edges[ri]
                lon1, lat1 = lon_edges[ci + 1], lat_edges[ri + 1]
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[lon0, lat0], [lon1, lat0],
                                         [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
                    },
                    "properties": {
                        "tt": (f"ASI={asi_n[ri,ci]:.2f} | "
                               f"FLI={fli_n[ri,ci]:.2f} | "
                               f"TII={tii_n[ri,ci]:.2f}"),
                    },
                })
        if features:
            folium.GeoJson(
                {"type": "FeatureCollection", "features": features},
                style_function=lambda f, c=color: {
                    "color": c,
                    "fillColor": c,
                    "fillOpacity": 0.35,
                    "weight": 2.0,
                    "opacity": 0.9,
                },
                tooltip=folium.GeoJsonTooltip(fields=["tt"], aliases=[""], labels=False),
            ).add_to(layer)
        layer.add_to(m)

    T = CORRIDOR_THRESHOLD
    _build_corridor_layer(
        name="Safety-Corridor",
        color="#ff7700",
        condition_fn=lambda ri, ci: (asi_n[ri, ci] >= T and fli_n[ri, ci] >= T),
    )
    _build_corridor_layer(
        name="Health-Noise-Corridor",
        color="#008B8B",
        condition_fn=lambda ri, ci: (fli_n[ri, ci] >= T and tii_n[ri, ci] >= T),
    )
    _build_corridor_layer(
        name="Multi-Stress-Corridor",
        color="#CC0000",
        condition_fn=lambda ri, ci: (
            asi_n[ri, ci] >= T and fli_n[ri, ci] >= T and tii_n[ri, ci] >= T
        ),
    )

    # ------------------------------------------------------------------
    # Deutschland-Grenze (immer sichtbar, kein LayerControl-Eintrag)
    # ------------------------------------------------------------------
    import urllib.request, json as _json, os as _os
    _border_cache = "data/germany_outline.geojson"
    _border_url   = ("https://raw.githubusercontent.com/isellsoap/"
                     "deutschlandGeoJSON/main/1_deutschland/4_niedrig.geo.json")
    _germany_geojson = None
    if _os.path.exists(_border_cache):
        with open(_border_cache) as _f:
            _germany_geojson = _json.load(_f)
    else:
        try:
            print("  [VIZ] Lade Deutschland-Grenze …", flush=True)
            with urllib.request.urlopen(_border_url, timeout=10) as _r:
                _germany_geojson = _json.loads(_r.read().decode())
            with open(_border_cache, "w") as _f:
                _json.dump(_germany_geojson, _f)
        except Exception as _e:
            print(f"  [VIZ] Grenze konnte nicht geladen werden: {_e}", flush=True)

    if _germany_geojson:
        folium.GeoJson(
            _germany_geojson,
            name="Deutschland-Grenze",
            control=False,
            interactive=False,   # Klicks/Popups auf darunter liegende Layer nicht blockieren
            style_function=lambda _: {
                "color": "#999999",
                "weight": 2.5,
                "fillOpacity": 0,
                "opacity": 0.9,
            },
        ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    # Titel-Box + Mobile-CSS für LayerControl + foliumpopup-Tabelle neutralisieren
    title_html = """
    <style>
    /* ---- Titel-Bubble ---- */
    #hsc-title {
        position: fixed;
        top: 10px;
        left: 50%;
        transform: translateX(-50%);
        background: rgba(255,255,255,0.92);
        padding: 10px 20px;
        border: 2px solid #333;
        border-radius: 8px;
        z-index: 9999;
        font-family: Arial, sans-serif;
        text-align: center;
        max-width: 500px;
        box-sizing: border-box;
    }
    @media (max-width: 768px) {
        #hsc-title {
            padding: 5px 10px;
            max-width: calc(100vw - 20px);
        }
        #hsc-title b  { font-size: 10px !important; }
        #hsc-title span { font-size: 9px !important; }
    }

    /* ---- LayerControl auf Mobilgeräten: unten rechts, kompakt ---- */
    @media (max-width: 768px) {
        .leaflet-control-layers {
            position: fixed !important;
            bottom: 20px !important;
            right: 10px !important;
            top: auto !important;
            max-height: none !important;
            overflow: visible !important;
        }
        /* Leaflet's expand() setzt style.height — per JS (setTimeout) nachträglich entfernt */
        .leaflet-control-layers-list {
            height: auto !important;
            max-height: none !important;
            overflow: visible !important;
        }
        .leaflet-control-layers-overlays label,
        .leaflet-control-layers-base label {
            font-size: 10px !important;
            padding: 1px 0 !important;
            margin: 0 !important;
            line-height: 1.3 !important;
            display: flex !important;
            align-items: center !important;
        }
        .leaflet-control-layers-overlays label span,
        .leaflet-control-layers-base label span {
            margin-left: 3px !important;
        }
        .leaflet-control-layers-overlays input,
        .leaflet-control-layers-base input {
            margin: 0 !important;
            width: 12px !important;
            height: 12px !important;
            flex-shrink: 0 !important;
        }
        .leaflet-control-layers-separator { margin: 2px 0 !important; }
        .leaflet-control-layers {
            padding: 4px 8px !important;
        }
        #hsc-sep-1, #hsc-sep-2 { margin: 3px 2px !important; }
    }

    /* Utility-Klasse zum Verstecken des Buttons (!important schlägt jede normale Deklaration) */
    .hsc-btn-hidden { display: none !important; }

    /* ---- Legende: auf Mobile standardmäßig ausgeblendet ---- */
    @media (max-width: 768px) {
        #hfc-legend { display: none; }
        #hsc-leg-btn { display: flex; }   /* KEIN !important → .hsc-btn-hidden (important) schlägt dies */
        #hsc-title-sources { display: none; }
    }
    @media (min-width: 769px) {
        #hsc-leg-btn { display: none !important; }
    }

    /* ---- "i"-Button (Legende öffnen, nur Mobile) ---- */
    #hsc-leg-btn {
        position: fixed;
        bottom: 40px;
        left: 10px;
        z-index: 10000;
        width: 32px;
        height: 32px;
        background: #fff;
        border: 2px solid rgba(0,0,0,0.2);
        border-radius: 5px;
        cursor: pointer;
        align-items: center;
        justify-content: center;
        font-size: 16px;
        font-weight: bold;
        color: #444;
        font-family: Arial, sans-serif;
        box-shadow: none;
    }

    /* ---- Schließen-Button innerhalb der Legende ---- */
    #hsc-leg-close {
        display: none;
        position: absolute;
        top: 5px;
        right: 8px;
        cursor: pointer;
        font-size: 14px;
        color: #888;
        line-height: 1;
        font-weight: bold;
        background: none;
        border: none;
        padding: 0;
    }
    @media (max-width: 768px) {
        #hsc-leg-close { display: block !important; }
    }

    /* GeoJsonPopup rendert Inhalte in einer Tabelle — diese unsichtbar machen. */
    .foliumpopup table { border-collapse: collapse; width: 100%; }
    .foliumpopup td    { padding: 0; border: none; }
    /* Score-Zellen Popup-Stil */
    .hcp               { font-size: 13px; line-height: 1.6; min-width: 220px; }
    .hcp-title         { font-size: 14px; display: block; margin-bottom: 0; }
    .hcp-loc           { display: block; font-size: 11px; color: #666; margin-top: 1px; }
    .hcp hr            { margin: 5px 0; border: none; border-top: 1px solid #ddd; }
    </style>

    <div id="hsc-title">
        <b style="font-size:14px;">Health Safety Corridor (HSC) Score</b><br>
        <span style="font-size:11px; color:#555;">Priorisierungskarte für Straßeninfrastruktur · Proof of Concept</span><br>
        <span id="hsc-title-sources" style="font-size:10px; color:#777;">Quellen: BASt Unfallorte 2024 | Toll Collect 2025 | EU-END 2021</span>
    </div>

    <!-- "i"-Button zum Einblenden der Legende (nur Mobile) -->
    <button id="hsc-leg-btn" title="Legende einblenden">ℹ</button>

    <script>
    (function() {
        function init() {
            var btn    = document.getElementById('hsc-leg-btn');
            var legend = document.getElementById('hfc-legend');
            var close  = document.getElementById('hsc-leg-close');

            if (!btn || !legend) return;  // safety check

            function getLayerControl() {
                return document.querySelector('.leaflet-control-layers');
            }

            function showLegend() {
                legend.style.display = 'block';
                btn.classList.add('hsc-btn-hidden');   // CSS-Klasse schlägt display:flex !important
                var lc = getLayerControl();
                if (lc) lc.style.display = 'none';
            }

            function hideLegend() {
                legend.style.display = '';
                btn.classList.remove('hsc-btn-hidden'); // Klasse weg → display:flex !important greift
                var lc = getLayerControl();
                if (lc) lc.style.display = '';
            }

            btn.addEventListener('click', showLegend);
            if (close) close.addEventListener('click', hideLegend);

            // Reset on resize (e.g. rotating device)
            window.addEventListener('resize', function() {
                if (window.innerWidth > 768) {
                    legend.style.display = '';
                    btn.classList.remove('hsc-btn-hidden');
                    var lc = getLayerControl();
                    if (lc) lc.style.display = '';
                }
            });
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
        } else {
            init();
        }
    })();

    // Leaflet's expand() setzt beim Init einmalig style.height auf der Layer-Liste.
    // Per setTimeout(100) entfernen — läuft nach allen synchronen Leaflet-Init-Skripten.
    document.addEventListener('DOMContentLoaded', function() {
        setTimeout(function() {
            var list = document.querySelector('.leaflet-control-layers-list');
            if (list) list.style.height = '';
            document.querySelectorAll('.leaflet-control-layers-scrollbar').forEach(function(el) {
                el.classList.remove('leaflet-control-layers-scrollbar');
            });
        }, 100);
    });
    </script>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    # Trennlinie im LayerControl nach dem Straßennetz-Eintrag (Index 0)
    # Overlay-Reihenfolge: Straßennetz | HSC Score | ASI | FLI | TII | Top-10 | [Safety/HN/MS]
    separator_js = """
    <script>
    (function insertLayerSeparators() {
        var HR_STYLE = 'margin:6px 2px; border:none; border-top:1px solid #bbb;';
        function doInsert() {
            var labels = document.querySelectorAll(
                '.leaflet-control-layers-overlays label'
            );
            // Erwarte mind. 9 Einträge (Straßennetz + 5 Score/Hotspot + 3 Korridor)
            if (labels.length < 6) {
                setTimeout(doInsert, 100);
                return;
            }
            // Trennlinie 1: nach Straßennetz (vor erstem Score-Layer, Index 1)
            if (!document.getElementById('hsc-sep-1')) {
                var hr1 = document.createElement('hr');
                hr1.id = 'hsc-sep-1';
                hr1.style.cssText = HR_STYLE;
                labels[1].parentNode.insertBefore(hr1, labels[1]);
            }
            // Trennlinie 2: vor Safety-Corridor (6. Label nach Straßennetz = Index 6)
            if (labels.length >= 7 && !document.getElementById('hsc-sep-2')) {
                var hr2 = document.createElement('hr');
                hr2.id = 'hsc-sep-2';
                hr2.style.cssText = HR_STYLE;
                labels[6].parentNode.insertBefore(hr2, labels[6]);
            }
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', doInsert);
        } else {
            doInsert();
        }
    })();
    </script>
    """
    m.get_root().html.add_child(folium.Element(separator_js))

    # ------------------------------------------------------------------
    # Legende (unten links, fest – auch auf Mobilgeräten)
    # ------------------------------------------------------------------
    legend_html = """
    <div id="hfc-legend" style="
        position: fixed;
        bottom: 40px;
        left: 10px;
        z-index: 9999;
        background: #fff;
        border-radius: 5px;
        padding: 8px 12px 10px 12px;
        box-shadow: none;
        border: 2px solid rgba(0,0,0,0.2);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 13px;
        min-width: 200px;
        max-width: 230px;
        max-height: 80vh;
        overflow-y: auto;
    ">
        <button id="hsc-leg-close" title="Legende ausblenden">✕</button>
        <div style="font-weight:bold; margin-bottom:6px;">Legende</div>

        <!-- HSC Gesamt-Score -->
        <div data-leg-section="leg-hsc">
        <div style="font-size:11px; color:#555; margin-bottom:3px;">HSC Korridor-Score</div>
        <div style="width:100%; height:12px; border-radius:2px;
                    background:linear-gradient(to right,#1a9641,#ffffbf,#d7191c);
                    margin-bottom:2px;"></div>
        <div style="display:flex; justify-content:space-between; font-size:10px; color:#555; margin-bottom:8px;">
            <span>0 – kein Bedarf</span><span>1 – kritisch</span>
        </div>
        </div>

        <!-- ASI -->
        <div data-leg-section="leg-asi">
        <div style="font-size:11px; color:#555; margin-bottom:2px;">
            <b>ASI</b> — Accident Severity Index
        </div>
        <div style="font-size:9.5px; color:#777; margin-bottom:3px; line-height:1.3;">
            Gewichtete Unfallschwere — tödliche Unfälle bei Nacht auf nasser Fahrbahn zählen deutlich mehr als kleine Sachschäden.
        </div>
        <div style="width:100%; height:10px; border-radius:2px;
                    background:linear-gradient(to right,#fff5f0,#cb181d);
                    margin-bottom:2px;"></div>
        <div style="display:flex; justify-content:space-between; font-size:9.5px; color:#555; margin-bottom:8px;">
            <span>0 = keine schweren Unfälle</span>
            <span>1 = max. Schwere</span>
        </div>
        </div>

        <!-- FLI -->
        <div data-leg-section="leg-fli">
        <div style="font-size:11px; color:#555; margin-bottom:2px;">
            <b>FLI</b> — Freight Load Index
        </div>
        <div style="font-size:9.5px; color:#777; margin-bottom:3px; line-height:1.3;">
            LKW-Befahrungen × Streckenlänge → Infrastrukturverschleiß (Tonnen-km-Druck auf den Asphalt).
        </div>
        <div style="width:100%; height:10px; border-radius:2px;
                    background:linear-gradient(to right,#f7fbff,#08306b);
                    margin-bottom:2px;"></div>
        <div style="display:flex; justify-content:space-between; font-size:9.5px; color:#555; margin-bottom:8px;">
            <span>0 = keine LKW-Ströme</span>
            <span>1 = max. Verschleiß</span>
        </div>
        </div>

        <!-- TII -->
        <div data-leg-section="leg-tii">
        <div style="font-size:11px; color:#555; margin-bottom:2px;">
            <b>TII</b> — Traffic Intensity Index
        </div>
        <div style="font-size:9.5px; color:#777; margin-bottom:3px; line-height:1.3;">
            Täglicher Gesamtverkehrsdurchsatz aller Abschnitte einer Zelle — der Puls einer Straßenregion.
        </div>
        <div style="width:100%; height:10px; border-radius:2px;
                    background:linear-gradient(to right,#fcfbfd,#3f007d);
                    margin-bottom:2px;"></div>
        <div style="display:flex; justify-content:space-between; font-size:9.5px; color:#555; margin-bottom:8px;">
            <span>0 = kaum Verkehr</span>
            <span>1 = max. Intensität</span>
        </div>
        </div>

        <!-- Hotspot-Marker -->
        <div data-leg-section="leg-hotspot">
        <div style="font-size:11px; color:#555; margin-bottom:4px;">Hotspot-Marker</div>
        <div style="display:flex; align-items:center; gap:7px; margin-bottom:3px;">
            <span style="color:#d63e2a; font-size:15px; line-height:1;">★</span>
            <span style="font-size:12px;">Rang #1 (höchster Bedarf)</span>
        </div>
        <div style="display:flex; align-items:center; gap:7px; margin-bottom:8px;">
            <span style="color:#f69730; font-size:15px; line-height:1;">★</span>
            <span style="font-size:12px;">Rang #2–10</span>
        </div>
        </div>

        <!-- Korridor-Typen -->
        <div data-leg-section="leg-corridors">
        <div style="font-size:11px; color:#555; margin-bottom:4px;">Korridor-Typen</div>
        <div style="display:flex; align-items:center; gap:7px; margin-bottom:3px;">
            <div style="width:16px; height:14px; background:#ff7700; border-radius:2px; flex-shrink:0; opacity:0.7;"></div>
            <span style="font-size:11px; line-height:1.3;">Safety-Corridor<br><span style="color:#888; font-size:10px;">hoher ASI + FLI</span></span>
        </div>
        <div style="display:flex; align-items:center; gap:7px; margin-bottom:3px;">
            <div style="width:16px; height:14px; background:#008B8B; border-radius:2px; flex-shrink:0; opacity:0.7;"></div>
            <span style="font-size:11px; line-height:1.3;">Health-Noise-Corridor<br><span style="color:#888; font-size:10px;">hoher FLI + TII</span></span>
        </div>
        <div style="display:flex; align-items:center; gap:7px;">
            <div style="width:16px; height:14px; background:#CC0000; border-radius:2px; flex-shrink:0; opacity:0.7;"></div>
            <span style="font-size:11px; line-height:1.3;">Multi-Stress-Corridor<br><span style="color:#888; font-size:10px;">hoher ASI + FLI + TII</span></span>
        </div>
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # Dynamic legend: show/hide sections based on LayerControl checkbox state
    legend_dynamic_js = """
    <script>
    (function() {
        // Map of lowercase keywords (from label text) to legend section IDs
        var SECTION_MAP = [
            { key: 'hsc score',              section: 'leg-hsc'       },
            { key: 'asi',                    section: 'leg-asi'       },
            { key: 'fli',                    section: 'leg-fli'       },
            { key: 'tii',                    section: 'leg-tii'       },
            { key: 'hotspot',                section: 'leg-hotspot'   },
            { key: 'safety-corridor',        section: 'leg-corridors' },
            { key: 'health-noise-corridor',  section: 'leg-corridors' },
            { key: 'multi-stress-corridor',  section: 'leg-corridors' },
        ];

        function stripHtml(s) { return s.replace(/<[^>]+>/g, '').toLowerCase(); }

        function getSectionId(labelInnerHTML) {
            var text = stripHtml(labelInnerHTML);
            for (var i = 0; i < SECTION_MAP.length; i++) {
                if (text.indexOf(SECTION_MAP[i].key) !== -1) return SECTION_MAP[i].section;
            }
            return null;
        }

        function updateLegend() {
            var checkboxes = document.querySelectorAll(
                '.leaflet-control-layers-overlays input[type=checkbox]'
            );
            var active = {};
            checkboxes.forEach(function(cb) {
                var lbl = cb.closest('label') || cb.parentElement;
                var span = lbl ? lbl.querySelector('span') : null;
                var html = span ? span.innerHTML : (lbl ? lbl.innerHTML : '');
                var sid = getSectionId(html);
                if (sid && cb.checked) active[sid] = true;
            });
            document.querySelectorAll('[data-leg-section]').forEach(function(el) {
                var sid = el.getAttribute('data-leg-section');
                el.style.display = active[sid] ? '' : 'none';
            });
        }

        function init() {
            var checkboxes = document.querySelectorAll(
                '.leaflet-control-layers-overlays input[type=checkbox]'
            );
            if (checkboxes.length < 6) { setTimeout(init, 100); return; }
            checkboxes.forEach(function(cb) {
                cb.addEventListener('change', updateLegend);
            });
            updateLegend();
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', init);
        } else {
            init();
        }
    })();
    </script>
    """
    m.get_root().html.add_child(folium.Element(legend_dynamic_js))

    m.save(out)
    print(f"[VIZ] Gespeichert: {out}", flush=True)


# ---------------------------------------------------------------------------
# 7. Zusammenfassung ausgeben
# ---------------------------------------------------------------------------

def print_summary(hfc, asi_n, fli_n, tii_n):
    print("\n" + "=" * 60)
    print("HSC SCORE — TOP 15 REGIONEN (Höchster Handlungsbedarf)")
    print("=" * 60)
    print(f"{'Rang':>4}  {'Lon-Bereich':>14}  {'Lat-Bereich':>14}  "
          f"{'HSC':>6}  {'ASI':>6}  {'FLI':>6}  {'TII':>6}")
    print("-" * 60)
    flat_sorted = np.argsort(hfc.ravel())[::-1]
    for rank, idx in enumerate(flat_sorted[:15]):
        ri, ci = np.unravel_index(idx, hfc.shape)
        print(f"{rank+1:>4}  "
              f"{lon_edges[ci]:6.2f}–{lon_edges[ci+1]:5.2f}  "
              f"{lat_edges[ri]:6.2f}–{lat_edges[ri+1]:5.2f}  "
              f"{hfc[ri,ci]:6.3f}  {asi_n[ri,ci]:6.3f}  "
              f"{fli_n[ri,ci]:6.3f}  {tii_n[ri,ci]:6.3f}")
    print("=" * 60)
    n_active = np.sum(hfc > 0)
    print(f"\nAktive Zellen (HSC > 0): {n_active} von {N_LON * N_LAT}")
    print(f"HSC Max: {hfc.max():.4f}  Min (excl. 0): {hfc[hfc>0].min():.4f}  Mean: {hfc[hfc>0].mean():.4f}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   Health Safety Corridor (HSC) Score Calculator    ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    # Daten laden
    asi = load_accidents()
    fli, roads_autobahn, roads_bundesstr = load_traffic()
    tii = load_noise()

    # Score berechnen
    hfc, asi_n, fli_n, tii_n = compute_hfc(asi, fli, tii)

    # Zusammenfassung
    print_summary(hfc, asi_n, fli_n, tii_n)

    # Visualisierungen
    plot_panels(hfc, asi_n, fli_n, tii_n, roads_autobahn, roads_bundesstr)
    plot_interactive(hfc, asi_n, fli_n, tii_n, roads_autobahn, roads_bundesstr, out="hsc_score_map.html")

    print("\n✓ Fertig! Ausgabedateien:")
    print("  • hsc_score_map.png   (4-Panel statische Karte für Pitch)")
    print("  • hsc_score_map.html  (interaktive Karte)")


if __name__ == "__main__":
    main()
