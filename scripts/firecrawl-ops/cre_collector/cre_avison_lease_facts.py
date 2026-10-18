"""Avison-owned rent normalization from attributed literal detail observations."""

import copy
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from cre_parse import _lease_rate_type, parse_lease_rate

KEY = "avison_lease_facts"
SCHEMA = "avison-lease-facts-v1"
_RATE = re.compile(
    r"^(USD|US\$|\$|CAD|C\$|AUD|A\$|EUR|€|£)?\s*"
    r"(\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(USD|US\$|\$|CAD|C\$|AUD|A\$|EUR|€|£)?\s*(\d+(?:,\d{3})*(?:\.\d+)?))?"
    r"\s*/\s*(SF|SQFT)\s*/\s*(MONTH|MO|YEAR|YR)$",
    re.I,
)


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else None


def _leaves(raw):
    if not isinstance(raw, dict):
        return
    if "primary" in raw or "secondary_pass" in raw:
        yield from _leaves(raw.get("primary"))
        yield from _leaves(raw.get("secondary_pass"))
    else:
        yield raw


def _owned_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (
            parsed.hostname in {"avisonyoung.us", "www.avisonyoung.us"}
            or (parsed.hostname or "").endswith(".sharplaunch.com")
        )
    )


def _clock(value):
    try:
        return (
            isinstance(value, str)
            and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset()
            is not None
        )
    except ValueError:
        return False


def _facts(leaf, external_id):
    facts = (leaf.get("extraFacts") or {}).get(KEY)
    if facts is None:
        return None
    if not isinstance(facts, dict) or facts.get("schema") != SCHEMA:
        raise ValueError("Avison lease facts schema is invalid")
    feed = leaf.get("rawSharpLaunch") or {}
    if (
        leaf.get("sourceKey") != "avison-young"
        or str(leaf.get("id")) != external_id
        or str(facts.get("external_id")) != external_id
        or str(feed.get("id")) != external_id
        or facts.get("listing_url") != leaf.get("url")
        or facts.get("listing_url")
        != (_text(feed.get("external_url")) or _text(feed.get("url")))
        or not _owned_url(facts.get("listing_url"))
    ):
        raise ValueError("Avison lease facts identity differs")
    if not isinstance(facts.get("pages"), list):
        raise ValueError("Avison lease pages are invalid")
    for page in facts["pages"]:
        if (
            not isinstance(page, dict)
            or page.get("role") not in {"availability", "overview"}
            or not _owned_url(page.get("source_url"))
            or page["source_url"]
            not in (leaf.get("detailScrape") or {}).get("urls", [])
            or page["source_url"]
            not in {
                leaf.get("url"),
                leaf.get("externalUrl"),
                leaf.get("sharpLaunchUrl"),
            }
            or page.get("observed_at") != leaf.get("detailObservedAt")
            or not _clock(page.get("observed_at"))
            or not isinstance(page.get("observations"), list)
        ):
            raise ValueError("Avison lease page provenance is invalid")
        for observation in page["observations"]:
            if not isinstance(observation, dict):
                raise ValueError("Avison lease observation is invalid")
            for field in (
                "suite_text",
                "status_text",
                "rent_text",
                "area_text",
                "lease_type_text",
            ):
                if field not in observation or (
                    observation[field] is not None
                    and not isinstance(observation[field], str)
                ):
                    raise ValueError("Avison lease literal fact is invalid")
            if not isinstance(observation.get("unparsed"), list) or any(
                not isinstance(text, str) for text in observation["unparsed"]
            ):
                raise ValueError("Avison lease unparsed facts are invalid")
            if not isinstance(observation.get("fields"), list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("label"), str)
                or (
                    item.get("value") is not None
                    and not isinstance(item.get("value"), str)
                )
                for item in observation["fields"]
            ):
                raise ValueError("Avison lease labelled facts are invalid")
    return facts


def _rent_texts(observation):
    texts = [observation.get("rent_text")]
    texts.extend(
        item.get("value")
        for item in observation.get("fields", [])
        if _text(item.get("label", "")).rstrip(":").casefold()
        in {"rent", "rental rate", "lease rate"}
    )
    return list(dict.fromkeys(_text(text) for text in texts if _text(text)))


def _rate(observation, feed):
    texts = _rent_texts(observation)
    if len(texts) > 1:
        parsed = [
            _rate({**observation, "rent_text": text, "fields": []}, feed)
            for text in texts
        ]
        blocked = {
            "foreign_currency",
            "conflicting_currency_context",
            "absolute_monthly_amount",
            "incompatible_area_basis",
            "reversed_monetary_range",
        }
        if any(reason in blocked for _, reason in parsed) or any(
            value is None and text.casefold() != "contact broker"
            for text, (value, _) in zip(texts, parsed)
        ):
            return None, "conflicting_rent_claims"
        supported = [value for value, _ in parsed if value is not None]
        signatures = {
            tuple(
                value[key]
                for key in (
                    "annual_psf_min",
                    "annual_psf_max",
                    "period",
                    "currency_code",
                    "lease_type",
                )
            )
            for value in supported
        }
        if len(signatures) > 1:
            return None, "conflicting_rent_claims"
        return (
            (supported[0], None) if supported else (None, "unknown_units_or_currency")
        )
    text = texts[0] if texts else None
    if not text:
        return None, "unpriced"
    if feed.get("sale_currency") not in {None, "USD"} or str(
        feed.get("country", "")
    ).casefold() not in {"", "us", "united states"}:
        return None, "conflicting_currency_context"
    if re.search(r"\b(?:CAD|AUD|EUR|GBP)\b|[€£¥]|[CA]\$", text, re.I):
        return None, "foreign_currency"
    if re.search(r"/(?:MONTH|MO)\b", text, re.I) and not re.search(
        r"/(?:SF|SQFT)/", text, re.I
    ):
        return None, "absolute_monthly_amount"
    if re.search(r"/(?:ACRE|ACRES|SQM|M2)\b", text, re.I):
        return None, "incompatible_area_basis"
    match = _RATE.fullmatch(text)
    if not match:
        return None, "unknown_units_or_currency"
    symbol, amount, second_symbol, maximum, _, period = match.groups()
    if maximum and float(maximum.replace(",", "")) < float(amount.replace(",", "")):
        return None, "reversed_monetary_range"
    symbols = {s.upper() for s in (symbol, second_symbol) if s}
    if symbols - {"USD", "US$", "$"}:
        return None, "foreign_currency"
    if not symbols:
        return None, "unknown_currency"
    explicit = bool(symbols & {"USD", "US$"})
    if not explicit and not (
        str(feed.get("country", "")).casefold() in {"us", "united states"}
        and feed.get("sale_currency") == "USD"
    ):
        return None, "unknown_currency_context"
    if explicit and feed.get("sale_currency") not in {None, "USD"}:
        return None, "conflicting_currency_context"
    period = "month" if period.upper() in {"MONTH", "MO"} else "year"
    raw_amount = f"${amount}" + (f" - ${maximum}" if maximum else "")
    minimum, high, _ = parse_lease_rate(f"{raw_amount}/SF/{period}")
    lease_type = _lease_rate_type(
        (_text(observation.get("lease_type_text")) or "").lower()
    )
    if minimum is None:
        return None, "outside_canonical_rate_bounds"
    return {
        "annual_psf_min": minimum,
        "annual_psf_max": high,
        "period": period,
        "area_basis": "sf",
        "currency_code": "USD",
        "currency_evidence": "explicit" if explicit else "inferred_us_source_context",
        "lease_type": lease_type,
    }, None


def project_row(row):
    """Recompute only Avison lease fields from flat or merged raw source facts."""
    if row.get("slug") != "avison-young":
        return row
    output = dict(row)
    external_id = str(row.get("external_id"))
    sources = [
        (leaf, _facts(leaf, external_id)) for leaf in _leaves(row.get("raw_data"))
    ]
    if not any(facts for _, facts in sources):
        output.update(lease_rate_min=None, lease_rate_max=None, lease_rate_type=None)
        return output
    pages, records, excluded = [], [], []
    normalized_records, seen_normalized = [], set()
    seen_pages = set()
    groups = {}
    unnamed = []
    available = unpriced = ambiguous = 0
    overview = []
    observed_periods = set()
    known_types = set()
    for leaf, facts in sources:
        if facts is None:
            continue
        feed = leaf.get("rawSharpLaunch") or {}
        for page in facts["pages"]:
            page_key = json.dumps(page, sort_keys=True)
            if page_key not in seen_pages:
                pages.append(copy.deepcopy(page))
                seen_pages.add(page_key)
            for observation in page["observations"]:
                if page["role"] == "overview":
                    for text in _rent_texts(observation):
                        if text.casefold() != "contact broker":
                            overview.append(
                                _rate(
                                    {**observation, "rent_text": text, "fields": []},
                                    feed,
                                )[0]
                            )
                    continue
                suite = _text(observation.get("suite_text"))
                if _text(observation.get("status_text")) == "Available" and suite:
                    for text in _rent_texts(observation):
                        normalized, _ = _rate(
                            {**observation, "rent_text": text, "fields": []}, feed
                        )
                        if normalized:
                            record = {
                                "source_url": page["source_url"],
                                "observed_at": page["observed_at"],
                                "suite_text": observation["suite_text"],
                                "rent_text": text,
                                **normalized,
                            }
                            signature = json.dumps(record, sort_keys=True)
                            if signature not in seen_normalized:
                                normalized_records.append(record)
                                seen_normalized.add(signature)
                item = (page, observation, feed)
                if suite:
                    groups.setdefault(suite.casefold(), []).append(item)
                elif item not in unnamed:
                    unnamed.append(item)
    # An unnamed card is retained, but cannot impersonate an identified suite.
    for items in [*groups.values(), *([item] for item in unnamed)]:
        active = [
            item for item in items if _text(item[1].get("status_text")) == "Available"
        ]
        if not active:
            excluded.append("not_currently_available")
            continue
        available += 1
        if any(item[1]["unparsed"] for item in active):
            excluded.append("unparsed_availability_observations")
        for _, observation, _ in active:
            lease_type = _lease_rate_type(
                (_text(observation.get("lease_type_text")) or "").lower()
            )
            if lease_type:
                known_types.add(lease_type)
            period = re.search(
                r"/(?:SF|SQFT)\s*/\s*(MONTH|MO|YEAR|YR|WEEK|WK|DAY)\b",
                observation.get("rent_text") or "",
                re.I,
            )
            if period:
                token = period[1].upper()
                observed_periods.add(
                    "month"
                    if token in {"MONTH", "MO"}
                    else "year"
                    if token in {"YEAR", "YR"}
                    else token.lower()
                )
        claims = {
            json.dumps(
                {
                    k: item[1].get(k)
                    for k in (
                        "status_text",
                        "rent_text",
                        "area_text",
                        "lease_type_text",
                    )
                },
                sort_keys=True,
            )
            for item in items
        }
        if len(claims) != 1:
            ambiguous += 1
            excluded.append("conflicting_suite_observations")
            continue
        page, observation, feed = active[0]
        if not _rent_texts(observation):
            if observation["unparsed"]:
                ambiguous += 1
            else:
                unpriced += 1
            continue
        normalized, reason = _rate(observation, feed)
        if normalized is None or not _text(observation.get("suite_text")):
            ambiguous += 1
            excluded.append(reason or "unknown_suite_identity")
            continue
        # Currency context is checked independently on every attributed route.
        if any(_rate(item[1], item[2]) != (normalized, None) for item in active):
            ambiguous += 1
            excluded.append("conflicting_currency_context")
            continue
        records.append(
            {
                "source_url": page["source_url"],
                "observed_at": page["observed_at"],
                "suite_text": observation["suite_text"],
                **normalized,
            }
        )
    rates = [
        (item["annual_psf_min"], item["annual_psf_max"] or item["annual_psf_min"])
        for item in records
    ]
    periods = {item["period"] for item in records}
    reason = None
    if len(periods | observed_periods) > 1:
        reason = "mixed_original_periods"
    elif len(known_types) > 1:
        reason = "conflicting_lease_types"
    elif any(
        x in excluded
        for x in (
            "conflicting_suite_observations",
            "unparsed_availability_observations",
            "foreign_currency",
            "conflicting_currency_context",
            "absolute_monthly_amount",
            "conflicting_rent_claims",
            "incompatible_area_basis",
        )
    ):
        reason = "conflicting_or_unparsed_observations"
    elif overview and any(
        value is None
        or (value["annual_psf_min"], value["annual_psf_max"] or value["annual_psf_min"])
        not in rates
        for value in overview
    ):
        reason = "overview_suite_conflict"
    elif not records:
        reason = "no_supported_priced_available_observations"
    minimum = min((lo for lo, _ in rates), default=None) if reason is None else None
    maximum = max((hi for _, hi in rates), default=None) if reason is None else None
    output["lease_rate_min"] = minimum
    output["lease_rate_max"] = maximum if maximum != minimum else None
    output["lease_rate_type"] = (
        next(iter(known_types)) if len(known_types) == 1 else None
    )
    projection = {
        "coverage": "priced_available_subset",
        "available_count": available,
        "priced_available_count": len(records),
        "unpriced_available_count": unpriced,
        "ambiguous_available_count": ambiguous,
        "excluded_reasons": sorted(set(excluded)),
        "reason": reason,
    }
    namespace = {
        "schema": SCHEMA,
        "external_id": external_id,
        "listing_url": next(
            (facts["listing_url"] for _, facts in sources if facts), None
        ),
        "pages": pages,
        "normalized_observations": normalized_records,
        "projection": projection,
    }
    output["extra_facts"] = {**(output.get("extra_facts") or {}), KEY: namespace}
    return output
