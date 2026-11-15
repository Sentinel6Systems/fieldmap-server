# fieldmap-server

A self-hosted, fully offline topographic map service for a region of your choice, with a browser-based tool
that prints exact-scale map sheets and atlases. One `docker compose up` on any Linux machine gives you:

- **A tile server** (TileServer GL) with six styles: `osm`, `topo-light`, `topo-dark`, `overlay`, `topo-print` and `topo-usgs`
  (tuned for paper), plus print-scale variants of `topo-usgs`. Vector tiles for web maps and MapLibre clients, raster tiles for TAK clients (ATAK, WinTAK, iTAK).
- **Build scripts** that make the data from public sources: OpenStreetMap extracts (Geofabrik or a mirror) via Planetiler,
  contours, hillshade and elevation tiles from Copernicus GLO-30 elevation data. Build once; nothing needs the internet afterwards.
- **The print tool** at `/mapprinter/`: Letter, Legal, Tabloid, Super B or Arch C sheets at 1:12k–1:100k, with an MGRS/USNG
  1 km grid, a true-angle declination diagram (World Magnetic Model, computed offline), scale bars, markers and arrows with range and
  true/grid/magnetic bearing, an optional list page, and **atlas mode**: 2×2 to 8×8 overlapping pages with a cover,
  an index page and neighbour tabs, ready for a binder; and **terrain analysis** (viewshed and VHF/UHF radio coverage)
  when the elevation tiles are built. Everything renders in the browser; the server serves files, plus a small
  service that downloads a new magnetic model when you ask it to.

It runs on a laptop, a home server, or a Raspberry Pi 4 behind Internet-in-a-Box (see `pi/`). It was built for an
emergency box that people reach over its own Wi-Fi with no internet connection, so there are no CDN calls, no web
fonts, no online services anywhere in the stack.

## Quick start

Requirements on the machine that builds the data and runs the server:

- Linux (or another system with Docker and a bash shell), x86-64 or ARM64
- Docker with the Compose plugin; every build tool (Planetiler, GDAL, Tippecanoe, Osmium) runs in a container
- `bash`, `git`, `curl` and `python3` on the host
- RAM: 8 GB is enough for a few US states or a small country; large regions want 16-32 GB (`PLANETILER_MEMORY`)
- Disk: see [Disk space](#disk-space); the build needs far more than the finished files

The server alone (no building) runs on much less: a Raspberry Pi 4 with 4 GB serves a large region (see `pi/`).
Node.js and Pillow are only needed to work on the print tool or the style generators.

Build a small area first to check that everything works, then your real region.

```bash
git clone https://github.com/Sentinel6Systems/fieldmap-server.git
cd fieldmap-server

# 1. Build the data for your region (hours, once), from a region file (see "Region files"):
./scripts/build-region.sh <region-file>
#    or from a single Geofabrik area plus a terrain bounding box:
./scripts/build-vector.sh <geofabrik-area>
./scripts/build-contours.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat> ft
./scripts/build-hillshade.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat>
./scripts/build-elevation.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat>   # optional: terrain analysis

# 2. Start the server
./scripts/data-placeholders.sh   # empty extras/usdata/elevation files unless you built them (needed after updating, too)
cp .env.example .env          # optional: ports, config file, image
./scripts/mem-limit.sh        # memory cap from this machine's RAM (see "Use with TAK clients")
docker compose up -d --build

# 3. Use it
#    http://localhost/            style list and map viewer
#    http://localhost/mapprinter/ print a map or an atlas
#    http://localhost:8080/       TileServer GL directly (TAK raster tiles)
```

The web front (nginx) serves the print tool at `/mapprinter/` and proxies everything else to TileServer GL, so the print
tool finds its tiles on the same host automatically. It also looks under `/maps/` (the Internet-in-a-Box layout) and on
port 8080, or you can point it anywhere with `?tiles=http://host:8080`.

### Disk space

Building needs much more space than the finished files take. Each build script estimates what its step needs and
stops with a clear message if the drive is too small. From a six-state build (about 64 square degrees):

| Step | Finished file | Working space needed |
|---|---|---|
| Base map | ~1 GB | ~15 GB |
| Contours | ~2 GB | ~40 GB |
| Hillshade | ~0.5 GB as PNG; about a quarter as lossy WebP (the default) | ~15 GB |
| Elevation (zoom 12) | ~0.05-0.15 GB | the elevation downloads only |

Finished downloads are kept in `data/sources` and `data/cache` and reused.

## Repository layout

| Path | What it is |
|---|---|
| `docker-compose.yml`, `.env.example` | The services: `tileserver`, `web` (print tool + proxy) and `wmm` (magnetic model updates) |
| `config.json`, `config-basic.json` | TileServer GL configs: all six styles and the `topo-usgs` scale bands, or `osm`+`overlay` with only a base map |
| `styles/`, `fonts/`, `sprites/` | The styles and every asset they need, all local |
| `planetiler/` | Custom Planetiler schema for the extras layers |
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
| `topo-usgs` | Second paper style in the manner of a USGS quadrangle: white paper, green woodland, brown contours with monospace elevations, red and black cased roads, railway ties, dashed seasonal water with flow arrows, banded park and military boundaries | all three; optional `extras`, `usdata` |

`topo-print` and `topo-usgs` read the same tile sets, so the print tool can switch between them (the **Map style**
menu; the choice is remembered). `topo-usgs` is generated, not hand-edited:

```
python3 tools/make-usgs-sprite.py   # sprites/usgs*: the topo sprite plus patterns and symbols (needs Pillow)
python3 tools/make-topo-usgs.py     # styles/topo-usgs.json
```

Line widths in the generator are millimetres on paper at 1:24,000 and scale gently with zoom. Route shields come from
the topo sprite, except the interstate shield (blue and red, white numbers), which `make-usgs-sprite.py` draws.
Motorway exit numbers, runway lengths and the grey large buildings (5,000 m² and more) need `data/extras.mbtiles`.
Every layer carries a `fieldmap:group` in its metadata (roads, trails, contours, parks, pois, ...), which the print
tool's **Layers** panel switches. Its fonts are Roboto Condensed and Atkinson Hyperlegible Mono (SIL Open Font
License, texts in `fonts/`). Not done yet: deciduous and conifer woodland (OpenMapTiles carries no leaf type) and
spot heights chosen by prominence (needs a hilltop set derived from the elevation data).

## The print tool

- Pick scale, paper, orientation and quality (200 or 300 dpi); drag the blue outline to place the sheet.
- Every sheet is at exact scale: the map zoom is computed from scale, dpi and the centre latitude. One CSS pixel is
  1/100 inch on paper, so print zooms are 1:12k ≈ z14.2, 1:24k/25k ≈ z13.2, 1:50k ≈ z12.2, 1:100k ≈ z11.2.
- MGRS/USNG grid of the sheet's UTM zone, 1 km lines with blue principal digits in the margin, 100 m ticks at 1:12k,
  10 km lines bold at 1:100k, extended across a zone boundary when a sheet straddles one.
- Declination from the World Magnetic Model for the print date (no network needed): TN, GN and MN drawn at true
  angles, the G-M angle, and which way magnetic north moves each year. See "Magnetic model updates" below.
- Markers (12-colour palette, draggable) and arrows (tap start, tap end, drag the ends) labelled with name, range and
  true/magnetic bearing; grid bearing in the side panel. Optional list page with MGRS, lat/lon and bearings.
- Shapes: circles (tap the centre, tap the edge, then type an exact radius), rectangles (two opposite corners) and
  polygons (tap the corners; finish on the first corner, with Enter or with Finish shape). Measurements, on screen,
  on every printed page and on the list page (a checkbox leaves them off the map; the list page always has them):
  a circle shows its radius and area; a rectangle its width and height, each beside one of its sides, and its area;
  a polygon the length of every side, beside that side, and its area. On paper the name sits in the middle, side
  lengths just outside their sides, and the radius or area once inside the shape along its lowest edge (curved along a
  circle). When part of a shape is off the sheet, the labels move onto the part that is printed: the radius or area
  follows the printed part of the outline, and a rectangle uses its north or west side when the south or east side
  is off the sheet. A Units menu switches between imperial
  (ft, mi, acres) and metric (m, km, ha); the circle's radius can be typed in either unit of the chosen system.
- Atlas mode: 2×2, 3×3, 4×4, 6×6 or 8×8 pages with 1 cm overlap, rows A… from the north and columns 1… from the west.
  A cover page built from the map data (places, peaks, lakes, rivers inside the atlas, a locator map), an index page
  with the page grid, neighbour tabs on every page, optional binder margin. Rendering is sequential with progress and
  a Cancel button; a laptop is recommended for atlases.
- Buildings at 1:24k/25k: tiles below z14 carry only large buildings, so the tool draws the building layer again from
  the z14 tiles. For a basemap that has houses at z13 as well, add `--building-merge-z13=false --min-feature-size=0.25`
  to the Planetiler arguments in `scripts/build-vector.sh` / `build-region.sh`.
- Layers panel: switch groups of layers off (woodland, roads, trails, place names, ...), print in colour or black and
  white, hillshade off/soft/sharp, contours off/index lines only/all, MGRS grid lines on or off (the edge numbers
  stay). The switches apply on screen and to every printed page, including atlas, index and cover pages, and are
  remembered with the other settings. Both print styles carry the groups: `topo-usgs` from its generator, `topo-print`
  from `tools/tag-print-groups.py` (run it again after editing that style by hand). In black and white, raster icons
  and area patterns keep their colours.
- The contour interval and data date in the margin come from `print/public/config.json`: set `contourMinor`,
  `contourMajor` and `contourUnit` to match your `build-contours.sh` run (the defaults are 40 and 200 ft), and
  `dataDate` to the date of your OpenStreetMap extract, for example `"OSM 2026-01"` (empty by default). Then rebuild
  the print tool (`docker compose up -d --build`, or `install-pi.sh` on a Pi). The credit line names OpenMapTiles
  and OpenStreetMap plus every other data set the style reads (from each tile set's attribution; empty files add
  nothing).
- Print scale bands: the generator also writes `topo-usgs-12k`, `-24k`, `-50k` and `-100k`, with every line width
  and text size fixed at its value for that scale (at 40°N). The print tool uses the band nearest the sheet's
  scale when the server has it, so a 1:24,000 sheet has the same line weights at any latitude; the screen map keeps
  the zoom-based `topo-usgs`. The band styles are listed with `serve_rendered: false` (no raster rendering).
- Development: `cd print && npm install && npm run build`; `npm run serve` serves `dist/` on :8090
  (`http://localhost:8090/?tiles=http://localhost:8080`).

### Terrain analysis: viewshed and radio coverage

With `data/elevation.mbtiles` on the server (`build-elevation.sh`, or the `elevation` step of `build-region.sh`) the
panel has a **Terrain** section, collapsed at first. Without the file the section is not shown and nothing else
changes. Everything runs in the browser, in a web worker, from the server's elevation tiles: no internet.

- **Viewshed**: what can be seen by eye from a point. Place the observer (tap **Place**, then the map; drag it to
  move it), set the observer's height above ground (6 ft standing) and the target's (0 ft = can I see the ground there;
  presets 6 ft person and 30 ft building, or any value) and a radius of 1 to 25 km. The overlay shows the ground that
  is visible, and with a target height also where the target is visible but the ground beneath it is not.
- **Radio**: where a handheld, vehicle or base radio can likely reach on VHF and UHF. Frequency presets: MURS 151 MHz,
  2 m amateur 146 MHz, GMRS/FRS 462 MHz, 70 cm amateur 440 MHz and 915 MHz ISM (LoRa, Meshtastic), or any frequency
  from 30 to 3000 MHz; antenna heights handheld 5 ft, vehicle roof 6 ft, mast 30 ft or any value (the receiver's height
  applies to every place on the map). Each place is classed by its path from the transmitter: **clear** (line of
  sight with 60 % of the first Fresnel zone clear), **marginal** (line of sight, Fresnel zone partly blocked),
  **diffraction** (one ridge in the way: extra loss by single knife-edge diffraction, ITU-R P.526, at the worst
  obstacle) or **blocked** (more than 20 dB, or two or more ridges cut the line of sight). Under **Link budget**
  (transmit power, antenna gains, cable loss, receiver sensitivity; the defaults are a 5 W handheld with its stock
  antenna) the map is coloured by link margin instead: received level = power + gains − cable loss − free-space loss
  (ITU-R P.525) − diffraction loss, against the sensitivity, in four bands: comfortable (20 dB or more to spare),
  workable (10-20 dB), unlikely (0-10 dB), none. CB and HF are not offered: their ground wave and skywave are outside
  this model.
- **Line A to B** (both modes): an elevation profile between two points, with the earth's curvature drawn as a bulge,
  the straight sight line or radio path between the antennas, for radio the 60 % and 100 % first Fresnel zone, and the
  worst obstruction marked (also on the map). Read-outs: distance, bearing (true and magnetic), clearance at the worst
  point as a share of the Fresnel zone, free-space and diffraction loss, and the link margin when a budget is set.
- **Advanced**: the earth's curvature with refraction is an earth of k × 6,371 km: k = 7/6 for sight and 4/3 for radio
  by default.
- Heights and distances follow the **Units** setting (feet and miles, or metres and kilometres); points are shown in
  MGRS and latitude/longitude. Settings and points are remembered with the other print settings.
- **On paper**: the overlay, the radius ring and the points print on the sheet and on every atlas page and the index
  (the Layers panel switches them off under "Terrain analysis"; in black and white the overlay prints as hatching and
  stipple, not a tint). The collar gets a strip with the inputs (mode, heights, frequency, radius, k, the link budget),
  a legend or the line's read-outs, the date of the elevation data and its notice. **Print the profile as an extra
  page** adds the A-to-B chart and read-outs after the map, like the list page.

**Limits** (also stated in the panel and on paper): it is a simple terrain-only model: no trees, buildings or clutter
loss, no ground reflection, no ducting, and only single-edge diffraction at the worst obstacle. Real coverage is
usually worse in forest and towns. It is an estimate for planning, not a guarantee. The elevation data is Copernicus
GLO-30, a surface model at 30 m: it holds some of the height of forest and large buildings, smoothed, and none of
anything smaller. A more complete radio model is Longley-Rice (the Irregular Terrain Model, ITM); NTIA's reference
implementation is public domain, and adding it, compiled to WebAssembly, is a possible later step. It is not built.

How it works: the worker fetches the elevation tiles around the point (cached for the session), resamples them to a
square grid in metres around the point at the data's resolution (about 30 m at zoom 12), and runs an R2 viewshed (a
ray from the observer to every edge cell; each cell takes the answer of the ray passing closest to its centre) or, for
radio, radials from the transmitter one grid cell apart at the edge, testing the path to every point on them. Radio
work grows with the cube of the radius, so past 12 km its grid coarsens to radius / 400 (63 m at 25 km). Moving a point
or changing an input cancels a run in progress. Measured on a desktop browser (a mid-range phone is perhaps three
times slower; not measured on one): viewshed 10 km 0.2 s, 25 km 0.8 s; radio 10 km 0.9 s, 25 km 1.6 s.

Checks (`cd print && npm test`, Node 22 or later): synthetic terrain with a known answer (cone, conical pit, pit with
vertical walls, single ridge, the radio horizon over flat ground) and the formulas against ITU-R P.525 and P.526 worked
values. Against `gdal_viewshed` on the same 30 m grid, 10 km radius, two areas (flat coastal plain, ridge and valley),
three observer and target settings: 98.8-99.95 % of the cells agree, and the visible areas overlap 82-91 %
(intersection over union); the differences are cells at the edges of visible areas, where the two algorithms
interpolate differently. Built from the tiles instead of the source, the agreement drops by at most 0.3 points. To
repeat it: `tools/viewshed-compare.py` (in the tool container) and `print/test/compare-viewshed.mjs`. A radio profile
across a ridge checked by hand from the source elevation data: diffraction loss 10.5 dB by hand, 10.4 dB in the tool
(the difference is the tiles' 30 m resampling).

### Magnetic model updates

The print tool has the World Magnetic Model built in (WMM-2025, valid to November 2029) and works out declination
for the day you print, so the angles move with time on their own. A model is published every five years. From
180 days before the model in use runs out, a notice at the top of the panel says so, with an **Update magnetic
model** button. The button asks the server's update service (`tools/wmm-update.py serve`; the `wmm` container, or
`fieldmap-wmm.service` on the Pi) to download a newer model. It tries these sources in order:

1. NOAA NCEI, the publisher: the coefficient file and NOAA's official test values
2. PyPI, the `pygeomag` package: a copy of NOAA's coefficient file
3. npm, the `geomagnetism` package: the coefficients as JSON

A downloaded model must pass NOAA's test values (when the download has them) and agree with the model it replaces,
then it is written to `data/wmm/wmm.json`, which the print tool loads at start-up. No rebuild is needed. If NOAA
answers with nothing newer, nothing changes. Without Internet, or when no source answers, the model you have stays
in use, even past its end date: declination is then carried forward at the model's last rate of change, and the
sheet's collar says the model has expired.

From the command line on the server (Docker: `docker exec fieldmap-wmm python3 /app/tools/wmm-update.py update`):

```bash
python3 tools/wmm-update.py status      # model in use, its dates, the last check
python3 tools/wmm-update.py update      # download a newer model if there is one
```

The service has no settings and answers only `GET /status` and `POST /update` (behind nginx at
`/mapprinter/wmm-api/`). It is meant for a private network; put the server behind your own access control if
others can reach it.

### Contour interval

The contours are a fixed set built from the elevation data (`build-contours.sh`: 40 ft with 200 ft index lines,
or metres), and the Layers panel can thin them to index lines only. A contour interval chosen per sheet from the
terrain (about 2.4 mm apart on paper for "sparse", 1.2 mm for "full", rounded to a standard interval and printed
in the collar) would need contour lines computed in the browser from an elevation tile set. That is not built: a
phone would have to fetch and trace elevation tiles for every page of an atlas, and the Pi would serve a third
large tile set. The planned route is two or three pre-built contour sets (for example 20, 40 and 100 ft) and a
choice per scale in the print tool.

## Build scripts

### `build-vector.sh [--world] [--language <code>] <area>`

Builds `data/basemap.mbtiles` with [Planetiler](https://github.com/onthegomap/planetiler) 0.10.2 (set `PLANETILER_IMAGE` to
use another release).
`<area>` is a Geofabrik path such as `europe/switzerland`, or just its last part (`switzerland`), which is looked
up in Geofabrik's index. The OpenStreetMap extract comes from the fastest mirror (see
[OpenStreetMap download mirrors](#openstreetmap-download-mirrors)).
For `--world` and `--language`, see [World layer and label language](#world-layer-and-label-language).
The output uses the OpenMapTiles schema.
The first run downloads about 1.5 GB of shared world data (coastlines, lakes).
This data stays in `data/sources` for the next runs.
Large areas need more memory: `PLANETILER_MEMORY=8g ./scripts/build-vector.sh germany`.

### Terrain outline (contours, hillshade, elevation)

The three terrain builds take an optional `--areas "<geofabrik paths>"` before the box (`build-region.sh` passes the
region file's `GEOFABRIK_AREAS`). They then cover only the areas' outlines inside the box: the Geofabrik `.poly`
files, merged when there are several, downloaded once to `data/cache/outlines`. Without `--areas`, or when an outline
cannot be downloaded, they cover the whole box. In detail (`tools/region-outline.py`):

- Only the 1° Copernicus cells that touch the outline are downloaded and processed; everything outside the outline
  is masked (no contour lines, transparent hillshade, no-data elevation).
- Cells for which Copernicus has no file (open sea, mostly) are listed in `data/cache/dem/ocean-cells.txt` and not
  requested again. Delete a line to try that cell again.
- An outline that reaches past the 180° line, like Alaska's Aleutians, is built as two parts, west and east of the
  line, into one output file. A box can cross the line as well: give a min-lon larger than the max-lon, for example
  `172 51 -129 72`.
- Before it starts, each build prints the outline, the cells it will download and an estimate of the output size
  and time; `build-region.sh` prints the estimate for all its terrain steps first. The estimates come from the test
  builds below and give a range from flat to rugged terrain.

Test builds on an 8-core desktop (sizes of the finished files; times include the tool container's start):

| Test area | Outline | Contours (40 ft) | Hillshade (lossy WebP) | Elevation (zoom 12) |
|---|---|---|---|---|
| Mountains and sound against an international border (one US state's outline, cut to a 2.2° × 1.4° box) | 12,134 km², 1 part | 61 MB, 109 s | 2.4 MB, 10 s | 16.6 MB, 15 s |
| One Aleutian island west of 180° (mostly sea) | 13,210 km², 1 part | 9.0 MB, 18 s | 0.5 MB, 6 s | 2.8 MB, 16 s |
| Two Aleutian islands, a box across 180° | 9,650 km², 2 parts | 1.9 MB, 9 s | 0.1 MB, 4 s | 0.7 MB, 4 s |

In the first area nothing reaches past the border (the last valid pixel is within one 30 m pixel of the outline), and the two
parts of the third join at 180° without a seam. The hillshade of the first area is 9.4 MB as lossless WebP and
10.4 MB as PNG.

### `build-contours.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]`

Builds `data/contours.mbtiles` from Copernicus GLO-30 elevation data.
Units are meters by default (minor 20 m, major 100 m).
Use `ft` for feet (minor 40 ft, major 200 ft):

```bash
./scripts/build-contours.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat> ft
```

### `build-hillshade.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat>`

Builds `data/hillshade.mbtiles` from the same elevation data: gray tiles from zoom 6 to `HILLSHADE_MAXZOOM` (default
12; the print styles stretch zoom 12 over the higher zooms without visible loss on paper), transparent outside the
outline. `HILLSHADE_FORMAT` chooses the encoding: `webp` (lossy, quality 80; the default), `webp-lossless` or `png`.

A hillshade file built before this option (PNG to zoom 13) can be converted in place of a rebuild:

```bash
python3 tools/hillshade-webp.py --maxzoom 12      # data/hillshade.mbtiles -> data/hillshade-webp.mbtiles
mv data/hillshade-webp.mbtiles data/hillshade.mbtiles && docker restart tileserver
```

The terrain scripts build a small tool container (GDAL + Tippecanoe) on first run.
Downloaded elevation tiles stay in `data/cache/dem` for reuse.

### `build-elevation.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> [maxzoom]`

Builds `data/elevation.mbtiles` from the same elevation data as the contours and hillshade: heights in metres for the
print tool's terrain analysis (viewshed and radio coverage). Tiles are 256 px, Web Mercator, from zoom 8 to `maxzoom`
(default 12, or `ELEVATION_MAXZOOM`), for every tile that touches the outline. Encoding: Mapbox terrain-RGB
(height = -10000 + (R × 65536 + G × 256 + B) × 0.1 m) in lossless WebP; R = G = B = 0 marks no data. Set
`ELEVATION_ENCODING=terrarium` and/or `ELEVATION_FORMAT=png` for the other choices; all four keep the heights to
0.1 m or better and TileServer GL serves all four. WebP was the smallest (about 30 % below PNG) on both test areas.
At `maxzoom` each pixel is the elevation data's value at the pixel centre; lower zooms average it.

Zoom 12 is about 30 m per pixel at US latitudes (38 m at the equator), like the source data; zoom 11 is about 60 m
and about a quarter of the size. Measured sizes (all zooms), and what they suggest for larger areas:

| Area type | Zoom 12 | Zoom 11 | A 150,000 km² state at 12 / 11 | Lower 48 at 12 / 11 |
|---|---|---|---|---|
| Ridge-and-valley (hills, 300-700 m relief) | 1.4 kB per km² | 0.4 kB per km² | ~210 MB / ~65 MB | ~11 GB / ~3.4 GB if all like this |
| Flat coastal plain with open water | 0.5 kB per km² | 0.13 kB per km² | ~80 MB / ~20 MB | ~4.4 GB / ~1 GB if all like this |

A whole country of mixed terrain falls in between (for the lower 48, roughly 8 GB at zoom 12 and 2-3 GB at zoom 11).
The step writes the tiles straight from the downloaded elevation data, so it needs little working space.
Check a build against the elevation data with `tools/check-elevation.py` (see the comments at its top): at pixel
centres the tiles and the source agree to within 0.05 m.

Without this file the print tool hides its terrain analysis and nothing else changes; `data-placeholders.sh`
writes an empty one, because `config.json` lists it. In `config.json` the source carries `"tileSize": 256`, which
TileServer GL's own elevation endpoint (`/data/elevation/elevation/<z>/<lon>/<lat>`) needs.

### `build-extras.sh [--bounds=<min-lon>,<min-lat>,<max-lon>,<max-lat>] <osm-file>`

Builds `data/extras.mbtiles` from an OSM extract already in `data/sources`: OpenStreetMap features
that the OpenMapTiles base map leaves out but that matter on a paper map. The layers are defined in
`planetiler/extras.yml` (Planetiler's custom schema format) and drawn by `topo-usgs`:

| Layer | Features | On the map |
|---|---|---|
| `power` | power lines (voltage), minor lines, cables, towers, substations, power plants | orange-red dashes on a white casing, heavier at 115 kV and above; towers as dots |
| `pipeline` | pipelines (substance, underground or not) | purple long dash-dot, fainter when underground |
| `barrier` | fences, walls, hedges, guard rails, gates | thin black line with ticks (fences), from 1:12k |
| `tower` | towers, masts, chimneys, water towers, tanks, silos (height) | ring-and-dot symbol, height in feet |
| `landmark` | springs, waterfalls, caves, sinkholes, wells, cairns, survey points, viewpoints, ruins | springs in blue, survey points as triangles with elevation |
| `building_large`, `exit`, `runway` | buildings of 5,000 m² and more, motorway junctions, runways | grey large buildings, exit numbers, runway length in feet |

`build-region.sh` (its `extras` step, on by default) and `build-vector.sh` (unless `NO_EXTRAS=1`) run it for you
after the base map. Without it `data/extras.mbtiles` is an empty placeholder (written by
`scripts/data-placeholders.sh`, which the build scripts and `install-pi.sh` call): the tile server needs every file
`config.json` lists, and the layers are simply blank. The OpenMapTiles base map has no power lines, pipelines,
fences or gates, so a map built before this step was on by default shows none: run
`./scripts/build-region.sh <region-file> extras` once and copy `data/extras.mbtiles` to the server.

Size: about 1 MB per 2,000 square miles of US countryside (roughly 70 MB for a six-state
region, 1-2 GB for the whole US), most of it z14 fences, gates and power towers.

```bash
./scripts/build-region.sh <region-file> extras    # only this step; reuses the merged OSM file of the vector step
./scripts/build-extras.sh <name>.osm.pbf          # any extract in data/sources, e.g. after build-vector.sh <name>
```

### `build-usdata.sh <region-file> [obstacles] [names] [marks] [protected] [flood]`

Optional, US only. Builds `data/usdata.mbtiles` from US federal data (public domain) for the region file's `BBOX`
and states (`US_STATES`, or the US states in `GEOFABRIK_AREAS`). Each step downloads its source, keeps it in
`data/cache/usdata`, and tiles it; the results are joined into one file, and a step whose source is down is
skipped without losing the others. Drawn by `topo-usgs`, each in its own Layers group:

| Step | Source | On the map |
|---|---|---|
| `obstacles` | [FAA Digital Obstacle File](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dof/) (daily, whole US) | chart obstacle symbol, height above ground in feet |
| `names` | [USGS GNIS](https://www.usgs.gov/tools/geographic-names-information-system-gnis) Domestic Names, per state | terrain names (gap, ridge, valley, summit ...) in italic black, water names (bay, rapids, falls ...) in blue, springs with the spring symbol |
| `marks` | [NOAA NGS](https://geodesy.noaa.gov/) survey mark shapefiles, per state | bench marks as "BM 412" (feet), horizontal control as triangles |
| `protected` | [USGS PAD-US 4.1](https://www.usgs.gov/programs/gap-analysis-project/science/protected-areas) fee lands, `Pub_Access` | band inside the edge: purple open or unknown access, dashed edge restricted, rust closed (`XA`) |
| `flood` | [FEMA National Flood Hazard Layer](https://www.fema.gov/flood-maps/national-flood-hazard-layer) | ruled blue floodway, dotted blue edge of the 1%-annual-chance (100-year) zone |

The default steps are the first four; `flood` runs only when named, because flood zones come through FEMA's web
service in 1° cells, 200 features at a time (allow an hour or more for a large state). PAD-US comes from the USGS
web service one state at a time; it holds fee lands only, so easements are not drawn. Both services refuse large
queries, so each state or cell is fetched on its own, retried with smaller pages, and kept in
`data/cache/usdata/<step>/`: when a piece still fails, the build says so, and running the same step again fetches
only the missing pieces (delete that folder to fetch everything fresh). Flood zones on the map are not an official
FEMA determination.

Size measured on a 30 × 28 km box around a small city: 2.2 MB (flood zones half of it, protected land most of
the rest). Scaled to a six-state region, expect a few hundred MB without `flood` and about 1 GB with it; rural
areas are far smaller.

```bash
./scripts/build-usdata.sh <region-file>            # obstacles, names, marks, protected
./scripts/build-usdata.sh <region-file> flood      # add flood zones
```

### `build-region.sh [--world] [--language <code>] <region-file> [vector] [contours] [hillshade] [elevation] [extras]`

Builds the data files for a region described in a region file: base map, contours, hillshade, elevation and extras.
A region can cover several Geofabrik extracts, for example several US states.
The script downloads each extract, merges them into one file with Osmium,
and runs Planetiler on the merged file. Then it builds contours, hillshade and
elevation tiles for the areas' outlines inside the region's bounding box (see "Terrain outline").

```bash
./scripts/build-region.sh <region-file>
```

Add step names to run only some steps:

```bash
./scripts/build-region.sh <region-file> contours hillshade
```

#### Region files

A region file is a short shell file. Write your own for your area, or start from one of the two examples in
`regions/`: `mid-atlantic.env` (an example of several US states) and `us-lower48.env` (an example of a whole
country on top of the world layer; its comments list sizes). A region file sets these values:

| Variable | Meaning |
|---|---|
| `REGION_NAME` | Short name for the merged OSM file |
| `GEOFABRIK_AREAS` | Geofabrik paths, for example `north-america/us/new-jersey` |
| `BBOX` | Box: `min-lon min-lat max-lon max-lat`. The base map stops at it; the terrain covers the areas' outlines inside it. A min-lon larger than the max-lon crosses the 180° line |
| `CONTOUR_UNITS` | `ft` or `m` (default `ft`) |
| `HILLSHADE_FORMAT` | `webp` (default, lossy), `webp-lossless` or `png` |
| `HILLSHADE_MAXZOOM` | Highest zoom of `hillshade.mbtiles` (default 12) |
| `ELEVATION_MAXZOOM` | Highest zoom of `elevation.mbtiles`: 12 (default, about 30 m) or 11 (about 60 m, a quarter of the size) |
| `PLANETILER_MEMORY` | Java heap for Planetiler (default `8g`) |
| `WORLD` | `yes`: the whole world at zoom 0-8, the region from zoom 9 (same as `--world`) |
| `LANGUAGE` | Keep labels in one language only (same as `--language`) |
| `US_STATES` | Two-letter state codes for `build-usdata.sh` (default: the US states in `GEOFABRIK_AREAS`) |

Planetiler output is limited to the region's `BBOX` (for a box across the 180° line, its wider side).

Planetiler also needs three shared inputs (water polygons, Natural Earth,
lake centerlines). The script downloads them to `data/sources` before it runs
Planetiler, so Planetiler itself makes no network calls. Each URL gets several
attempts. If the primary source still fails, the script tries the next URL in
the `SHARED_SOURCES` list in `scripts/lib.sh`. The water polygons
fall back to the North America mirror at `www.osmdata.org`. Files that are
already present are reused. Add more mirrors to that list if a source is
unreliable.

### `merge-osm.sh <output-name> <geofabrik-path> ...`

Downloads Geofabrik extracts to `data/sources/extracts` and merges them into
`data/sources/<output-name>.osm.pbf`. `build-region.sh` and `build-vector.sh` call it for you.

### World layer and label language

Two options work the same for `build-region.sh` and `build-vector.sh` (in a region file: `WORLD=yes`,
`LANGUAGE=<code>`; a flag on the command line wins):

- `--world`: the map covers the whole world from zoom 0 to 8, and your region from zoom 9 up. Zooming out past your
  region shows continents, countries, cities, major roads, borders and water everywhere, instead of empty space.
  The world part comes from `build-world.sh`, which runs on its own the first time: it downloads the whole
  OpenStreetMap planet (about 96 GB, from the fastest fresh planet mirror; an interrupted download resumes),
  keeps only what shows at zoom 0-8 with `osmium tags-filter`, deletes the planet file and builds
  `data/world-z0-8.mbtiles`. Allow several hours and about 160 GB of free disk for that first run. Later regions
  reuse the world layer; delete `data/world-z0-8*.mbtiles` and `data/sources/planet-z0-8.osm.pbf` to rebuild it from
  a newer planet. The region is built at zoom 9-14 and the world tiles are copied into the same `basemap.mbtiles`,
  so nothing else changes for the tile server or the print tool. Contours and hillshade stay region-only.
- `--language <code>`: keep map labels in one language only, to save space. The codes are the ten most spoken
  languages by total speakers (Ethnologue 2025): `en` English, `zh` Chinese, `hi` Hindi, `es` Spanish, `ar` Arabic,
  `fr` French, `bn` Bengali, `pt` Portuguese, `ru` Russian, `id` Indonesian. Without it every language is kept.
  The styles show the chosen language where a name exists in it and the local name otherwise. With all languages
  kept, they show English. The topo styles label places with their local names either way, like a printed topo
  map. A world layer is built per language (`world-z0-8-<code>.mbtiles`).

```bash
./scripts/build-region.sh --world --language <code> <region-file>
./scripts/build-vector.sh --world <geofabrik-area>
```

### OpenStreetMap download mirrors

Geofabrik can be slow, so each extract can come from one of three servers:
[Geofabrik](https://download.geofabrik.de), [Movisda](https://osm.download.movisda.io) and
[geo2day](https://geo2day.com). All three take the same Geofabrik paths, update daily and serve the same OpenStreetMap
data, cut to slightly different boundaries.

Before the first download, the build downloads the first 2 MB of that extract from each server and ranks them by
speed. Each extract then comes from the fastest server whose copy is at most 30 days old (from its `Last-Modified`
date). A server that is slow to answer, lacks the area, or has an older copy is skipped for the next one. Set
`OSM_MAX_AGE_DAYS` to change the limit. The list is `OSM_MIRRORS` in `scripts/lib.sh`.
Extracts already in `data/sources/extracts` are reused; delete that folder to download fresh copies.

## Bring your own data

You do not have to use the build scripts.
Put your own files in `data/` with these names:

| File | Type | Content |
|---|---|---|
| `data/basemap.mbtiles` | vector | OpenMapTiles schema base map |
| `data/contours.mbtiles` | vector | layers `contours_major` and `contours_minor`, attribute `elev_ft` (feet) or `elev_m` (meters) |
| `data/hillshade.mbtiles` | raster | gray hillshade tiles, WebP or PNG |
| `data/extras.mbtiles` | vector | the layers of `planetiler/extras.yml` (built by default); `./scripts/data-placeholders.sh` writes an empty one |
| `data/usdata.mbtiles` | vector | optional: layers `obstacle`, `gnis`, `survey_mark`, `protected`, `flood` (see `tools/us-data.py`); empty from the same script |
| `data/elevation.mbtiles` | raster | optional: heights in metres as Mapbox terrain-RGB or Terrarium (metadata `encoding`), PNG or WebP, 256 px tiles; empty from the same script |

`extras.mbtiles` and `usdata.mbtiles` cover only the region they were built for (its OSM extract and `BBOX`), even
when the base map covers more, for example the whole world at zoom 0-8 with `--world`. Outside that region the
extra layers are simply blank. To widen them, build them again from a larger region file.

The hillshade source is marked `"sparse": false` in `config.json`. Outside the area it covers, the tile server
answers with a transparent tile, so the topo styles still render there, without shading.

The contour labels read `elev_ft` first, then `elev_m`.
`ft` builds write `elev_ft`. `m` builds write `elev_m`.
Contour files from other builders work too if they carry one of those attributes.

## Use with TAK clients

TileServer GL serves rendered raster tiles for each style, on x86 and ARM alike.
Add a map source in ATAK/WinTAK/iTAK with this URL pattern:

```
http://<server-host>:8080/styles/topo-light/{z}/{x}/{y}.png
```

On a Raspberry Pi installed with `scripts/install-pi.sh`, the tile server sits behind nginx at `/maps/` instead:

```
http://<pi-host>/maps/styles/topo-light/{z}/{x}/{y}.png
```

Replace `topo-light` with `osm`, `topo-dark`, or `overlay` for the other styles.
For higher resolution screens, use `.../{z}/{x}/{y}@2x.png`.

Both configs are set for devices with little RAM, such as a Raspberry Pi. No raster renderer starts with the server;
one is made the first time a style is asked for, so styles nobody uses take no memory. Each style gets at most two
renderers for normal tiles and one for `@2x` (`minRendererPoolSizes`, `maxRendererPoolSizes`), and `@3x` tiles are
off (`maxScaleFactor` 2). Renderers stay in memory once made. On a large server with many TAK clients, raise the
maximums.

The tile server container also has a hard memory cap with no swap, `TILESERVER_MEM_LIMIT` in `.env`.
`scripts/mem-limit.sh` picks it from the machine's RAM: `1g` under 8 GB, no cap (`0`) at 8 GB or more.
If the cap is reached, only the tile server is killed and Docker restarts it within seconds.
Without the script, the cap is `1g`.

## Raspberry Pi behind Internet-in-a-Box

See [pi/README.md](pi/README.md): clone onto the Pi, copy your `.mbtiles` files in, run `sudo ./scripts/install-pi.sh`.
It starts TileServer GL in Docker on localhost, builds the print tool on the Pi, and adds the nginx snippets so
IIAB serves `/maps/` and `/mapprinter/`.
`/print/` is left free, for example for CUPS.

## Updating

Data files are built on a machine with Docker and enough RAM and disk, then copied to the server's `data/` folder.
After `git pull`, the steps are the same whatever region you use.

Docker Compose install:

```bash
cd <repo-dir>
git pull
./scripts/data-placeholders.sh          # empty files for optional data you have not built
docker compose up -d --build
docker restart tileserver               # loads changed config, styles and data
```

Raspberry Pi with Internet-in-a-Box:

```bash
ssh <user>@<pi-host>
cd <repo-dir>
git pull                                # if you edited files on the Pi: git stash -u && git pull && git stash drop
sudo ./scripts/install-pi.sh            # placeholders, print tool, nginx, and a tile server restart when needed
```

New or rebuilt data files (any of `basemap`, `contours`, `hillshade`, `elevation`, `extras`, `usdata`):

```bash
scp data/<name>.mbtiles <user>@<pi-host>:<repo-dir>/data/
ssh <user>@<pi-host> 'cd <repo-dir> && sudo ./scripts/install-pi.sh'     # or: docker restart tileserver
```

## Start at boot

See [systemd/README.md](systemd/README.md).

## Changelog

### Since v1.0.0

- Terrain builds follow the region's Geofabrik outlines instead of its box (no neighbouring countries), skip
  known open-sea cells, handle the 180° line, print size and time estimates first, and write hillshade as lossy
  WebP by default.
- Terrain analysis in the print tool: viewshed and VHF/UHF radio coverage from a point, point-to-point profiles,
  printed on sheets and atlases with a collar strip and an optional profile page; `build-elevation.sh` writes the
  elevation tiles it uses.
- Print styles: `topo-usgs` (USGS-quadrangle look, generated by `tools/make-topo-usgs.py`) with print-scale bands,
  English-only labels in both print styles, and a Map style menu in the print tool.
- Print tool: circles with an exact radius, rectangles and polygons, with imperial or metric units.
- Magnetic model updates: a notice before the model runs out, an update button, and `tools/wmm-update.py` with
  NOAA plus two mirrors; the model now loads from `data/wmm/` without a rebuild.
- Print tool: Layers panel (group switches, black-and-white ink, hillshade and contour controls, grid lines),
  credit line built from the data sets in use; moved from `/print/` to `/mapprinter/`.
- Extra data: `build-extras.sh`, part of the default build (OSM power, pipelines, barriers, towers, landmarks, large buildings, exits,
  runways) and `build-usdata.sh` (FAA obstacles, GNIS names, NGS survey marks, PAD-US access, FEMA flood zones).
- Builds: world layer for zoom 0-8, single-language labels, download mirrors, disk-space checks, region files.
- Raspberry Pi: one-step `install-pi.sh` with a memory cap for the tile server, restarted when its inputs change.
- `tools/hillshade-webp.py` to shrink hillshade files.

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
- Radio formulas: ITU-R P.525 (free-space loss) and P.526 (Fresnel zone, single knife-edge diffraction)
- Elevation data: © ESA, [Copernicus DEM GLO-30](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model).
  Contours, hillshade and elevation tiles are produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus
  Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.
- Optional US layers (public domain, US federal government): FAA Digital Obstacle File, USGS GNIS, NOAA National
  Geodetic Survey, USGS PAD-US, FEMA National Flood Hazard Layer
- World Magnetic Model: NOAA NCEI and the British Geological Survey for the US NGA and UK DGC (public domain)
- Fonts for the print styles: Roboto Condensed and Atkinson Hyperlegible Mono (SIL Open Font License)
- Fonts: [Open Sans](https://fonts.google.com/specimen/Open+Sans) (Apache License 2.0)
- Server: [TileServer GL](https://github.com/maptiler/tileserver-gl) (BSD)

Show the OpenStreetMap attribution when you show these maps in public.
The code in this repository is under the MIT license. See [LICENSE](LICENSE).
