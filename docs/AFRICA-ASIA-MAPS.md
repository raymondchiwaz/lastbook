# Africa and Asia: offline country detail and world overview

The map screen supports an optional, compact worldwide Natural Earth overview.
It adds country borders, major roads, populated places, rivers and lakes directly
to Lastbook's existing canvas map. There are Zimbabwe, Africa and Asia buttons,
metric distances, offline place search, and a Zimbabwe-centred default view.
An explicitly saved home or a linked location still takes precedence.

Detailed country databases add OSM roads, tracks, paths, rail, waterways,
vegetation and water polygons, villages and mapped survival-relevant places.
The map offers Survival, Water, Emergency, Supplies and Travel presets and an
installed-country selector. Water records retain mapped potability, access and
seasonality tags; facilities retain available contact and opening-hours tags.
Ordinary building footprints are omitted to keep the maps compact. Country
databases install independently and become available without restarting the map.

## Install detailed country maps

Use a separate build environment with `osmium==4.1.1` and `curl`:

```sh
python tools/install_regional_maps.py --data /path/to/LastbookData \
  --countries zimbabwe botswana zambia mozambique sri-lanka --snapshot 261116
# Larger queue (substantial downloads, disk space and build time):
python tools/install_regional_maps.py --data /path/to/LastbookData \
  --continents africa asia --snapshot 261116
```

The queue uses Geofabrik's country extract catalog, not a promise of complete
continental coverage. Russia and Turkey are outside these catalog branches;
some small countries are grouped in shared extracts. Snapshot availability
depends on Geofabrik's retention. `--skip-countries` excludes an extract handled
by another job. One queue holds an installation lock; never build the same
country in another process.

Sources and map databases are kept in `derived/regional-maps/`. Downloads resume
from `.partial` files and are checked against Geofabrik's transfer MD5; builds
record a SHA-256 inventory, source URL, OSM snapshot timestamp and ODbL license.
The runtime only reads completed `.sqlite` databases, never `.building.sqlite`.
An interrupted build is retained for inspection, not automatically discarded.
`install-status.json` and per-country `.install.log` files show actual progress;
a queue being started does **not** mean its countries are installed.

For an already downloaded source:

```sh
python tools/build_regional_osm.py --input zimbabwe-261116.osm.pbf \
  --data /path/to/LastbookData --slug zimbabwe --title Zimbabwe \
  --source-url https://download.geofabrik.de/africa/zimbabwe-261116.osm.pbf --disk-index
```

OSM data is © OpenStreetMap contributors, licensed under ODbL 1.0. Preserve the
database attribution and source sidecars when redistributing. See
<https://www.openstreetmap.org/copyright>.

## Install the compact world overview

Only the build tool needs `pyshp==2.3.1`; the running app uses Python's standard
library and never contacts a map service. With pyshp in a separate build environment:

```sh
python tools/build_overview_maps.py --data /path/to/LastbookData
```

Five public-domain Natural Earth ZIPs are fetched over HTTPS, CRC-checked, and
converted to a SQLite/R*Tree database. Download hashes, source URLs, and a build
date are recorded in `derived/regional-maps/SOURCES.json`. These hashes are a
local inventory, not independent authenticity signatures. The installed database
is about 23 MB, separate from the US/state map set in `derived/geo/`. The importer
only replaces the overview after completing and checking the database. Do not run
two imports at once. A retained `overview.building.sqlite` means an earlier build
was interrupted and must be inspected before retrying.

Lastbook discovers the overview automatically, even when no US map is installed.
The regular reference library still needs installing: this map database is not a
replacement for PDFs, videos, encyclopaedias or their catalogue. Existing state
maps and library setup are not disabled or fabricated by this addition.

## Important limits

- Natural Earth's **1:10m** means **1:10,000,000 map scale**, not 10-metre accuracy.
- The Natural Earth layer is broad regional reference only. Local streets and
  service pins require an installed detailed country database.
- OSM is incomplete and historical. A mapped well/tap is **not** tested water;
  a hospital or fuel station is **not** proof of an operating service. Schools
  and community facilities are not necessarily designated shelters. Check legal
  access and local conditions. Polygon facility pins mark a boundary coordinate,
  not a verified entrance.
- There is no turn-by-turn routing, live hazard feed, road-passability or safe
  crossing assessment. Never use mapped fords or bridges as proof of safe travel.
- The layers have different historical publication dates. The installation date
  does not imply the geography was surveyed then. Borders reflect the source's
  cartographic policy and can be disputed.
- Existing US terrain, aerial photos, crop and emergency-service layers do not
  gain African or Asian coverage. Blank layers are not evidence of no hazards.
- Zimbabwe is the default country view, **not the user's inferred location**.

Source and terms: <https://www.naturalearthdata.com/about/terms-of-use/>.
Worldwide coastlines were already bundled with Lastbook; this adds searchable
place and transport/water features rather than merely changing the start position.
