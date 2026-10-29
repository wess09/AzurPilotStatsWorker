"""Commission identity and amount rules from the pinned AzurStats catalog.

Reference: LmeSzinc/azur-stats-server@3a743f091b0a98d42a8dc04c3d16be9552a9c7d6,
AzurStats/scene/commission_items.py. Rules operate on individual reward slots,
before grouping quantities. Night conversion deliberately uses its arguments.
"""
from numbers import Integral, Real
import re


def parse_amount(value):
    if isinstance(value, bool) or not isinstance(value, (Integral, Real, str)):
        raise ValueError("invalid_amount")
    if isinstance(value, str) and not re.fullmatch(r"\+?[0-9]+", value.strip()):
        raise ValueError("invalid_amount")
    amount = int(value)
    if not isinstance(value, str) and value != amount:
        raise ValueError("invalid_amount")
    return amount


NIGHT_RULES = (
    ("Small Merchant Escort", "Coins", 260, 450),
    ("Medium Merchant Escort", "Coins", 300, 550),
    ("Large Merchant Escort", "Coins", 350, 660),
    ("Coastal Defense Patrol", "Oil", 65, 105),
    ("Buoy Inspection", "Oil", 100, 160),
    ("Frontier Defense Patrol", "Oil", 150, 230),
)
ITEM_ALIASES = {"Gem": "Gems", "Chip": "CognitiveChips", "Cube": "Cubes", "Coin": "Coins"}
COMMON_LIMITS = {
    "Awakening Tactical Research Ⅰ": (50, 80), "Awakening Tactical Research Ⅱ": (100, 170),
    "Daily Resource Extraction Ⅰ": (50, 80), "Daily Resource Extraction Ⅱ": (100, 170),
    "Daily Resource Extraction Ⅲ": (60, 90), "Daily Resource Extraction Ⅳ": (120, 200),
    "Daily Resource Extraction Ⅴ": (70, 100), "Daily Resource Extraction Ⅵ": (140, 220),
}
COIN_LIMITS = {
    **{name + " Ⅲ": (4500, 6000) for name in
       ("Cargo Transport", "Defense Exercise", "Research Mission", "Self Training", "Tactical Class", "Tool Prep")},
    "Forest Protection Commission Ⅱ": (40, 85), "Vein Protection Commission Ⅱ": (40, 85),
    "Forest Protection Commission Ⅲ": (70, 190), "Vein Protection Commission Ⅲ": (70, 190),
    "Large Merchant Escort": (350, 660), "Small Launch Ceremony": (420, 540),
    "Fleet Launch Ceremony": (880, 1040), "Alliance Launch Ceremony": (1760, 2000),
}
OIL_LIMITS = {
    "Small-scale Oil Extraction Ⅰ": (15, 30), "Small-scale Oil Extraction Ⅱ": (20, 40),
    "Small-scale Oil Extraction Ⅲ": (25, 50), "Mid-scale Oil Extraction Ⅰ": (80, 140),
    "Mid-scale Oil Extraction Ⅱ": (100, 180), "Mid-scale Oil Extraction Ⅲ": (120, 220),
    "Large-scale Oil Extraction Ⅰ": (150, 300), "Large-scale Oil Extraction Ⅱ": (200, 400),
    "Large-scale Oil Extraction Ⅲ": (250, 500), "Coastal Defense Patrol": (12, 28),
    "Buoy Inspection": (15, 35), "Frontier Defense Patrol": (25, 55),
}


def revise_commission(code, entries):
    """Return the canonical English identity; entries are unmerged slot rows."""
    original = code
    for item, _tag, amount in entries:
        for expected, material, lower, upper in NIGHT_RULES:
            if code == expected and item == material and lower <= amount <= upper:
                code += " N"
        if original in ("BIW VIP Escort", "NYB VIP Escort"):
            if (item == "Gems" and 50 <= amount <= 80) or (item == "Oil" and 480 <= amount <= 720):
                code = original.replace("VIP", "Patrol")
        if item != "Coins" or original is None:
            continue
        if original.startswith("Cargo Defense"):
            for suffix, lower, upper in (("Ⅰ", 50, 80), ("Ⅱ", 85, 125), ("Ⅲ", 145, 225)):
                if lower <= amount <= upper:
                    code = "Cargo Defense " + suffix
        for family in ("Forest Protection Commission", "Vein Protection Commission"):
            if original == family + " Ⅱ":
                if 20 <= amount <= 39:
                    code = family + " Ⅰ"
                elif 86 <= amount <= 190:
                    code = family + " Ⅲ"
            elif original == family + " Ⅲ":
                if 20 <= amount <= 39:
                    code = family + " Ⅰ"
                elif 41 <= amount <= 69:
                    code = family + " Ⅱ"
    return code


def revise_amount(code, item, amount):
    """Apply the upstream digit corrections, then its explicit range checks."""
    if item == "CognitiveChips":
        if 4400 < amount < 4500:
            amount %= 100
        if amount > 1000:
            amount %= 100
        if amount < 1000:
            amount %= 100
        if 70 < amount <= 80:
            amount = 30 + amount % 10
        if 32 < amount < 80:
            amount %= 10
        if code == "Awakening Tactical Research Ⅰ" and amount < 10:
            amount += 10
        if code == "Awakening Tactical Research Ⅱ":
            if amount == 0:
                amount = 30
            if amount <= 3:
                amount *= 10
        if code in ("Short-range Sailing Training", "Mid-range Sailing Training", "Coastal Defense Patrol"):
            amount %= 10
        if code in ("Long-range Sailing Training", "Buoy Inspection"):
            if amount == 0:
                amount = 10
            if amount <= 1:
                amount *= 10
            if amount > (10 if code == "Long-range Sailing Training" else 14):
                amount %= 10
        if code in ("Coastal Defense Patrol N", "Buoy Inspection N", "Frontier Defense Patrol"):
            if amount == 0:
                amount = 20
            if amount <= 2:
                amount *= 10
        if code == "Frontier Defense Patrol N":
            if amount == 0:
                amount = 30
            if amount <= 3:
                amount *= 10
            if amount < 10:
                amount += 20
    if item == "Cubes" and amount > 10:
        amount %= 10
    limits = None
    if item in ("Coins", "Oil"):
        limits = COMMON_LIMITS.get(code)
    if item == "Coins":
        limits = COIN_LIMITS.get(code, limits)
    elif item == "Oil":
        limits = OIL_LIMITS.get(code, limits)
    elif item == "CognitiveChips":
        limits = {"Awakening Tactical Research Ⅰ": (10, 18),
                  "Awakening Tactical Research Ⅱ": (24, 32)}.get(code)
    if limits and not limits[0] <= amount <= limits[1]:
        raise ValueError("amount_out_of_range")
    return amount
