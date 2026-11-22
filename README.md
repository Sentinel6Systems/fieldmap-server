# fieldmap-server

A self-hosted, fully offline topographic map server for the region of your choice. Install it on one Linux
machine, x86-64 or ARM64, build your maps once, and the server delivers them over its local network with no
internet.

- **Maps** in the browser, in six styles (light and dark topographic, OpenStreetMap, two print styles and a
  transparent overlay), as vector tiles for web and mobile apps and raster tiles for ATAK, WinTAK and iTAK.
- **One shared map** in the browser: markers, arrows, shapes and routes along roads and trails, shared live with
  the devices on the server, plus **Messages** between them.
- **Print**: exact-scale sheets (1:12k to 1:100k, Letter to Arch C) and binder-ready atlases, with an
  MGRS/USNG grid, declination, markers, arrows, shapes and a legend page.
- **Terrain tools**: viewshed and VHF/UHF radio line of sight from the server's elevation data, on screen and on paper.
- **Map Builder**: choose regions on a world map, check the disk space and time, and build from the browser.
  OpenStreetMap, Copernicus 30 m elevation worldwide, and USGS 10 m and 1 m elevation and US federal map data
  in the United States.
- **The Fieldmap Android app**, installed straight from the server: offline areas, GPS, markers, offline routes,
  track recording, location sharing and messages, also over Meshtastic mesh radios.
- **Remote access** for paired devices, through a port on your router or through NetBird, disabled until you enable
  it. The server has its own firewall.

It runs on a laptop, a home server, or a Raspberry Pi 4 or 5 (64-bit), with the same installer. There are no CDN calls, web fonts
or online services anywhere in the stack.

## Install

You need Linux with Docker and the Compose plugin, and `bash`, `git`, `curl` and `python3`. Building maps needs more:
8 GB of RAM for a few US states or a small country, 16-32 GB for large regions, and a lot of free disk (see
[Disk space](docs/reference.md#disk-space)). Tested on Ubuntu 24.04 and 26.04.

```bash
git clone https://github.com/Sentinel6Systems/fieldmap-server.git
cd fieldmap-server
cp .env.example .env          # optional: ports and other settings
./scripts/install.sh
```

Run it as yourself, without sudo. It installs what is missing (Docker, on Debian 13, Ubuntu 24.04 or Raspberry Pi
OS), starts everything, keeps it running across reboots, checks the pages and prints their addresses. On a Raspberry
Pi, see [pi/README.md](pi/README.md) first. A machine whose own nginx already serves port 80 (another installer may
call it as one step) uses `sudo ./scripts/install-nginx.sh` instead (also in [pi/README.md](pi/README.md)).

## First steps

1. Open `http://<server-host>/` in a browser on the local network.
2. Go to **Admin** › **Map Builder**, choose a small region, and build it to check that everything works. Then
   build the regions you need.
3. Open **Map** to view them, draw and plan routes; its **Print** tab prints.
4. To use the Fieldmap app, open **Admin** › **Devices** and follow its steps: install the app on the device,
   then enable **Location Sharing and Messages**. **Remote Connectivity** is only for use away from the local
   network.

## Documentation

- [User guide](docs/user-guide.md): set up the server, build maps, print, terrain tools, the app, location sharing,
  remote access, updating, backup and troubleshooting. The server shows the same guide on its **User Guide** tab.
- [Reference](docs/reference.md): pages and addresses, install options, build scripts and region files, styles,
  data files, tile addresses, firewall, remote access options.
- [How it works](docs/how-it-works.md): design and security notes, the terrain model, test results.
- [Raspberry Pi, and installing beside a machine's own nginx](pi/README.md); [systemd units](systemd/README.md).
- [Changelog](CHANGELOG.md).
- The [Fieldmap Android app](https://github.com/Sentinel6Systems/fieldmap-android).

## Updating

```bash
cd <repo-dir>
git pull
./scripts/install.sh          # beside the machine's own nginx: sudo ./scripts/install-nginx.sh
```

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
The code in this repository is under the GNU General Public License, version 3 (GPL-3.0). See
[LICENSE](LICENSE). Copies from before November 2026, when it was under the MIT license, keep that license.
