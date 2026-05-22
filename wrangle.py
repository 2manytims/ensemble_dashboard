#!/usr/bin/env python3
"""
EC Ensemble CSV Wrangler
Reads ECens_member_fcst_*.csv files from a folder and outputs data/ensemble_data.json.

Usage:
    python wrangle.py <csv_folder> [output_folder]

Example (Windows):
    python wrangle.py "C:/Users/tconstable/OneDrive - MetService/Desktop/Projects/Ens_dashboard"

The script will create a 'data/' subfolder (or output_folder if supplied)
and write ensemble_data.json there.
"""

import os
import sys
import re
import json
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

# Variables to extract
VARIABLES = {
    "tmax":   {"label": "Max Temperature", "unit": "°C"},
    "ff100":  {"label": "100m Wind Speed",  "unit": "m/s"},
    "swrad6": {"label": "Solar Radiation",  "unit": "W/m²"},
    "tcld":   {"label": "Cloud Cover",      "unit": "%"},
    "td":     {"label": "Dew Point",        "unit": "°C"},
}

SWRAD6_PERIOD_SEC = 6 * 3600  # J/m² → W/m²


# ── Parser ───────────────────────────────────────────────────────────────────

def parse_station(filepath, station_id):
    """
    Extract member forecast data for one station from a multi-station CSV.
    Returns dict {times, members[50], mean} or None if no data rows found.
    """
    times        = []
    members      = [[] for _ in range(50)]
    means        = []
    in_section   = False
    header_found = False

    try:
        with open(filepath, encoding="utf-8-sig", errors="replace") as fh:
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

    except OSError as exc:
        print(f"  WARNING: Cannot read {filepath}: {exc}")
        return None

    return {"times": times, "members": members, "mean": means} if times else None


def parse_station_with_fallback(filepath, station_ids):
    """Try each station ID in order, return first successful parse."""
    for sid in station_ids:
        result = parse_station(filepath, sid)
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
            factor = 100.0 / 8.0   # oktas → percent
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

    pattern = re.compile(r"^ECens_member_fcst_(.+?)_(\d{8,12})\.csv$", re.IGNORECASE)
    csv_map  = {}
    run_tag  = ""

    for fname in sorted(os.listdir(input_dir)):
        m = pattern.match(fname)
        if not m:
            continue
        var_key = m.group(1).lower()
        run_tag = m.group(2)
        if var_key in VARIABLES:
            csv_map[var_key] = os.path.join(input_dir, fname)
            print(f"  Found: {var_key} → {fname}")

    if not csv_map:
        print("ERROR: No matching ECens_member_fcst_*.csv files found in:", input_dir)
        sys.exit(1)

    try:
        run_dt    = datetime.strptime(run_tag[:10], "%Y%m%d%H")
        run_label = run_dt.strftime("%d %b %Y %HZ").lstrip("0") or run_dt.strftime("%d %b %Y %HZ")
    except ValueError:
        run_label = run_tag

    print(f"\nRun: {run_label}")

    output = {
        "meta":     {"run_label": run_label, "run_tag": run_tag},
        "stations": {}
    }

    for stn_name, stn_ids in STATIONS.items():
        print(f"\n  ── {stn_name} (trying: {', '.join(stn_ids)})")
        output["stations"][stn_name] = {}

        for var_key, cfg in VARIABLES.items():
            if var_key not in csv_map:
                print(f"    {var_key}: file not found, skipping")
                continue

            data, used_id = parse_station_with_fallback(csv_map[var_key], stn_ids)
            if data is None:
                print(f"    {var_key}: no data found for any station ID")
                continue

            data = postprocess(data, var_key)
            data["label"] = cfg["label"]
            data["unit"]  = cfg["unit"]

            note = f" (via {used_id})" if used_id != stn_ids[0] else ""
            print(f"    {var_key}{note}: {len(data['times'])} timesteps  "
                  f"(mean {min(data['mean']):.1f}–{max(data['mean']):.1f} {cfg['unit']})")

            output["stations"][stn_name][var_key] = data

    out_path = os.path.join(output_dir, "ensemble_data.json")
    with open(out_path, "w") as fh:
        json.dump(output, fh, separators=(",", ":"))

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\n✓  Written: {out_path}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
