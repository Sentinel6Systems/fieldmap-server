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
- Tested on Ubuntu 24.04 and 26.04 (with Ubuntu's own `docker.io` and `docker-compose-v2` packages): `install.sh`,
  the map builder service, and a build from the map builder page
- Disk: see [Disk space](#disk-space); the build needs far more than the finished files

The server alone (no building) runs on much less: a Raspberry Pi 4 with 4 GB serves a large region (see `pi/`).
Node.js and Pillow are only needed to work on the print tool or the style generators.

```bash
git clone https://github.com/Sentinel6Systems/fieldmap-server.git
cd fieldmap-server
cp .env.example .env          # optional: ports, config file, image
./scripts/install.sh
```

`install.sh` starts everything and keeps it running across reboots: the tile server, the print tool, and the map
builder page (as a systemd service; that part asks for sudo). It ends with the addresses:

```
Maps                    http://<address>/
Print a map             http://<address>/mapprinter/
Tile server (TAK)       http://<address>:8080/
Map builder             http://<address>/mapadmin/
Phones                  http://<address>/phones/
```

The pages share one look and one header, with a link to each of the others (see "The pages and their
header").

A new install has no maps yet, so the Maps page is blank and says so. Build them in one of two ways, a small area first to check
that everything works, then your real region:

- on the **map builder page**: choose regions and layers on a map and press the button (next section);
- or from the command line, with a region file (see "Region files"):

```bash
./scripts/build-region.sh --staged <region-file>
docker restart tileserver
#    or step by step, from a single Geofabrik area plus a terrain bounding box:
./scripts/build-vector.sh <geofabrik-area>
./scripts/build-contours.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat> ft
./scripts/build-hillshade.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat>
./scripts/build-elevation.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat>   # optional: terrain analysis
```

Builds use all the CPU that is free, at low priority: the tile server, the print tool and anything else on the
machine come first when they need it (the build containers get a CPU weight of 128 against Docker's 1024;
`BUILD_CPU_SHARES=1024` gives a build the same weight as everything else).

`./scripts/install.sh --no-builder` leaves the map builder page out (and removes its service if it was installed).
Without systemd, or to do the steps yourself: `./scripts/data-placeholders.sh` (empty files for the data you have
not built, which the tile server needs to start), `./scripts/mem-limit.sh` (memory cap from this machine's RAM, see
"Use with TAK clients"), `docker compose up -d --build`, and `./scripts/admin.sh` for the map builder.

The web front (nginx) serves the print tool at `/mapprinter/` and proxies everything else to TileServer GL, so the print
tool finds its tiles on the same host automatically. It also looks under `/maps/` (the Internet-in-a-Box layout) and on
port 8080, or you can point it anywhere with `?tiles=http://host:8080`.

### Map builder page

`install.sh` runs the map builder as a service, at `http://<this device's address>/mapadmin/`, beside the print
tool (`./scripts/admin.sh` starts it by hand). The page shows a map of the world divided into regions (Geofabrik's extracts: continents,
countries, and states or provinces of the larger countries). A tap on a small region adds it; a tap on a large one
asks whether you want all of it or to choose among its parts, so several US states or several European countries
are a few taps. You choose the regions, tick the layers to build (base map, contour lines,
hillshade, elevation, extras, and the US government data for regions in the United States), choose how detailed
the terrain is (standard; the 10 m USGS data in the United States; or that plus one small area of 1 m hillshade
that you place on the map, 10 to 100 km across), and the page shows how
much disk space and time the build needs next to what the device has free. It will not start a build that does not
fit. One button then starts the build; the page shows its progress and can be closed meanwhile.

What a build from the page does (`tools/admin-server.py`):

- It writes a region file, `data/admin/region.env`, and runs `build-region.sh --staged` with it: the same scripts
  as by hand.
- **New files replace the old ones; they are not merged into them.** A device that holds one country and builds
  another then holds only the other, so the page says this before the build starts. Layers that are not ticked stay
  as they are.
- The new files are written to `data/incoming` and moved into `data/` together when every step has finished, and
  the tile server is then restarted (or started, the first time). Until then, and if a step fails or the build is
  stopped, the maps in use stay untouched. The steps that did finish are kept: starting the same build again
  carries on where it stopped (a different selection starts afresh). The page keeps the last build's choices:
  **Start it again** (or **Repeat the last build** in step 4) fills them back in and asks before it starts.
  **Clear my choices** in step 4 takes every region off and sets the layers and options back to the first visit's.
- The estimate is made before anything is downloaded, from the bundled list of regions (`admin/regions.json`:
  size of each OpenStreetMap extract, number of 1° elevation cells with land, land area) and the same rates as the
  build scripts' own checks. It is on the safe side; the build scripts still run their own checks at each step.
  It follows every change on the page, and the free space on the device. The steps run one after another, so it
  counts the working room of one step at a time. Elevation data that earlier builds downloaded for the chosen
  regions, and what a build that did not finish left behind, are already on the device and not counted again.
  The sharper terrain (USGS 10 m) needs roughly ten times the space of the standard one, mostly for the download
  (about 0.35 to 0.5 GB per 1° cell) and the contour lines' working room; when it is most of the total, the page
  says what the standard terrain would need.

The page is open to everyone who can reach the device, like the tile server and the print tool: it is meant for a
network you trust. Whoever can open it can replace the maps and fill the disk, so on any other network start it
with `ADMIN_PIN=<code>`, and the page asks for that code first. The page has no port of its own: the web front
passes `/mapadmin/` on to it through a socket file in `data/admin`, so nothing needs opening in a firewall beyond
the web port. `ADMIN_PORT=<port>` adds a port of its own, for a machine that runs no web front, and
`ADMIN_HOST=127.0.0.1` keeps that port to the device itself.
These three can also be set in `.env`. The Raspberry Pi install (`install-pi.sh`) does not set the page up: a Pi is
too small to build maps on, so build them on a larger machine and copy the files over (see "Updating"). It runs on the device itself with Python's standard library, not in a
container, because it starts the build scripts; `install.sh` installs `systemd/fieldmap-admin.service` for it. The page needs no internet to open; the
builds do.

`tools/make-admin-regions.py` (run in the tool container) writes `admin/regions.json`, `admin/land.json` and
`admin/places.json` again, to pick up new extracts and current download sizes.

### The pages and their header

| Page | Address | What it is |
|---|---|---|
| Maps | `/` (`/maps/` behind Internet-in-a-Box) | The interactive map, with a menu of the styles and, under **Use in another app**, the tile addresses for TAK clients and other map apps |
| Print a map | `/mapprinter/` | The print tool |
| Map builder | `/mapadmin/` | Choose and build the maps (not on a Raspberry Pi) |
| Phones | `/phones/` | Remote access for the Fieldmap Android app: on or off, pairing, the paired phones (see "Remote access for the Android app") |

Every page has the same header: the name, a link to each of the other pages with the current one marked, and
nothing else to learn. The header finds the other pages by itself on the address it was opened at (`ui/nav.js`): a
page that is not installed gets no link, and the map builder is shown as "not running" when its service is
stopped. The look (colours, type, buttons, form controls) is one file, `ui/fieldmap.css`, used by all three. It
uses the fonts of the device and follows its light or dark setting; nothing is loaded from anywhere else.

**A Home button.** If these pages sit behind a landing page of your own (a start page for the whole device, for
example), set its address in `print/public/config.json` and the header of every page gets a button back to it:

```json
"homeUrl": "/",
"homeName": "Home"
```

`homeUrl` is the address of your landing page (a path on the same device such as `/`, or a full `http://` address);
`homeName` is the word on the button (`Home` when empty). Empty `homeUrl`, the default, means no button. Then run
`./scripts/install.sh` again (`sudo ./scripts/install-pi.sh` on a Pi). To link the other way, point your landing
page at `/` or `/maps/` (add `?style=<id>#<zoom>/<lat>/<lon>` to open a style at a place), `/mapprinter/` and
`/mapadmin/`.

**What keeps TileServer GL's own look.** The Maps page is TileServer GL's front page, replaced through its
`frontPage` option (`ui/maps.tmpl`, set in `config.json` and `config-basic.json`), and the shared files reach it
through its `files` folder. TileServer GL 5.7.0 has no option to replace its other pages, so these look as they
always did and have no header: its style viewer (`/styles/<id>/`), its data inspector (`/data/<id>/`) and the WMTS
and TileJSON documents. The Maps page replaces the viewer for everyday use; the Inspect, TileJSON and WMTS links
under **Use in another app** say that they lead to the tile server's own pages. Opened on the tile server's own
port (8080), the Maps page has no links to the print tool or the map builder, which live on the web port.

### Disk space

Building needs much more space than the finished files take. Each build script estimates what its step needs and
stops with a clear message if the drive is too small. The terrain steps check twice: before the elevation download
(about 30 MB a cell) and again after it, for the working space, counting only the cells that turned out to have land.
From a six-state build (about 64 square degrees):

| Step | Finished file | Working space needed |
|---|---|---|
| Base map | ~1 GB | ~15 GB |
| Contours | ~2 GB | under 16 GB (checked as 0.2 GB per one-degree cell with land) |
| Hillshade | ~0.5 GB as PNG; about a quarter as lossy WebP (the default) | under 4 GB (checked as 0.03 GB per cell with land) |
| Elevation (zoom 12) | ~0.05-0.15 GB | the elevation downloads only |

Finished downloads are kept in `data/sources` and `data/cache` and reused.

## Repository layout

| Path | What it is |
|---|---|
| `docker-compose.yml`, `.env.example` | The services: `tileserver`, `web` (print tool + proxy) and `wmm` (magnetic model updates) |
| `config.json`, `config-basic.json` | TileServer GL configs: all six styles and the `topo-usgs` scale bands, or `osm`+`overlay` with only a base map |
| `ui/` | What every page shares: the look (`fieldmap.css`), the header (`nav.js`), and the Maps page (`maps.tmpl`) |
| `styles/`, `fonts/`, `sprites/` | The styles and every asset they need, all local |
| `planetiler/` | Custom Planetiler schema for the extras layers |
| `scripts/`, `regions/`, `tools/` | `install.sh` (install and update), and the data builders (Planetiler, GDAL, Tippecanoe, Osmium in a tool container) |
| `admin/`, `scripts/admin.sh` | The optional map builder page: choose regions and layers on a map, check the disk space, start the build |
| `print/` | The print tool: `src/`, `public/`, `build.mjs` (esbuild). Built inside the `web` image |
| `web/` | nginx config and the Dockerfile that builds and serves the print tool |
| `pi/` | Raspberry Pi + Internet-in-a-Box deployment: compose file, nginx snippets, notes |
| `systemd/` | Start the compose stack, and optionally the map builder page, at boot |
| `phones/`, `scripts/remote.sh`, `scripts/firewall.sh`, `tools/remote-server.py`, `tools/portmap.py` | Remote access for the Android app: the Phones page, pairing, the router's port, the gateway, the box's firewall |
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
  true/magnetic bearing; grid bearing in the side panel. Optional key page: at the top, the map symbols the printed
  pages show (icon and meaning, gathered while the pages render: only the ones that are on them), then the list of
  markers, arrows and shapes with MGRS, lat/lon and bearings.
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
- Save / Load markers, arrows & shapes (only those, not the map or the print settings): they are kept in the
  browser only, so the panel saves them
  to a GeoJSON file (`<title>-<date>.geojson`) and loads one again, on this device or another. Loading adds the
  items to those on the map. The file keeps everything exactly (a circle's centre and radius, a rectangle's corners,
  names and colours) and is plain GeoJSON for other map software too; files from such software load as well: points
  as markers, two-point lines as arrows, polygons as polygon shapes (anything else is left out, and the panel says
  how much). A polygon of more than 24 sides, such as a boundary loaded from other software, shows its area but no
  side lengths. See `print/src/drawings.js`.
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
- Development: `cd print && npm install && npm run build`; `npm run serve` serves `dist/` on :8731
  (`http://localhost:8731/?tiles=http://localhost:8080`).

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
| Mountains and sound against an international border (one US state's outline, cut to a 2.2° × 1.4° box) | 12,134 km², 1 part | 61 MB, 47 s | 2.4 MB, 7 s | 16.6 MB, 15 s |
| One Aleutian island west of 180° (mostly sea) | 13,210 km², 1 part | 9.0 MB, 9 s | 0.5 MB, 6 s | 2.8 MB, 16 s |
| Two Aleutian islands, a box across 180° | 9,650 km², 2 parts | 1.9 MB, 5 s | 0.1 MB, 4 s | 0.7 MB, 4 s |

In the first area nothing reaches past the border (the last valid pixel is within one 30 m pixel of the outline), and the two
parts of the third join at 180° without a seam. The hillshade of the first area is 9.4 MB as lossless WebP and
10.4 MB as PNG.

### Higher-resolution elevation in the United States (`DEM_SOURCE=usgs`)

By default the terrain is built from Copernicus GLO-30: 30 m, the whole world, and the surface as a radar saw it,
with the tops of trees and roofs. For the United States there is finer data of the bare ground: the USGS 3D
Elevation Program's 1/3 arc-second layer, about 10 m, made mostly from lidar. Set `DEM_SOURCE="usgs"` in the region
file (or `DEM_SOURCE=usgs` in front of one of the three terrain scripts) to build contours, hillshade and elevation
tiles from it:

- Each 1° cell is built from its USGS tile where USGS has one. A cell without a tile uses Copernicus, and so do the
  parts of a tile that have no data (sea, the far side of a border, land USGS has not covered): the Copernicus cell
  is laid underneath. Cells without a tile are listed in `data/cache/dem-usgs/no-tile.txt` and not asked for again.
- The hillshade goes to zoom 14 and the elevation tiles to zoom 13 unless `HILLSHADE_MAXZOOM` or `ELEVATION_MAXZOOM`
  says otherwise (zoom 12 would throw the extra detail away). Each zoom more is about four times the size.
- The contour lines keep the same intervals. tippecanoe thins them with half its usual tolerance
  (`--simplification=0.5`), so that the finer shapes stay.
- Where a USGS cell meets a Copernicus cell the two surfaces differ, by the height of the trees for one thing: the
  hillshade shows a faint line there and the contour lines do not join. Inside the USGS data everything joins.
- The USGS tiles are in NAD 83 and NAVD 88 heights, Copernicus in WGS 84 and EGM2008 heights. Both differences are
  about a metre or less and are not corrected. The 1 m lidar layer is far too large to build a region from; it
  can be used for one small area (next section).
- The tiles are large: 90 to 480 MB for each cell of land, kept in `data/cache/dem-usgs` (delete the folder after
  the build to get the space back; a rebuild downloads them again).

The first test area of the table above (12,134 km², six cells), both ways:

| | Copernicus, 30 m | USGS 3DEP, 10 m |
|---|---|---|
| Download | 0.15 GB | 1.2 GB |
| Contours (40 ft) | 61 MB, 47 s, 0.21 GB at the peak | 113 MB, 114 s, 0.58 GB at the peak |
| Hillshade (lossy WebP) | 2.4 MB to zoom 12, 7 s | 27 MB to zoom 14, 46 s |
| Elevation | 16.6 MB to zoom 12, 15 s | 52 MB to zoom 13, 89 s |

For a large region count on about 350 MB of download per cell of land, and let the estimate that each build prints
first tell the rest. The disk checks allow 0.5 GB of working space per cell for the contours and 0.2 GB for the
hillshade.

### A small area of 1 m hillshade (`build-detail.sh`, `DETAIL_BBOX`)

USGS also publishes lidar elevation at 1 m, survey by survey, for much of the United States. At that detail the
hillshade shows single ditches, tracks, terraces and building platforms. It is also about a hundred times the data
of the 10 m layer, so it is built for one small area only:

```bash
./scripts/build-detail.sh <min-lon> <min-lat> <max-lon> <max-lat>
```

or `DETAIL_BBOX="<min-lon> <min-lat> <max-lon> <max-lat>"` in a region file, which makes `build-region.sh` run it
as its `detail` step after the hillshade.

- The area is limited to 10,000 km² (100 × 100 km); `DETAIL_MAX_KM2` moves the limit. Each 10 × 10 km of it is a
  download of 0.3 to 1.5 GB (1 GB and 4 GB on the two test areas of about 100 km² each: more where several surveys
  overlap), kept in `data/cache/dem-usgs1m`, and about 3,000 tiles (10 to 17 MB as lossy WebP on the test areas).
  A 100 × 100 km area is therefore 30 to 150 GB of downloads and a few hours of work.
- The script asks The National Map for the 1 m tiles that touch the area, every survey's, lays them on top of each
  other with the newest on top (a survey's tile holds only what it flew), and shades them the same way as the rest (`tools/detail-area.py`, `tools/hillshade-raster.py`). Where
  no 1 m survey has been published, the area keeps the hillshade it had.
- The result is a tile set of its own, `data/hillshade-detail.mbtiles`: the zooms above the hillshade's own, up to
  `DETAIL_MAXZOOM` (default 17), inside the area only. The topo styles draw it over the hillshade. (It cannot be
  part of `hillshade.mbtiles`: a tile set has one highest zoom, and outside the area there would be nothing to
  show at the higher ones.) One file holds one area; a new one replaces the old.
- The hillshade itself must exist first. Contours and the elevation tiles are not changed.

`config.json` lists `hillshade-detail.mbtiles`; `scripts/data-placeholders.sh` writes an empty one when there is
none, as it does for the other optional files.

### `build-contours.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]`

Builds `data/contours.mbtiles` from Copernicus GLO-30 elevation data.
Units are meters by default (minor 20 m, major 100 m).
Use `ft` for feet (minor 40 ft, major 200 ft):

```bash
./scripts/build-contours.sh --areas <geofabrik-area> <min-lon> <min-lat> <max-lon> <max-lat> ft
```

The lines are computed one 1° elevation cell at a time, several cells in parallel (`CONTOUR_JOBS=<n>` sets how many;
the default is one fewer than the CPUs), by `tools/contour-cells.py`. Each cell also reads one pixel row and column
of its neighbours, and its lines are cut along shared pixel centres, so they meet the neighbours' lines exactly
(where the pixel width changes, at 50°, 60°, 70° ..., the row between two cells is contoured on a grid that holds
both cells' pixel centres). One tippecanoe run per layer then tiles all cells together, so the low zooms are thinned
the same way everywhere.

Finished cells, and each finished layer's tiles, are kept in `data/cache/contour-cells` until the build is
complete: a build that stops (power, disk, a closed terminal) carries on where it was when the same command is run
again. To keep the working space small, no file holds more than one cell's lines, a layer's lines are deleted as
soon as its tiles exist, and tippecanoe's temporary files are on the data drive, where the disk check counts them.
On the first test area above the peak was 0.21 GB, the finished 61 MB file included: about 17 kB per km² of
rugged mountains (the disk check allows 0.2 GB per cell with land, less what a stopped run already wrote).

### `build-hillshade.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat>`

Builds `data/hillshade.mbtiles` from the same elevation data: gray tiles from zoom 6 to `HILLSHADE_MAXZOOM` (default
12; the print styles stretch zoom 12 over the higher zooms without visible loss on paper), transparent outside the
outline. `HILLSHADE_FORMAT` chooses the encoding: `webp` (lossy, quality 80; the default), `webp-lossless` or `png`.

The elevation data is reprojected and shaded in memory, in chunks of 4096 × 4096 pixels with one pixel of overlap
(`tools/hillshade-raster.py`), several in parallel; chunks of sea or outside the outline are skipped. Only the
gray hillshade raster and its overviews are written to the working folder, not a reprojected copy of the heights:
20 MB at the peak on the first test area above, where the earlier whole-region pass took 64 MB for the same pixels.

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

A region file is a short shell file. Write your own for your area, or start from one of the examples in
`regions/`: `mid-atlantic.env` (an example of several US states), `us-all.env` (all 50 states, see below) and `us-lower48.env` (an example of a whole
country on top of the world layer; its comments list sizes). A region file sets these values:

| Variable | Meaning |
|---|---|
| `REGION_NAME` | Short name for the merged OSM file |
| `GEOFABRIK_AREAS` | Geofabrik paths, for example `north-america/us/new-jersey` |
| `BBOX` | Box: `min-lon min-lat max-lon max-lat`. The base map stops at it; the terrain covers the areas' outlines inside it. A min-lon larger than the max-lon crosses the 180° line |
| `CONTOUR_UNITS` | `ft` or `m` (default `ft`) |
| `HILLSHADE_FORMAT` | `webp` (default, lossy), `webp-lossless` or `png` |
| `DEM_SOURCE` | Elevation data for contours, hillshade and elevation: `copernicus` (default, 30 m, the surface) or `usgs` (10 m, bare ground, United States; see "Higher-resolution elevation") |
| `DETAIL_BBOX` | A small area (at most 10,000 km²) of 1 m hillshade from USGS lidar: `min-lon min-lat max-lon max-lat` (see "A small area of 1 m hillshade") |
| `HILLSHADE_MAXZOOM` | Highest zoom of `hillshade.mbtiles` (default 12; 14 with `DEM_SOURCE="usgs"`) |
| `ELEVATION_MAXZOOM` | Highest zoom of `elevation.mbtiles`: 12 (default, about 30 m) or 11 (about 60 m, a quarter of the size); 13 by default with `DEM_SOURCE="usgs"` |
| `PLANETILER_MEMORY` | Java heap for Planetiler (default `8g`) |
| `WORLD` | `yes`: the whole world at zoom 0-8, the region from zoom 9 (same as `--world`) |
| `LANGUAGE` | Keep labels in one language only (same as `--language`) |
| `US_STATES` | Two-letter state codes for `build-usdata.sh` (default: the US states in `GEOFABRIK_AREAS`) |

Planetiler output is limited to the region's `BBOX` (for a box across the 180° line, its wider side).

#### Example: a full set for the whole world at low zoom and all of the US

`regions/us-all.env` covers all 50 states and Washington DC (Puerto Rico and the other territories are not in the
Geofabrik `north-america/us` extract) on top of the world layer: the whole world at zoom 0-8, the US at zoom 9-14.
These commands build every file the styles and the print tool read:

```bash
# Base map (world layer zoom 0-8 plus the US to zoom 14), contours, hillshade,
# elevation and extras, in that order. Prints the outline and a size and time
# estimate for the terrain steps first.
./scripts/build-region.sh regions/us-all.env

# US federal data for topo-usgs: obstacles, names, survey marks, public land.
./scripts/build-usdata.sh regions/us-all.env
# Optional: FEMA flood zones (very slow for the whole country; see build-usdata.sh).
./scripts/build-usdata.sh regions/us-all.env flood

# Load the new files.
docker restart tileserver
```

The world layer is built once (about 96 GB to download and about 160 GB of free disk the first time) and reused.
The box crosses the 180° line, so the terrain covers Alaska's Aleutians on both sides of it, while the base map,
extras and US data stop at 180°. Geofabrik's US outline takes in a wide band of sea, so the first estimate is high
(it counts about 3,700 one-degree cells); cells of open sea have no elevation file and are recorded on the first
download, which makes later estimates closer. The contours step's disk check allows 0.2 GB of working space per
one-degree cell with land, about 230 GB for all of the US, and the hillshade step's 0.03 GB, about 40 GB, freed
when each finishes; those figures are scaled up from small test builds, not measured at this size. Allow a few
days on a fast machine with 32 GB of RAM. A failed or stopped step can be run again on its own, for example
`./scripts/build-region.sh regions/us-all.env contours` (the contours step carries on where it stopped).

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
`LANGUAGE=<code>`; a flag on the command line wins). A `LANGUAGE` that your shell already has set (many Linux
desktops set it, to `en_US` for example, for the language of program messages) is not taken as this option:

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
Extracts already in `data/sources/extracts` are used again for 30 days (`OSM_REFRESH_DAYS`), then downloaded anew at
the next build. `OSM_REFRESH=yes` in front of a build downloads them again now, however recent; the map builder's
"Get the newest map data" box does the same. When no newer copy can be downloaded, the build says so and uses the
one on hand.

## Bring your own data

You do not have to use the build scripts.
Put your own files in `data/` with these names:

| File | Type | Content |
|---|---|---|
| `data/basemap.mbtiles` | vector | OpenMapTiles schema base map |
| `data/contours.mbtiles` | vector | layers `contours_major` and `contours_minor`, attribute `elev_ft` (feet) or `elev_m` (meters) |
| `data/hillshade.mbtiles` | raster | gray hillshade tiles, WebP or PNG |
| `data/hillshade-detail.mbtiles` | raster | optional: gray hillshade tiles of a small area at higher zooms (`build-detail.sh`), drawn over the hillshade; empty from `data-placeholders.sh` |
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
http://<server-host>:8080/styles/topo-light/{z}/{x}/{y}.jpg
```

On a Raspberry Pi installed with `scripts/install-pi.sh`, the tile server sits behind nginx at `/maps/` instead:

```
http://<pi-host>/maps/styles/topo-light/{z}/{x}/{y}.jpg
```

Replace `topo-light` with `osm` or `topo-dark` for the other styles. For higher resolution screens, use
`.../{z}/{x}/{y}@2x.jpg`. `overlay` is see-through and needs PNG, which JPEG cannot do: `.../overlay/{z}/{x}/{y}.png`.

Every style answers both endings, so map sources that already use `.png` keep working. JPEG is the one to use for
the maps that fill the whole picture: at the quality set in `config.json` (`formatOptions.jpeg.quality`, 60 here;
TileServer GL's own default is 80) a tile is about a fifth of the size of the same tile as PNG, which matters for
the storage of the devices that keep them and on a weak connection. At 60 soft colour areas show faint squares when
a tile is enlarged past its own zoom level; raise the number for cleaner tiles (70 is about 20% larger, 80 about
50% larger), then restart the tile server. If a map source file names the tile type, it must match the ending.
The Maps page lists these addresses ready to copy, for the address you opened it at: press **Use in another app**.
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

## Remote access for the Android app

The [Fieldmap Android app](https://github.com/Sentinel6Systems/fieldmap-android) can use this box's maps away
from home, while the box and the phone both have internet. It is **off until you switch it on**: a box that nobody
switched on cannot be reached from outside at all.

1. Plug the box into your home router and install as above.
2. On the box's network, open the **Phones** page and press **Switch on**. That also opens pairing for ten minutes.
3. On the phone, on the same network: open the Fieldmap app, connect to the box, then **Menu › Server › Pair this
   phone**. To pair another phone later, press **Pair a phone** on the Phones page first.
4. Away from home the app reaches the box by itself; its strip reads **Remote**.

The **Phones** page (`/phones/`, a tab in every page's header, on the box's own network only) is where all of this
is managed:

- **Remote access on or off** (off by default). Off keeps the box completely to its own network: the router is
  not asked for anything, the box's firewall shuts the gateway, nobody can pair. Switched off again, the router's
  port is closed; the paired phones stay on the list, and the app keeps them paired, for when it is switched on.
- **Port forwarding**, for a router that does not open the port by itself: tick "Forward the port on the router
  myself" and the page shows one line, *On your router, forward TCP port 41234 to 192.168.1.10*, with this box's
  own port and home address. Give the box's internet address below it (**Find it** asks one public server on the
  internet, only when pressed), or a name of your own. **New random port** gives the box another port.
- **Switch off by itself after 90 days with no phone coming in from outside** (ticked by default), so that a
  forgotten box closes itself. The page shows the date it would happen; each phone that comes in moves it on.

Every page's header shows the state at a glance, beside the Phones tab: **on** (amber: the box can be reached from
outside, by paired phones) or **off**; hovering over it gives the box's own words about it.
- **From outside:** whether phones can reach the box, and if not, why, in plain words; the addresses phones are
  told to try; **Ask the router again**.
- **Pair a phone**, and the **paired phones**, each with when it last came in from outside, and **Remove**.

No account, no outside service, nothing of anyone else's in between: the box talks only to your own router.

### How it works

- **Pairing on the home network only.** The app makes a key in the phone's key store (the private half cannot be
  read out again) and the box gives it a certificate for that key. The box is its own certificate authority; its
  key stays in `data/remote/`. It pairs only while pairing is open, and only callers on the local network: a
  request from outside, or one that came through any proxy or tunnel, is refused.
- **The router opens one port.** The box asks the home router to forward a random outside port to its gateway,
  the way games and video calls do (UPnP or NAT-PMP), renews it every quarter of an hour, and notes the address
  the router has outside. It also offers its own IPv6 address, which needs no forwarding, and opens the router's
  IPv6 firewall where the router lets it (UPnP IGD 2). The phone learns all these addresses whenever it is home.
- **The gateway lets in only paired phones.** It is an nginx container that refuses everyone without a
  certificate this box issued, checks every request against the list of phones, and serves only what the app
  needs: styles, fonts, symbols and data tiles. Not the pictures the tile server draws, not the print tool, not
  the map builder, nothing else. It limits connections per visitor and takes nginx's security fixes every week.
- **A firewall on the box itself** lets nothing in from outside the local network but the gateway and SSH, over
  IPv4 and IPv6, whatever the router does (`scripts/firewall.sh`). Devices at home, and the box's own connections
  out, are not affected.
- **Nothing gives the box away.** The names in its certificates are random letters.

### When it cannot work

The Phones page and `./scripts/remote.sh status` say which of these applies:

- **The router does not open ports by itself** (UPnP switched off): switch UPnP or NAT-PMP on in the router's
  settings, or forward one port by hand (Port forwarding, on the Phones page).
- **Two routers in a row** (the provider's and your own): put the provider's in bridge mode, or forward the port
  on both.
- **The provider shares one address among customers** (Starlink, most cellular home internet, some fibre): no way
  in over IPv4. The box's IPv6 address may still work for phones that have IPv6 at the time.
- **The home address changed while a phone was away:** that phone finds the box again once it has been home.
  Home addresses usually stay the same for weeks or months.

### Optional

```
REMOTE_PIN=<code>                      # pairing, and removing a phone, ask for this code too
REMOTE_ADDRESS=tak.example.com:8443    # an address of your own: a port you forwarded, a name of yours
REMOTE_DIRECT=off                      # do not ask the router for anything
REMOTE_FIREWALL=off                    # no firewall on the box (for a box that serves other things from outside)
REMOTE_FIREWALL_ALLOW=22               # ports open from outside besides the gateway; empty for none
REMOTE_RELAY=tailscale                 # also reach the gateway through Tailscale Funnel (sign the box in first)
```

Put them in `.env` and run `./scripts/remote.sh setup` again. To forward a port by hand, use the Phones page (above).

```bash
./scripts/remote.sh status          # can phones reach the box from outside, and how
./scripts/remote.sh pair            # open pairing for ten minutes
./scripts/remote.sh devices         # the paired phones
./scripts/remote.sh remove <id>     # lock one out, at once
./scripts/remote.sh off             # remove it (the service, the gateway, the firewall, the Phones tab); the phones are kept
./scripts/install.sh --no-remote    # install without it (on a Pi: sudo REMOTE=no ./scripts/install-pi.sh)
```

### What to know

- Anyone who can join the box's network while pairing is open can pair a phone. On an open hotspot, set
  `REMOTE_PIN`. A VPN into the home network counts as the home network.
- Pairing runs over the home network unencrypted: it assumes that network is trusted at that moment.
- A phone that was removed is told so the next time it tries from outside, and can be paired again at home.
- Back up `data/remote/` with the box. If it is lost, every phone has to be paired again.
- The service is `tools/remote-server.py` with `tools/portmap.py` (Python's standard library, `openssl` and `ip`),
  run by `fieldmap-remote.service`. Tests: `python3 -m unittest tools/test_remote_server.py tools/test_portmap.py`.
- Tested against a pretend router (UPnP and NAT-PMP) and in containers (the firewall), not yet on many real
  routers. If yours behaves differently, `./scripts/remote.sh status` and `journalctl -u fieldmap-remote` say what
  it answered.

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
./scripts/install.sh                    # placeholders, containers, tile server restart, map builder service
```

Raspberry Pi with Internet-in-a-Box:

```bash
ssh <user>@<pi-host>
cd <repo-dir>
git pull                                # if you edited files on the Pi: git stash -u && git pull && git stash drop
sudo ./scripts/install-pi.sh            # placeholders, print tool, nginx, and a tile server restart when needed
```

New or rebuilt data files (any of `basemap`, `contours`, `hillshade`, `hillshade-detail`, `elevation`, `extras`, `usdata`):

```bash
scp data/<name>.mbtiles <user>@<pi-host>:<repo-dir>/data/
ssh <user>@<pi-host> 'cd <repo-dir> && sudo ./scripts/install-pi.sh'     # or: docker restart tileserver
```

## Start at boot

See [systemd/README.md](systemd/README.md).

## Changelog

### Since v1.0.0

- Remote access for the Fieldmap Android app, off until the owner switches it on: phones paired on the box's
  network can use the maps from anywhere. The box opens a port on its own router (UPnP, NAT-PMP, IPv6), lets in only paired phones through a
  gateway that serves only what the app needs, and puts a firewall on itself. No account or outside service. A new
  **Phones** page in the header switches it on and off, pairs phones and removes them.
  `./scripts/install.sh --no-remote` leaves it out.
- TAK tile addresses end in `.jpg` for the maps that fill the whole picture (the overlay stays `.png`), and
  `config.json` sets JPEG quality 60: about a fifth of the size of PNG tiles. `.png` addresses keep working.
- One look and one header for every page: Maps, Print a map and Map builder link to each other, follow the
  device's light or dark setting, and can carry a Home button back to a landing page of your own (`homeUrl` in
  `print/public/config.json`). The shared files are in `ui/`.
- The Maps page (`/`, or `/maps/` behind Internet-in-a-Box) replaces TileServer GL's front page: the map itself
  with a menu of the styles, starting on the area the base map covers, and the tile addresses for TAK clients
  under "Use in another app". Browsers without WebGL 2 get the same maps as pictures drawn by the server.
- Print tool on a phone: the folded panel stays at the bottom. Map builder on a phone: the layer list and the
  space table fit the screen.
- Builds run at low CPU priority, so the maps stay usable while they run.
- OpenStreetMap extracts are downloaded anew after 30 days, or at once with the map builder's "Get the newest map
  data" box (`OSM_REFRESH=yes`); the page shows how old the data on the device is.
- Print tool: save the markers, arrows and shapes to a GeoJSON file and load them again (also from other map
  software).
- Print tool: the list page is now a key page, with the map symbols the sheet shows at the top.
- US interstate, US highway and state route shields were drawn at half size in the high-resolution sprites (the
  ones printing and sharp screens use); fixed in `sprites/topo@2x.png` and `sprites/usgs@2x.png`.
- Map builder: "Repeat the last build" fills in the last build's choices; downloads that break off are retried, and
  damaged 1 m elevation tiles are found and downloaded again.

- Terrain builds follow the region's Geofabrik outlines instead of its box (no neighbouring countries), skip
  known open-sea cells, handle the 180° line, print size and time estimates first, and write hillshade as lossy
  WebP by default. `regions/us-all.env` builds the whole world at low zoom plus all 50 states; `build-usdata.sh`
  takes the wider side of a box across the 180° line. The terrain disk checks run after the elevation download
  and count only cells with land.
- `scripts/install.sh`: one command installs or updates everything and keeps it running across reboots, the map
  builder page included, which is at `/mapadmin/` beside the print tool.
- Map builder page (`scripts/admin.sh`): choose regions on a world map, tick the layers, see the disk space and time
  the build needs, and start it from the browser. `build-region.sh --staged` (which the page uses) swaps all new
  files in together at the end and leaves the old ones in place if the build fails.
- `DEM_SOURCE="usgs"`: contours, hillshade and elevation tiles from the USGS 3DEP 10 m bare-ground data in the
  United States, with Copernicus wherever USGS has none. `build-detail.sh` / `DETAIL_BBOX`: one small area of
  hillshade from the USGS 1 m lidar data, as its own tile set (`hillshade-detail.mbtiles`) over the hillshade.
- Contours are computed one elevation cell at a time, in parallel, and a stopped build resumes (the tiling too):
  under half the time and about a third of the working space of the earlier whole-region pass, with the same lines.
  Hillshade is shaded in chunks in memory: the same pixels with a third of the working space.
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
- Optional higher-resolution elevation data for the United States (`DEM_SOURCE=usgs`, `build-detail.sh`): USGS 3D
  Elevation Program, 1/3 arc-second and 1 m DEMs, courtesy of the U.S. Geological Survey (public domain)
- Map builder page: region outlines from [Geofabrik](https://download.geofabrik.de/)'s extract index (derived from
  OpenStreetMap, ODbL); coastlines and town names from [Natural Earth](https://www.naturalearthdata.com/) (public domain)
- Optional US layers (public domain, US federal government): FAA Digital Obstacle File, USGS GNIS, NOAA National
  Geodetic Survey, USGS PAD-US, FEMA National Flood Hazard Layer
- World Magnetic Model: NOAA NCEI and the British Geological Survey for the US NGA and UK DGC (public domain)
- Fonts for the print styles: Roboto Condensed and Atkinson Hyperlegible Mono (SIL Open Font License)
- Fonts: [Open Sans](https://fonts.google.com/specimen/Open+Sans) (Apache License 2.0)
- Server: [TileServer GL](https://github.com/maptiler/tileserver-gl) (BSD)

Show the OpenStreetMap attribution when you show these maps in public.
The code in this repository is under the MIT license. See [LICENSE](LICENSE).
