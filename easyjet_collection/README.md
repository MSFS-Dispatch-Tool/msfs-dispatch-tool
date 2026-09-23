# easyJet FlightRadar24 collection (scratch branch)

This branch exists only to hold the accumulating output of the scheduled
`easyjet-collect` GitHub Actions workflow (see `.github/workflows/` on
`main`). It is intentionally kept separate from `main`'s commit history.

Files:
- `easyjet_fr24_cache.jsonl` — resumable cache, one JSON record per
  observed easyJet flight number (status `ok` or `failed`).
- `easyjet_routes_enriched.json` — rebuilt from the cache on every run;
  entries in the same schema as `data/routes_enriched.json` on `main`.

Once collection has run long enough for coverage to look stable, merge
`easyjet_routes_enriched.json` into `data/routes_enriched.json` /
`data/airports.json` on `main` (same process used for the Ryanair
expansion), then this branch can be deleted.
