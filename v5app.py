# inventory_check_v3_v4.py
# Combines fetching from BoxHero API + processing merged_labels.csv

import requests
import json
import time
import pandas as pd
import re
from pathlib import Path

# ────────────────────────────────────────────────
#  CONFIG
# ────────────────────────────────────────────────

API_TOKEN = ""
BASE_URL = "https://rest.boxhero-app.com"
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Accept": "application/json"
}

BASE_PATH = Path('/Users/jia/projects/gen/inventory_checker/version2/')
FULL_ITEMS_JSON = BASE_PATH / 'full_items.json'
MERGED_LABELS_CSV = BASE_PATH / 'merged_labels.csv'
FULFILLABLE_CSV = BASE_PATH / 'fulfillable.csv'
UNFULFILLABLE_CSV = BASE_PATH / 'unfulfillable.csv'
MISC_CSV = BASE_PATH / 'misc.csv'

# Optional: if you want to filter by specific locations
LOCATION_IDS = None  # ← change to e.g. [12345, 67890] if needed

# ────────────────────────────────────────────────
#  PART 1: Fetch all items from BoxHero
# ────────────────────────────────────────────────

def fetch_all_items(location_ids=None, limit=100):
    all_items = []
    cursor = None
    params = {"limit": limit}

    if location_ids:
        params["location_ids"] = location_ids

    page = 1
    while True:
        print(f"Fetching page {page} (cursor={cursor})...")

        if cursor is not None:
            params["cursor"] = cursor

        try:
            resp = requests.get(
                f"{BASE_URL}/v1/items",
                headers=HEADERS,
                params=params,
                timeout=15
            )

            print(f"  → Status: {resp.status_code}")

            if resp.status_code != 200:
                print("Error response:")
                print(resp.text)
                return None

            data = resp.json()
            print("  → Response keys:", list(data.keys()))

            # Try to find the items list
            items = []
            for key in ["items", "data", "results", "item_list"]:
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    print(f"  → Found {len(items)} items under key '{key}'")
                    break

            if not items and isinstance(data, list):
                items = data
                print("  → Response is a list → using directly")

            all_items.extend(items)

            # Pagination
            has_more = False
            next_cursor = None

            for hm_key in ["has_more", "hasNext", "more", "has_next_page"]:
                if hm_key in data:
                    has_more = bool(data[hm_key])
                    break

            for c_key in ["cursor", "next_cursor", "next", "pagination_cursor", "nextPageCursor"]:
                if c_key in data and data[c_key]:
                    next_cursor = data[c_key]
                    break

            print(f"  → has_more: {has_more}, next cursor: {next_cursor}")

            if not has_more or not next_cursor:
                print("Reached end of pagination.")
                break

            cursor = next_cursor
            page += 1
            time.sleep(0.6)  # polite rate limiting

        except requests.RequestException as e:
            print(f"Request failed: {e}")
            return None

    print(f"\nFinished. Total items fetched: {len(all_items)}")

    if all_items:
        with open(FULL_ITEMS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        print(f"Saved items to: {FULL_ITEMS_JSON}")

        if len(all_items) > 0:
            print("Example item keys:", list(all_items[0].keys()))
    else:
        print("No items were returned.")
        return None

    return all_items


# ────────────────────────────────────────────────
#  PART 2: Process inventory vs orders
# ────────────────────────────────────────────────

def process_orders():
    if not FULL_ITEMS_JSON.exists():
        print(f"Error: {FULL_ITEMS_JSON} not found. Cannot continue.")
        return

    # Load inventory
    with open(FULL_ITEMS_JSON, 'r', encoding='utf-8') as f:
        items = json.load(f)

    stock = {}
    sku_dict = {}
    for item in items:
        bc = item.get('barcode')
        if bc:
            stock[bc] = int(item.get('quantity', 0))
            sku_dict[bc] = item.get('sku', '')

    if not stock:
        print("No valid barcodes found in full_items.json")
        return

    # Load customer orders
    if not MERGED_LABELS_CSV.exists():
        print(f"Error: {MERGED_LABELS_CSV} not found.")
        return

    df = pd.read_csv(MERGED_LABELS_CSV)

    # Prepare output dataframes
    fulfillable_df = pd.DataFrame(columns=list(df.columns) + ['sku_qty'])
    unfulfillable_df = pd.DataFrame(columns=df.columns)
    misc_df = pd.DataFrame(columns=df.columns)

    def parse_label(label):
        match = re.search(r'^\[.*?\]/\[.*?\]\s*(.*)$', str(label).strip())
        if not match:
            return []
        content = match.group(1).strip()
        if not content:
            return []
        parts = re.split(r'\s*,\s*', content)
        orders = []
        for part in parts:
            if not part or '*' not in part:
                return []  # invalid format → treat whole row as misc
            barcode, qty_str = part.rsplit('*', 1)
            barcode = barcode.strip()
            qty_str = qty_str.strip()
            if not qty_str.isdigit():
                return []
            qty = int(qty_str)
            orders.append((barcode, qty))
        return orders

    print("\nProcessing orders...")
    for _, row in df.iterrows():
        label = row.get('custom_label', '')
        try:
            orders = parse_label(label)
            if not orders:
                misc_df = pd.concat([misc_df, row.to_frame().T], ignore_index=True)
                continue

            # Heuristic: if any "barcode" doesn't start with digit → misc
            is_misc = any(not bc or not bc[0].isdigit() for bc, _ in orders)
            if is_misc:
                misc_df = pd.concat([misc_df, row.to_frame().T], ignore_index=True)
                continue

            # Check fulfillability
            can_fulfill = all(
                bc in stock and stock[bc] >= q
                for bc, q in orders
            )

            if can_fulfill:
                # Decrease stock
                for bc, q in orders:
                    stock[bc] -= q
                # Build sku*qty string
                sku_qty_parts = []
                for bc, q in orders:
                    sku = sku_dict.get(bc, 'UNKNOWN')
                    sku_qty_parts.append(f"{sku}*{q}")
                sku_qty_str = ', '.join(sku_qty_parts)

                new_row = row.copy()
                new_row['sku_qty'] = sku_qty_str
                fulfillable_df = pd.concat([fulfillable_df, new_row.to_frame().T], ignore_index=True)
            else:
                unfulfillable_df = pd.concat([unfulfillable_df, row.to_frame().T], ignore_index=True)

        except Exception as e:
            print(f"Error processing row: {label} → {e}")
            misc_df = pd.concat([misc_df, row.to_frame().T], ignore_index=True)

    # Save results
    fulfillable_df.to_csv(FULFILLABLE_CSV, index=False)
    unfulfillable_df.to_csv(UNFULFILLABLE_CSV, index=False)
    misc_df.to_csv(MISC_CSV, index=False)

    print("\n" + "="*60)
    print("Processing complete!")
    print(f"  Fulfillable  → {FULFILLABLE_CSV} ({len(fulfillable_df)} rows)")
    print(f"  Unfulfillable → {UNFULFILLABLE_CSV} ({len(unfulfillable_df)} rows)")
    print(f"  Misc         → {MISC_CSV} ({len(misc_df)} rows)")
    print("="*60)


# ────────────────────────────────────────────────
#  MAIN
# ────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting BoxHero inventory fetch...")
    items = fetch_all_items(location_ids=LOCATION_IDS)

    if items is not None and len(items) > 0:
        print("\nStarting order fulfillment check...")
        process_orders()
    else:
        print("\nFetch failed or returned no items → skipping processing.")