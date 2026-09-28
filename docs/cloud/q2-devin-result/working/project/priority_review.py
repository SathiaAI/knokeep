#!/usr/bin/env python3
"""Compute priority-review CSV from stock inventory.

Rows: active SKUs whose free units (stock - reserved, floored at 0) are below
threshold and whose lead_days >= 7. Output columns: sku,reason,owner.
"""

import csv
import sys

LEAD_DAYS_MIN = 7
REASON = "long lead time"
OWNER = "Procurement"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python priority_review.py stock.csv priority-review.csv", file=sys.stderr)
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
            lead_days = int(row["lead_days"])
            free_units = max(0, stock - reserved)
            if free_units >= threshold:
                continue
            if lead_days < LEAD_DAYS_MIN:
                continue
            rows.append({"sku": row["sku"].strip(), "reason": REASON, "owner": OWNER})

    rows.sort(key=lambda r: r["sku"])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "reason", "owner"])
        writer.writeheader()
        writer.writerows(rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
