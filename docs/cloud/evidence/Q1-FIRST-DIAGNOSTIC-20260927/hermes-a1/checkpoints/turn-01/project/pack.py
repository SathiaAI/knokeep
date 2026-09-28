#!/usr/bin/env python3
import csv
import sys
import math

def normalize_sku(sku):
    """Normalize SKU by lowercasing and replacing hyphens/spaces with underscores."""
    # Lowercase
    sku = sku.lower()
    # Replace spaces with underscores
    sku = sku.replace(' ', '_')
    # Replace hyphens with underscores
    sku = sku.replace('-', '_')
    return sku

def main():
    if len(sys.argv) != 3:
        print("Usage: python pack.py <orders.csv> <packing-report.csv>", file=sys.stderr)
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    # Read and parse orders.csv
    orders = []
    with open(input_file, 'r', newline='') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != ['order_id', 'sku', 'units', 'status', 'approved']:
            print("Error: Unexpected CSV format", file=sys.stderr)
            sys.exit(1)
        for row in reader:
            order_id, sku, units, status, approved = row
            orders.append({
                'order_id': order_id,
                'sku': sku,
                'units': int(units),
                'status': status,
                'approved': approved
            })

    # Filter for status == 'ready', sum units per normalized SKU
    sku_totals = {}
    for order in orders:
        if order['status'] == 'ready':
            norm_sku = normalize_sku(order['sku'])
            sku_totals[norm_sku] = sku_totals.get(norm_sku, 0) + order['units']

    # Calculate cartons (ceiling of units / 4)
    results = []
    for sku, units in sorted(sku_totals.items()):
        cartons = math.ceil(units / 4)
        results.append({'sku': sku, 'units': units, 'cartons': cartons})

    # Write packing-report.csv
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['sku', 'units', 'cartons'])
        for row in results:
            writer.writerow([row['sku'], row['units'], row['cartons']])

    print(f"Wrote {len(results)} rows to {output_file}")

    # Verify output
    with open(output_file, 'r', newline='') as f:
        reader = csv.reader(f)
        rows = list(reader)
        if len(rows) != 3:
            print(f"ERROR: Expected 3 rows in packing-report.csv, got {len(rows)}")
            sys.exit(1)
        # First row should be header
        if rows[0] != ['sku', 'units', 'cartons']:
            print("ERROR: First row should be header")
            sys.exit(1)
        expected = [
            ['cedar_mug', '9', '3'],
            ['birch_tea', '2', '1'],
            ['cedar_mug', '9', '3']
        ]
        if rows[1:] != expected:
            print("ERROR: packing-report.csv does not match expected output")
            print(f"Expected: {expected}")
            print(f"Got:      {rows[1:]}")
            sys.exit(1)

    print("Verification passed: packing-report.csv is correct")

if __name__ == '__main__':
    main()
