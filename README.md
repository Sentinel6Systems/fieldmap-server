# fieldmap-server

A self-hosted, fully offline topographic map service for a region of your choice, with a browser-based tool
that prints exact-scale map sheets and atlases. One `docker compose up` on any Linux machine gives you:

- **A tile server** (TileServer GL) with five styles: `osm`, `topo-light`, `topo-dark`, `overlay`, and `topo-print`
  (tuned for paper). Vector tiles for web maps and MapLibre clients, raster tiles for TAK clients (ATAK, WinTAK, iTAK).
- **Build scripts** that make the data from public sources: OpenStreetMap extracts (Geofabrik) via Planetiler,
  contours and hillshade from Copernicus GLO-30 elevation data. Build once; nothing needs the internet afterwards.
- **The print tool** at `/print/`: Letter, Legal, Tabloid, Super B or Arch C sheets at 1:12k–1:100k, with an MGRS/USNG
  1 km grid, a true-angle declination diagram (WMM2025, computed offline), scale bars, markers and arrows with range and
  true/grid/magnetic bearing, an optional list page, and **atlas mode**: 2×2 to 8×8 overlapping pages with a cover,
  an index page and neighbour tabs, ready for a binder. Everything renders in the browser; the server only serves files.

It runs on a laptop, a home server, or a Raspberry Pi 4 behind Internet-in-a-Box (see `pi/`). It was built for an
emergency box that people reach over its own Wi-Fi with no internet connection, so there are no CDN calls, no web
fonts, no online services anywhere in the stack.

## Quick start

Requirements: Docker with the Compose plugin; disk space for the build (see below).

```bash
git clone https://github.com/Sentinel6Systems/fieldmap-server.git
cd fieldmap-server

# 1. Build the data for a region (hours, once). Example: a group of US states from a region file:
./scripts/build-region.sh regions/mid-atlantic.env
#    or a single Geofabrik area plus a terrain bounding box:
./scripts/build-vector.sh switzerland
./scripts/build-contours.sh 5.9 45.8 10.5 47.8 m
./scripts/build-hillshade.sh 5.9 45.8 10.5 47.8

# 2. Start the server
cp .env.example .env          # optional: ports, config file, image
docker compose up -d --build

# 3. Use it
#    http://localhost/            style list and map viewer
#    http://localhost/print/      print a map or an atlas
#    http://localhost:8080/       TileServer GL directly (TAK raster tiles)
```

The web front (nginx) serves the print tool at `/print/` and proxies everything else to TileServer GL, so the print
tool finds its tiles on the same host automatically. It also looks under `/maps/` (the Internet-in-a-Box layout) and on
port 8080, or you can point it anywhere with `?tiles=http://host:8080`.

### Disk space

Building needs much more space than the finished files take. Each build script estimates what its step needs and
stops with a clear message if the drive is too small. From a six-state build (about 64 square degrees):

| Step | Finished file | Working space needed |
|---|---|---|
| Base map | ~1 GB | ~15 GB |
| Contours | ~2 GB | ~40 GB |
| Hillshade | ~0.5 GB | ~15 GB |

Finished downloads are kept in `data/sources` and `data/cache` and reused.

## Repository layout

| Path | What it is |
|---|---|
| `docker-compose.yml`, `.env.example` | The two services: `tileserver` and `web` (print tool + proxy) |
| `config.json`, `config-basic.json` | TileServer GL configs: all five styles, or `osm`+`overlay` with only a base map |
| `styles/`, `fonts/`, `sprites/` | The five styles and every asset they need, all local |
| `scripts/`, `regions/`, `tools/` | Data builders (Planetiler, GDAL, Tippecanoe, Osmium in a tool container) |
| `print/` | The print tool: `src/`, `public/`, `build.mjs` (esbuild). Built inside the `web` image |
| `web/` | nginx config and the Dockerfile that builds and serves the print tool |
| `pi/` | Raspberry Pi + Internet-in-a-Box deployment: compose file, nginx snippets, notes |
| `systemd/` | Start the compose stack at boot |
| `data/` | Your mbtiles files (not in git) |

## The styles

| Style | Description | Data files needed |
|---|---|---|
| `osm` | OpenStreetMap look | `basemap.mbtiles` |
| `topo-light` | Topographic, light colours, contours, hillshade; the screen style | all three |
| `topo-dark` | Topographic, dark colours, for night use | all three |
| `overlay` | Transparent roads and labels, for use over aerial imagery | `basemap.mbtiles` |
| `topo-print` | `topo-light` tuned for paper: smaller labels, road names from z11/z13, dark outlined buildings, town names, lighter hillshade | all three |

## The print tool

- Pick scale, paper, orientation and quality (200 or 300 dpi); drag the blue outline to place the sheet.
- Every sheet is at exact scale: the map zoom is computed from scale, dpi and the centre latitude. One CSS pixel is
  1/100 inch on paper, so print zooms are 1:12k ≈ z14.2, 1:24k/25k ≈ z13.2, 1:50k ≈ z12.2, 1:100k ≈ z11.2.
- MGRS/USNG grid of the sheet's UTM zone, 1 km lines with blue principal digits in the margin, 100 m ticks at 1:12k,
  10 km lines bold at 1:100k, extended across a zone boundary when a sheet straddles one.
- Declination from the World Magnetic Model (bundled coefficients, no network): TN, GN and MN drawn at true angles,
  the G-M angle, and which way magnetic north moves each year.
- Markers (12-colour palette, draggable) and arrows (tap start, tap end, drag the ends) labelled with name, range and
  true/magnetic bearing; grid bearing in the side panel. Optional list page with MGRS, lat/lon and bearings.
- Atlas mode: 2×2, 3×3, 4×4, 6×6 or 8×8 pages with 1 cm overlap, rows A… from the north and columns 1… from the west.
  A cover page built from the map data (places, peaks, lakes, rivers inside the atlas, a locator map), an index page
  with the page grid, neighbour tabs on every page, optional binder margin. Rendering is sequential with progress and
  a Cancel button; a laptop is recommended for atlases.
- Buildings at 1:24k/25k: tiles below z14 carry only large buildings, so the tool draws the building layer again from
  the z14 tiles. For a basemap that has houses at z13 as well, add `--building-merge-z13=false --min-feature-size=0.25`
  to the Planetiler arguments in `scripts/build-vector.sh` / `build-region.sh`.
- Contour interval and data date shown in the margin come from `print/public/config.json`.
- Development: `cd print && npm install && npm run build`; `npm run serve` serves `dist/` on :8090
  (`http://localhost:8090/?tiles=http://localhost:8080`).

## Build scripts

### `build-vector.sh <area>`

Builds `data/basemap.mbtiles` with [Planetiler](https://github.com/onthegomap/planetiler).
Planetiler downloads the OpenStreetMap extract for the area from Geofabrik.
The output uses the OpenMapTiles schema.
The first run downloads about 1.5 GB of shared world data (coastlines, lakes).
This data stays in `data/sources` for the next runs.
Large areas need more memory: `PLANETILER_MEMORY=8g ./scripts/build-vector.sh germany`.

### `build-contours.sh <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]`

Builds `data/contours.mbtiles` from Copernicus GLO-30 elevation data.
Units are meters by default (minor 20 m, major 100 m).
Use `ft` for feet (minor 40 ft, major 200 ft):

```bash
./scripts/build-contours.sh -150.5 61.0 -149.0 61.6 ft
```

### `build-hillshade.sh <min-lon> <min-lat> <max-lon> <max-lat>`

Builds `data/hillshade.mbtiles` from the same elevation data.

The terrain scripts build a small tool container (GDAL + Tippecanoe) on first run.
Downloaded elevation tiles stay in `data/cache/dem` for reuse.

### `build-region.sh <region-file> [vector] [contours] [hillshade]`

Builds all three data files for a region described in a region file.
A region can cover several Geofabrik extracts, for example several US states.
The script downloads each extract, merges them into one file with Osmium,
and runs Planetiler on the merged file. Then it builds contours and hillshade
for the region's bounding box.

```bash
./scripts/build-region.sh regions/mid-atlantic.env
```

Add step names to run only some steps:

```bash
./scripts/build-region.sh regions/mid-atlantic.env contours hillshade
```

A region file sets these values (see `regions/mid-atlantic.env`):

| Variable | Meaning |
|---|---|
| `REGION_NAME` | Short name for the merged OSM file |
| `GEOFABRIK_AREAS` | Geofabrik paths, for example `north-america/us/new-jersey` |
| `BBOX` | Terrain box: `min-lon min-lat max-lon max-lat` |
| `CONTOUR_UNITS` | `ft` or `m` (default `ft`) |
| `PLANETILER_MEMORY` | Java heap for Planetiler (default `8g`) |

Planetiler output is limited to the region's `BBOX`.

Planetiler also needs three shared inputs (water polygons, Natural Earth,
lake centerlines). The script downloads them to `data/sources` before it runs
Planetiler, so Planetiler itself makes no network calls. Each URL gets several
attempts. If the primary source still fails, the script tries the next URL in
the `SHARED_SOURCES` list at the top of `build-region.sh`. The water polygons
fall back to the North America mirror at `www.osmdata.org`. Files that are
already present are reused. Add more mirrors to that list if a source is
unreliable.

### `merge-osm.sh <output-name> <geofabrik-path> ...`

Downloads Geofabrik extracts to `data/sources/extracts` and merges them into
`data/sources/<output-name>.osm.pbf`. `build-region.sh` calls it for you.

## Bring your own data

You do not have to use the build scripts.
Put your own files in `data/` with these names:

| File | Type | Content |
|---|---|---|
| `data/basemap.mbtiles` | vector | OpenMapTiles schema base map |
| `data/contours.mbtiles` | vector | layers `contours_major` and `contours_minor`, attribute `elev_ft` (feet) or `elev_m` (meters) |
| `data/hillshade.mbtiles` | raster | gray hillshade PNG tiles |

The contour labels read `elev_ft` first, then `elev_m`.
`ft` builds write `elev_ft`. `m` builds write `elev_m`.
Contour files from other builders work too if they carry one of those attributes.

## Use with TAK clients

TileServer GL serves rendered raster tiles for each style, on x86 and ARM alike.
Add a map source in ATAK/WinTAK/iTAK with this URL pattern:

```
http://YOUR_SERVER:8080/styles/topo-light/{z}/{x}/{y}.png
```

On a Raspberry Pi installed with `scripts/install-pi.sh`, the tile server sits behind nginx at `/maps/` instead:

```
http://YOUR_PI/maps/styles/topo-light/{z}/{x}/{y}.png
```

Replace `topo-light` with `osm`, `topo-dark`, or `overlay` for the other styles.
For higher resolution screens, use `.../{z}/{x}/{y}@2x.png`.

Both configs are set for devices with little RAM, such as a Raspberry Pi. No raster renderer starts with the server;
one is made the first time a style is asked for, so styles nobody uses take no memory. Each style gets at most two
renderers for normal tiles and one for `@2x` (`minRendererPoolSizes`, `maxRendererPoolSizes`), and `@3x` tiles are
off (`maxScaleFactor` 2). Renderers stay in memory once made. On a large server with many TAK clients, raise the
maximums.

## Raspberry Pi behind Internet-in-a-Box

See [pi/README.md](pi/README.md): clone onto the Pi, copy your `.mbtiles` files in, run `sudo ./scripts/install-pi.sh`.
It starts TileServer GL in Docker on localhost, builds the print tool on the Pi, and adds the nginx snippets so
IIAB serves `/maps/` and `/print/`.

## Start at boot

See [systemd/README.md](systemd/README.md).

## Changelog

### v1.0.0

- Merged the tile server, the data builders and the print tool into one repository (previously `tileserver-starter`
  v0.3 plus a separate print tool).
- New `web` service: nginx fronts TileServer GL and serves the print tool at `/print/`, built in the image.
- New `topo-print` style.
- Print tool: five paper sizes, tiled rendering for large sheets, markers and arrows, list page, atlas mode with cover
  and index pages.

## Attribution and licenses

- Map data: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL)
- Tile schema and base styles: [OpenMapTiles](https://openmaptiles.org/) (BSD / CC-BY)
- Elevation data: © ESA, [Copernicus DEM GLO-30](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model)
- Fonts: [Open Sans](https://fonts.google.com/specimen/Open+Sans) (Apache License 2.0)
- Server: [TileServer GL](https://github.com/maptiler/tileserver-gl) (BSD)

Show the OpenStreetMap attribution when you show these maps in public.
The code in this repository is under the MIT license. See [LICENSE](LICENSE).
