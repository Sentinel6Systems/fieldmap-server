# How fieldmap-server works

Design notes, security details, limits and test results. For step-by-step instructions, see the
[user guide](user-guide.md); for scripts and options, see the [reference](reference.md).

## Map Builder

A build from the page writes a region file, `data/admin/region.env`, and runs `build-region.sh --staged` with it:
the same scripts as by hand.

- **New files replace the old ones; they are not merged into them.** A device that holds one country and builds
  another then holds only the other, so the page says this before the build starts. Layers that are not ticked stay
  as they are.
- The new files are written to `data/incoming` and moved into `data/` together when every step has finished, and
  the tile server is then restarted (or started, the first time). Until then, and if a step fails or the build is
  stopped, the maps in use stay untouched. The steps that did finish are kept: starting the same build again
  carries on where it stopped (a different selection starts afresh). The page keeps the last build's choices:
  **Rebuild** (or **Repeat Last Build** in step 4) fills them back in and asks before it starts.
  **Reset** in step 4 takes every region off and sets the layers and options back to the first visit's.
- The estimate is made before anything is downloaded, from the bundled list of regions (`admin/regions.json`:
  size of each OpenStreetMap extract, number of 1° elevation cells with land, land area) and the same rates as the
  build scripts' own checks. It is on the safe side; the build scripts still run their own checks at each step.
  It follows every change on the page, and the free space on the device. The steps run one after another, so it
  counts the working room of one step at a time. Elevation data that earlier builds downloaded for the chosen
  regions, and what a build that did not finish left behind, are already on the device and not counted again.
  The sharper terrain (USGS 10 m) needs roughly ten times the space of the standard one, mostly for the download
  (about 0.35 to 0.5 GB per 1° cell) and the contour lines' working room; when it is most of the total, the page
  says what the standard terrain would need.

## Terrain analysis

The print tool's terrain tools run in the browser, in a web worker, from the server's elevation tiles
(`data/elevation.mbtiles`). No internet is needed. The model in detail:

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
  obstacle) or **blocked** (more than 20 dB, or two or more ridges cut the line of sight). Under **Link Budget**
  (transmit power, antenna gains, cable loss, receiver sensitivity; the defaults are a 5 W handheld with its stock
  antenna) the map is colored by link margin instead: received level = power + gains − cable loss − free-space loss
  (ITU-R P.525) − diffraction loss, against the sensitivity, in four bands: comfortable (20 dB or more to spare),
  workable (10-20 dB), unlikely (0-10 dB), none. CB and HF are not offered: their ground wave and skywave are outside
  this model.
- **Line A to B** (both modes): an elevation profile between two points, with the earth's curvature drawn as a bulge,
  the straight sight line or radio path between the antennas, for radio the 60 % and 100 % first Fresnel zone, and the
  worst obstruction marked (also on the map). Read-outs: distance, bearing (true and magnetic), clearance at the worst
  point as a share of the Fresnel zone, free-space and diffraction loss, and the link margin when a budget is set.
- **Advanced**: the earth's curvature with refraction is an earth of k × 6,371 km: k = 7/6 for sight and 4/3 for radio
  by default.
- Heights and distances follow the **Units** setting (feet and miles, or meters and kilometers); points are shown in
  MGRS and latitude/longitude. Settings and points are remembered with the other print settings.
- **On paper**: the overlay, the radius ring and the points print on the sheet and on every atlas page and the index
  (the Layers panel switches them off under "Terrain analysis"; in black and white the overlay prints as hatching and
  stipple, not a tint). The collar gets a strip with the inputs (mode, heights, frequency, radius, k, the link budget),
  a legend or the line's read-outs, the date of the elevation data and its notice. **Print the profile as an extra
  page** adds the A-to-B chart and read-outs after the map, like the list page.

### Limits

The limits are also stated in the panel and on paper. It is a simple terrain-only model: no trees, buildings or clutter
loss, no ground reflection, no ducting, and only single-edge diffraction at the worst obstacle. Real coverage is
usually worse in forest and towns. It is an estimate for planning, not a guarantee. The elevation data is Copernicus
GLO-30, a surface model at 30 m: it holds some of the height of forest and large buildings, smoothed, and none of
anything smaller. A more complete radio model is Longley-Rice (the Irregular Terrain Model, ITM); NTIA's reference
implementation is public domain, and adding it, compiled to WebAssembly, is a possible later step. It is not built.

### How it is computed

The worker fetches the elevation tiles around the point (cached for the session), resamples them to a
square grid in meters around the point at the data's resolution (about 30 m at zoom 12), and runs an R2 viewshed (a
ray from the observer to every edge cell; each cell takes the answer of the ray passing closest to its center) or, for
radio, radials from the transmitter one grid cell apart at the edge, testing the path to every point on them. Radio
work grows with the cube of the radius, so past 12 km its grid coarsens to radius / 400 (63 m at 25 km). Moving a point
or changing an input cancels a run in progress. Measured on a desktop browser (a mid-range phone is perhaps three
times slower; not measured on one): viewshed 10 km 0.2 s, 25 km 0.8 s; radio 10 km 0.9 s, 25 km 1.6 s.

**Checks** (`cd print && npm test`, Node 22 or later): synthetic terrain with a known answer (cone, conical pit, pit with
vertical walls, single ridge, the radio horizon over flat ground) and the formulas against ITU-R P.525 and P.526 worked
values. Against `gdal_viewshed` on the same 30 m grid, 10 km radius, two areas (flat coastal plain, ridge and valley),
three observer and target settings: 98.8-99.95 % of the cells agree, and the visible areas overlap 82-91 %
(intersection over union); the differences are cells at the edges of visible areas, where the two algorithms
interpolate differently. Built from the tiles instead of the source, the agreement drops by at most 0.3 points. To
repeat it: `tools/viewshed-compare.py` (in the tool container) and `print/test/compare-viewshed.mjs`. A radio profile
across a ridge checked by hand from the source elevation data: diffraction loss 10.5 dB by hand, 10.4 dB in the tool
(the difference is the tiles' 30 m resampling).

## Magnetic model updates

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

## Contour interval

The contours are a fixed set built from the elevation data (`build-contours.sh`: 40 ft with 200 ft index lines,
or meters), and the Layers panel can thin them to index lines only. A contour interval chosen per sheet from the
terrain (about 2.4 mm apart on paper for "sparse", 1.2 mm for "full", rounded to a standard interval and printed
in the collar) would need contour lines computed in the browser from an elevation tile set. That is not built: a
phone would have to fetch and trace elevation tiles for every page of an atlas, and the Pi would serve a third
large tile set. The planned route is two or three pre-built contour sets (for example 20, 40 and 100 ft) and a
choice per scale in the print tool.

## Remote access

- **Pairing on the home network only.** The app makes a key in the device's key store (the private half cannot be
  read out again) and the server gives it a certificate for that key. The server is its own certificate authority; its
  key stays in `data/remote/`. It pairs only while pairing is open, and only callers on the local network: a
  request from outside, or one that came through any proxy or tunnel, is refused.
- **The router opens one port.** The server asks the home router to forward a random outside port to its gateway,
  the way games and video calls do (UPnP or NAT-PMP), renews it every quarter of an hour, and notes the address
  the router has outside. It also offers its own IPv6 address, which needs no forwarding, and opens the router's
  IPv6 firewall where the router lets it (UPnP IGD 2). The device learns all these addresses whenever it is home.
- **The gateway lets in only paired devices.** It is an nginx container that refuses everyone without a
  certificate this server issued, checks every request against the list of paired devices, and serves only what the app
  needs: styles, fonts, symbols and data tiles, and the few sharing addresses (below). Not the
  pictures the tile server draws, not the print tool, not the Map Builder, nothing else. It limits connections per visitor and takes nginx's security fixes every week.
- **A firewall on the server itself** (see [Firewall](reference.md#firewall) in the reference) lets nothing in from outside the local network
  and VPNs but the gateway, and answers nothing else. Devices at home, and the server's own connections out, are not
  affected. Where the machine has a firewall already (Internet-in-a-Box's drops everything from the
  home network but its own ports), the gateway's port is let through it too, only while remote access is on.
- **Nothing gives the server away.** The names in its certificates are random letters.

### Security notes and tests

- Anyone who can join the local network while pairing is open can pair a device. On an open hotspot, set
  `REMOTE_PIN`. A VPN into the home network counts as the home network.
- Pairing runs over the home network unencrypted: it assumes that network is trusted at that moment.
- A device that was removed is told so the next time it tries from outside, and can be paired again at home.
- The server's firewall closes SSH (port 22) to callers from outside the local network. From the local network, and
  through a VPN into it, SSH works as before. If you log in to the server from outside directly, put
  `REMOTE_FIREWALL_ALLOW=22` in `.env` **before** you update, then run `sudo ./scripts/firewall.sh apply`.
- Back up `data/remote/` with the server. If it is lost, every device has to be paired again.
- The service is `tools/remote-server.py` with `tools/portmap.py` (Python's standard library, `openssl` and `ip`),
  run by `fieldmap-remote.service`. Tests: `python3 -m unittest tools/test_remote_server.py tools/test_portmap.py`.
- NetBird: tested against a self-hosted NetBird server (v0.83.1) in containers, with the whole server (systemd, Docker,
  the installers) in a container and a second NetBird member as the device, direct and through NetBird's relay; not
  yet against NetBird's cloud or over a real Starlink line.
- Tested against a pretend router (UPnP and NAT-PMP) and in containers (the firewall), not yet on many real
  routers. If yours behaves differently, `./scripts/remote.sh status` and `journalctl -u fieldmap-remote` say what
  it answered.

### NetBird

- **NetBird is only the road; the lock stays the same.** Devices reach the same gateway, with the same certificate,
  at the server's address in the NetBird network. A member of the NetBird network without a certificate of this server
  gets nothing. NetBird carries only TLS that it cannot read.
- **The setup key is a secret, and only paired devices get it:** in the answer to pairing (`POST /mapremote/enrol`,
  on the local network) and through the gateway with a paired certificate (`GET /mapremote/netbird`). Not in
  `/mapremote/info`, not in a log, not on the page after it was saved. It is kept in `data/remote/netbird/setup-key`
  (only the owner of this folder can read it).
- **The client is NetBird's own container** (`netbirdio/netbird`, the `netbird` service in the compose files, profile
  `netbird`; x86 and ARM). It is not downloaded until the owner presses **Connect**; `fieldmap-netbird.path` then runs
  `./scripts/remote.sh netbird-apply` as root, which starts or removes it. It runs on the machine's own network with
  only the right to change network settings, on its own interface `nb-fieldmap`, with NetBird's DNS, routes, firewall
  changes, IPv6, SSH server and remote jobs off (`tools/netbird-run.sh`). Its identity is kept in
  `data/remote/netbird/state/`, so it comes back after a restart with the same address. It is updated every week with
  the gateway. A container (not NetBird's package) keeps everything of NetBird inside this folder and out of the
  machine's package list, and it updates with the rest.
- **Use a NetBird network made only for the server.** A new NetBird account lets all its members reach each other. Each
  paired device becomes a member. In a network shared with other machines, a device can then reach whatever that
  network's rules permit.
- **A lost device:** press **Remove** on the Devices page (the server then refuses it at once), delete it under
  **Peers** in the NetBird dashboard, revoke the setup key there, and give the server a new one (**Change Setup Key**,
  type the new key, **Connect**). Devices that joined before keep working with the identity they have. NetBird does not check
  a new key for a server that is a member already; the page then asks you to see that the key shows as valid in the
  dashboard.

## Sharing positions and markers

- **Its own small service**, `tools/share-server.py` (Python's standard library), run by `fieldmap-share.service`
  apart from the one that pairs devices. It takes what devices send, so it is shut in: it may write only
  `data/share/`, cannot read `data/remote/` (the server's certificate authority and its list of devices), talks to
  nothing but this machine, and has a ceiling on its memory.
- **No new port.** At home the app asks the server's web server, at `/mapshare/`. The service answers only callers
  on the local network there, and refuses a request that came through any other proxy or tunnel. From outside,
  the gateway passes on exactly the [sharing addresses](reference.md#sharing-interface), for paired devices only, a few small requests a second each.
- **Everything is checked and limited:** each field of a position and of a marker, 2 kB for a position and 8 kB
  for a marker, at most 100 people and 2000 markers, and a limit on how often one device may ask.
- **Nothing about people goes into the log:** no names, no places, no addresses.

### Security notes and tests

- **There is no sign-in.** While sharing is on, any device on the local network can read the positions and send
  one, under any name. Leave it off on a network that strangers can join. From outside, only paired devices can.
- Sharing is part of the Devices set-up (`./scripts/remote.sh setup`, run by the installers): a server installed with
  `--no-remote` has neither. `./scripts/remote.sh status` says whether it is on.
- With `REMOTE_PIN` set, the switch asks for the Access Code like the rest of the Devices page.
- Back up `data/share/` if the shared markers matter.
- Tests: `python3 -m unittest tools/test_share_server.py`. Tested with curl against the web server and the gateway
  in containers, not yet under systemd on a real server.
