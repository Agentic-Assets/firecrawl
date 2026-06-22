# Mac Studio 512GB Search Log

## Endpoint probe, 2026-06-19T02:12:44-0500

- GET http://localhost:3002/health -> 404 (17 ms)
- GET http://localhost:3002/ -> 200 (1 ms)
- POST http://localhost:3002/v1/scrape -> 200 (124 ms)
- POST http://localhost:3002/v1/search -> 200 (1918 ms)
- POST http://localhost:3002/v1/crawl -> 200 (107 ms)
- GET http://localhost:3000/health -> connection-failed (1 ms)
- GET http://localhost:3000/ -> connection-failed (1 ms)
- POST http://localhost:3000/v1/scrape -> connection-failed (0 ms)
- POST http://localhost:3000/v1/search -> connection-failed (0 ms)
- POST http://localhost:3000/v1/crawl -> connection-failed (0 ms)
- GET http://localhost:8080/health -> connection-failed (0 ms)
- GET http://localhost:8080/ -> connection-failed (0 ms)
- POST http://localhost:8080/v1/scrape -> connection-failed (0 ms)
- POST http://localhost:8080/v1/search -> connection-failed (0 ms)
- POST http://localhost:8080/v1/crawl -> connection-failed (0 ms)
- GET http://127.0.0.1:3002/health -> 404 (2 ms)
- GET http://127.0.0.1:3002/ -> 200 (1 ms)
- POST http://127.0.0.1:3002/v1/scrape -> 200 (146 ms)
- POST http://127.0.0.1:3002/v1/search -> 200 (1811 ms)
- POST http://127.0.0.1:3002/v1/crawl -> 200 (42 ms)

Raw: `raw/endpoint_probe.json`

## Local Firecrawl search parse/update, 2026-06-19T02:16:27-0500

- `Mac Studio 512GB unified memory 4TB SSD` -> 8 parsed results, raw `raw/search_01.json`
  - Buy Mac Studio - Apple | https://www.apple.com/shop/buy-mac/mac-studio
  - Mac Studio - Technical Specifications - Apple | https://www.apple.com/mac-studio/specs/
  - Apple Mac Studio with M3 Ultra Z1CD001Q3 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884033-REG/apple_msm4ul23_mac_studio_with_m3.html
  - Apple Mac Studio 2025 (M3 Ultra 32-core / 80-core GPU / 512GB RAM / 4TB ... | https://www.turbovs.com/review/apple-mac-studio-2025-m3-5213
  - Mac Studio (M4 Max) and Mac Studio (M3 Ultra) - Best Buy | https://www.bestbuy.com/site/mac/mac-studio-m4-max-and-m3-ultra/pcmcat1741219730537.c?id=pcmcat1741219730537
- `Mac Studio 512GB unified memory 8TB SSD` -> 8 parsed results, raw `raw/search_02.json`
  - Buy Mac Studio - Apple | https://www.apple.com/shop/buy-mac/mac-studio
  - Apple unveils new Mac Studio, the most powerful Mac ever | https://www.apple.com/newsroom/2025/03/apple-unveils-new-mac-studio-the-most-powerful-mac-ever/
  - Apple Mac Studio with M3 Ultra Z1CD001Q5 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884034-REG/apple_msm4ul24_mac_studio_with_m3.html
  - Apple Mac Studio Kit with AppleCare+ (M3 Ultra) B&H Photo Video | https://www.bhphotovideo.com/c/product/1911714-REG/apple_mac_studio_kit_with.html
  - Apple Mac Studio 2025 (M3 Ultra 32-core / 80-core GPU / 512GB RAM / 8TB ... | https://www.turbovs.com/review/apple-mac-studio-2025-m3-49318
- `Mac Studio 512GB unified memory 16TB SSD` -> 8 parsed results, raw `raw/search_03.json`
  - Mac Studio - Technical Specifications - Apple | https://www.apple.com/mac-studio/specs/
  - Apple unveils new Mac Studio, the most powerful Mac ever | https://www.apple.com/newsroom/2025/03/apple-unveils-new-mac-studio-the-most-powerful-mac-ever/
  - Apple Mac Studio with M3 Ultra Z1CD001Q7 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884035-REG/apple_msm4ul25_mac_studio_with_m3.html
  - Apple no longer offers M3 Ultra Mac Studio with original ... - 9to5Mac | https://9to5mac.com/2026/03/05/apple-no-longer-offers-m3-ultra-mac-studio-with-original-highest-ram-configuration/
  - Apple unveils new Mac Studio, the most powerful Mac ever, featuring M4 ... | https://macsources.com/apple-unveils-new-mac-studio-the-most-powerful-mac-ever-featuring-m4-max-and-new-m3-ultra/
- `M3 Ultra Mac Studio 512GB unified memory 4TB` -> 8 parsed results, raw `raw/search_04.json`
  - Apple Mac Studio with M3 Ultra Z1CD001Q3 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884033-REG/apple_msm4ul23_mac_studio_with_m3.html
  - Buy Mac Studio, M3 Ultra Chip, 32-core CPU, 80-core GPU, 96GB memory ... | https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-96gb-memory-4tb-storage
  - Mac Studio - Apple | https://www.apple.com/mac-studio/
  - Mac Studio (M4 Max) and Mac Studio (M3 Ultra) - Best Buy | https://www.bestbuy.com/site/mac/mac-studio-m4-max-and-m3-ultra/pcmcat1741219730537.c?id=pcmcat1741219730537
  - Apple Mac Studio 2025 (M3 Ultra 32-core / 80-core GPU / 512GB RAM / 4TB ... | https://www.turbovs.com/review/apple-mac-studio-2025-m3-5213
- `M3 Ultra Mac Studio 512GB unified memory 8TB` -> 8 parsed results, raw `raw/search_05.json`
  - Apple Mac Studio with M3 Ultra Z1CD001Q5 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884034-REG/apple_msm4ul24_mac_studio_with_m3.html
  - Buy Mac Studio, M3 Ultra Chip, 28-core CPU, 60-core GPU, 96GB memory ... | https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-28-core-cpu-60-core-gpu-96gb-memory-1tb-storage
  - Mac Studio - Apple | https://www.apple.com/mac-studio/
  - Mac Studio (M4 Max) and Mac Studio (M3 Ultra) - Best Buy | https://www.bestbuy.com/site/mac/mac-studio-m4-max-and-m3-ultra/pcmcat1741219730537.c?id=pcmcat1741219730537
  - Mac Studio 512GB RAM Removed: Why Apple Pulled Its Top Config | https://apple.gadgethacks.com/news/mac-studio-512gb-ram-removed-why-apple-pulled-its-top-config/
- `M3 Ultra Mac Studio 512GB unified memory 16TB` -> 8 parsed results, raw `raw/search_06.json`
  - Buy Mac Studio, M3 Ultra Chip, 32-core CPU, 80-core GPU, 96GB memory ... | https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-96gb-memory-16tb-storage
  - Mac Studio - Technical Specifications - Apple | https://www.apple.com/mac-studio/specs/
  - Apple Mac Studio with M3 Ultra Z1CD001Q7 B&H Photo Video | https://www.bhphotovideo.com/c/product/1884035-REG/apple_msm4ul25_mac_studio_with_m3.html
  - Apple no longer offers M3 Ultra Mac Studio with original ... - 9to5Mac | https://9to5mac.com/2026/03/05/apple-no-longer-offers-m3-ultra-mac-studio-with-original-highest-ram-configuration/
  - Apple debuts M3 Ultra in refreshed Mac Studio with up to 512GB memory | https://www.tomshardware.com/desktops/apple-debuts-m3-ultra-in-refreshed-mac-studio-with-up-to-512gb-memory
- `Refurbished Mac Studio 512GB unified memory 4TB` -> 8 parsed results, raw `raw/search_07.json`
  - Refurbished Mac Deals - Apple | https://www.apple.com/shop/refurbished/mac/mac-studio
  - Mac Studio for sale | eBay | https://www.ebay.com/sch/i.html?_nkw=mac+studio&_sop=12
  - Refurbished Mac Studio Apple M3 Ultra chip with 28‑Core CPU and 60‑Core GPU | https://www.apple.com/shop/product/fu973ll/a/Refurbished-Mac-Studio-Apple-M3-Ultra-chip-with-28‑Core-CPU-and-60‑Core-GPU
  - Amazon.com: Mac Studio Refurbished | https://www.amazon.com/mac-studio-refurbished/s?k=mac+studio+refurbished
  - Apple Mac Studio - eBay | https://www.ebay.com/shop/apple-mac-studio?_nkw=apple+mac+studio
- `Apple Certified Refurbished Mac Studio 512GB 8TB` -> 0 parsed results, raw `raw/search_08.json`
- `site:apple.com/shop/product Refurbished Mac Studio Apple M3 Ultra chip 512GB` -> 0 parsed results, raw `raw/search_09.json`
- `site:bestbuy.com Mac Studio 512GB unified memory 4TB` -> 0 parsed results, raw `raw/search_10.json`
- `site:bhphotovideo.com Mac Studio 512GB unified memory 4TB` -> 0 parsed results, raw `raw/search_11.json`
- `site:adorama.com Mac Studio 512GB unified memory 4TB` -> 0 parsed results, raw `raw/search_12.json`
- `site:microcenter.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_13.json`
- `site:expercom.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_14.json`
- `site:cdw.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_15.json`
- `site:connection.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_16.json`
- `site:insight.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_17.json`
- `site:shi.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_18.json`
- `site:zones.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_19.json`
- `site:amazon.com Mac Studio 512GB unified memory 4TB` -> 0 parsed results, raw `raw/search_20.json`
- `site:ebay.com Mac Studio 512GB unified memory 4TB SSD` -> 0 parsed results, raw `raw/search_21.json`
- `site:owc.com Mac Studio 512GB unified memory 4TB` -> 0 parsed results, raw `raw/search_22.json`
- `site:macofalltrades.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_23.json`
- `site:backmarket.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_24.json`
- `site:swappa.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_25.json`
- `site:reebelo.com Mac Studio 512GB 4TB` -> 0 parsed results, raw `raw/search_26.json`

## Local Firecrawl scrape/map pass, 2026-06-19T02:17:58-0500

- Scrape `https://www.apple.com/shop/buy-mac/mac-studio` -> 200 success=True raw `raw/scrape_candidate_01.json`
- Scrape `https://www.apple.com/mac-studio/specs/` -> 200 success=True raw `raw/scrape_candidate_02.json`
- Scrape `https://www.apple.com/shop/refurbished/mac/mac-studio` -> 200 success=True raw `raw/scrape_candidate_03.json`
- Scrape `https://www.apple.com/shop/product/fu973ll/a/Refurbished-Mac-Studio-Apple-M3-Ultra-chip-with-28%E2%80%91Core-CPU-and-60%E2%80%91Core-GPU` -> 200 success=True raw `raw/scrape_candidate_04.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-512gb-memory-4tb-storage` -> 200 success=True raw `raw/scrape_candidate_05.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-512gb-memory-8tb-storage` -> 200 success=True raw `raw/scrape_candidate_06.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-512gb-memory-16tb-storage` -> 200 success=True raw `raw/scrape_candidate_07.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-256gb-memory-4tb-storage` -> 200 success=True raw `raw/scrape_candidate_08.json`
- Scrape `https://www.bhphotovideo.com/c/product/1884033-REG/apple_msm4ul23_mac_studio_with_m3.html` -> 200 success=True raw `raw/scrape_candidate_09.json`
- Scrape `https://www.bhphotovideo.com/c/product/1884034-REG/apple_msm4ul24_mac_studio_with_m3.html` -> 200 success=True raw `raw/scrape_candidate_10.json`
- Scrape `https://www.bhphotovideo.com/c/product/1884035-REG/apple_msm4ul25_mac_studio_with_m3.html` -> 200 success=True raw `raw/scrape_candidate_11.json`
- Scrape `https://www.bhphotovideo.com/c/product/1911714-REG/apple_mac_studio_kit_with.html` -> 200 success=True raw `raw/scrape_candidate_12.json`
- Scrape `https://www.cdw.com/product/apple-mac-studio-m3-ultra-512-gb-ram-8-tb-ssd/8288107` -> 200 success=True raw `raw/scrape_candidate_13.json`
- Scrape `https://hssl.us/apple-mac-studio-with-m3-ultra-512gb-unified-ram-4tb-ssd-apple-m3-ultra-32-core-cpu-msm4ul23/` -> 200 success=True raw `raw/scrape_candidate_14.json`
- Scrape `https://www.ldlc.com/en/product/PB00718462.html` -> 200 success=True raw `raw/scrape_candidate_15.json`
- Scrape `https://www.ebay.com/itm/298406215591` -> 200 success=True raw `raw/scrape_candidate_16.json`
- Scrape `https://www.macofalltrades.com/mac-studio/` -> 200 success=True raw `raw/scrape_candidate_17.json`
- Scrape `https://www.bestbuy.com/site/mac/mac-studio-m4-max-and-m3-ultra/pcmcat1741219730537.c?id=pcmcat1741219730537` -> 200 success=True raw `raw/scrape_candidate_18.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-96gb-memory-4tb-storage` -> 200 success=True raw `raw/scrape_candidate_19.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-28-core-cpu-60-core-gpu-96gb-memory-1tb-storage` -> 200 success=True raw `raw/scrape_candidate_20.json`
- Scrape `https://www.apple.com/shop/buy-mac/mac-studio/m3-ultra-chip-32-core-cpu-80-core-gpu-96gb-memory-16tb-storage` -> 200 success=True raw `raw/scrape_candidate_21.json`
- Scrape `https://www.ebay.com/sch/i.html?_nkw=mac+studio&_sop=12` -> 200 success=True raw `raw/scrape_candidate_22.json`
- Scrape `https://www.apple.com/shop/product/fu973ll/a/Refurbished-Mac-Studio-Apple-M3-Ultra-chip-with-28‑Core-CPU-and-60‑Core-GPU` -> 200 success=True raw `raw/scrape_candidate_23.json`
- Scrape `https://www.ebay.com/shop/apple-mac-studio?_nkw=apple+mac+studio` -> 200 success=True raw `raw/scrape_candidate_24.json`
- Map `https://www.apple.com/shop/buy-mac/mac-studio` -> raw `raw/map_01.json`
- Map `https://www.apple.com/shop/refurbished/mac/mac-studio` -> raw `raw/map_02.json`
- Map `https://www.macofalltrades.com/mac-studio/` -> raw `raw/map_03.json`

## Watcher smoke test, 2026-06-19T02:27:30-0500

- Ran `python3 mac_studio_512gb_search/check_inventory.py --base-url http://localhost:3002 --out mac_studio_512gb_search/watcher_run.json` successfully.
- Validated `watcher_run.json` with `python3 -m json.tool`; compact output size was 34,013 bytes.

