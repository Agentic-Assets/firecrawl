"""Avison-only current asking visibility; no lifecycle or collection authority."""

import copy
import math
import re
from decimal import Decimal

SOURCE = "avison-young"
SCHEMA = "avison-asking-price-v1"
CONTROLS = {"absent", "visible", "withheld"}
ASK_KEYS = set(
    "askingamount askingprice saleprice salepricepersf salepricetext salepriceusd "
    "saleunitprice ".split()
)
PROTECTED = set(
    "address avatarurl bodysha256 canonicalurl city detailobservedat email embedurl "
    "externalid finishedat generationid generationstartedat id inventoryobservedat "
    "latitude license longitude observedat phone postalcode profileurl sha256 "
    "sourcefeedurl sourcekey startedat state street url validatedat vcardurl zip ".split()
)
PROSE = set(
    "amenities caption description headline hidepricetext highlights label markdown "
    "note summary title ".split()
)
INDEPENDENT = set(
    "availabilitiesmaxrent availabilitiesminrent caprate capratepct expenses grm "
    "grossrentmult grossrentmultiplier grossrevenue lease leaseratemax leaseratemin "
    "leaseratetext leaseratetype netoperatingincome noi operatingexpenses rent revpar "
    "salecurrency tax taxes ".split()
)
ASK_LABEL = r"\b(?:asking(?:\s+price)?|sale\s+price|purchase\s+price|offered\s+at|for\s+sale\s+at)\b"
OTHER_LABEL = r"\b(?:rent|lease\s+rate|noi|net\s+operating\s+income|cap\s+rate|grm|tax(?:es)?|operating\s+expenses)\b"
LABEL = f"(?:{ASK_LABEL}|{OTHER_LABEL})"
LEASE_BASIS = r"/\s*sf\s*/\s*(?:yr|year|mo|month)\b"
MONEY = (
    r"(?:[$€£¥]\s*\d|\b(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF)\s*\d|"
    r"\b\d[\d,.]*\s*(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?)\b|"
    r"\b\d[\d,.]*\s*(?:million|thousand)\b"
    r"(?!\s*(?:SF\b|sq\.?\s*(?:ft|feet|meters?)\b|acres?\b|ac\b|ft\b|feet\b|years?\b|(?:parking\s+)?spaces?\b|units?\b|rooms?\b|doors?\b)))"
)

PROTECTED_TEXT = r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+|\b\d{3}[- .]\d{3}[- .]\d{4}\b|\b\d+(?:\.\d+)?\s*(?:(?:million|thousand|[kKmM])\s*)?(?:%|SF\b|sq\.?\s*(?:ft|feet|meters?)\b|acres?\b|ac\b|feet\b|ft\b|years?\b|(?:parking\s+)?spaces?\b|units?\b|rooms?\b|doors?\b)"
PROTECTED_TEXT += r"|\b(?:built|since|year|replaced)\s+\d{4}\b|\b\d+\s+\w+(?:\s+\w+)?\s+(?:St(?:reet)?|Rd|Road|Ave(?:nue)?|Blvd|Drive|Dr)\b"


def _key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _tokens(value):
    return (
        re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value))
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def asking_control(feed):
    if not isinstance(feed, dict):
        raise ValueError("Avison asking control requires a feed object")
    aliases = [key for key in feed if _key(key) == "hideprice"]
    if not aliases:
        return "absent"
    if aliases != ["hide_price"]:
        raise ValueError("Avison asking control key is ambiguous")
    value = feed["hide_price"]
    if type(value) not in (int, float) or value not in (0, 1):
        raise ValueError("Avison asking visibility has malformed raw control")
    return "withheld" if value == 1 else "visible"


def _direct(raw):
    if not isinstance(raw, dict):
        return []
    if "sourceKey" not in raw and not {"primary", "secondary_pass"} <= raw.keys():
        return []
    leaves = []
    if "sourceKey" in raw:
        if raw["sourceKey"] != SOURCE:
            return []
        leaves.append(raw)
    for key in ("primary", "secondary_pass"):
        if key in raw:
            children = _direct(raw[key])
            if not children:
                return []
            leaves.extend(children)
    return leaves


def _control(raw):
    value = asking_control(raw.get("rawSharpLaunch", {}))
    current = raw.get("latestInventoryObservation")
    effective = value
    if current is not None:
        if not isinstance(current, dict) or not _direct(current):
            raise ValueError(
                "Avison current inventory visibility has foreign provenance"
            )
        effective = effective_control(current)
    if "avisonPriceVisibility" in raw:
        marker = raw["avisonPriceVisibility"]
        if (
            not isinstance(marker, dict)
            or set(marker) != {"schema", "control"}
            or marker.get("schema") != SCHEMA
            or not isinstance(marker.get("control"), str)
            or marker.get("control") not in {value, effective}
        ):
            raise ValueError(
                "Avison asking visibility envelope contradicts raw control"
            )
    return effective


def effective_control(raw):
    leaves = _direct(raw)
    if not leaves:
        raise ValueError(
            "Avison asking visibility needs canonical direct source provenance"
        )
    controls = [_control(item) for item in leaves]
    control = (
        "withheld"
        if "withheld" in controls
        else "visible"
        if "visible" in controls
        else "absent"
    )
    if "sourceKey" not in raw and "avisonPriceVisibility" in raw:
        marker = raw["avisonPriceVisibility"]
        if (
            not isinstance(marker, dict)
            or set(marker) != {"schema", "control"}
            or marker.get("schema") != SCHEMA
            or marker.get("control") != control
        ):
            raise ValueError(
                "Avison merged visibility envelope contradicts direct passes"
            )
    return control


def asking_amounts(value):
    result = []
    if isinstance(value, list):
        for item in value:
            result.extend(asking_amounts(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if _key(key) in ASK_KEYS | {"hidepricetext"} and (
                type(item) in (int, float) or isinstance(item, str)
            ):
                try:
                    number = (
                        float(item.replace(",", "").replace("$", ""))
                        if isinstance(item, str)
                        else item
                    )
                except ValueError:
                    number = 0
                if math.isfinite(number) and number > 0:
                    result.append(number)
            result.extend(asking_amounts(item))
    return sorted(set(result))


def _amount_pattern(amounts):
    variants = []
    for amount in amounts:
        number = Decimal(str(amount))
        for scale, suffix in [
            (1, ""),
            (1000, "[kK]"),
            (1000000, "(?:[mM]|[ ]+million)"),
        ]:
            text = (
                format(number / scale, "f").rstrip("0").rstrip(".")
                if "." in format(number / scale, "f")
                else format(number / scale, "f")
            )
            variants.append(
                re.escape(text) + (r"(?:\.0+)?" if "." not in text else "0*") + suffix
            )
        grouped = format(number, ",f")
        if "." in grouped:
            grouped = grouped.rstrip("0").rstrip(".")
        variants.append(re.escape(grouped) + ("0*" if "." in grouped else r"(?:\.0+)?"))
    return (
        r"(?<![\w.])(?<!\d,)(?:" + "|".join(variants) + r")(?![\w.]|,\d)"
        if variants
        else r"(?!)"
    )


def sanitize_text(value, amounts=(), role="narrative"):
    if not isinstance(value, str):
        return value
    spans = []

    def protect(match):
        spans.append(match.group(0))
        return chr(0xE000 + len(spans))

    def link(match):
        label = sanitize_text(match.group(1), amounts, "child-title")
        spans.append(f"[{label}]({match.group(2)})")
        return chr(0xE000 + len(spans))

    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", link, value)
    text = re.sub(PROTECTED_TEXT, protect, text, flags=re.I)

    pieces = re.split(r"(\n|;\s*|[.!?](?=\s|$)\s*|•\s*)", text)
    kept = []
    for i in range(0, len(pieces), 2):
        clause = pieces[i]
        delimiter = pieces[i + 1] if i + 1 < len(pieces) else ""
        parts = re.split(f"(?={LABEL})", clause, flags=re.I)
        safe = []
        for part in parts:
            part = re.sub(r"\s+and\s*$", "", part.strip(), flags=re.I)
            if not part:
                continue
            independent = (
                re.match(OTHER_LABEL, part, re.I)
                or (
                    re.search(LEASE_BASIS, part, re.I)
                    and (
                        not re.search(ASK_LABEL, part, re.I)
                        or re.match(r"offered\s+at\b", part, re.I)
                    )
                )
                or (role == "independent" and not re.search(ASK_LABEL, part, re.I))
            )
            asking = re.search(ASK_LABEL, part, re.I)
            disclosure = re.search(MONEY, part, re.I) or (
                role not in {"unknown", "identity", "identity-caption"}
                and re.search(_amount_pattern(amounts), part)
            )
            if independent or (not asking and not disclosure):
                safe.append(part)
        joined = " ".join(safe).rstrip(" -–—")
        if joined:
            kept.append(joined + delimiter)
    result = "".join(kept).strip().rstrip("; ")
    for i, original in enumerate(spans, 1):
        result = result.replace(chr(0xE000 + i), original)
    return result


def _context(value, context):
    if not isinstance(value, dict):
        return context
    function = value.get("businessFunction")
    if function in {"sell", "sale"}:
        return "asking"
    if function in {"lease", "rent"}:
        return "lease"
    if function is not None and "lease" in str(function) and "sale" in str(function):
        return "ambiguous"
    if context == "lease":
        return context
    if any(
        isinstance(v, str) and re.search(ASK_LABEL, v, re.I)
        for k, v in value.items()
        if _key(k) in {"note", "label"}
    ):
        return "asking"
    return context


def _lease_fact_pairs(value, amounts):
    """Sanitize the new literal label/value pairs atomically, before role loss."""
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result
    for page in result.get("pages", []):
        if not isinstance(page, dict):
            continue
        for observation in page.get("observations", []):
            if not isinstance(observation, dict) or not isinstance(observation.get("fields"), list):
                continue
            for key in ("suite_text", "status_text", "rent_text", "area_text", "lease_type_text"):
                text = observation.get(key)
                if isinstance(text, str):
                    role = "independent" if key == "rent_text" else "identity-caption" if key in {"suite_text", "area_text"} else "narrative"
                    if sanitize_text(text, amounts, role) != text:
                        observation[key] = None
            if isinstance(observation.get("unparsed"), list):
                observation["unparsed"] = [
                    sanitize_text(text, amounts) if isinstance(text, str) else text
                    for text in observation["unparsed"]
                ]
            fields = []
            for pair in observation["fields"]:
                if (not isinstance(pair, dict) or not isinstance(pair.get("label"), str)
                        or (pair.get("value") is not None and not isinstance(pair.get("value"), str))):
                    fields.append(pair)  # The strict source consumer still refuses malformed input.
                    continue
                label, text = pair["label"], pair.get("value")
                if re.search(ASK_LABEL, label, re.I):
                    continue
                if re.search(OTHER_LABEL, label, re.I) or re.search(r"\brental\s+rate\b", label, re.I):
                    if isinstance(text, str) and re.search(ASK_LABEL, text, re.I):
                        continue
                elif text is not None and sanitize_text(str(text), amounts) != str(text):
                    continue
                fields.append(pair)
            observation["fields"] = fields
    return result


def project_value(value, amounts=(), context="", role="raw"):
    if isinstance(value, list):
        return [project_value(item, amounts, context, role) for item in value]
    if not isinstance(value, dict):
        if isinstance(value, str):
            if (
                role == "unknown"
                and not context
                and not re.search(f"{ASK_LABEL}|{MONEY}", value, re.I)
            ):
                return value
            return sanitize_text(
                value, amounts, "independent" if context == "lease" else role
            )
        return value
    context = _context(value, context)
    projected = {}
    for key, item in value.items():
        normalized = _key(key)
        tokens = _tokens(key)
        if normalized == "avisonleasefacts":
            item = _lease_fact_pairs(item, amounts)
        if normalized == "avisonpricevisibility":
            if (
                not isinstance(item, dict)
                or set(item) != {"schema", "control"}
                or item.get("schema") != SCHEMA
                or not isinstance(item.get("control"), str)
                or item.get("control") not in CONTROLS
            ):
                raise ValueError("Avison asking marker is invalid")
            projected[key] = copy.deepcopy(item)
            continue
        if (
            normalized in PROTECTED
            or normalized == "hideprice"
            or (
                isinstance(item, str)
                and tokens[-1:] in (["url"], ["href"], ["src"])
                and re.match(r"^https?://", item, re.I)
            )
        ):
            projected[key] = copy.deepcopy(item)
            continue
        if normalized in INDEPENDENT:
            projected[key] = (
                sanitize_text(item, amounts, "independent")
                if isinstance(item, str)
                else project_value(item, amounts, "lease")
            )
            continue
        if (
            normalized in ASK_KEYS
            or "asking" in tokens
            or ("sale" in tokens and ("price" in tokens or "amount" in tokens))
        ):
            if isinstance(item, (dict, list)):
                projected[key] = project_value(item, amounts, "asking")
            continue
        if normalized == "hidepricetext" and not isinstance(item, str):
            continue
        child_context = (
            "lease"
            if "lease" in tokens or "rent" in tokens
            else "pricing"
            if normalized in {"pricing", "pricespecification", "offers"}
            else "asking"
            if normalized in {"sale", "saleoffer"}
            else context
        )
        if normalized in {
            "price",
            "amount",
            "value",
            "priceperunit",
            "priceperacre",
        } and not isinstance(item, (dict, list)):
            if (
                normalized == "value"
                and isinstance(value.get("unit"), str)
                and value["unit"].strip()
                and not re.search(
                    r"[$€£]|USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?|euros?|pounds?|price|money",
                    value["unit"],
                    re.I,
                )
            ):
                projected[key] = copy.deepcopy(item)
                continue
            if context == "asking":
                continue
            if context != "lease" and (
                context in {"pricing", "ambiguous"}
                or normalized not in {"amount", "value"}
            ):
                raise ValueError(
                    "Avison asking projection encountered ambiguous financial input"
                )
        child_role = (
            "identity"
            if normalized in {"name", "headline", "metatitle"}
            else "narrative"
            if normalized in PROSE
            or child_context in {"asking", "pricing", "ambiguous"}
            else "unknown"
        )
        clean = project_value(item, amounts, child_context, child_role)
        if clean != "":
            projected[key] = clean
    return projected


def project_row(row):
    if not isinstance(row, dict) or row.get("slug") != SOURCE:
        return row
    raw = row.get("raw_data")
    control = effective_control(raw)
    result = dict(row)
    if control == "withheld":
        amounts = asking_amounts(raw)
        result["raw_data"] = project_value(raw, amounts)
        for column in ("sale_price_usd", "sale_price_per_sf"):
            result[column] = None
        for column in (
            "title",
            "description",
            "markdown",
            "highlights",
            "amenities",
            "extra_facts",
            "contacts",
            "documents",
            "media",
        ):
            if column in result:
                value = result[column]
                result[column] = project_value(
                    value,
                    amounts,
                    role="identity" if column == "title" else "narrative",
                )
    result["raw_data"] = dict(result["raw_data"])
    result["raw_data"]["avisonPriceVisibility"] = {"schema": SCHEMA, "control": control}
    return result


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _pg_pattern(value):
    return value.replace(r"\b", r"\y")


def sql_policy(rows):
    """Emit only transaction-local Avison functions for a hidden Avison batch."""
    controls = [
        effective_control(row.get("raw_data"))
        for row in rows
        if row.get("slug") == SOURCE
    ]
    if "withheld" not in controls:
        return {"definitions": "", "prior_select": "", "post_sql": ""}
    patterns = {
        "ask": ASK_LABEL,
        "other": OTHER_LABEL,
        "label": LABEL,
        "basis": LEASE_BASIS,
        "money": MONEY,
        "protected": PROTECTED_TEXT,
    }
    literals = {key: _lit(_pg_pattern(value)) for key, value in patterns.items()}
    sql = r"""
CREATE OR REPLACE FUNCTION pg_temp.avison_asking_control(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $av$
DECLARE control text; a text; b text; m jsonb;
BEGIN
 IF jsonb_typeof(v)<>'object' THEN RETURN NULL; END IF;
 IF v ? 'sourceKey' THEN
  IF v->>'sourceKey' IS DISTINCT FROM 'avison-young' THEN RETURN NULL; END IF;
  control:='absent';
  IF v ? 'rawSharpLaunch' AND jsonb_typeof(v->'rawSharpLaunch') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Avison raw control object required'; END IF;
  IF EXISTS(SELECT 1 FROM jsonb_object_keys(coalesce(v->'rawSharpLaunch','{}'::jsonb)) k WHERE lower(regexp_replace(k,'[^a-zA-Z0-9]','','g'))='hideprice' AND k<>'hide_price') THEN RAISE EXCEPTION 'Avison control key ambiguous'; END IF;
  IF coalesce(v->'rawSharpLaunch','{}'::jsonb) ? 'hide_price' THEN
   IF jsonb_typeof(v#>'{rawSharpLaunch,hide_price}')<>'number'
      OR (v#>>'{rawSharpLaunch,hide_price}')::numeric NOT IN (0,1) THEN
    RAISE EXCEPTION 'Avison malformed raw control';
   END IF;
   control:=CASE WHEN (v#>>'{rawSharpLaunch,hide_price}')::numeric=1 THEN 'withheld' ELSE 'visible' END;
  END IF;
  IF v ? 'primary' THEN a:=pg_temp.avison_asking_control(v->'primary'); IF a IS NULL THEN RAISE EXCEPTION 'Avison foreign direct pass'; END IF; control:=CASE WHEN 'withheld' IN (control,a) THEN 'withheld' WHEN 'visible' IN (control,a) THEN 'visible' ELSE 'absent' END; END IF;
  IF v ? 'secondary_pass' THEN a:=pg_temp.avison_asking_control(v->'secondary_pass'); IF a IS NULL THEN RAISE EXCEPTION 'Avison foreign direct pass'; END IF; control:=CASE WHEN 'withheld' IN (control,a) THEN 'withheld' WHEN 'visible' IN (control,a) THEN 'visible' ELSE 'absent' END; END IF;
 ELSE
  IF NOT (v ? 'primary' AND v ? 'secondary_pass') THEN RETURN NULL; END IF;
  a:=pg_temp.avison_asking_control(v->'primary'); b:=pg_temp.avison_asking_control(v->'secondary_pass');
  IF a IS NULL OR b IS NULL THEN RETURN NULL; END IF;
  control:=CASE WHEN 'withheld' IN (a,b) THEN 'withheld' WHEN 'visible' IN(a,b) THEN 'visible' ELSE 'absent' END;
 END IF;
 IF v ? 'avisonPriceVisibility' THEN
  m:=v->'avisonPriceVisibility';
  IF jsonb_typeof(m)<>'object' OR (SELECT count(*) FROM jsonb_object_keys(m))<>2
     OR m->>'schema' IS DISTINCT FROM 'avison-asking-price-v1' OR NOT coalesce(m->>'control'=control OR (v ? 'latestInventoryObservation' AND m->>'control'=pg_temp.avison_asking_control(v->'latestInventoryObservation')),false) THEN
   RAISE EXCEPTION 'Avison visibility envelope contradiction';
  END IF;
 END IF;
 IF v ? 'latestInventoryObservation' THEN
  a:=pg_temp.avison_asking_control(v->'latestInventoryObservation');
  IF a IS NULL THEN RAISE EXCEPTION 'Avison current inventory foreign provenance'; END IF;
  RETURN a;
 END IF;
 RETURN control;
END $av$;

CREATE OR REPLACE FUNCTION pg_temp.avison_asking_amounts(v jsonb) RETURNS numeric[]
LANGUAGE plpgsql IMMUTABLE AS $av$
DECLARE result numeric[] := ARRAY[]::numeric[]; k text; x jsonb;
BEGIN
 IF jsonb_typeof(v)='object' THEN
  FOR k,x IN SELECT * FROM jsonb_each(v) LOOP
   IF lower(regexp_replace(k,'[^a-zA-Z0-9]','','g')) = ANY(__AMOUNT_KEYS__)
      AND (jsonb_typeof(x)='number' OR (jsonb_typeof(x)='string' AND replace(replace(x#>>'{}',',',''),'$','') ~ '^[0-9]+([.][0-9]+)?$')) THEN
    IF replace(replace(x#>>'{}',',',''),'$','')::numeric>0 THEN result := array_append(result,replace(replace(x#>>'{}',',',''),'$','')::numeric); END IF;
   END IF;
   result := result || pg_temp.avison_asking_amounts(x);
  END LOOP;
 ELSIF jsonb_typeof(v)='array' THEN
  FOR x IN SELECT * FROM jsonb_array_elements(v) LOOP
   result := result || pg_temp.avison_asking_amounts(x);
  END LOOP;
 END IF;
 RETURN result;
END $av$;

CREATE OR REPLACE FUNCTION pg_temp.avison_asking_text(v text, amounts numeric[], role text DEFAULT 'narrative')
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $av$
DECLARE t text:=v; spans text[]:=ARRAY[]::text[]; m text[]; clause text; delimiter text;
 part text; joined text; result text:=''; probe text; a numeric; scale numeric;
 n text; variants text[]:=ARRAY[]::text[]; amount_pattern text; i integer;
 independent boolean; asking boolean; disclosure boolean;
BEGIN
 IF v IS NULL THEN RETURN NULL; END IF;
 FOR m IN SELECT regexp_matches(t,'(\[([^\]]*)\]\(([^)]*)\))','g') LOOP
  spans:=array_append(spans,'['||pg_temp.avison_asking_text(m[2],amounts,'child-title')||']('||m[3]||')');
  t:=replace(t,m[1],chr(57344+cardinality(spans)));
 END LOOP;
 FOR m IN SELECT regexp_matches(t,'('||__PROTECTED__||')','gi') LOOP
  spans:=array_append(spans,m[1]); t:=replace(t,m[1],chr(57344+cardinality(spans)));
 END LOOP;
 FOREACH a IN ARRAY coalesce(amounts,ARRAY[]::numeric[]) LOOP
  IF a IS NULL OR a<=0 THEN CONTINUE; END IF;
  FOREACH scale IN ARRAY ARRAY[1,1000,1000000]::numeric[] LOOP
   n:=(a/scale)::text;
   IF strpos(n,'.')>0 THEN n:=rtrim(rtrim(n,'0'),'.'); END IF;
   probe:=CASE WHEN strpos(n,'.')>0 THEN '0*' ELSE '(\.0+)?' END;
   n:=replace(n,'.','\.');
   IF scale=1 THEN variants:=array_append(variants,n||probe);
   ELSIF scale=1000 THEN variants:=array_append(variants,n||probe||'[kK]');
   ELSE variants:=array_append(variants,n||probe||'([mM]|[ ]+million)'); END IF;
  END LOOP;
  n:=a::text; IF strpos(n,'.')>0 THEN n:=rtrim(rtrim(n,'0'),'.'); END IF;
  probe:=CASE WHEN strpos(n,'.')>0 THEN '0*' ELSE '(\.0+)?' END;
  n:=reverse(regexp_replace(reverse(split_part(n,'.',1)),'([0-9]{3})(?=[0-9])','\1,','g')) || CASE WHEN strpos(n,'.')>0 THEN '.'||split_part(n,'.',2) ELSE '' END;
  variants:=array_append(variants,replace(n,'.','\.')||probe);
 END LOOP;
 amount_pattern:='(?<![[:alnum:]_.])(?<![0-9],)('||array_to_string(variants,'|')||')(?![[:alnum:]_.]|,[0-9])';
 FOR m IN SELECT regexp_matches(t,'(.*?)([;\n•]+[[:space:]]*|[.!?](?=[[:space:]]|$)[[:space:]]*|$)','g') LOOP
  clause:=m[1]; delimiter:=m[2]; joined:='';
  FOR part IN SELECT regexp_split_to_table(clause,'(?='||__LABEL__||')','i') LOOP
   part:=regexp_replace(btrim(part),'\s+and\s*$','','i');
   IF part='' THEN CONTINUE; END IF;
   independent:=part ~* ('^'||__OTHER__) OR (part ~* __BASIS__ AND (NOT part ~* __ASK__ OR part ~* '^offered\s+at\y')) OR (role='independent' AND NOT part ~* __ASK__);
   asking:=part ~* __ASK__;
   disclosure:=part ~* __MONEY__ OR (role NOT IN ('unknown','identity','identity-caption') AND cardinality(variants)>0 AND part ~* amount_pattern);
   IF independent OR (NOT asking AND NOT disclosure) THEN joined:=concat_ws(' ',nullif(joined,''),part); END IF;
  END LOOP;
  joined:=rtrim(joined,' -–—');
  IF joined<>'' THEN result:=result||joined||delimiter||CASE WHEN delimiter<>'' THEN ' ' ELSE '' END; END IF;
 END LOOP;
 result:=rtrim(btrim(result),'; ');
 FOR i IN 1..cardinality(spans) LOOP result:=replace(result,chr(57344+i),spans[i]); END LOOP;
 RETURN result;
END $av$;

CREATE OR REPLACE FUNCTION pg_temp.avison_lease_field_pairs(v jsonb, amounts numeric[])
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $av$
DECLARE result jsonb:=v; page jsonb; observation jsonb; pair jsonb; fields jsonb;
 pi bigint; oi bigint; label text; value text; k text; clean_unparsed jsonb;
BEGIN
 IF jsonb_typeof(v)<>'object' OR jsonb_typeof(v->'pages') IS DISTINCT FROM 'array' THEN RETURN v; END IF;
 FOR page,pi IN SELECT x,n FROM jsonb_array_elements(v->'pages') WITH ORDINALITY AS p(x,n) LOOP
  IF jsonb_typeof(page->'observations') IS DISTINCT FROM 'array' THEN CONTINUE; END IF;
  FOR observation,oi IN SELECT x,n FROM jsonb_array_elements(page->'observations') WITH ORDINALITY AS p(x,n) LOOP
   IF jsonb_typeof(observation->'fields') IS DISTINCT FROM 'array' THEN CONTINUE; END IF;
   FOREACH k IN ARRAY ARRAY['suite_text','status_text','rent_text','area_text','lease_type_text'] LOOP
    IF jsonb_typeof(observation->k)='string' AND pg_temp.avison_asking_text(observation->>k,amounts,CASE WHEN k='rent_text' THEN 'independent' WHEN k IN ('suite_text','area_text') THEN 'identity-caption' ELSE 'narrative' END) IS DISTINCT FROM observation->>k THEN
     observation:=jsonb_set(observation,ARRAY[k],'null'::jsonb);
    END IF;
   END LOOP;
   IF jsonb_typeof(observation->'unparsed')='array' THEN
    SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(x)='string' THEN to_jsonb(pg_temp.avison_asking_text(x#>>'{}',amounts)) ELSE x END),'[]'::jsonb)
     INTO clean_unparsed FROM jsonb_array_elements(observation->'unparsed') AS u(x);
    observation:=jsonb_set(observation,ARRAY['unparsed'],clean_unparsed);
   END IF;
   result:=jsonb_set(result,ARRAY['pages',(pi-1)::text,'observations',(oi-1)::text],observation);
   fields:='[]'::jsonb;
   FOR pair IN SELECT * FROM jsonb_array_elements(observation->'fields') LOOP
    IF jsonb_typeof(pair)<>'object' OR jsonb_typeof(pair->'label') IS DISTINCT FROM 'string' OR (pair ? 'value' AND jsonb_typeof(pair->'value') NOT IN ('string','null')) THEN
     fields:=fields||jsonb_build_array(pair); CONTINUE;
    END IF;
    label:=pair->>'label'; value:=pair->>'value';
    IF label ~* __ASK__ THEN CONTINUE; END IF;
    IF label ~* __OTHER__ OR label ~* '\yrental[[:space:]]+rate\y' THEN
     IF value ~* __ASK__ THEN CONTINUE; END IF;
    ELSIF value IS NOT NULL AND pg_temp.avison_asking_text(value,amounts) IS DISTINCT FROM value THEN
     CONTINUE;
    END IF;
    fields:=fields||jsonb_build_array(pair);
   END LOOP;
   result:=jsonb_set(result,ARRAY['pages',(pi-1)::text,'observations',(oi-1)::text,'fields'],fields);
  END LOOP;
 END LOOP;
 RETURN result;
END $av$;

CREATE OR REPLACE FUNCTION pg_temp.avison_asking_raw(v jsonb, amounts numeric[], context text DEFAULT '', role text DEFAULT 'raw')
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $av$
DECLARE result jsonb; k text; x jsonb; normalized text; tokens text; child_context text;
 child_role text; clean jsonb; function text; note text;
BEGIN
 IF jsonb_typeof(v)='array' THEN
  result:='[]'::jsonb;
  FOR x IN SELECT * FROM jsonb_array_elements(v) LOOP
   result:=result||jsonb_build_array(pg_temp.avison_asking_raw(x,amounts,context,role));
  END LOOP;
  RETURN result;
 ELSIF jsonb_typeof(v)<>'object' THEN
  IF jsonb_typeof(v)='string' THEN
   IF role='unknown' AND context='' AND NOT v#>>'{}' ~* __ASK__ AND NOT v#>>'{}' ~* __MONEY__ THEN RETURN v; END IF;
   RETURN to_jsonb(pg_temp.avison_asking_text(v#>>'{}',amounts,CASE WHEN context='lease' THEN 'independent' ELSE role END)); END IF;
  RETURN v;
 END IF;
 function:=v->>'businessFunction';
 IF function IN ('sell','sale') THEN context:='asking';
 ELSIF function IN ('lease','rent') THEN context:='lease';
 ELSIF function LIKE '%lease%' AND function LIKE '%sale%' THEN context:='ambiguous';
 ELSIF context='lease' THEN NULL;
 ELSIF coalesce(v->>'note',v->>'label','') ~* __ASK__ THEN context:='asking'; END IF;
 result:='{}'::jsonb;
 FOR k,x IN SELECT * FROM jsonb_each(v) LOOP
  normalized:=lower(regexp_replace(k,'[^a-zA-Z0-9]','','g'));
  IF normalized='avisonleasefacts' THEN x:=pg_temp.avison_lease_field_pairs(x,amounts); END IF;
  tokens:=' '||lower(regexp_replace(regexp_replace(k,'([a-z])([A-Z])','\1 \2','g'),'[_-]',' ','g'))||' ';
  IF normalized='avisonpricevisibility' THEN
   IF jsonb_typeof(x) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(x))<>2 OR x->>'schema' IS DISTINCT FROM 'avison-asking-price-v1' OR NOT coalesce(x->>'control' IN ('absent','visible','withheld'),false) THEN RAISE EXCEPTION 'Avison invalid marker'; END IF;
   result:=result||jsonb_build_object(k,x); CONTINUE; END IF;
  IF normalized=ANY(__PROTECTED_KEYS__) OR normalized='hideprice' OR (jsonb_typeof(x)='string' AND (tokens LIKE '% url ' OR tokens LIKE '% href ' OR tokens LIKE '% src ') AND x#>>'{}' ~* '^https?://') THEN
   result:=result||jsonb_build_object(k,x); CONTINUE;
  END IF;
  IF normalized=ANY(__INDEPENDENT_KEYS__) THEN
   result:=result||jsonb_build_object(k,CASE WHEN jsonb_typeof(x)='string' THEN to_jsonb(pg_temp.avison_asking_text(x#>>'{}',amounts,'independent')) ELSE pg_temp.avison_asking_raw(x,amounts,'lease') END); CONTINUE;
  END IF;
  IF normalized=ANY(__ASK_KEYS__) OR tokens LIKE '% asking %' OR (tokens LIKE '% sale %' AND (tokens LIKE '% price %' OR tokens LIKE '% amount %')) THEN
   IF jsonb_typeof(x) IN ('object','array') THEN result:=result||jsonb_build_object(k,pg_temp.avison_asking_raw(x,amounts,'asking')); END IF;
   CONTINUE; END IF;
  IF normalized='hidepricetext' AND jsonb_typeof(x)<>'string' THEN CONTINUE; END IF;
  child_context:=CASE WHEN tokens LIKE '% lease %' OR tokens LIKE '% rent %' THEN 'lease' WHEN normalized IN ('pricing','pricespecification','offers') THEN 'pricing' WHEN normalized IN ('sale','saleoffer') THEN 'asking' ELSE context END;
  IF normalized IN ('price','amount','value','priceperunit','priceperacre') AND jsonb_typeof(x) NOT IN ('object','array') THEN
   IF normalized='value' AND jsonb_typeof(v->'unit')='string' AND btrim(v->>'unit')<>'' AND v->>'unit' !~* '[$€£]|USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?|euros?|pounds?|price|money' THEN result:=result||jsonb_build_object(k,x); CONTINUE; END IF;
   IF context='asking' THEN CONTINUE; END IF;
   IF context<>'lease' AND (context IN ('pricing','ambiguous') OR normalized NOT IN ('amount','value')) THEN RAISE EXCEPTION 'Avison ambiguous financial input'; END IF;
  END IF;
  child_role:=CASE WHEN normalized IN ('name','headline','metatitle') THEN 'identity' WHEN normalized=ANY(__PROSE_KEYS__) OR child_context IN ('asking','pricing','ambiguous') THEN 'narrative' ELSE 'unknown' END;
  clean:=pg_temp.avison_asking_raw(x,amounts,child_context,child_role);
  IF clean IS DISTINCT FROM '""'::jsonb THEN result:=result||jsonb_build_object(k,clean); END IF;
 END LOOP;
 RETURN result;
END $av$;
"""

    def array(values):
        return "ARRAY[" + ",".join(_lit(key) for key in sorted(values)) + "]::text[]"

    replacements = {
        "__ASK_KEYS__": array(ASK_KEYS),
        "__AMOUNT_KEYS__": array(ASK_KEYS | {"hidepricetext"}),
        "__PROTECTED_KEYS__": array(PROTECTED),
        "__INDEPENDENT_KEYS__": array(INDEPENDENT),
        "__PROSE_KEYS__": array(PROSE),
    }
    replacements.update(
        {"__" + key.upper() + "__": value for key, value in literals.items()}
    )
    for key, value in replacements.items():
        sql = sql.replace(key, value)
    guard = """EXISTS (SELECT 1 FROM credeals.cre_brokerages avb
 WHERE avb.id=s.brokerage_id AND avb.slug='avison-young')
 AND pg_temp.avison_asking_control(s.raw_data)='withheld'"""
    prior_select = f""", CASE WHEN {guard}
 THEN pg_temp.avison_asking_amounts(t.raw_data) || ARRAY[t.sale_price_usd]::numeric[]
 ELSE ARRAY[]::numeric[] END AS avison_asking_amounts"""
    # Incoming asking channels are projected before staging. Only retained old
    # values need transient amount matching; collect those once before upsert.
    amounts = "coalesce(p.avison_asking_amounts,ARRAY[]::numeric[])"
    joins = f"""FROM _up u JOIN _src s USING(brokerage_id,external_id)
 LEFT JOIN _prior_vals p ON p.id=u.id
 JOIN credeals.cre_listings t ON t.id=u.id
 WHERE {{alias}}.listing_id=u.id AND {guard}"""
    post = ""
    for table in ("cre_listing_contacts", "cre_listing_documents", "cre_listing_media"):
        update = f"""UPDATE credeals.{table} c
 SET title=NULLIF(pg_temp.avison_asking_text(c.title,({amounts}),'child-title'),'')
 {joins.format(alias="c")}
 AND c.title IS DISTINCT FROM NULLIF(pg_temp.avison_asking_text(c.title,({amounts}),'child-title'),'');
"""
        if table == "cre_listing_media":
            update = (
                "DO $av$ BEGIN IF to_regclass('credeals.cre_listing_media') IS NOT NULL THEN\n"
                + update
                + "END IF; END $av$;\n"
            )
        post += update
    post += f"""UPDATE credeals.cre_listings t SET
 sale_price_usd=NULL, sale_price_per_sf=NULL,
 title=pg_temp.avison_asking_text(t.title,({amounts}),'identity'),
 description=pg_temp.avison_asking_text(t.description,({amounts})),
 markdown=pg_temp.avison_asking_text(t.markdown,({amounts})),
 highlights=CASE WHEN t.highlights IS NULL THEN NULL ELSE ARRAY(
   SELECT clean FROM (SELECT pg_temp.avison_asking_text(x,({amounts})) clean FROM unnest(t.highlights) x) q WHERE clean<>'') END,
 amenities=CASE WHEN t.amenities IS NULL THEN NULL ELSE ARRAY(
   SELECT clean FROM (SELECT pg_temp.avison_asking_text(x,({amounts})) clean FROM unnest(t.amenities) x) q WHERE clean<>'') END,
 raw_data=pg_temp.avison_asking_raw(t.raw_data,({amounts})) || jsonb_build_object('avisonPriceVisibility',jsonb_build_object('schema','avison-asking-price-v1','control','withheld'))
 FROM _up u JOIN _src s USING(brokerage_id,external_id)
 LEFT JOIN _prior_vals p ON p.id=u.id
 WHERE t.id=u.id AND {guard};
DO $av$ BEGIN
 IF EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='credeals' AND table_name='cre_listings' AND column_name='extra_facts') THEN
 UPDATE credeals.cre_listings t SET extra_facts=pg_temp.avison_asking_raw(t.extra_facts,coalesce(p.avison_asking_amounts,ARRAY[]::numeric[]))
 FROM _up u JOIN _src s USING(brokerage_id,external_id) LEFT JOIN _prior_vals p ON p.id=u.id
 WHERE t.id=u.id AND {guard};
 END IF;
END $av$;
"""
    return {"definitions": sql, "prior_select": prior_select, "post_sql": post}
