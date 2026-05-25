#!/usr/bin/env python3
"""
EC Ensemble CSV Wrangler
Reads ECens_member_fcst_*.csv files from zip archives in the script folder
and outputs data/ensemble_data.json.

Usage:
    python wrangle.py [csv_folder] [output_folder]

With no arguments, looks for zip files in the same folder as the script.
The zips can be named anything starting with 'ECens' — just save them
directly from your email into the Ens_dashboard folder.

Output: <csv_folder>/data/ensemble_data.json
"""

import os
import sys
import re
import io
import json
import math
import zipfile
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────

# NEM stations to extract
# Each entry maps display name → list of station IDs to try in order (fallback)
STATIONS = {
    "Brisbane":  ["94575"],          # Archerfield
    "Sydney":    ["94765"],          # Bankstown
    "Melbourne": ["95936"],
    "Adelaide":  ["94648", "94675"], # Adelaide obs, fallback to Kent Town
}

# Raw variables to extract from CSVs
VARIABLES = {
    "tmax":   {"label": "Max Temperature",  "unit": "°C"},
    "tmin":   {"label": "Min Temperature",  "unit": "°C"},
    "tt":     {"label": "Temperature",      "unit": "°C"},
    "ff100":  {"label": "100m Wind Speed",  "unit": "m/s"},
    "ff10":   {"label": "10m Wind Speed",   "unit": "m/s"},
    "swrad6": {"label": "Solar Radiation",  "unit": "W/m²"},
    "tcld":   {"label": "Cloud Cover",      "unit": "%"},
    "td":     {"label": "Dew Point",        "unit": "°C"},
    "t850":   {"label": "850 hPa Temp",     "unit": "°C"},
    "dd10":   {"label": "10m Wind Direction", "unit": "°"},
}

SWRAD6_PERIOD_SEC = 6 * 3600  # J/m² → W/m²


# ── Apparent Temperature ─────────────────────────────────────────────────────

def compute_apparent_temp(tt_data, td_data, ff10_data):
    """
    BOM Steadman apparent temperature:
        AT = Ta + 0.33 * e - 0.70 * ws - 4.00
    where:
        e  = vapour pressure (hPa) from dewpoint
           = 6.1078 * exp(17.269 * Td / (237.3 + Td))
        ws = 10m wind speed (m/s)
        Ta = dry bulb temperature (°C)

    All three inputs must share the same time axis.
    Returns a dict matching the standard variable structure.
    """
    # Use the shortest common time axis
    n_times = min(len(tt_data["times"]), len(td_data["times"]), len(ff10_data["times"]))
    times   = tt_data["times"][:n_times]

    members = []
    for i in range(50):
        series = []
        for t in range(n_times):
            ta = tt_data["members"][i][t]
            td = td_data["members"][i][t]
            ws = ff10_data["members"][i][t]
            e  = 6.1078 * math.exp(17.269 * td / (237.3 + td))
            at = ta + 0.33 * e - 0.70 * ws - 4.00
            series.append(round(at, 1))
        members.append(series)

    # Mean from member means
    mean = [
        round(sum(members[i][t] for i in range(50)) / 50, 1)
        for t in range(n_times)
    ]

    return {
        "times":   times,
        "members": members,
        "mean":    mean,
        "label":   "Apparent Temperature",
        "unit":    "°C",
    }


# ── CSV discovery from zips ───────────────────────────────────────────────────

def discover_csvs_from_zips(folder):
    """
    Scan folder for any ECens*.zip files.
    Return dict: var_key (lowercase) → (zip_path, inner_filename, run_tag)
    Uses the most recent run_tag if multiple runs are present.
    """
    pattern  = re.compile(r"ECens_member_fcst_(.+?)_(\d{8,12})\.csv$", re.IGNORECASE)
    csv_map  = {}
    run_tags = {}

    zip_files = [
        os.path.join(folder, f)
        for f in sorted(os.listdir(folder))
        if f.lower().endswith(".zip") and f.lower().startswith("ecens")
    ]

    if not zip_files:
        print("  No ECens*.zip files found — falling back to loose CSVs in folder")
        return discover_csvs_from_folder(folder)

    print(f"  Found {len(zip_files)} zip file(s):")
    for zp in zip_files:
        print(f"    {os.path.basename(zp)}")

    for zip_path in zip_files:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    m = pattern.search(name)
                    if not m:
                        continue
                    var_key = m.group(1).lower()
                    run_tag = m.group(2)
                    if var_key not in VARIABLES:
                        continue
                    # Keep the entry with the latest run_tag
                    if var_key not in run_tags or run_tag > run_tags[var_key]:
                        csv_map[var_key]  = (zip_path, name)
                        run_tags[var_key] = run_tag
        except zipfile.BadZipFile:
            print(f"  WARNING: Cannot open zip: {zip_path}")

    # Determine the dominant run tag (most common across variables)
    if run_tags:
        run_tag = max(set(run_tags.values()), key=list(run_tags.values()).count)
    else:
        run_tag = ""

    return csv_map, run_tag


def discover_csvs_from_folder(folder):
    """Fallback: read loose CSVs directly from folder."""
    pattern = re.compile(r"^ECens_member_fcst_(.+?)_(\d{8,12})\.csv$", re.IGNORECASE)
    csv_map = {}
    run_tag = ""
    for fname in sorted(os.listdir(folder)):
        m = pattern.match(fname)
        if not m:
            continue
        var_key = m.group(1).lower()
        run_tag = m.group(2)
        if var_key in VARIABLES:
            csv_map[var_key] = (None, os.path.join(folder, fname))
    return csv_map, run_tag


# ── CSV reader (handles both zip-member and loose file) ───────────────────────

def open_csv(zip_path, inner_path):
    """Return a text-mode file-like object for the CSV."""
    if zip_path is None:
        # inner_path is a real filesystem path
        return open(inner_path, encoding="utf-8-sig", errors="replace")
    else:
        zf   = zipfile.ZipFile(zip_path, "r")
        raw  = zf.read(inner_path)
        return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", errors="replace")


# ── Parser ───────────────────────────────────────────────────────────────────

def parse_station(zip_path, inner_path, station_id):
    """
    Extract member forecast data for one station.
    Returns dict {times, members[50], mean} or None if station not found / no data.
    """
    times        = []
    members      = [[] for _ in range(50)]
    means        = []
    in_section   = False
    header_found = False

    try:
        with open_csv(zip_path, inner_path) as fh:
            for raw in fh:
                line = raw.strip()

                if "EC Ensemble members" in line:
                    if station_id in line:
                        in_section   = True
                        header_found = False
                        times        = []
                        members      = [[] for _ in range(50)]
                        means        = []
                    elif in_section:
                        break
                    else:
                        in_section = False
                    continue

                if not in_section:
                    continue
                if "DateTime" in line:
                    header_found = True
                    continue
                if not header_found or not line:
                    continue

                parts = line.split(",")
                if len(parts) < 52:
                    continue

                try:
                    dt = datetime.strptime(parts[0].strip(), "%d/%m/%Y %H:%M")
                    times.append(dt.strftime("%Y-%m-%dT%H:%MZ"))
                    for i in range(50):
                        members[i].append(round(float(parts[i + 1]), 2))
                    means.append(round(float(parts[51]), 2))
                except (ValueError, IndexError):
                    continue

    except Exception as exc:
        print(f"  WARNING: Error reading {inner_path}: {exc}")
        return None

    return {"times": times, "members": members, "mean": means} if times else None


def parse_station_with_fallback(zip_path, inner_path, station_ids):
    for sid in station_ids:
        result = parse_station(zip_path, inner_path, sid)
        if result:
            return result, sid
    return None, None


# ── Post-processing ──────────────────────────────────────────────────────────

def postprocess(data, var_key):
    if var_key == "swrad6":
        data["members"] = [
            [round(v / SWRAD6_PERIOD_SEC, 1) for v in s]
            for s in data["members"]
        ]
        data["mean"] = [round(v / SWRAD6_PERIOD_SEC, 1) for v in data["mean"]]

    elif var_key == "tcld":
        all_vals = [v for s in data["members"] for v in s]
        mx = max(all_vals) if all_vals else 1
        if mx <= 1.0:
            factor = 100.0
        elif mx <= 8.0:
            factor = 100.0 / 8.0
        elif mx <= 100.0:
            factor = 1.0
        else:
            factor = 100.0 / mx
            print(f"  WARNING: tcld max={mx:.1f}; normalising")
        if factor != 1.0:
            data["members"] = [
                [round(min(100.0, max(0.0, v * factor)), 1) for v in s]
                for s in data["members"]
            ]
            data["mean"] = [round(min(100.0, max(0.0, v * factor)), 1) for v in data["mean"]]

    return data


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    input_dir  = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(input_dir, "data")
    os.makedirs(output_dir, exist_ok=True)

    print(f"Scanning: {input_dir}\n")
    csv_map, run_tag = discover_csvs_from_zips(input_dir)

    if not csv_map:
        print("ERROR: No matching ECens_member_fcst_*.csv files found in any zip.")
        sys.exit(1)

    print(f"\nVariables found: {', '.join(sorted(csv_map.keys()))}")

    try:
        run_dt    = datetime.strptime(run_tag[:10], "%Y%m%d%H")
        run_label = run_dt.strftime("%-d %b %Y %HZ")
    except (ValueError, TypeError):
        try:
            run_label = datetime.strptime(run_tag[:10], "%Y%m%d%H").strftime("%d %b %Y %HZ").lstrip("0")
        except Exception:
            run_label = run_tag

    # Fallback label formatting for Windows (no %-d)
    try:
        run_dt    = datetime.strptime(run_tag[:10], "%Y%m%d%H")
        run_label = f"{run_dt.day} {run_dt.strftime('%b %Y %HZ')}"
    except Exception:
        run_label = run_tag

    print(f"Run: {run_label}\n")

    output = {
        "meta":     {"run_label": run_label, "run_tag": run_tag},
        "stations": {}
    }

    for stn_name, stn_ids in STATIONS.items():
        print(f"  ── {stn_name}")
        output["stations"][stn_name] = {}
        stn_vars = {}  # cache raw parsed data for derived calculations

        for var_key, cfg in VARIABLES.items():
            if var_key not in csv_map:
                continue

            zip_path, inner_path = csv_map[var_key]
            data, used_id = parse_station_with_fallback(zip_path, inner_path, stn_ids)

            if data is None:
                print(f"    {var_key}: not found")
                continue

            data = postprocess(data, var_key)
            data["label"] = cfg["label"]
            data["unit"]  = cfg["unit"]

            note = f" (via {used_id})" if used_id != stn_ids[0] else ""
            print(f"    {var_key}{note}: {len(data['times'])} timesteps  "
                  f"({min(data['mean']):.1f}–{max(data['mean']):.1f} {cfg['unit']})")

            output["stations"][stn_name][var_key] = data
            stn_vars[var_key] = data

        # Derived: apparent temperature (requires tt, td, ff10)
        if all(k in stn_vars for k in ("tt", "td", "ff10")):
            at_data = compute_apparent_temp(stn_vars["tt"], stn_vars["td"], stn_vars["ff10"])
            print(f"    apparent_temp: {len(at_data['times'])} timesteps  "
                  f"({min(at_data['mean']):.1f}–{max(at_data['mean']):.1f} °C)")
            output["stations"][stn_name]["apparent_temp"] = at_data
        else:
            missing = [k for k in ("tt", "td", "ff10") if k not in stn_vars]
            print(f"    apparent_temp: skipped (missing: {', '.join(missing)})")

    out_path = os.path.join(output_dir, "ensemble_data.json")
    with open(out_path, "w") as fh:
        json.dump(output, fh, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\n✓  Written: {out_path}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
