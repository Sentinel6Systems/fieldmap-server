# Changelog

## Since v1.0.0

- **Map: Clear All clears everything.** It used to delete only the items kept in this browser and did nothing,
  without a word, when every item was shared. It now deletes every marker, arrow, route and shape in every group
  after one warning that says how many are shared (those are removed from all devices too), and says so when
  there is nothing to clear.

- **Map: routes show that they are being worked out.** While the server works out legs, a note at the top of the
  map says how many are left, and the route's row shows a spinner ("working out 2 legs"). A leg still being worked
  out is no longer counted as a straight leg with no way found.

- **Longer route legs.** The route service now gives a leg up to 5 minutes (was 30 seconds) and may use 768 MB
  (was 512 MB), with a Java heap of two thirds of that that shrinks again after a long route. Both are settings in
  `.env`: `ROUTE_TIMEOUT` (seconds, at most 900) and `BROUTER_MEM_LIMIT`. The web server waits up to 900 seconds
  for a leg. Run the installer again (or rebuild the brouter image) to use it.

- **Map: items on top of each other.** A tap on the map where several markers, arrows, routes or shapes overlap
  lists them to choose from; the chosen one opens in the Markers tab (its group opened), and the next drag on that
  spot moves it. A tap on a single item opens its row the same way.

- **Map: grid labels you can read at any zoom; arrow heads that stay.** The MGRS grid's digits now sit upright at
  the top and left edges of the map instead of along the lines, and each 100 km square in view keeps its full name
  (such as 18S XJ) in the middle of the part you see, however far you zoom in. The digits follow MGRS: one on
  10 km lines, two on 1 km lines, three on 100 m lines. Arrow heads are drawn as lines again, sized for the zoom
  each time it changes and over the handle at the tip; the head drawn as an icon could fail to show when zoomed
  out.

- **Map: Add and groups in the Markers tab.** The tool bar that floated on the map is gone. **Add** at the top of
  the Markers tab puts a marker, arrow, route, circle, rectangle or polygon on the map, into the group chosen under
  **To**. **New Group** makes a group ("Water Sources", "Egress Routes"); each group's heading shows or hides it on
  the map and in print, folds its list, and exports (⤓), renames (✎) or deletes (✕) it. Each item can be moved to
  another group. **Export All** and **Import** keep the groups; a file from other map software comes in as a
  group named after the file. A shared item carries its group's name (the app does not show groups yet). Terrain
  Analysis is now a **Terrain** tab, and **Set My Location** is in Layers under the Shared switches. While
  something is added, the hint above the map has **Cancel**.

- **Map: arrow heads at every zoom.** An arrow's head is drawn the same size on screen at any zoom, over the handle
  at its tip; before, it was a few meters long on the ground and hidden under the handle when zoomed out.

- **Map: MGRS grid on screen.** **MGRS Grid Lines** (Layers > Map Features) now draws the grid on the map as well as
  on printed sheets: 100 km, 10 km, 1 km or 100 m lines as you zoom in, labeled with their numbers and the 100 km
  square letters, and the grid zones when zoomed far out. Each UTM zone in view gets its own lines.

- **Tile URLs: the right addresses again, JPEG at quality 50.** **Tile URLs** on the map page showed
  `styles/undefined/...png` for every style; it now shows each style's own address, `.jpg` for the maps that fill
  the whole picture and `.png` for the transparent overlay. The print scale styles are no longer listed. JPEG tiles
  are now made at quality 50 (was 60): smaller tiles for devices and weak connections. Restart the tile server to
  use it.

- **Map: Shared switches grayed out while sharing is off.** In **Layers**, **Device Locations** and **Shared
  Markers** are grayed out while location sharing is disabled on the server, as the note under them says.

- **More RAM for Pi builds.** Pi mode now lets a build use 3.0 GB (`BUILD_MEMORY` default 3072m, was 2560m),
  which leaves 1 GB for the system on a 4 GB Pi. The Map Builder now builds a base map from up to about 3.0 GB of
  map data (was 2.3 GB). Set `BUILD_MEMORY=2560m` in `.env` to keep the old cap.

- **Build time estimates closer to real builds.** Measured on an 8-CPU PC with an NVMe drive (15 US states,
  2.8 GB of map data, 900,000 km² of land: about 67 minutes in all), the estimates of the base map, contours,
  hillshade, elevation data, printed map details and US government data were two to ten times too long. The Map
  Builder and the build scripts' own terrain estimates now use the measured rates; a Raspberry Pi still counts
  about three times as long per core.

- **Map Builder: Free Up Storage.** Under step 4, a card shows how much space the downloads and build files take
  (`data/sources`, `cache`, `tmp`, `incoming`), and **Delete Downloads and Build Files** deletes them, for servers
  with little storage. The maps don't change; the next build downloads its data again. The server deletes them in
  the background, so a slow SD card does not time out the page. The card names the World Overview's planet data when
  it is there. A build whose finished steps were deleted offers **Start Again** instead of **Resume Build**.
- **One build at a time.** Every build script holds `data/admin/build.lock` while it runs, and so do builds from the
  Map Builder and Free Up Storage: a second build stops at once with a message, and the Map Builder neither starts a
  build nor deletes files while a build from the command line runs.

- **Map Builder: the size of the finished maps.** Step 3 says how much space the finished maps take, and the
  downloaded source data kept for later builds; **Storage Breakdown** ends with a Total row (final size and time).

- **Builds follow Geofabrik's new download addresses.** Geofabrik now keeps each area in a folder of its own
  (`north-america/us/alaska/alaska.poly`), and its old addresses lead nowhere. The terrain outlines could not be
  downloaded, so contours, hillshade and elevation covered the whole box round all regions instead: for Alaska and the
  East Coast states that was most of Canada as well, and the contour step stopped for lack of disk space (about 600 GB
  asked for, where the page said 147 GB). The outlines and the Geofabrik mirror of the OpenStreetMap downloads now use
  the new addresses, with the old ones as a second try; region files keep the plain paths (`north-america/us/alaska`).
  When an outline still cannot be downloaded, the build stops with a plain message instead of building the whole box.

- **The Map Builder runs on a Raspberry Pi.** On a machine with less than 8 GB of RAM (or `PI_MODE=yes` in `.env`) it
  builds in Pi mode: every build container within `BUILD_MEMORY` (2.5 GB, swap included), Planetiler's node locations
  and working data in memory-mapped files on disk with a heap of two thirds of the cap, contour lines one cell at a
  time, the 30 m terrain only (10 m, 1 m and the world layer are refused), and 5 GB of the drive left free
  (`BUILD_RESERVE_GB`). The estimate also checks RAM: a selection too large for the cap is refused before it starts,
  in plain words, and the build time counts with a Pi's pace. The page warns when the maps are on an SD card.
  `FIELDMAP_PAUSE_UNITS` in `.env` names services (systemd, or `docker:<name>`) that a build stops to free RAM and
  starts again when it ends, fails or is canceled, and after a crash or a reboot in the middle; the page names them
  before the build (**Stop Services and Start Build**) and while it runs. Stopping them is done by a root-owned helper
  that one sudoers rule lets the Map Builder run with `stop` or `restore` only (`scripts/pause-setup.sh`).
  `install-nginx.sh` installs the Map Builder (`/mapadmin/` in `FIELDMAP_NGINX_DIR`), and no longer stops when no maps
  are there yet; `install.sh` on a Pi needs no `--no-builder`. The build scripts read `FIELDMAP_DATA`, and use the
  machine's network when Docker has none of its own.
- **Admin › Devices sets up devices step by step**, and Admin opens on it: 1. Android App, 2. Location Sharing and
  Messages (the sharing switch, moved up: messages need it), 3. Remote Connectivity (optional; its switch moved
  here from Remote Access, with pairing and the connectivity status), 4. Paired Devices. Remote Access keeps the
  connection method, port forwarding, NetBird VPN and auto-disable. A **Messages** tab that is unavailable says
  which switch to enable.
- **One installer for a PC and a Raspberry Pi**, and no more Internet-in-a-Box: `./scripts/install.sh` (as yourself,
  without sudo) installs Docker with Compose v2 where it is missing (Debian 13, Ubuntu 24.04, Raspberry Pi OS 64-bit),
  adds you to the docker group, turns on a Raspberry Pi's memory cgroup for the tile server's cap (from the next
  reboot), builds the web front and the route service only when their sources changed, restarts only what changed,
  ends with checks of the pages and a list of what it changed, and stops with the reason when another program holds
  port 80 or 8080. It also takes away the containers, service and nginx snippets of an earlier install beside nginx in
  the same folder. See [pi/README.md](pi/README.md), also for moving a server off Internet-in-a-Box (keep `data/` and
  `.env`). The tile server image is pinned by digest on every machine now.
- `install-pi.sh` is now `scripts/install-nginx.sh`: for a machine whose own nginx serves port 80 (another
  installer's), and it needs `FIELDMAP_NGINX_DIR`; the old name still runs it. It installs on Raspberry Pi OS Lite
  64-bit, Debian 13 or Ubuntu 24.04 whose own nginx serves port 80, as one step of another installer.
  `FIELDMAP_DATA` puts the data folder outside the repository (every container and service reads it directly), `FIELDMAP_NGINX_DIR` the nginx snippets into a folder
  included inside the server block (rolled back if `nginx -t` fails), `FIELDMAP_PORTS_FILE` tells the machine's own
  firewall what remote access needs from outside; the services then run as their own user `fieldmap`, and only once
  the data folder is mounted. The script installs Docker (and Docker Compose v2 on Ubuntu too) and Node.js where
  they are missing, never asks anything, ends with checks through nginx and a list of what it changed, and a second
  run changes and restarts nothing. See [pi/README.md](pi/README.md).
- The remote gateway's nginx image, NetBird's client and the route service's Java base image are pinned by their
  digests, like the tile server: a newer one comes with an update of this repository, not with the weekly pull.
- **New Random Port** on the Remote Access page works again: the service asks for the port, and `remote.sh setup`
  (run as root) writes it to `.env`, which the service, shut in by systemd, cannot write.
- With `install-nginx.sh`, the route service's image builds: Docker there has no network of its own, so the build
  uses the machine's.

- NetBird **Network Isolation** on Admin › Remote Access, for several servers in one NetBird account: each server
  makes its own groups, an access policy (its devices to its gateway port only) and a setup key for its devices,
  with a NetBird access token that is used once and never stored. Devices paired earlier are moved into the
  server's device group by the NetBird address they connect from. **Check Isolation** reads the account's
  policies and reports the account-wide Default policy, or any other policy that reaches this server's peers; it
  never changes them. **Replace Device Key** makes a new device key and revokes the old one. Nothing changes in
  the app. Each server now joins NetBird as `fieldmap-server-<id>` (unique per installation); a server that
  joined as `fieldmap-box` is renamed when isolation is set up. After `git pull`, run the installer again.
- Documentation split up: a short README, a user guide by task (`docs/user-guide.md`), a reference
  (`docs/reference.md`), design and test notes (`docs/how-it-works.md`) and this changelog.
- A **User Guide** tab (`/guide/`) that works offline and prints. The header is now **Maps**, **Print a map**,
  **User Guide** and **Admin**; **Remote access**, **Devices** and **Map builder** are under Admin.
- NetBird as the other way in from outside, for a box whose router cannot open a port (Starlink, mobile internet, a
  router you cannot change): **Way in** on Admin, Remote access chooses the port on the router (as before, the
  default) or NetBird, one at a time; choosing one closes the other with its firewall rules. For NetBird the owner
  types one setup key of a NetBird network (NetBird's free cloud, or an own server). Paired devices get what they
  need from the box and reach the same gateway, with the same certificate, over NetBird. Nothing of NetBird is
  downloaded before. "From outside" and `./scripts/remote.sh status` say which way is in use and whether a device
  came in through it. The box's firewall lets in only the gateway's port on NetBird's interface, and now runs before
  Docker when the machine starts. After `git pull`,
  run the installer again as under "Updating".
- The header reads **Maps**, **Print a map**, **Devices**, **Admin**. Admin holds what not everyone needs: the map
  builder (where it is installed) and a new **Remote access** page with the remote access switch, "From outside" and
  port forwarding, moved there from the Devices page; a second row on those pages goes between them. Devices keeps
  the app, pairing, the paired devices and sharing.
- The Fieldmap Android app can be installed straight from the box: a **Get the Fieldmap app** card on the Devices
  page, for devices on the box's network. The installers put the newest published release on the box
  (`scripts/app.sh`); a file of your own can be added instead.
- The box's firewall answers nothing from outside the local network and VPNs: pings from outside go unanswered too,
  as do closed ports (dropped, never refused). It is put up by every install, also without remote access, and stays
  when remote access is removed. VPN interfaces of ZeroTier and OpenVPN are let in beside Tailscale and WireGuard.
- The box's own firewall no longer leaves SSH (port 22) open to callers from outside the local network: by
  default nothing but the gateway gets in from outside, and that only while remote access is switched on. Whoever
  logs in to the box from outside directly sets `REMOTE_FIREWALL_ALLOW=22` in `.env` first. The firewall is now
  also put up on a home network that has a private IPv6 prefix of its own (`fd..`), where its rules were refused.
- Sharing positions and markers between phones with the Fieldmap Android app, through the box and off until the
  owner switches it on (Devices page): at home for every phone on the box's network, away from home for the paired
  ones. Positions are held in memory only; shared markers are kept in `data/share/`. No new port, no sign-in, no
  outside service. After `git pull`, run the installer again as under "Updating".
- Remote access for the Fieldmap Android app, off until the owner switches it on: phones paired on the box's
  network can use the maps from anywhere. The box opens a port on its own router (UPnP, NAT-PMP, IPv6), lets in only paired phones through a
  gateway that serves only what the app needs, and puts a firewall on itself. No account or outside service. A new
  **Devices** page in the header switches it on and off, pairs phones and removes them.
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

## v1.0.0

- Merged the tile server, the data builders and the print tool into one repository (previously `tileserver-starter`
  v0.3 plus a separate print tool).
- New `web` service: nginx fronts TileServer GL and serves the print tool at `/print/`, built in the image.
- New `topo-print` style.
- Print tool: five paper sizes, tiled rendering for large sheets, markers and arrows, list page, atlas mode with cover
  and index pages.
