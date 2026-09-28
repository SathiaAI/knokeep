"""Build a packing report from ready orders.

Usage: python pack.py orders.csv packing-report.csv
"""
import csv
import sys
from collections import defaultdict

CARTON_SIZE = 6


def normalize_sku(sku):
    return sku.lower().replace("-", "_").replace(" ", "_")


def main(argv):
    if len(argv) != 3:
        sys.exit("usage: python pack.py orders.csv packing-report.csv")
    src, dst = argv[1], argv[2]

    units_by_sku = defaultdict(int)
    with open(src, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # "ready" ships. Held orders have unreleased inventory unless the
            # release team approved them (approved exactly "yes"). Everything
            # else, including approved cancelled orders, stays out.
            status = row["status"]
            if not (status == "ready"
                    or (status == "hold" and row["approved"] == "yes")):
                continue
            units_by_sku[normalize_sku(row["sku"])] += int(row["units"])

    with open(dst, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sku", "units", "cartons"])
        for sku in sorted(units_by_sku):
            units = units_by_sku[sku]
            writer.writerow([sku, units, -(-units // CARTON_SIZE)])


if __name__ == "__main__":
    main(sys.argv)
