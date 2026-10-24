"""Tiny therapeutic-class and allergy-class tables for the demo formulary (synthetic, not clinical)."""
from __future__ import annotations

import re

_INTERVAL = re.compile(r"q(\d+)h", re.IGNORECASE)


def frequency_hours(frequency: str) -> int | None:
    """Scheduled interval in hours for 'qNh' / 'daily'; None for continuous, titrated, or unknown."""
    m = _INTERVAL.search(frequency or "")
    if m:
        return int(m.group(1))
    return 24 if "daily" in (frequency or "").lower() else None

DRUG_CLASSES: dict[str, tuple[str, ...]] = {
    "penicillin": ("penicillin", "amoxicillin", "ampicillin", "piperacillin", "nafcillin", "oxacillin"),
    "sulfonamide": ("sulfamethoxazole", "sulfadiazine", "sulfasalazine"),
    "anticoagulant": ("heparin", "enoxaparin", "apixaban", "rivaroxaban", "warfarin", "dabigatran", "dalteparin"),
    "opioid": ("morphine", "hydromorphone", "oxycodone", "fentanyl", "codeine", "hydrocodone", "tramadol"),
    "insulin": ("insulin",),
    "benzodiazepine": ("lorazepam", "diazepam", "midazolam", "alprazolam"),
    "proton_pump_inhibitor": ("pantoprazole", "omeprazole", "esomeprazole"),
    "nsaid_salicylate": ("aspirin", "ibuprofen", "ketorolac", "naproxen"),
    "corticosteroid": ("prednisone", "methylprednisolone", "hydrocortisone", "dexamethasone"),
    "beta_blocker": ("metoprolol", "carvedilol", "atenolol", "labetalol"),
}

# Allergy substance keyword -> drug class it implies. Anything not listed matches by drug name only
# (e.g. a codeine intolerance does not flag every opioid).
ALLERGY_CLASSES: dict[str, str] = {
    "penicillin": "penicillin",
    "sulfa": "sulfonamide",
    "sulfonamide": "sulfonamide",
    "nsaid": "nsaid_salicylate",
    "aspirin": "nsaid_salicylate",
}


def norm(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").split())


def drug_class(med_name: str) -> str | None:
    n = norm(med_name)
    for cls, members in DRUG_CLASSES.items():
        if any(m in n for m in members):
            return cls
    return None


def allergy_matches(substance: str, med_name: str) -> bool:
    s, n = norm(substance), norm(med_name)
    if not s or not n:
        return False
    if s in n or n.split()[0] in s.split():
        return True
    for keyword, cls in ALLERGY_CLASSES.items():
        if keyword in s and drug_class(med_name) == cls:
            return True
    return False


def same_medication(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or x in y or y in x or x.split()[0] == y.split()[0])


# High-alert medications (ISMP-style) for the demo formulary: the classes and named drugs the seeded
# patients carry with `high_alert: true`. A captured medication field naming one needs two verifiers.
HIGH_ALERT_CLASSES = ("anticoagulant", "opioid", "insulin")
HIGH_ALERT_NAMES = ("potassium chloride", "kcl")


def is_high_alert(med_name: str | None) -> bool:
    n = norm(med_name or "")
    return bool(n) and (drug_class(n) in HIGH_ALERT_CLASSES or any(m in n for m in HIGH_ALERT_NAMES))
