# Changelog

## Since v1.0.0

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
