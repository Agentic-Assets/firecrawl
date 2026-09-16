"""Fail-closed host coordinator for the C10 v3 browser sidecar.

This module is deliberately the only Python surface which owns the C10 lock,
durable arm claim, private receipt directory, lifecycle keys, compose overlay,
and deadline.  The TypeScript child is an untrusted narrow transport: it is
given one signed capability and can return only the sidecar's signed evidence.
It cannot mint a capability, create a receipt store, or acquire a CRE lock.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import (
    C10Error,
    canonical_bytes,
    require_sha256,
    sha256,
    validate_plan,
)
from .jll_admission import (
    JLL_COHORT_KIND,
    JLL_MEMBER_COUNT,
    JLL_PLAN_KIND,
    _collection_intent_sha256,
)

_ENV_MODE = 0o600
_ROOT_MODE = 0o700
_FILE_MODE = 0o600
_MAX_RAW_BODY_BYTES = 2 * 1024 * 1024
# Signed JSON contains base64 data. Keep the response cap independent from the
# envelope cap so a valid two-MiB browser response can still be sealed.
_MAX_PRIVATE_ARTIFACT = 8 * 1024 * 1024
_MAX_CHILD_FRAME_BYTES = 64 * 1024
_MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_CHILD_STDERR_BYTES = 64 * 1024
_MAX_CARD_TIMEOUT_MS = 30_000
_C10_COMPOSE_SERVICE = "playwright-service-c10"
_JLL_HOST = "property.jll.com"
_JLL_BOOTSTRAP_URL = "https://property.jll.com/"
# This is deliberately a fixed recipe rather than a caller supplied request
# graph.  The query is the reviewed public property search operation.  The
# immutable cohort supplies only the sixteen selected canonical member routes.
_JLL_QUERY = """
  query SearchResults(
    $market: String!
    $language: String!
    $propertyTypes: [String!]
    $tenureTypes: [String!]
    $skip: Int
    $take: IntString = 50
    $orderBy: PropertiesOrderInput
  ) {
    properties(
      market: $market
      language: $language
      propertyTypes: $propertyTypes
      tenureTypes: $tenureTypes
      skip: $skip
      take: $take
      orderBy: $orderBy
    ) {
      count
      items {
        id
        title
        images
        address
        propertyTypes
        tenureTypes
        rentPrice {
          amount
          currency
          unit
        }
        salePrice {
          amount
          currency
          unit
        }
        hidePrice
        pageUrl
        latitude
        longitude
        city
        state
        postcode
        surfaceAreas {
          value
          unit
          label
          alternativeUnit
          showEstimateDesks
          metrics {
            value
            unit
          }
        }
      }
    }
  }
"""
_JLL_RECIPE = {"transaction": "sale", "property_type": "office", "page": 1}
_COHORT_HASH_FIELDS = (
    "schema_version",
    "config_sha256",
    "sampling",
    "sources",
    "planes",
    "aggregate",
)
_HEALTH_FIELDS = {
    "activePages",
    "configuredCapacity",
    "coordinatorKeyId",
    "evidenceKeyId",
    "healthSignature",
    "protocolVersion",
    "replayEntries",
    "status",
    "transport",
}
_EVIDENCE_FIELDS = {
    "binding",
    "bodyBase64",
    "cacheRead",
    "cacheWrite",
    "challengeDetected",
    "configuredCapacity",
    "contentType",
    "context",
    "elapsedMs",
    "engineAttempt",
    "evidenceSignature",
    "finalUrl",
    "jobId",
    "leaseEndMonotonicNs",
    "leaseStartMonotonicNs",
    "observedActivePages",
    "pageLease",
    "protocolVersion",
    "proxy",
    "queueMs",
    "redirectCount",
    "status",
}


def _canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _key_id(public_key_pem: str) -> str:
    return hashlib.sha256(public_key_pem.encode("utf-8")).hexdigest()


def _safe_json(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise C10Error(f"{label} must be an object")
    # Re-encoding rejects non-finite and unsupported values through contracts.
    json.loads(_canonical_text(value))
    return value


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise C10Error("C10 lifecycle deadline expired")
    return value


class C10SealedCardRegistry:
    """The only C10 request graph: one fixed JLL enumeration and 16 members.

    Construction intentionally takes a hash-bound v1 cohort, never an
    arbitrary card map.  The route bytes and GraphQL body are manufactured by
    the host from the immutable selected JLL membership.
    """

    def __init__(self, plan: Mapping[str, Any], cohort: Mapping[str, Any]) -> None:
        validate_plan(plan)
        if plan.get("kind") == JLL_PLAN_KIND:
            self._init_jll_only(plan, cohort)
            return
        if cohort.get("cohort_sha256") != plan["cohort_sha256"] or sha256(
            {key: cohort.get(key) for key in _COHORT_HASH_FIELDS}
        ) != cohort.get("cohort_sha256"):
            raise C10Error("C10 registry rejects a different plan or cohort")
        sources = cohort.get("sources")
        if not isinstance(sources, list) or len(sources) != 20:
            raise C10Error("C10 registry cohort is invalid")
        self.source_projection_sha256 = self._validate_source_projection(plan, sources)
        jll = next(
            (
                value
                for value in sources
                if isinstance(value, Mapping) and value.get("source_key") == "jll"
            ),
            None,
        )
        if not isinstance(jll, Mapping) or jll.get("core_state") != "ready":
            raise C10Error("C10 registry requires the sealed JLL cohort")
        members = jll.get("core")
        fresh = jll.get("fresh_enumeration")
        if (
            not isinstance(members, list)
            or len(members) != 16
            or jll.get("core_selected_rows") != 16
            or jll.get("core_target_rows") != 16
            or not isinstance(fresh, Mapping)
            or fresh.get("population_state") != "verified"
        ):
            raise C10Error("C10 registry requires exactly sixteen sealed JLL members")
        require_sha256(fresh.get("receipt_sha256"), "JLL enumeration receipt")
        observed_routes: set[str] = set()
        observed_ids: set[str] = set()
        member_routes: list[str] = []
        for index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise C10Error("C10 JLL member is invalid")
            provider_id = member.get("provider_id")
            route = self._member_route(member.get("canonical_url"))
            if (
                not isinstance(provider_id, str)
                or not provider_id.isdigit()
                or provider_id in observed_ids
                or route in observed_routes
            ):
                raise C10Error(
                    "C10 JLL membership does not have exact canonical identity"
                )
            observed_ids.add(provider_id)
            observed_routes.add(route)
            member_routes.append(route)
        frozen: dict[str, Mapping[str, Any]] = {
            "jll-enumeration": self._enumeration_card(member_routes)
        }
        for index, route in enumerate(member_routes):
            frozen[f"jll-member-{index}"] = self._member_card(index, route)
        # Keep canonical bytes, not caller-reachable mutable dictionaries. A
        # resolve always returns a fresh decoded projection for one capability.
        self._cards = {
            card_id: _canonical_text(card) for card_id, card in frozen.items()
        }
        self.cohort_sha256 = plan["cohort_sha256"]
        self.plan_sha256 = plan["plan_sha256"]
        self.manifest_sha256 = sha256(
            {card_id: json.loads(card) for card_id, card in self._cards.items()}
        )

    def _init_jll_only(
        self, plan: Mapping[str, Any], cohort: Mapping[str, Any]
    ) -> None:
        """Build the same fixed card graph from the separately admitted JLL cohort."""
        fields = {
            "schema_version",
            "kind",
            "collection_intent_sha256",
            "members",
            "enumeration_receipt_sha256",
            "member_receipt_sha256",
            "receipt_manifest_sha256",
            "adapter_implementation_sha256",
            "collection_binding",
            "artifact_index_sha256",
            "no_write",
            "cohort_sha256",
        }
        if (
            set(cohort) != fields
            or cohort.get("kind") != JLL_COHORT_KIND
            or cohort.get("cohort_sha256") != plan["cohort_sha256"]
            or cohort.get("cohort_sha256")
            != sha256(
                {key: value for key, value in cohort.items() if key != "cohort_sha256"}
            )
        ):
            raise C10Error("C10 registry JLL cohort is invalid")
        members = cohort.get("members")
        source = plan.get("source")
        if (
            not isinstance(members, list)
            or len(members) != JLL_MEMBER_COUNT
            or not isinstance(source, Mapping)
            or source.get("cohort_member_sha256") != sha256(members)
            or source.get("enumeration_receipt_sha256")
            != cohort.get("enumeration_receipt_sha256")
            or source.get("receipt_manifest_sha256")
            != cohort.get("receipt_manifest_sha256")
            or source.get("collection_intent_sha256")
            != cohort.get("collection_intent_sha256")
            or source.get("collection_binding_sha256")
            != sha256(cohort.get("collection_binding"))
        ):
            raise C10Error("C10 registry JLL cohort projection differs from plan")
        self.source_projection_sha256 = sha256(source)
        observed_routes: set[str] = set()
        observed_ids: set[str] = set()
        member_routes: list[str] = []
        for index, member in enumerate(members):
            if (
                not isinstance(member, Mapping)
                or member.get("key") != f"jll-{index + 1}"
            ):
                raise C10Error("C10 JLL membership is not canonical")
            provider_id = member.get("provider_id")
            route = self._member_route(member.get("canonical_url"))
            if (
                not isinstance(provider_id, str)
                or not provider_id.isdigit()
                or provider_id in observed_ids
                or route in observed_routes
            ):
                raise C10Error(
                    "C10 JLL membership does not have exact canonical identity"
                )
            observed_ids.add(provider_id)
            observed_routes.add(route)
            member_routes.append(route)
        normalized_members = [
            {
                "key": f"jll-{index + 1}",
                "provider_id": str(members[index]["provider_id"]),
                "canonical_url": member_routes[index],
            }
            for index in range(JLL_MEMBER_COUNT)
        ]
        if source.get("collection_intent_sha256") != _collection_intent_sha256(
            normalized_members
        ):
            raise C10Error("C10 registry JLL source card intent differs from cohort")
        frozen: dict[str, Mapping[str, Any]] = {
            "jll-enumeration": self._enumeration_card(member_routes)
        }
        for index, route in enumerate(member_routes):
            frozen[f"jll-member-{index}"] = self._member_card(index, route)
        self._cards = {
            card_id: _canonical_text(card) for card_id, card in frozen.items()
        }
        self.cohort_sha256 = plan["cohort_sha256"]
        self.plan_sha256 = plan["plan_sha256"]
        self.manifest_sha256 = sha256(
            {card_id: json.loads(card) for card_id, card in self._cards.items()}
        )

    @staticmethod
    def _validate_source_projection(plan: Mapping[str, Any], sources: list[Any]) -> str:
        """Bind all twenty plan source projections to the sealed cohort.

        A plan hash alone is not enough at this boundary: a forged Plan B can
        retain the same cohort hash while changing its source projection.  The
        registry retains the exact admitted projection and execute compares it
        before it acquires a lock or creates any lifecycle material.
        """
        plan_sources = plan.get("sources")
        if not isinstance(plan_sources, list) or len(plan_sources) != 20:
            raise C10Error("C10 registry plan lacks the twenty-source contract")
        by_key: dict[str, Mapping[str, Any]] = {}
        for source in sources:
            if not isinstance(source, Mapping) or not isinstance(
                source.get("source_key"), str
            ):
                raise C10Error("C10 registry cohort source is invalid")
            key = source["source_key"]
            if key in by_key:
                raise C10Error("C10 registry cohort source keys are not unique")
            core, fresh = source.get("core"), source.get("fresh_enumeration")
            if not isinstance(core, list) or not isinstance(fresh, Mapping):
                raise C10Error("C10 registry cohort source projection is invalid")
            by_key[key] = source
        if len(by_key) != 20:
            raise C10Error("C10 registry cohort source count is invalid")
        seen: set[str] = set()
        for projection in plan_sources:
            if not isinstance(projection, Mapping):
                raise C10Error("C10 registry plan source projection is invalid")
            key = projection.get("key")
            source = by_key.get(key) if isinstance(key, str) else None
            if source is None or key in seen:
                raise C10Error("C10 registry plan source set differs from cohort")
            seen.add(key)
            core = source["core"]
            fresh = source["fresh_enumeration"]
            if (
                projection.get("plane") != source.get("plane")
                or projection.get("cohort_member_count") != len(core)
                or projection.get("cohort_member_sha256") != sha256(core)
                or projection.get("enumeration_receipt_sha256")
                != fresh.get("receipt_sha256")
            ):
                raise C10Error(
                    "C10 registry plan source projection differs from cohort"
                )
        if seen != set(by_key):
            raise C10Error("C10 registry plan source set differs from cohort")
        return sha256(plan_sources)

    @staticmethod
    def _member_route(value: Any) -> str:
        if not isinstance(value, str):
            raise C10Error("C10 JLL member lacks a canonical route")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != _JLL_HOST
            or not parsed.path.startswith("/listings/")
            or parsed.path.rstrip("/") == "/listings"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise C10Error("C10 JLL member route is outside the reviewed path")
        return urlunsplit(("https", _JLL_HOST, parsed.path.rstrip("/"), "", ""))

    @staticmethod
    def _card(
        *,
        card_id: str,
        stage: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content_type: str | None,
        body: str | None,
        expected_member_routes: list[str] | None,
    ) -> Mapping[str, Any]:
        return {
            "id": card_id,
            "sourceKey": "jll",
            "stage": stage,
            "method": method,
            "url": url,
            "allowedHost": _JLL_HOST,
            "headers": dict(headers),
            "contentType": content_type,
            "body": body,
            "browserBootstrapUrl": _JLL_BOOTSTRAP_URL,
            "cacheMode": "no-store",
            "timeoutMs": _MAX_CARD_TIMEOUT_MS,
            "maxBytes": _MAX_RAW_BODY_BYTES,
            "bodySha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
            if body is not None
            else None,
            "expectedMemberRoutes": expected_member_routes,
        }

    @classmethod
    def _enumeration_card(cls, expected_member_routes: list[str]) -> Mapping[str, Any]:
        variables = {
            "market": "us",
            "language": "en",
            "propertyTypes": [_JLL_RECIPE["property_type"]],
            "tenureTypes": ["sale"],
            "skip": 0,
            "take": 50,
            "orderBy": {
                "field": "dateModified",
                "direction": "desc",
                "imagePriority": True,
            },
        }
        return cls._card(
            card_id="jll-enumeration",
            stage="enumeration",
            method="POST",
            url=f"https://{_JLL_HOST}/api/graphql",
            headers={
                "accept": "application/json",
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            content_type="application/json",
            body=_canonical_text(
                {
                    "operationName": "SearchResults",
                    "query": _JLL_QUERY,
                    "variables": variables,
                }
            ),
            expected_member_routes=expected_member_routes,
        )

    @classmethod
    def _member_card(cls, index: int, route: str) -> Mapping[str, Any]:
        return cls._card(
            card_id=f"jll-member-{index}",
            stage="member",
            method="GET",
            url=route,
            headers={"accept": "text/html,application/xhtml+xml"},
            content_type=None,
            body=None,
            expected_member_routes=None,
        )

    def resolve(self, card_id: str) -> Mapping[str, Any]:
        if not isinstance(card_id, str) or card_id not in self._cards:
            raise C10Error("C10 rejects a card outside the sealed registry")
        return json.loads(self._cards[card_id])

    def assert_plan_identity(self, plan: Mapping[str, Any]) -> None:
        expected_projection = (
            sha256(plan.get("source"))
            if plan.get("kind") == JLL_PLAN_KIND
            else sha256(plan.get("sources"))
        )
        if (
            self.plan_sha256 != plan.get("plan_sha256")
            or self.cohort_sha256 != plan.get("cohort_sha256")
            or self.source_projection_sha256 != expected_projection
        ):
            raise C10Error("C10 registry rejects an alternate plan/source projection")
