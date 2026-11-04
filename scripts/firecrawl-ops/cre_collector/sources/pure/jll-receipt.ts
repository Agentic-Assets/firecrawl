/**
 * Side-effect-free JLL request-shape and identity parsing shared by the
 * collector and C10 receipts.  Keep this module free of cache, filesystem,
 * and scraper imports: it is safe to load in the sealed receipt lane.
 */
import * as cheerio from "cheerio";

export const JLL_SEARCH_PAGE_SIZE = 50;
export const JLL_GRAPHQL_URL = "https://property.jll.com/api/graphql";
// The public endpoint allowlists the complete client operation, including unused
// optional filters and fields. Keep parity rather than reducing its selection.
// Observed 2026-10-10 in JLL's public client:
// https://property.jll.com/_next/static/chunks/pages/_app-b7af077e8099160f.js
export const JLL_SEARCH_RESULTS_QUERY = `
query SearchResults($market: String!, $language: String!, $propertyTypes: [String!], $tenureTypes: [String!], $cities: [String!], $districts: [String!], $departments: [String!], $marketArea: String, $regions: [String!], $state: String, $postcodes: [String!], $postcodePrefix: [String!], $isSublease: Boolean, $surfaceArea: SurfaceSearchInput, $inCircumference: InCircumferenceInput, $inRectangle: InRectangleInput, $searchInput: SearchInputUnion, $skip: Int, $orderBy: PropertiesOrderInput, $polygonId: Int, $propertySubTypes: [String!], $availableAfter: String, $buildingClasses: [String!], $statuses: [String!], $submarket: String, $withVideos: Boolean, $withFloorPlans: Boolean, $withVirtualTours: Boolean, $withView360URLs: Boolean, $wls: String, $ids: [String!], $placeId: String, $price: PropertyPriceSearchInput, $take: IntString = 50, $hasParking: Boolean, $hasRaisedFloor: Boolean, $hasAllDaySecurity: Boolean, $hasAirConditioning: Boolean, $hasBreeamRating: Boolean, $hasCanteen: Boolean, $hasChargingRoom: Boolean, $hasColdChamber: Boolean, $hasCrane: Boolean, $hasDockAccess: Boolean, $hasDPE: Boolean, $hasEVChargingPoints: Boolean, $hasElevator: Boolean, $hasEmergencyElevator: Boolean, $hasExtractionDuct: Boolean, $hasFalseCeiling: Boolean, $hasFireExtinguisher: Boolean, $hasFireHydrant: Boolean, $hasFoodBeverage: Boolean, $hasGroundLevelAccess: Boolean, $hasIndustrialElevator: Boolean, $hasIronConnection: Boolean, $hasLicense: Boolean, $hasLoadingDock: Boolean, $hasOffice: Boolean, $hasPlotArea: Boolean, $hasPopupStore: Boolean, $hasSeismicStatus: Boolean, $hasSolarPanels: Boolean, $hasSprinklers: Boolean, $hasSunProtection: Boolean, $hasTerrace: Boolean, $hasWellness: Boolean, $hasRampway: Boolean, $hasReception: Boolean, $isBranchable: Boolean, $isCommissionFree: Boolean, $isSingleTenant: Boolean, $isEquippedOffice: Boolean, $isHighFloor: Boolean, $isJLLExclusive: Boolean, $isLowFloor: Boolean, $isOneStory: Boolean, $isPetFriendly: Boolean, $isSustainableOffice: Boolean, $locationType: String, $distanceFromStation: NumberIntervalSearchInput, $distanceFromIC: NumberIntervalSearchInput, $yearBuilt: NumberIntervalSearchInput, $availableFrom: DateIntervalSearchInput, $ceilingHeight: NumberIntervalSearchInput, $floorPlate: NumberIntervalSearchInput, $closestRoads: [String!], $closestTrains: [String!], $windowRange: NumberIntervalSearchInput, $floorLoad: NumberIntervalSearchInput, $loadingDocks: NumberIntervalSearchInput, $amenities: [String!], $energyRatings: [String!], $tags: [String!], $title: String, $brokerIds: [String!], $locations: [LocationInput!]) {
  properties(
    market: $market
    language: $language
    propertyTypes: $propertyTypes
    tenureTypes: $tenureTypes
    price: $price
    cities: $cities
    districts: $districts
    departments: $departments
    marketArea: $marketArea
    regions: $regions
    state: $state
    postcodes: $postcodes
    postcodePrefix: $postcodePrefix
    locations: $locations
    isSublease: $isSublease
    surfaceArea: $surfaceArea
    inCircumference: $inCircumference
    inRectangle: $inRectangle
    searchInput: $searchInput
    take: $take
    skip: $skip
    orderBy: $orderBy
    polygonId: $polygonId
    availableAfter: $availableAfter
    buildingClasses: $buildingClasses
    propertySubTypes: $propertySubTypes
    statuses: $statuses
    submarket: $submarket
    withVideos: $withVideos
    withFloorPlans: $withFloorPlans
    withVirtualTours: $withVirtualTours
    withView360URLs: $withView360URLs
    wls: $wls
    ids: $ids
    placeId: $placeId
    hasParking: $hasParking
    hasRaisedFloor: $hasRaisedFloor
    hasAllDaySecurity: $hasAllDaySecurity
    hasAirConditioning: $hasAirConditioning
    hasBreeamRating: $hasBreeamRating
    hasCanteen: $hasCanteen
    hasChargingRoom: $hasChargingRoom
    hasColdChamber: $hasColdChamber
    hasCrane: $hasCrane
    hasDockAccess: $hasDockAccess
    hasDPE: $hasDPE
    hasEVChargingPoints: $hasEVChargingPoints
    hasElevator: $hasElevator
    hasEmergencyElevator: $hasEmergencyElevator
    hasExtractionDuct: $hasExtractionDuct
    hasFireExtinguisher: $hasFireExtinguisher
    hasFireHydrant: $hasFireHydrant
    hasFoodBeverage: $hasFoodBeverage
    hasGroundLevelAccess: $hasGroundLevelAccess
    hasIndustrialElevator: $hasIndustrialElevator
    hasIronConnection: $hasIronConnection
    hasLicense: $hasLicense
    hasLoadingDock: $hasLoadingDock
    hasOffice: $hasOffice
    hasPlotArea: $hasPlotArea
    hasPopupStore: $hasPopupStore
    hasFalseCeiling: $hasFalseCeiling
    hasSeismicStatus: $hasSeismicStatus
    hasSolarPanels: $hasSolarPanels
    hasSprinklers: $hasSprinklers
    hasSunProtection: $hasSunProtection
    hasTerrace: $hasTerrace
    hasWellness: $hasWellness
    hasRampway: $hasRampway
    hasReception: $hasReception
    isBranchable: $isBranchable
    isCommissionFree: $isCommissionFree
    isSingleTenant: $isSingleTenant
    isEquippedOffice: $isEquippedOffice
    isHighFloor: $isHighFloor
    isJLLExclusive: $isJLLExclusive
    isLowFloor: $isLowFloor
    isOneStory: $isOneStory
    isPetFriendly: $isPetFriendly
    isSustainableOffice: $isSustainableOffice
    locationType: $locationType
    distanceFromStation: $distanceFromStation
    distanceFromIC: $distanceFromIC
    yearBuilt: $yearBuilt
    availableFrom: $availableFrom
    ceilingHeight: $ceilingHeight
    floorPlate: $floorPlate
    closestRoads: $closestRoads
    closestTrains: $closestTrains
    windowRange: $windowRange
    floorLoad: $floorLoad
    loadingDocks: $loadingDocks
    amenities: $amenities
    energyRatings: $energyRatings
    tags: $tags
    title: $title
    brokerIds: $brokerIds
  ) {
    count
    items {
      id
      title
      images
      address
      approxLocation
      district
      buildingClasses
      videos
      floorPlans
      virtualTours
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
      labels
      latitude
      longitude
      region
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
      customAttributes {
        name
        value
      }
    }
  }
}
`;

export function normalizedJllListingUrl(href: string): string {
  const url = new URL(href.startsWith("http") ? href : `https://property.jll.com${href}`);
  url.hash = "";
  url.search = "";
  return url.toString().replace(/\/$/, "");
}

export function jllGraphqlVariables(
  tx: "sale" | "lease",
  propertyType: string,
  page: number,
): Record<string, unknown> {
  if (!Number.isInteger(page) || page < 1) throw new Error(`JLL GraphQL page must be a positive integer, received ${page}`);
  return {
    market: "us", language: "en", propertyTypes: [propertyType],
    tenureTypes: [tx === "sale" ? "sale" : "rent"], skip: (page - 1) * JLL_SEARCH_PAGE_SIZE,
    take: JLL_SEARCH_PAGE_SIZE,
    orderBy: { field: "dateModified", direction: "desc", imagePriority: true },
  };
}

export function parseJllGraphqlSearchEnvelope(payload: unknown): { readonly total: number; readonly items: readonly Record<string, unknown>[] } {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("JLL GraphQL response is not an object");
  const record = payload as { errors?: unknown; data?: { properties?: unknown } };
  if (record.errors !== undefined && (!Array.isArray(record.errors) || record.errors.length > 0)) {
    throw new Error("JLL GraphQL response contains errors");
  }
  const properties = record.data?.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) throw new Error("JLL GraphQL response lacks data.properties");
  const shape = properties as { count?: unknown; items?: unknown };
  if (!Number.isInteger(shape.count) || (shape.count as number) < 0) throw new Error("JLL GraphQL response lacks a finite nonnegative count");
  if (!Array.isArray(shape.items) || shape.items.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
    throw new Error("JLL GraphQL response lacks a properties.items array");
  }
  return { total: shape.count as number, items: shape.items as readonly Record<string, unknown>[] };
}

export function jllNextData(rawHtml: string): unknown | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}
