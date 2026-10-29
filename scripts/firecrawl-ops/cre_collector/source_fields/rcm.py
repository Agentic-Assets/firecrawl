"""Source-qualified RCM field staging, separate from source authentication.

Legacy artifacts keep their existing normalization. New source witnesses bind
currency to the exact displayed value; contact markers prevent invented company
affiliation and unstable child identities without discarding their source facts.
"""
import json

CONTACT_SOURCES = {
    "cbre-dealflow": "cbre_dealflow_rcm",
    "colliers": "colliers_sales_tracker",
}


def source_contact(source_key, contact):
    marker = CONTACT_SOURCES.get(source_key)
    return bool(marker and isinstance(contact, dict) and contact.get("rcmContactSource") == marker)


def permits_psf_inference(source_key, listing, price_text):
    """Allow the legacy fallback or an exactly bound new CBRE USD witness."""
    facts = listing.get("extraFacts")
    context = facts.get("cbre_dealflow_reported_price") if isinstance(facts, dict) else None
    if source_key != "cbre-dealflow" or not isinstance(context, dict):
        return True
    encoded = context.get("reportedTextJson")
    if not isinstance(encoded, str) or len(encoded) > 8192:
        return False
    try:
        reported = json.loads(encoded)
    except (ValueError, RecursionError):
        return False
    if not isinstance(reported, str):
        return False
    symbol, currency = context.get("symbol"), context.get("currency")
    prefix = symbol if isinstance(symbol, str) else ""
    expected = "USD " + (prefix if not reported.startswith(prefix) else "") + reported
    return (context.get("source") == "cbre_dealflow_rcm"
            and context.get("source_field") == "projectfields.value"
            and expected == price_text and context.get("currencyBasis") == "source_currency_usd"
            and isinstance(currency, str) and currency.strip().upper() == "USD"
            and (symbol is None or (isinstance(symbol, str) and symbol.strip().upper() in ("$", "US$", "USD"))))
