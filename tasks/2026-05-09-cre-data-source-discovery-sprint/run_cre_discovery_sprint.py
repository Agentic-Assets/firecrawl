#!/usr/bin/env python3
"""Run a compact CRE public-source discovery sprint through local Firecrawl."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests


BASE = "http://localhost:3002"
OUT = Path(__file__).resolve().parent
RAW = OUT / "raw"
EXTRACTS = OUT / "SAMPLE_EXTRACTS"
CRAWL_DATE = datetime.now(timezone.utc).isoformat()
UA = "Firecrawl-CRE-Discovery-Sprint/0.1 (+public-source-compliance-review)"


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    category: str
    source_type: str
    expected_signals: str
    geography: str = "mixed"
    asset_types: str = "mixed"


SOURCES = [
    Source("cbre_insights", "https://www.cbre.com/insights/us-quarterly-figures", "brokerage_listing_research", "brokerage research", "market metrics, sector trends, rent, vacancy, cap rates"),
    Source("cushman_marketbeat", "https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/us-capital-markets-marketbeat", "brokerage_listing_research", "brokerage research", "capital markets, investment volume, sector commentary"),
    Source("jll_insights", "https://www.jll.com/en-us/insights", "brokerage_listing_research", "brokerage research", "research articles, market movement, tenant and sector signals"),
    Source("colliers_research", "https://www.colliers.com/en/countries/united-states/commercial-real-estate-research", "brokerage_listing_research", "brokerage research", "market reports, asset class, metro and property type signals"),
    Source("marcus_research", "https://www.marcusmillichap.com/research", "brokerage_listing_research", "brokerage research", "investment forecasts, listings/research teasers"),
    Source("sec_prologis", "https://data.sec.gov/submissions/CIK0001045609.json", "reit_public_company_disclosures", "SEC submissions JSON", "filings, dates, 10-K/10-Q/8-K links"),
    Source("realty_income_pr", "https://www.realtyincome.com/investors/press-releases", "reit_public_company_disclosures", "REIT press releases", "acquisitions, dispositions, earnings, portfolio stats"),
    Source("simon_ir", "https://investors.simon.com/", "reit_public_company_disclosures", "REIT investor relations", "supplements, occupancy, NOI, debt, tenant mix"),
    Source("reit_market_data", "https://www.reit.com/data-research/reit-market-data", "reit_public_company_disclosures", "industry data", "REIT market data, sector metrics"),
    Source("cook_assessor", "https://assessorpropertydetails.cookcountyil.gov/", "county_public_records", "county assessor", "parcel, assessed value, building class"),
    Source("nyc_dob_bis", "https://www.nyc.gov/site/buildings/property-or-business-owner/bis.page", "county_public_records", "permits and violations", "permits, complaints, jobs, violations"),
    Source("la_zoning", "https://planning.lacity.gov/zoning/zoning-search", "county_public_records", "zoning/planning", "zoning constraints, overlays, parcel planning context"),
    Source("oc_auctions", "https://www.ocgov.com/business/bids-auctions-purchasing/property-auctions", "county_public_records", "county auctions", "tax-defaulted property auctions and sale notices"),
    Source("harris_tax_sales", "https://www.hctax.net/Property/TaxSales", "distress_sources", "tax sale", "delinquent tax sales, auction schedule, rules"),
    Source("dallas_foreclosures", "https://www.dallascounty.org/government/county-clerk/foreclosures.php", "distress_sources", "foreclosure notices", "trustee sale notices, posting dates"),
    Source("fdic_asset_sales", "https://www.fdic.gov/asset-sales", "distress_sources", "lender-owned/asset sales", "REO, loan sale events, failed-bank asset sales"),
    Source("omni_cases", "https://omniagentsolutions.com/cases", "distress_sources", "bankruptcy claims agent", "chapter 11 case pages, case status, docket links"),
    Source("prologis_property_search", "https://www.prologis.com/property-search", "owner_operator_tenant_sites", "owner/operator listings", "industrial availability, market, square footage"),
    Source("hines_about", "https://www.hines.com/about", "owner_operator_tenant_sites", "developer/operator", "portfolio scale, markets, asset classes"),
    Source("brookfield_properties", "https://www.brookfieldproperties.com/en.html", "owner_operator_tenant_sites", "owner/operator", "portfolio, asset classes, market exposure"),
    Source("nycedc_docs", "https://edc.nyc/about-nycedc/financial-public-documents-recordings", "market_reports_pdfs_libraries", "public document library", "public PDFs, economic reports, planning and disparity studies"),
    Source("newmark_sv_pdf", "https://www.nmrk.com/storage-nmrk/uploads/fields/pdf-market-reports/2Q24-Silicon-Valley-Office-Market-Report.pdf", "market_reports_pdfs_libraries", "broker market report PDF", "office market fundamentals, vacancy, leasing, rent, market commentary"),
    Source("naiop_research", "https://www.naiop.org/research-and-publications/", "market_reports_pdfs_libraries", "industry research library", "development, capital markets, sentiment reports"),
    Source("census_construction", "https://www.census.gov/construction/", "market_reports_pdfs_libraries", "government market data", "permits, starts, construction spending"),
    Source("fred", "https://fred.stlouisfed.org/", "market_reports_pdfs_libraries", "economic data", "interest rates, construction, employment, rent series"),
    Source("globest", "https://www.globest.com/", "news_press_tenant_signals", "CRE news", "deals, tenant movement, distress, sector news"),
    Source("bisnow", "https://www.bisnow.com/", "news_press_tenant_signals", "CRE news", "leases, sales, development, local market news"),
    Source("jll_capital_markets", "https://www.jll.com/en-us/services/capital-markets/", "industry_directories_capital_markets", "capital markets service page", "brokerage teams, debt/equity services, investment sales signals"),
    Source("colliers_capital_markets", "https://www.colliers.com/en/services/capital-markets", "industry_directories_capital_markets", "capital markets service page", "investment sales, debt advisory, capital markets services"),
    Source("costar", "https://www.costar.com/", "blocked_or_low_value_control", "listing marketplace", "known bot-protected baseline"),
    Source("loopnet", "https://www.loopnet.com/", "blocked_or_low_value_control", "listing marketplace", "known bot-protected baseline"),
]


def load_continuation_sources() -> list[Source]:
    path = OUT / "continuation_sources.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Source(**item) for item in data]


def all_sources() -> list[Source]:
    seen: set[str] = set()
    out: list[Source] = []
    for source in [*SOURCES, *load_continuation_sources()]:
        key = source.id
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return out


BLOCK_PATTERNS = [
    "access denied",
    "captcha",
    "verify you are human",
    "checking your browser",
    "cloudflare",
    "akamai",
    "forbidden",
    "not authorized",
]
LOGIN_PATTERNS = ["sign in", "log in", "create account", "subscribe", "register"]
FIELD_PATTERNS = {
    "address": r"\b\d{2,6}[ \t]+(?:[A-Z][A-Za-z0-9.'-]+[ \t]+){1,6}(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|Way|Place|Pl\.?)\b",
    "asset_type": r"\b(office|industrial|retail|multifamily|hotel|hospitality|mixed-use|self storage|medical office|life sciences|logistics|warehouse|data center)\b",
    "size": r"\b\d[\d,\.]+\s*(?:SF|sq\.?\s*ft\.?|square feet|acres|units)\b",
    "price": r"\$\s?\d[\d,\.]*(?:\s?(?:million|billion|m|bn))?",
    "cap_rate": r"\b\d{1,2}(?:\.\d+)?%\s*(?:cap|capitalization)?\s*rate\b|\bcap rate\b",
    "occupancy": r"\b\d{1,3}(?:\.\d+)?%\s*occup(?:ancy|ied)\b|\boccupancy\b",
    "debt": r"\b(debt maturity|maturity|loan|lender|mortgage|CMBS|financing)\b",
    "distress": r"\b(foreclosure|bankruptcy|receivership|delinquent|tax sale|auction|REO|distressed|loan default|mortgage default)\b",
    "disposition": r"\b(disposition|sale|sold|acquisition|acquired|divest|asset sale|leaseback|sale-leaseback)\b",
    "broker": r"\b(broker|capital markets|investment sales|advisor|advisory)\b",
    "tenant": r"\b(tenant|lease|leased|expansion|relocation|occupier)\b",
}


def post_json(path: str, payload: dict[str, Any], timeout: int = 90) -> tuple[int, dict[str, Any]]:
    r = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"success": False, "error": r.text[:500]}


def get_json(path: str, timeout: int = 60) -> tuple[int, dict[str, Any]]:
    r = requests.get(f"{BASE}{path}", timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"success": False, "error": r.text[:500]}


def robots_result(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "not_checked", "unsupported_scheme"
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        response = requests.get(robots_url, headers={"User-Agent": UA}, timeout=8)
        if response.status_code >= 400:
            return "unknown", f"{robots_url} (status {response.status_code})"
        rp.parse(response.text.splitlines())
        if rp.can_fetch(UA, url) and rp.can_fetch("*", url):
            return "allowed", robots_url
        return "disallowed", robots_url
    except Exception as exc:
        return "unknown", f"{robots_url} ({exc})"


def classify(markdown: str, error: str, robots_status: str) -> tuple[str, str]:
    text = (markdown or "").lower()
    if robots_status == "disallowed":
        return "robots_disallowed", "robots.txt disallows this URL for generic/user-agent access"
    if error:
        err = error.lower()
        if any(p in err for p in BLOCK_PATTERNS):
            return "blocked", error[:180]
        return "error", error[:180]
    if any(p in text for p in BLOCK_PATTERNS):
        return "blocked", "block/CAPTCHA/access-control language detected in scraped content"
    login_hits = [p for p in LOGIN_PATTERNS if p in text]
    if login_hits and len(markdown) < 2500:
        return "login_or_paywall_gated", f"login/paywall terms detected: {', '.join(login_hits)}"
    if not markdown:
        return "empty", "no markdown returned"
    if len(markdown) < 500:
        return "minimal", "very small public text surface"
    if login_hits:
        return "partial_public", f"substantial public text but login/paywall prompts also present: {', '.join(login_hits)}"
    return "approved_public", "public markdown returned without detected access-control stop signal"


def available_fields(markdown: str) -> list[str]:
    found = []
    for field, pat in FIELD_PATTERNS.items():
        if re.search(pat, markdown or "", re.I):
            found.append(field)
    return found


def first_match(markdown: str, pat: str) -> str | None:
    m = re.search(pat, markdown or "", re.I)
    return m.group(0).strip() if m else None


def snippet(markdown: str, fields: list[str]) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (markdown or "").splitlines()]
    candidates = [line for line in lines if len(line) > 40 and any(term in line.lower() for term in ["property", "market", "office", "industrial", "sale", "lease", "report", "filing", "auction", "foreclosure", "tenant", "capital"])]
    if candidates:
        return candidates[0][:500]
    compact = re.sub(r"\s+", " ", markdown or "").strip()
    return compact[:500]


def difficulty(status: str, markdown_len: int, fields: list[str]) -> str:
    if status in {"approved_public", "partial_public"} and markdown_len > 5000 and len(fields) >= 3:
        return "low"
    if status in {"approved_public", "partial_public", "minimal"}:
        return "medium"
    return "high"


def workflow_for(source: Source, status: str, fields: list[str]) -> str:
    if status not in {"approved_public", "partial_public", "minimal"}:
        return "Stop automated collection; document block and use licensed/API/manual alternative."
    if source.category == "reit_public_company_disclosures":
        return "Scrape IR/SEC index, map linked filings/PDFs, parse public docs, schema-extract portfolio/debt/disposition fields with citations."
    if source.category in {"county_public_records", "distress_sources"}:
        return "Prefer official API/download if available; otherwise low-rate scrape public notices/search help pages, then route parcel/detail pages to manual/API-backed enrichment."
    if source.category == "market_reports_pdfs_libraries":
        return "Scrape page links, filter PDF/report URLs, parse documents, extract market/asset/report metadata, table-QA numeric fields."
    if source.category == "brokerage_listing_research":
        return "Use scrape+links for public pages, map research hubs, batch scrape report/listing pages, extract listing/research fields with source citations."
    if source.category == "news_press_tenant_signals":
        return "Search/map public articles conservatively, scrape allowed pages, extract sale/lease/tenant/distress signals, keep headline/source citations."
    if source.category in {"listing_marketplaces", "specialty_asset_marketplaces", "auction_firms"}:
        return "Probe public listing pages lightly; stop on robots/login/CAPTCHA; for production prefer licensed feeds, broker permission, or manual review of public listings."
    if source.category in {"permits_zoning_planning", "municipal_agendas_packets", "economic_development_incentives", "environmental_records"}:
        return "Prefer official agenda/open-data/PDF downloads; scrape public indexes lightly, parse packets/PDFs, extract parcel/project/zoning/incentive evidence."
    if source.category in {"tenant_business_movement", "warn_notices", "franchise_location_data", "public_company_property"}:
        return "Use public notices/company pages/APIs, extract location and closure/opening signals, then corroborate against property records and news."
    if source.category in {"lenders_servicers_receivers", "sale_leaseback_netlease", "pe_real_estate_firms"}:
        return "Scrape public pages for portfolio, REO, sale-leaseback, lender/receiver signals; use as lead context and confirm with filings/records."
    return "Scrape public page, map relevant subpages, extract organization/property/source metadata."


def source_quality(source: Source, status: str, markdown_len: int, fields: list[str]) -> tuple[str, str, str]:
    if status not in {"approved_public", "partial_public", "minimal"}:
        return "low", "unknown", "low"
    freshness = "high" if re.search(r"\b202[5-6]\b|\bQ[1-4]\b|\bMay\b|\bApril\b", source.url, re.I) else "medium"
    if source.geography in {"national", "multi-state", "global"}:
        coverage = "national/multi-market"
    elif source.geography in {"state", "county", "city", "regional"}:
        coverage = source.geography
    else:
        coverage = "national/multi-market" if source.category in {"brokerage_listing_research", "reit_public_company_disclosures", "market_reports_pdfs_libraries", "news_press_tenant_signals"} else "local/source-specific"
    if markdown_len > 8000 and len(fields) >= 4:
        quality = "high"
    elif markdown_len > 1500 and fields:
        quality = "medium"
    else:
        quality = "low"
    return quality, freshness, coverage


def confidence(status: str, fields: list[str], markdown_len: int) -> float:
    if status == "approved_public":
        return min(0.9, 0.45 + 0.05 * len(fields) + min(markdown_len, 10000) / 50000)
    if status == "partial_public":
        return min(0.7, 0.35 + 0.04 * len(fields))
    if status == "minimal":
        return 0.25
    return 0.1


def build_extract(source: Source, markdown: str, status: str, fields: list[str], robots_status: str, robots_url: str) -> dict[str, Any]:
    is_distress_source = source.category == "distress_sources"
    return {
        "property_name": None,
        "address": first_match(markdown, FIELD_PATTERNS["address"]),
        "city": None,
        "state": None,
        "asset_type": first_match(markdown, FIELD_PATTERNS["asset_type"]),
        "size": first_match(markdown, FIELD_PATTERNS["size"]),
        "units": first_match(markdown, r"\b\d[\d,]+\s+units\b"),
        "year_built": first_match(markdown, r"\b(?:built|year built)[:\s]+(18|19|20)\d{2}\b"),
        "owner": None,
        "manager": None,
        "broker": first_match(markdown, FIELD_PATTERNS["broker"]),
        "lender": first_match(markdown, r"\b(?:lender|loan|mortgage|financing|CMBS)\b"),
        "tenant": first_match(markdown, FIELD_PATTERNS["tenant"]),
        "listing_status": first_match(markdown, r"\b(?:for sale|for lease|available|sold|leased|under contract|auction)\b"),
        "asking_price": first_match(markdown, FIELD_PATTERNS["price"]),
        "sale_price": None,
        "cap_rate": first_match(markdown, FIELD_PATTERNS["cap_rate"]),
        "NOI": first_match(markdown, r"\bNOI\b|net operating income"),
        "occupancy": first_match(markdown, FIELD_PATTERNS["occupancy"]),
        "lease_expiration": first_match(markdown, r"\blease expiration\b|\bexpires?\b"),
        "debt_maturity": first_match(markdown, r"\bdebt maturity\b|\bmaturity\b"),
        "distress_signal": first_match(markdown, FIELD_PATTERNS["distress"]) if is_distress_source or re.search(FIELD_PATTERNS["distress"], markdown or "", re.I) else None,
        "disposition_signal": first_match(markdown, FIELD_PATTERNS["disposition"]),
        "source_url": source.url,
        "source_type": source.source_type,
        "document_url": source.url if source.url.lower().endswith(".pdf") else None,
        "extracted_text_snippet": snippet(markdown, fields),
        "confidence_score": round(confidence(status, fields, len(markdown)), 2),
        "crawl_date": CRAWL_DATE,
        "compliance_notes": f"{status}; robots={robots_status}; robots_url={robots_url}; no login/paywall/CAPTCHA bypass attempted.",
    }


def run_source(source: Source) -> dict[str, Any]:
    robots_status, robots_url = robots_result(source.url)
    payload = {
        "url": source.url,
        "formats": ["markdown", "links"],
        "timeout": 45000,
        "blockAds": True,
    }
    method = "v2/scrape markdown+links"
    status_code = None
    data: dict[str, Any] = {}
    markdown = ""
    links: list[str] = []
    error = ""
    if robots_status != "disallowed":
        status_code, response = post_json("/v2/scrape", payload)
        data = response.get("data") or {}
        markdown = data.get("markdown") or ""
        links = data.get("links") or []
        if not response.get("success"):
            error = response.get("error") or json.dumps(response)[:500]
    else:
        error = "robots.txt disallowed"
    access_status, failure_modes = classify(markdown, error, robots_status)
    fields = available_fields(markdown)
    quality, freshness, coverage = source_quality(source, access_status, len(markdown), fields)
    raw_path = RAW / f"{source.id}.json"
    raw = {
        "source": source.__dict__,
        "crawl_date": CRAWL_DATE,
        "robots_status": robots_status,
        "robots_url": robots_url,
        "http_status": status_code,
        "firecrawl_method": method,
        "access_status": access_status,
        "failure_modes": failure_modes,
        "markdown_len": len(markdown),
        "links_len": len(links),
        "available_fields": fields,
        "snippet": snippet(markdown, fields),
        "raw_sha256": hashlib.sha256((markdown or "").encode("utf-8")).hexdigest(),
        "response_keys": sorted(data.keys()),
    }
    raw_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return {
        "id": source.id,
        "url_domain": urlparse(source.url).netloc,
        "url": source.url,
        "source_type": source.source_type,
        "category": source.category,
        "geography": source.geography,
        "asset_types": source.asset_types,
        "cre_relevance": source.expected_signals,
        "access_status": access_status,
        "best_firecrawl_method_settings": method,
        "available_fields": ";".join(fields) or "none_observed",
        "freshness": freshness,
        "coverage": coverage,
        "data_quality": quality,
        "compliance_risk": "low" if access_status == "approved_public" and source.category not in {"county_public_records", "distress_sources"} else "medium" if access_status in {"approved_public", "partial_public", "minimal"} else "high",
        "engineering_difficulty": difficulty(access_status, len(markdown), fields),
        "failure_modes": failure_modes,
        "recommended_production_workflow": workflow_for(source, access_status, fields),
        "markdown_len": len(markdown),
        "links_len": len(links),
        "robots_status": robots_status,
        "robots_url": robots_url,
        "sample_extract_path": "",
        "_markdown": markdown,
        "_fields_list": fields,
    }


def run_method_smokes() -> list[dict[str, Any]]:
    checks = []
    sc, scrape = post_json("/v2/scrape", {"url": "https://www.cbre.com/insights/us-quarterly-figures", "formats": ["markdown", "links"], "timeout": 45000})
    checks.append({"method": "v2/scrape", "status_code": sc, "success": scrape.get("success"), "evidence": f"markdown_len={len((scrape.get('data') or {}).get('markdown') or '')}"})
    sc, search = post_json("/v2/search", {"query": "site:cbre.com CRE market report PDF 2026", "limit": 3, "scrapeOptions": {"formats": ["markdown"]}}, timeout=120)
    checks.append({"method": "v2/search", "status_code": sc, "success": search.get("success"), "evidence": f"results={len(search.get('data') or [])}"})
    sc, mapped = post_json("/v2/map", {"url": "https://www.jll.com/en-us/insights", "limit": 5}, timeout=120)
    checks.append({"method": "v2/map", "status_code": sc, "success": mapped.get("success"), "evidence": f"links={len((mapped.get('links') or (mapped.get('data') or [])))}"})
    sc, batch = post_json("/v2/batch/scrape", {"urls": ["https://www.reit.com/data-research/reit-market-data", "https://fred.stlouisfed.org/"], "formats": ["markdown"]}, timeout=120)
    batch_id = batch.get("id")
    evidence = f"id={batch_id}"
    success = bool(batch.get("success") and batch_id)
    if batch_id:
        for _ in range(12):
            time.sleep(2)
            _, state = get_json(f"/v2/batch/scrape/{batch_id}")
            st = state.get("status") or state.get("data", {}).get("status")
            if st in {"completed", "failed", "cancelled"}:
                evidence = f"id={batch_id}; status={st}; completed={state.get('completed')}"
                break
    checks.append({"method": "v2/batch/scrape", "status_code": sc, "success": success, "evidence": evidence})
    sc, crawl = post_json("/v2/crawl", {"url": "https://www.census.gov/construction/", "limit": 2, "scrapeOptions": {"formats": ["markdown"]}}, timeout=120)
    crawl_id = crawl.get("id")
    evidence = f"id={crawl_id}"
    success = bool(crawl.get("success") and crawl_id)
    if crawl_id:
        for _ in range(12):
            time.sleep(2)
            _, state = get_json(f"/v2/crawl/{crawl_id}")
            st = state.get("status") or state.get("data", {}).get("status")
            if st in {"completed", "failed", "cancelled"}:
                evidence = f"id={crawl_id}; status={st}; data_items={len(state.get('data') or [])}"
                break
    checks.append({"method": "v2/crawl", "status_code": sc, "success": success, "evidence": evidence})
    extract_payload = {
        "urls": ["https://www.jll.com/en-us/services/capital-markets/"],
        "prompt": "Extract high-level CRE capital markets service signals from this public page.",
        "schema": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string"},
                "asset_types": {"type": "array", "items": {"type": "string"}},
                "services": {"type": "array", "items": {"type": "string"}},
                "origination_signals": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["source_type", "asset_types", "services", "origination_signals"],
        },
        "enableWebSearch": False,
    }
    sc, extract = post_json("/v2/extract", extract_payload, timeout=120)
    extract_id = extract.get("id")
    evidence = f"id={extract_id}"
    success = bool(extract.get("success") and extract_id)
    if extract_id:
        for _ in range(18):
            time.sleep(3)
            _, state = get_json(f"/v2/extract/{extract_id}", timeout=120)
            st = state.get("status") or state.get("data", {}).get("status")
            if st in {"completed", "failed", "cancelled"}:
                data = state.get("data") or []
                evidence = f"id={extract_id}; status={st}; data_items={len(data) if isinstance(data, list) else 1}"
                (RAW / "jll_capital_markets_v2_extract.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
                break
    checks.append({"method": "v2/extract", "status_code": sc, "success": success, "evidence": evidence})
    return checks


def maybe_parse_pdf() -> dict[str, Any]:
    candidates = [
        "https://www.nmrk.com/storage-nmrk/uploads/fields/pdf-market-reports/2Q24-Silicon-Valley-Office-Market-Report.pdf",
        "https://www.fdic.gov/resources/resolutions/asset-sales/documents/owned-real-estate-brochure.pdf",
        "https://edc.nyc/sites/default/files/2026-04/NYCEDC-CRE-Disparity-Study-2025.pdf",
    ]
    out = {"method": "v2/parse PDF", "url": candidates[0], "success": False, "evidence": "not attempted"}
    url = candidates[0]
    robots_status = "unknown"
    robots_url = ""
    for candidate in candidates:
        robots_status, robots_url = robots_result(candidate)
        if robots_status != "disallowed":
            url = candidate
            break
    out["url"] = url
    out["robots_status"] = robots_status
    out["robots_url"] = robots_url
    if robots_status == "disallowed":
        out["evidence"] = "all PDF candidates robots disallowed"
        return out
    try:
        pdf = requests.get(url, headers={"User-Agent": UA}, timeout=90)
        pdf.raise_for_status()
        pdf_path = RAW / Path(urlparse(url).path).name
        pdf_path.write_bytes(pdf.content)
        with pdf_path.open("rb") as fh:
            r = requests.post(
                f"{BASE}/v2/parse",
                files={"file": (pdf_path.name, fh, "application/pdf")},
                data={"options": json.dumps({"formats": ["markdown"]})},
                timeout=180,
            )
        j = r.json()
        md = (j.get("data") or {}).get("markdown", "")
        out.update({"success": bool(j.get("success")), "status_code": r.status_code, "evidence": f"markdown_len={len(md)}; downloaded_bytes={len(pdf.content)}"})
        (RAW / "nycedc_pdf_parse.json").write_text(json.dumps({"parse": out, "snippet": snippet(md, [])}, indent=2), encoding="utf-8")
    except Exception as exc:
        out["evidence"] = str(exc)[:300]
    return out


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    EXTRACTS.mkdir(parents=True, exist_ok=True)
    for stale in EXTRACTS.glob("*.json"):
        stale.unlink()
    rows = []
    sources = all_sources()
    for i, source in enumerate(sources, start=1):
        print(f"[{i}/{len(sources)}] {source.id} {source.url}", flush=True)
        row = run_source(source)
        rows.append(row)
        time.sleep(1.0)

    preferred_extract_ids = [
        "jll_insights",
        "newmark_sv_pdf",
        "jll_capital_markets",
        "dallas_foreclosures",
        "harris_tax_sales",
        "bisnow",
        "simon_ir",
        "cushman_marketbeat",
        "marcus_research",
        "omni_cases",
    ]
    rows_by_id = {row["id"]: row for row in rows}
    extract_candidates = [
        rows_by_id[source_id]
        for source_id in preferred_extract_ids
        if source_id in rows_by_id
        and rows_by_id[source_id]["access_status"] in {"approved_public", "partial_public", "minimal"}
        and rows_by_id[source_id]["markdown_len"] >= 500
    ]
    represented_categories = {row["category"] for row in extract_candidates}
    broad_candidates = [
        row for row in rows
        if row["access_status"] in {"approved_public", "partial_public", "minimal"}
        and row["markdown_len"] >= 1000
        and row["id"] not in {r["id"] for r in extract_candidates}
    ]
    broad_candidates.sort(key=lambda r: (r["category"] not in represented_categories, len(r["_fields_list"]), r["markdown_len"]), reverse=True)
    for row in broad_candidates:
        if len(extract_candidates) >= 16:
            break
        if row["category"] in represented_categories and len(extract_candidates) >= 12:
            continue
        extract_candidates.append(row)
        represented_categories.add(row["category"])

    for row in extract_candidates[:16]:
        source = next(s for s in SOURCES if s.id == row["id"])
        extract = build_extract(source, row["_markdown"], row["access_status"], row["_fields_list"], row["robots_status"], row["robots_url"])
        path = EXTRACTS / f"{row['id']}.json"
        path.write_text(json.dumps(extract, indent=2), encoding="utf-8")
        row["sample_extract_path"] = str(path.relative_to(OUT))

    method_checks = run_method_smokes()
    method_checks.append(maybe_parse_pdf())
    (OUT / "method_test_results.json").write_text(json.dumps({"crawl_date": CRAWL_DATE, "checks": method_checks}, indent=2), encoding="utf-8")

    csv_fields = [
        "url_domain",
        "url",
        "source_type",
        "category",
        "geography",
        "asset_types",
        "cre_relevance",
        "access_status",
        "best_firecrawl_method_settings",
        "available_fields",
        "freshness",
        "coverage",
        "data_quality",
        "compliance_risk",
        "engineering_difficulty",
        "failure_modes",
        "recommended_production_workflow",
        "markdown_len",
        "links_len",
        "robots_status",
        "robots_url",
        "sample_extract_path",
    ]
    with (OUT / "SOURCE_TEST_RESULTS.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            public = {k: row[k] for k in csv_fields}
            writer.writerow(public)

    summary = {
        "crawl_date": CRAWL_DATE,
        "sources_tested": len(rows),
        "categories_tested": sorted({r["category"] for r in rows if r["category"] != "blocked_or_low_value_control"}),
        "access_counts": {},
        "sample_extract_count": len(list(EXTRACTS.glob("*.json"))),
        "method_checks": method_checks,
    }
    for row in rows:
        summary["access_counts"][row["access_status"]] = summary["access_counts"].get(row["access_status"], 0) + 1
    (OUT / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
