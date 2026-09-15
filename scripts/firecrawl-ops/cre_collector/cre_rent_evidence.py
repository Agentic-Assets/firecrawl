"""Source evidence only; no market qualification, network, or database writes."""

import math
import re

HIGH_ANNUAL_PSF = 500  # Review flag only, never a rejection threshold.


def rent_evidence(text, source_field_label=None):
    raw = text if isinstance(text, str) else None
    label = source_field_label if isinstance(source_field_label, str) else None
    s = " ".join(filter(None, [raw, label]))
    monthly = bool(
        re.search(r"/\s*(?:mo|month)\b|\bmonthly\b|\bper\s+month\b", s, re.IGNORECASE)
    )
    annual = bool(
        re.search(
            r"/\s*(?:yr|year)\b|\bper\s+year\b|\bannual(?:ly)?\b", s, re.IGNORECASE
        )
    )
    period = (
        "conflict"
        if monthly and annual
        else "monthly"
        if monthly
        else "annual"
        if annual
        else "unknown"
    )
    denominator = (
        "sf"
        if re.search(
            r"(?:/|\bper\s+)\s*(?:sf\b|sq\.?\s*ft\b|square\s*(?:feet|foot))|\bpsf\b|\bsf\s*/\s*(?:yr|mo|year|month)",
            s,
            re.IGNORECASE,
        )
        else "unknown"
    )
    if re.search(
        r"\b(?:sqm|m2|acres?|units?)\b|square\s*met(?:er|re)s?", s, re.IGNORECASE
    ):
        denominator = "conflict" if denominator == "sf" else "unknown"
    currencies = set(re.findall(r"\b(?:USD|CAD|EUR|GBP|AUD)\b", s.upper()))
    if "€" in s:
        currencies.add("EUR")
    if "£" in s:
        currencies.add("GBP")
    currency = (
        next(iter(currencies))
        if len(currencies) == 1
        else "conflict"
        if currencies
        else "unknown"
    )
    basis = None
    for pattern, value in [
        (r"modified[ _-]gross|mod gross", "modified_gross"),
        (r"full[ _-]service|\bfsg\b", "full_service"),
        (r"\bnnn\b|triple[ -]net", "nnn"),
        (r"\big\b|\bgross\b", "gross"),
    ]:
        if re.search(pattern, s, re.IGNORECASE):
            basis = value
            break
    basis_conflict = bool(
        re.search(r"\bnnn\b|triple[ -]net", s, re.IGNORECASE)
    ) and bool(
        re.search(r"\bgross\b|\big\b|full[ _-]service|\bfsg\b", s, re.IGNORECASE)
    )
    if basis_conflict:
        basis = None
    amounts = []
    if raw:
        clean = raw.replace(",", "")
        number = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
        money = re.search(
            r"(?:\bUSD|\bCAD|\bEUR|\bGBP|\bAUD|\$|€|£)\s*(" + number + r")",
            clean,
            re.IGNORECASE,
        )
        trailing = re.search(
            r"(" + number + r")\s*(?:USD|CAD|EUR|GBP|AUD)\b", clean, re.IGNORECASE
        )
        if money or trailing:
            clean = clean[(money or trailing).start(1) :]
        match = re.match(
            r"(" + number + r")\s*(?:-|–|—|to)\s*\$?\s*(" + number + r")",
            clean.strip(),
            re.IGNORECASE,
        )
        if match:
            amounts = [float(match[1]), float(match[2])]
        else:
            match = re.match(
                number
                + r"(?=\s*(?:$|/|USD\b|CAD\b|EUR\b|GBP\b|AUD\b|SF\b|PSF\b|per\b|NNN\b|gross\b))",
                clean.strip(),
                re.IGNORECASE,
            )
            if match:
                amounts = [float(match[0])]
    anomalies = []
    if basis_conflict:
        anomalies.append("lease_basis_conflict")
    for key, value in [
        ("period", period),
        ("denominator", denominator),
        ("currency", currency),
    ]:
        if value in ("unknown", "conflict"):
            anomalies.append(f"{key}_{value}")
    valid = amounts and all(
        math.isfinite(n)
        and n > 0
        and math.isfinite(n * (12 if period == "monthly" else 1) * 100)
        for n in amounts
    )
    if raw and re.search(r"(?:^|USD\s*|\$\s*)-\s*\d", raw, re.IGNORECASE):
        valid = False
    if amounts and not valid:
        anomalies.append("invalid_amount")
    lo = hi = None
    if (
        valid
        and period in ("annual", "monthly")
        and denominator == "sf"
        and currency == "USD"
    ):
        # Positive IEEE-754 amounts: same half-up scaling as JS Math.round.
        values = [
            math.floor(n * (12 if period == "monthly" else 1) * 100 + 0.5) / 100
            for n in amounts
        ]
        lo = min(values)
        hi = max(values) if max(values) > lo else None
        if max(values) > HIGH_ANNUAL_PSF:
            anomalies.append("unusually_high_annual_psf")
        if len(values) > 1 and max(values) / min(values) >= 10:
            anomalies.append("wide_range")
    return {
        "raw_quote": raw,
        "source_field_label": label,
        "original_period": period,
        "denominator": denominator,
        "currency": currency,
        "lease_basis": basis,
        "annual_psf_min": lo,
        "annual_psf_max": hi,
        "anomalies": anomalies,
    }


def _coordinate(value, bound):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return value if math.isfinite(value) and abs(value) <= bound else None


def listing_evidence(listing, source_updated_at=None, collected_at=None):
    """Conservative projection: normalized generic size/lat/lng are not proof."""
    areas = []
    for field, kind in [
        ("buildingSizeText", "building"),
        ("availableSpaceText", "offered_space"),
        ("lotSizeText", "land"),
        ("unitsText", "units"),
        ("sizeText", "unknown"),
    ]:
        raw = listing.get(field)
        if not isinstance(raw, str):
            continue
        match = re.search(
            r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(sf\b|sq\.?\s*ft\b|acres?\b|units?\b)",
            raw,
            re.IGNORECASE,
        )
        token = match[2].lower() if match else ""
        unit = (
            "acres"
            if token.startswith("acre")
            else "units"
            if token.startswith("unit")
            else "sf"
            if match
            else "unknown"
        )
        area_value = float(match[1].replace(",", "")) if match else None
        if re.search(r"[0-9][0-9,.]*\s*(?:-|–|—|to)\s*[0-9]", raw, re.IGNORECASE):
            area_value = None
        if area_value is not None and (
            not math.isfinite(area_value) or area_value <= 0
        ):
            area_value = None
        areas.append(
            {
                "raw_quote": raw,
                "source_field_label": field,
                "area_kind": kind,
                "area_unit": unit,
                "value": area_value,
            }
        )
    return {
        "version": 1,
        "rent": rent_evidence(
            listing.get("leaseRateText"), listing.get("leaseRateSourceLabel")
        ),
        "areas": areas,
        "coordinates": {
            "latitude": _coordinate(listing.get("latitude"), 90),
            "longitude": _coordinate(listing.get("longitude"), 180),
            "precision": "unknown",
            "source_field_label": None,
            "raw_quote": None,
        },
        "source_updated_at": source_updated_at,
        "collected_at": collected_at,
    }


def with_listing_evidence(listing, source_updated_at=None, collected_at=None):
    return {
        **listing,
        "rent_comp_evidence_v1": listing_evidence(
            listing, source_updated_at, collected_at
        ),
    }


def replay_evidence(rows, limit=1000):
    """Bounded, pure dry-run projection of saved listing observations."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Replay limit must be 1..1000")
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Select a bounded list of saved observations")
    return [
        listing_evidence(
            row["listing"], row.get("source_updated_at"), row.get("collected_at")
        )
        for row in rows
    ]
