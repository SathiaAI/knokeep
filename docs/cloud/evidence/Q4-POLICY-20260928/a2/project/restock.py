#!/usr/bin/env python3
"""Compute restock CSV from stock inventory."""

import csv
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python restock.py stock.csv restock.csv", file=sys.stderr)
        return 2

    in_path, out_path = sys.argv[1], sys.argv[2]
    rows = []

    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("active", "").strip().lower() != "yes":
                continue
            stock = int(row["stock"])
            reserved = int(row["reserved"])
            threshold = int(row["threshold"])
            free_units = max(0, stock - reserved)
            reorder_units = max(0, threshold - free_units)
            rows.append(
                {
                    "sku": row["sku"].strip(),
                    "free_units": free_units,
                    "reorder_units": reorder_units,
                }
            )

    rows.sort(key=lambda r: r["sku"])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "free_units", "reorder_units"])
        writer.writeheader()
        writer.writerows(rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
