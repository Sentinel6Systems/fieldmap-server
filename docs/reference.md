# fieldmap-server reference

The scripts, files, options and addresses of fieldmap-server. For step-by-step instructions, see the
[user guide](user-guide.md). For design notes, security details and test results, see
[how it works](how-it-works.md).

## Install options

```bash
./scripts/install.sh                  # install or update everything (asks for sudo for the services)
./scripts/install.sh --no-builder     # leave the Map Builder out (removes its service if it was installed)
./scripts/install.sh --no-remote      # leave remote access and sharing out; the firewall is still put up
sudo FIELDMAP_NGINX_DIR=<folder> ./scripts/install-nginx.sh   # beside a machine's own nginx on port 80 (see pi/README.md)
```

Settings go in `.env` (copy `.env.example`): ports, the config file, the tile server image, and the `ADMIN_*`,
`REMOTE_*` and `APP_FETCH` options below.

Without systemd, or to do the steps yourself: `./scripts/data-placeholders.sh` (empty files for the data you have
not built, which the tile server needs to start), `./scripts/mem-limit.sh` (memory cap from this machine's RAM, see
[Tile addresses for TAK and other apps](#tile-addresses-for-tak-and-other-apps)), `docker compose up -d --build`,
and `./scripts/admin.sh` for the Map Builder. To start everything at boot without the installer, see
[systemd/README.md](../systemd/README.md).

The web front (nginx) serves the print tool at `/mapprinter/` and proxies everything else to TileServer GL, so the print
tool finds its tiles on the same host automatically. It also looks under `/maps/` (the layout beside a machine's own nginx) and on
port 8080, or you can point it anywhere with `?tiles=http://host:8080`.

Builds use all the CPU that is free, at low priority: the tile server, the print tool and anything else on the
machine come first when they need it (the build containers get a CPU weight of 128 against Docker's 1024;
`BUILD_CPU_SHARES=1024` gives a build the same weight as everything else).

## Pages and addresses

| Tab | Page | Address | Source |
|---|---|---|---|
| Maps | Maps | `/` (`/maps/` beside a machine's own nginx) | `ui/maps.tmpl` (TileServer GL's `frontPage`) |
| Print Map | Print Map | `/mapprinter/` | `print/` |
| User Guide | User Guide | `/guide/` | `guide/index.html` |
| Admin | Remote Access | `/phones/remote.html` | `phones/`, served by `tools/remote-server.py` |
| Admin | Devices | `/phones/` | the same |
| Admin | Map Builder | `/mapadmin/` | `admin/`, served by `tools/admin-server.py` |
| | Tile server | `:8080` | TileServer GL |

The header (`ui/nav.js`) finds the pages by itself on the address it was opened at: a page that is not installed
gets no link, and the Map Builder shows as **Unavailable** when its service is stopped. The Admin tab opens the
first Admin page that is running; a second row links the Admin pages when there is more than one. The look is one
file, `ui/fieldmap.css`: the device's own fonts, light or dark by the device's setting, nothing loaded from
elsewhere.

**Home button.** If these pages sit behind a landing page of your own, set its address in
`print/public/config.json` and every page's header gets a button back to it:

```json
"homeUrl": "/",
"homeName": "Home"
```

`homeUrl` is a path on the same device or a full `http://` address; `homeName` is the button text (`Home` when
empty). Empty `homeUrl`, the default, means no button. Run the installer again afterwards. To link the other way,
point your landing page at `/` or `/maps/` (add `?style=<id>#<zoom>/<lat>/<lon>` to open a style at a place),
`/mapprinter/`, `/guide/` and `/mapadmin/`.

**Pages that keep TileServer GL's own look.** TileServer GL 5.7.0 can replace only its front page, so its style
viewer (`/styles/<id>/`), data inspector (`/data/<id>/`) and the WMTS and TileJSON documents look as they always
did and have no header. Opened on port 8080, the Maps page has no links to the other pages, which live on the web
port.

## Map Builder service

`install.sh` runs the Map Builder as the `fieldmap-admin` systemd service (`systemd/fieldmap-admin.service`);
`./scripts/admin.sh` starts it by hand. It runs on the host with Python's standard library, not in a container,
because it starts the build scripts. It has no port of its own: the web front passes `/mapadmin/` to it through a
socket file in `data/admin` (`install-nginx.sh`: the machine's nginx, through `pi/nginx/mapadmin-nginx.conf` in
`FIELDMAP_NGINX_DIR`, with the socket in the data folder). The page needs no internet to open; the builds do.
On a machine with less than 8 GB of RAM it builds in Pi mode (see [Builds on a Raspberry Pi](#builds-on-a-raspberry-pi)).

| Setting (`.env` or environment) | Effect |
|---|---|
| `ADMIN_PIN=<code>` | The page asks for this code first. Set it on any network you do not fully trust |
| `ADMIN_PORT=<port>` | A port of its own, for a machine with no web front |
| `ADMIN_HOST=127.0.0.1` | Keep that port to the machine itself |
| `PI_MODE=yes` / `no` | Pi mode on or off; without it, on below 8 GB of RAM |
| `BUILD_MEMORY=3072m` | The most RAM a build may use; 3072m (3.0 GB) in Pi mode, no cap otherwise |
| `BUILD_RESERVE_GB=5` | Storage a build leaves free; 5 GB in Pi mode, 0 otherwise |
| `FIELDMAP_PAUSE_UNITS="..."` | Services a build stops to free RAM and starts again afterwards |
| `FIELDMAP_BUILD_USER=<user>` | `install-nginx.sh` only: the user the service runs as (default: the data folder's owner) |

A build from the page writes `data/admin/region.env` and runs `build-region.sh --staged` with it. New files are
written to `data/incoming` and moved into `data/` together when every step has finished; then the tile server is
restarted. `tools/make-admin-regions.py` (run in the tool container) rewrites `admin/regions.json`,
`admin/land.json` and `admin/places.json` to pick up new extracts and current download sizes.

## Builds on a Raspberry Pi

Pi mode is on by itself on a machine with less than 8 GB of RAM (`PI_MODE=yes` or `no` in `.env` chooses). The build
scripts (`scripts/lib.sh`) and the Map Builder then:

- run every build container with `docker run --memory BUILD_MEMORY --memory-swap BUILD_MEMORY` (3072m by default;
  one container runs at a time). A container that reaches it is stopped, and the log says it ran out of memory.
- give Planetiler a Java heap of two thirds of that (`-Xmx`), and keep its node locations and other working data in
  memory-mapped files in `data/tmp` (`--storage=mmap --nodemap-type=sparsearray --nodemap-storage=mmap`). Its heap
  still grows with the region: the Map Builder refuses a base map (or printed map details) whose OpenStreetMap data is
  larger than about 3.0 GB within 3.0 GB.
- compute the contour lines one elevation cell at a time (`CONTOUR_JOBS=1`) and keep GDAL's cache at 256 MB.
- use the 30 m Copernicus data only: `DEM_SOURCE=usgs`, the detail step (1 m) and the world layer are refused.
- leave `BUILD_RESERVE_GB` (5) of the drive free: every step's disk check adds it.
- count with a pace about three times slower than a PC's for the build time.

The page warns when the data folder is on an SD card (an `mmcblk` device).

**Services stopped during a build.** `FIELDMAP_PAUSE_UNITS` in `.env` lists services the Map Builder stops when a build
starts, to free RAM: systemd services by name and Docker containers as `docker:<name>`, separated by spaces, for
example `FIELDMAP_PAUSE_UNITS="jellyfin calibre-web docker:<container>"`. The page names them before the build (the
build starts only once that is confirmed) and while it runs. They start again when the build finishes, fails or is
canceled; when the build process dies, the Map Builder starts them as soon as it notices; and after a reboot in the
middle of a build, `fieldmap-build-restore.service` does it at boot. The web server, SSH, the network, Docker and
fieldmap-server's own services are never stopped, even when named.

Only root can stop a system service, and the Map Builder runs as an ordinary user. The installers run
`scripts/pause-setup.sh <user>`, which installs a copy of `tools/pause-services.py` owned by root at
`/usr/local/lib/fieldmap-server/pause-services`, one sudoers rule in `/etc/sudoers.d/fieldmap-pause` that lets the
Map Builder's user run exactly that file with `stop` or `restore` and nothing else, and the boot unit. The copy reads
the list from the repository's `.env`; what it stopped is listed in `/var/lib/fieldmap-server/paused.json` (root's),
and `restore` starts only what that file lists.

## Disk space

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

Finished downloads are kept in `data/sources` and `data/cache` and reused. **Free Up Storage** in the Map Builder
deletes what is in `data/sources`, `data/cache`, `data/tmp` and `data/incoming` (the finished steps of a build that
did not finish); the maps stay. That includes `data/sources/planet-z0-8.osm.pbf`: a new world layer then needs the
whole planet again.

One build at a time: every build script holds `data/admin/build.lock` while it runs (the scripts it starts share
it), and so do a build from the Map Builder and Free Up Storage. A script started while another build holds the lock
stops at once with a message; the Map Builder refuses to start a build or to delete files while a build from the
command line runs.

## Repository layout

| Path | What it is |
|---|---|
| `docker-compose.yml`, `.env.example` | The services: `tileserver`, `web` (print tool + proxy) and `wmm` (magnetic model updates) |
| `config.json`, `config-basic.json` | TileServer GL configs: all six styles and the `topo-usgs` scale bands, or `osm`+`overlay` with only a base map |
| `guide/` | The User Guide page, served at `/guide/` |
| `docs/` | This reference, the user guide and the design notes |
| `ui/` | What every page shares: the look (`fieldmap.css`), the header (`nav.js`), and the Maps page (`maps.tmpl`) |
| `styles/`, `fonts/`, `sprites/` | The styles and every asset they need, all local |
| `planetiler/` | Custom Planetiler schema for the extras layers |
| `scripts/`, `regions/`, `tools/` | `install.sh` (install and update), and the data builders (Planetiler, GDAL, Tippecanoe, Osmium in a tool container) |
| `admin/`, `scripts/admin.sh` | The optional Map Builder page: choose regions and layers on a map, check the disk space, start the build |
| `print/` | The print tool: `src/`, `public/`, `build.mjs` (esbuild). Built inside the `web` image |
| `web/` | nginx config and the Dockerfile that builds and serves the print tool |
| `pi/` | Install beside a machine's own nginx (`scripts/install-nginx.sh`): compose file, nginx snippets, notes; and the Raspberry Pi notes |
| `systemd/` | Start the compose stack, and optionally the Map Builder page, at boot |
| `phones/`, `scripts/remote.sh`, `scripts/firewall.sh`, `tools/remote-server.py`, `tools/portmap.py`, `tools/netbird-run.sh`, `tools/netbird_isolation.py` | Remote access for the Android app: the Devices page, pairing, the router's port, NetBird and its network isolation, the gateway, the server's firewall |
| `tools/share-server.py` | Sharing positions and markers between devices with the Android app |
| `scripts/app.sh`, `tools/app-file.py` | The Android app kept on the server, for devices to install from the Devices page |
| `data/` | Your mbtiles files (not in git) |

## Styles

| Style | Description | Data files needed |
|---|---|---|
| `osm` | OpenStreetMap look | `basemap.mbtiles` |
| `topo-light` | Topographic, light colors, contours, hillshade; the screen style | all three |
| `topo-dark` | Topographic, dark colors, for night use | all three |
| `overlay` | Transparent roads and labels, for use over aerial imagery | `basemap.mbtiles` |
| `topo-print` | `topo-light` tuned for paper: smaller labels, road names from z11/z13, dark outlined buildings, town names, lighter hillshade | all three |
| `topo-usgs` | Second paper style in the manner of a USGS quadrangle: white paper, green woodland, brown contours with monospace elevations, red and black cased roads, railway ties, dashed seasonal water with flow arrows, banded park and military boundaries | all three; optional `extras`, `usdata` |

`topo-print` and `topo-usgs` read the same tile sets, so the print tool can switch between them (the **Map Style**
menu; the choice is remembered). `topo-usgs` is generated, not hand-edited:

```
python3 tools/make-usgs-sprite.py   # sprites/usgs*: the topo sprite plus patterns and symbols (needs Pillow)
python3 tools/make-topo-usgs.py     # styles/topo-usgs.json
```

Line widths in the generator are millimeters on paper at 1:24,000 and scale gently with zoom. Route shields come from
the topo sprite, except the interstate shield (blue and red, white numbers), which `make-usgs-sprite.py` draws.
Motorway exit numbers, runway lengths and the gray large buildings (5,000 m² and more) need `data/extras.mbtiles`.
Every layer carries a `fieldmap:group` in its metadata (roads, trails, contours, parks, pois, ...), which the print
tool's **Layers** panel switches. Its fonts are Roboto Condensed and Atkinson Hyperlegible Mono (SIL Open Font
License, texts in `fonts/`). Not done yet: deciduous and conifer woodland (OpenMapTiles carries no leaf type) and
spot heights chosen by prominence (needs a hilltop set derived from the elevation data).

## Print tool

- Pick scale, paper, orientation and quality (200 or 300 dpi); drag the blue outline to place the sheet.
- Every sheet is at exact scale: the map zoom is computed from scale, dpi and the center latitude. One CSS pixel is
  1/100 inch on paper, so print zooms are 1:12k ≈ z14.2, 1:24k/25k ≈ z13.2, 1:50k ≈ z12.2, 1:100k ≈ z11.2.
- MGRS/USNG grid of the sheet's UTM zone, 1 km lines with blue principal digits in the margin, 100 m ticks at 1:12k,
  10 km lines bold at 1:100k, extended across a zone boundary when a sheet straddles one.
- Declination from the World Magnetic Model for the print date (no network needed): TN, GN and MN drawn at true
  angles, the G-M angle, and which way magnetic north moves each year. See [Magnetic model updates](how-it-works.md#magnetic-model-updates).
- Markers (12-color palette, draggable) and arrows (tap start, tap end, drag the ends) labeled with name, range and
  true/magnetic bearing; grid bearing in the side panel. Optional key page: at the top, the map symbols the printed
  pages show (icon and meaning, gathered while the pages render: only the ones that are on them), then the list of
  markers, arrows and shapes with MGRS, lat/lon and bearings.
- Shapes: circles (tap the center, tap the edge, then type an exact radius), rectangles (two opposite corners) and
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
  items to those on the map. The file keeps everything exactly (a circle's center and radius, a rectangle's corners,
  names and colors) and is plain GeoJSON for other map software too; files from such software load as well: points
  as markers, two-point lines as arrows, polygons as polygon shapes (anything else is left out, and the panel says
  how much). A polygon of more than 24 sides, such as a boundary loaded from other software, shows its area but no
  side lengths. See `print/src/drawings.js`.
- Atlas mode: 2×2, 3×3, 4×4, 6×6 or 8×8 pages with 1 cm overlap, rows A… from the north and columns 1… from the west.
  A cover page built from the map data (places, peaks, lakes, rivers inside the atlas, a locator map), an index page
  with the page grid, neighbor tabs on every page, optional binder margin. Rendering is sequential with progress and
  a Cancel button; a laptop is recommended for atlases.
- Layers panel: switch groups of layers off (woodland, roads, trails, place names, ...), print in color or black and
  white, hillshade off/soft/sharp, contours off/index lines only/all, MGRS grid lines on or off (the edge numbers
  stay). The switches apply on screen and to every printed page, including atlas, index and cover pages, and are
  remembered with the other settings. Both print styles carry the groups: `topo-usgs` from its generator, `topo-print`
  from `tools/tag-print-groups.py` (run it again after editing that style by hand). In black and white, raster icons
  and area patterns keep their colors.

### Settings

- Buildings at 1:24k/25k: tiles below z14 carry only large buildings, so the tool draws the building layer again from
  the z14 tiles. For a basemap that has houses at z13 as well, add `--building-merge-z13=false --min-feature-size=0.25`
  to the Planetiler arguments in `scripts/build-vector.sh` / `build-region.sh`.
- The contour interval and data date in the margin come from `print/public/config.json`: set `contourMinor`,
  `contourMajor` and `contourUnit` to match your `build-contours.sh` run (the defaults are 40 and 200 ft), and
  `dataDate` to the date of your OpenStreetMap extract, for example `"OSM 2026-01"` (empty by default). Then rebuild
  the print tool (`docker compose up -d --build`, or `install-nginx.sh` beside a machine's own nginx). The credit line names OpenMapTiles
  and OpenStreetMap plus every other data set the style reads (from each tile set's attribution; empty files add
  nothing).
- Print scale bands: the generator also writes `topo-usgs-12k`, `-24k`, `-50k` and `-100k`, with every line width
  and text size fixed at its value for that scale (at 40°N). The print tool uses the band nearest the sheet's
  scale when the server has it, so a 1:24,000 sheet has the same line weights at any latitude; the screen map keeps
  the zoom-based `topo-usgs`. The band styles are listed with `serve_rendered: false` (no raster rendering).
- Development: `cd print && npm install && npm run build`; `npm run serve` serves `dist/` on :8731
  (`http://localhost:8731/?tiles=http://localhost:8080`).

### Magnetic model service

From the command line on the server (Docker: `docker exec fieldmap-wmm python3 /app/tools/wmm-update.py update`):

```bash
python3 tools/wmm-update.py status      # model in use, its dates, the last check
python3 tools/wmm-update.py update      # download a newer model if there is one
```

The service has no settings and answers only `GET /status` and `POST /update` (behind nginx at
`/mapprinter/wmm-api/`). It is meant for a private network; put the server behind your own access control if
others can reach it.

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
files, merged when there are several, downloaded once to `data/cache/outlines`. Without `--areas` they cover the whole
box. When an outline cannot be downloaded, the build stops: run it again later. In detail (`tools/region-outline.py`):

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
  about a meter or less and are not corrected. The 1 m lidar layer is far too large to build a region from; it
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
of its neighbors, and its lines are cut along shared pixel centers, so they meet the neighbors' lines exactly
(where the pixel width changes, at 50°, 60°, 70° ..., the row between two cells is contoured on a grid that holds
both cells' pixel centers). One tippecanoe run per layer then tiles all cells together, so the low zooms are thinned
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

Builds `data/elevation.mbtiles` from the same elevation data as the contours and hillshade: heights in meters for the
print tool's terrain analysis (viewshed and radio coverage). Tiles are 256 px, Web Mercator, from zoom 8 to `maxzoom`
(default 12, or `ELEVATION_MAXZOOM`), for every tile that touches the outline. Encoding: Mapbox terrain-RGB
(height = -10000 + (R × 65536 + G × 256 + B) × 0.1 m) in lossless WebP; R = G = B = 0 marks no data. Set
`ELEVATION_ENCODING=terrarium` and/or `ELEVATION_FORMAT=png` for the other choices; all four keep the heights to
0.1 m or better and TileServer GL serves all four. WebP was the smallest (about 30 % below PNG) on both test areas.
At `maxzoom` each pixel is the elevation data's value at the pixel center; lower zooms average it.

Zoom 12 is about 30 m per pixel at US latitudes (38 m at the equator), like the source data; zoom 11 is about 60 m
and about a quarter of the size. Measured sizes (all zooms), and what they suggest for larger areas:

| Area type | Zoom 12 | Zoom 11 | A 150,000 km² state at 12 / 11 | Lower 48 at 12 / 11 |
|---|---|---|---|---|
| Ridge-and-valley (hills, 300-700 m relief) | 1.4 kB per km² | 0.4 kB per km² | ~210 MB / ~65 MB | ~11 GB / ~3.4 GB if all like this |
| Flat coastal plain with open water | 0.5 kB per km² | 0.13 kB per km² | ~80 MB / ~20 MB | ~4.4 GB / ~1 GB if all like this |

A whole country of mixed terrain falls in between (for the lower 48, roughly 8 GB at zoom 12 and 2-3 GB at zoom 11).
The step writes the tiles straight from the downloaded elevation data, so it needs little working space.
Check a build against the elevation data with `tools/check-elevation.py` (see the comments at its top): at pixel
centers the tiles and the source agree to within 0.05 m.

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
| `building_large`, `exit`, `runway` | buildings of 5,000 m² and more, motorway junctions, runways | gray large buildings, exit numbers, runway length in feet |

`build-region.sh` (its `extras` step, on by default) and `build-vector.sh` (unless `NO_EXTRAS=1`) run it for you
after the base map. Without it `data/extras.mbtiles` is an empty placeholder (written by
`scripts/data-placeholders.sh`, which the build scripts and the installers call): the tile server needs every file
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
elevation tiles for the areas' outlines inside the region's bounding box (see [Terrain outline](#terrain-outline-contours-hillshade-elevation)).

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
| `DEM_SOURCE` | Elevation data for contours, hillshade and elevation: `copernicus` (default, 30 m, the surface) or `usgs` (10 m, bare ground, United States; see [Higher-resolution elevation](#higher-resolution-elevation-in-the-united-states-dem_sourceusgs)) |
| `DETAIL_BBOX` | A small area (at most 10,000 km²) of 1 m hillshade from USGS lidar: `min-lon min-lat max-lon max-lat` (see [A small area of 1 m hillshade](#a-small-area-of-1-m-hillshade-build-detailsh-detail_bbox)) |
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
the next build. `OSM_REFRESH=yes` in front of a build downloads them again now, however recent; the Map Builder's
**Download Latest Map Data** option does the same. When no newer copy can be downloaded, the build says so and uses the
one on hand.

## Your own data files

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
| `data/elevation.mbtiles` | raster | optional: heights in meters as Mapbox terrain-RGB or Terrarium (metadata `encoding`), PNG or WebP, 256 px tiles; empty from the same script |

`extras.mbtiles` and `usdata.mbtiles` cover only the region they were built for (its OSM extract and `BBOX`), even
when the base map covers more, for example the whole world at zoom 0-8 with `--world`. Outside that region the
extra layers are simply blank. To widen them, build them again from a larger region file.

The hillshade source is marked `"sparse": false` in `config.json`. Outside the area it covers, the tile server
answers with a transparent tile, so the topo styles still render there, without shading.

The contour labels read `elev_ft` first, then `elev_m`.
`ft` builds write `elev_ft`. `m` builds write `elev_m`.
Contour files from other builders work too if they carry one of those attributes.

## Tile addresses for TAK and other apps

TileServer GL serves rendered raster tiles for each style, on x86 and ARM alike.
Add a map source in ATAK/WinTAK/iTAK with this URL pattern:

```
http://<server-host>:8080/styles/topo-light/{z}/{x}/{y}.jpg
```

On a server installed with `scripts/install-nginx.sh`, the tile server sits behind nginx at `/maps/` instead:

```
http://<pi-host>/maps/styles/topo-light/{z}/{x}/{y}.jpg
```

Replace `topo-light` with `osm` or `topo-dark` for the other styles. For higher resolution screens, use
`.../{z}/{x}/{y}@2x.jpg`. `overlay` is see-through and needs PNG, which JPEG cannot do: `.../overlay/{z}/{x}/{y}.png`.

Every style answers both endings, so map sources that already use `.png` keep working. JPEG is the one to use for
the maps that fill the whole picture: at the quality set in `config.json` (`formatOptions.jpeg.quality`, 50 here;
TileServer GL's own default is 80) a tile is a fifth of the size of the same tile as PNG or less, which matters for
the storage of the devices that keep them and on a weak connection. At 50 soft color areas show faint squares when
a tile is enlarged past its own zoom level; raise the number for cleaner tiles (60, 70 or 80 give larger tiles),
then restart the tile server. If a map source file names the tile type, it must match the ending.
The Maps page lists these addresses ready to copy, for the address you opened it at: press **Tile URLs**.
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

## Firewall

The installers put a firewall on the server itself (`scripts/firewall.sh`), whether or not remote access is set up.
Only the local network and VPNs get in. From anywhere else nothing gets an answer: no reply to a ping, no
"closed" for a closed port, nothing. Packets from outside are dropped without a word, so that a scan finds no server
at all. It does not rely on the router, over IPv4 and IPv6: some routers let IPv6 in unfiltered.

- **Gets in:** the local network (home and office address ranges, and the IPv6 networks the server is on), VPNs
  (Tailscale, WireGuard, ZeroTier, OpenVPN), and answers to what the server asked for itself.
- **The one exception:** the remote gateway's port, while Remote Connectivity is enabled (Admin › Remote Access; see the
  [user guide](user-guide.md#remote-access)). Devices away from home connect to it from anywhere, so it does answer: with a demand for a certificate
  this server issued, and nothing more to anyone without one. While NetBird is the way in, the gateway's port is
  closed to the world too.
- **NetBird is not one of those VPNs.** On NetBird's interface (`nb-fieldmap`), while NetBird is connected, the
  gateway's port gets in and nothing else: not the pages, not SSH, not a ping. A device that was removed from the server
  can still be a member of the NetBird network. This rule stays also with `REMOTE_FIREWALL=off`.
- **Closed from outside, SSH included.** To log in from outside without a VPN, set `REMOTE_FIREWALL_ALLOW=22` in
  `.env` and run `sudo ./scripts/firewall.sh apply`. `REMOTE_FIREWALL=off` leaves the firewall out (for a machine
  that serves other things to the internet).
- `sudo ./scripts/firewall.sh show` lists the rules, with a count of what was dropped.
- **A machine with a firewall of its own** that drops everything from outside (one that does not use iptables,
  which the script opens itself): this firewall cannot open anything through that one. With `FIELDMAP_PORTS_FILE` (see
  [pi/README.md](../pi/README.md)), every apply writes what remote access needs from outside, now, to that file, for
  the other firewall to open, and rewrites it only when that changes (a `.path` unit can watch it):
  `wan tcp <port>` while the router's port is the way in (the gateway, from anywhere; and each
  `REMOTE_FIREWALL_ALLOW` port), `iface nb-fieldmap tcp <port>` while NetBird is the way in (the gateway, on NetBird's
  interface only). Comments only: nothing from outside. NetBird's own traffic needs nothing open: it connects out,
  and the answers come back as answers. A Wi-Fi hotspot of the machine needs only the web server's port 80.

## The app file on the server

The installers put the newest published release of the app on the server (`./scripts/app.sh fetch`, from the app's
GitHub releases, only when it is newer; its SHA-256 is checked against the one GitHub lists). The server then keeps it
for its devices, also when it has no internet later. It is offered only on the server's own network, never through the
remote gateway.

```bash
./scripts/app.sh status                    # which app the server offers
./scripts/app.sh fetch                     # the newest published release, when it is newer
./scripts/app.sh add <file.apk> [version]  # a file of your own instead (fetch leaves it alone from then on)
./scripts/app.sh remove                    # offer none
```

`APP_FETCH=off` in `.env` stops the installers from downloading it. The file is kept in `data/app/`.

## Remote access options and commands

```
REMOTE_PIN=<code>                      # pairing, and removing a device, ask for this code too
REMOTE_ADDRESS=tak.example.com:8443    # an address of your own: a port you forwarded, a name of yours
REMOTE_DIRECT=off                      # do not ask the router for anything
REMOTE_FIREWALL=off                    # no firewall on the server (for a server that serves other things from outside)
REMOTE_FIREWALL_ALLOW=22               # ports open from outside besides the gateway (none by default); 22 is SSH
REMOTE_RELAY=tailscale                 # also reach the gateway through Tailscale Funnel (sign the server in first)
NETBIRD_IMAGE=netbirdio/netbird@sha256:<digest>  # another NetBird client image than the pinned one (set up on the Remote Access page, not here)
```

Put them in `.env` and run `./scripts/remote.sh setup` again. To forward a port by hand, use the Remote Access page (see the [user guide](user-guide.md#remote-access)).

```bash
./scripts/remote.sh status          # can devices reach the server from outside, and how
./scripts/remote.sh pair            # open pairing for ten minutes
./scripts/remote.sh devices         # the paired devices
./scripts/remote.sh remove <id>     # lock one out, at once
./scripts/remote.sh netbird-forget  # take NetBird off the server: the container, the setup key, the server's NetBird identity
./scripts/remote.sh off             # remove it (the services, the gateway, the Devices page); the paired devices and the firewall are kept
./scripts/install.sh --no-remote    # install without it (beside a machine's own nginx: sudo REMOTE=no ./scripts/install-nginx.sh); the firewall is still put up
```

## Sharing interface

| The app asks | For |
|---|---|
| `GET /mapshare/info` | whether sharing is on, and how often to ask |
| `POST /mapshare/position`, `POST /mapshare/leave` | its own position; and to be gone from the others at once |
| `GET /mapshare/state?since=<rev>` | everybody's position, and the shared markers that changed |
| `PUT` and `DELETE /mapshare/markers/<id>` | a shared marker (a GeoJSON point, as the print tool's drawings file has them) |

The messages are short JSON, with the same ideas as TAK's Cursor on Target (an id, a name, a time, a point,
course and speed), not CoT itself: TAK clients cannot connect to it.

