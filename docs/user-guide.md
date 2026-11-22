# fieldmap-server user guide

This guide covers the everyday tasks, step by step. The server also shows the same guide on its **User Guide**
tab, so you can read it with no internet. Scripts, options and file formats are in the [reference](reference.md);
design and security notes are in [how it works](how-it-works.md).

In this guide, the **server** is the machine that runs fieldmap-server. A **device** is an Android phone or tablet
with the Fieldmap app. The **local network** is the Wi-Fi or wired network the server is on.

## Contents

1. [Set up the server](#set-up-the-server)
2. [Build maps](#build-maps)
3. [View maps and use them in other apps](#view-maps-and-use-them-in-other-apps)
4. [Print a map](#print-a-map)
5. [Use the terrain tools](#use-the-terrain-tools)
6. [Install the app and pair a device](#install-the-app-and-pair-a-device)
7. [Location sharing](#location-sharing)
8. [Remote access](#remote-access)
9. [Update the server](#update-the-server)
10. [Back up the server](#back-up-the-server)
11. [Troubleshooting](#troubleshooting)

## Set up the server

You need a Linux machine (x86-64 or ARM64) with Docker and the Compose plugin, plus `bash`, `git`, `curl` and
`python3`. Building maps on the server itself also needs plenty of RAM and disk space (see
[Disk space](reference.md#disk-space)). A Raspberry Pi 4 can serve maps but is too small to build them: see
[pi/README.md](../pi/README.md).

1. Get the code and run the installer:

   ```bash
   git clone https://github.com/Sentinel6Systems/fieldmap-server.git
   cd fieldmap-server
   cp .env.example .env          # optional: ports and other settings
   ./scripts/install.sh
   ```

   The installer asks for `sudo` to set up the services. It starts everything, keeps it running across reboots,
   and prints the addresses of the pages at the end.
2. On any computer or device on the local network, open `http://<server-host>/` in a browser.

Every page has the same header:

| Tab | What it is for |
|---|---|
| **Maps** | View the maps. |
| **Print Map** | Print map sheets or atlases, or save them as PDF. |
| **User Guide** | This guide, on the server. |
| **Admin** | Pages for whoever runs the server: **Remote Access**, **Devices** and **Map Builder**. |

A tab marked **Unavailable** means that page is installed but its service is stopped (see
[Troubleshooting](#troubleshooting)). If the owner set an **Access Code** (`ADMIN_PIN` and `REMOTE_PIN`, see the
[reference](reference.md)), the Admin pages ask for it.

A new server has no maps yet, so the Maps page shows **No Maps Installed**. Build some next.

## Build maps

The **Map Builder** downloads public map data (OpenStreetMap and elevation data) once and turns it into map files.
After that, the maps work with no internet.

1. Open **Admin** › **Map Builder**.
2. **Select Regions.** Click a region on the map (a country, or a state or province of a large country) to select
   it; click it again to remove it. For a large region you can take all of it or only some parts. You can also use
   **Search Regions**.
3. **Select Layers.** Each line is one layer: the base map, contour lines, hillshade, elevation data (needed for the
   terrain tools), extra map features, and US government data for regions in the United States. Press **?** beside
   a line to read about it. Under **Terrain Resolution**, choose **Standard (All Regions)**, or **High (United
   States)** for the sharper USGS terrain.
4. **Storage and Build Time.** The page compares the space the build needs with the free space on the server. It
   won't start a build that doesn't fit.
5. **Build.** Read **Before You Start** and press **Start Build**, then **Start Build** again in **Confirm Build**.

> **Note:** A build replaces the layers you select. If the server holds one region and you build another, it then
> holds only the new one. Select every region you want to keep in one build.

A build needs the internet the whole time and can take hours or, for large regions, days. You can close the page;
the build carries on. The old maps keep working until the build has finished. If a build fails or you press
**Cancel Build**, the maps don't change, and starting the same build again resumes where it stopped.
**Repeat Last Build** fills in the last build's choices; **Reset** clears them.

Try a small region first to check that everything works.

**From the command line** you can do the same with a region file:

```bash
./scripts/build-region.sh --staged <region-file>
docker restart tileserver
```

See [Build scripts](reference.md#build-scripts) for region files and the single-step scripts.

## View maps and use them in other apps

Open **Maps**. Drag to move the map, scroll or pinch to zoom, and right-drag or turn two fingers to rotate or tilt
it. The compass button puts north back at the top. Pick a style in **Map Style**, for example a light map for the
day or a dark one for the night. The address in the browser keeps the place and the zoom, so you can bookmark it.

**ATAK, WinTAK, iTAK and other map apps** can use the server's maps as an online tile source:

1. On the Maps page, press **Tile URLs**.
2. Press **Copy** beside the style you want.
3. In the other app, add a custom map source (often called "XYZ", "custom map" or "map source") and paste the
   address.

The addresses work for devices that reach the server the same way your browser does. See
[Tile addresses](reference.md#tile-addresses-for-tak-and-other-apps) for the address format.

## Print a map

Open **Print Map**. The panel beside the map holds the settings; the blue outline on the map is the sheet.

### Print one sheet

1. Choose **Map Style**, **Scale**, **Paper** and **Orientation**.
2. Drag the blue outline to the area you want, or tap the map to move the sheet there.
3. Type a **Title** if you want one, and choose **Quality**: **Standard (200 dpi)** or **High (300 dpi)**, which is
   sharper but slower.
4. Press **Print** and choose your printer, or press **Download PDF** to save a file.

**Center Sheet Here** moves the sheet to the middle of the screen; **Fit Map to Sheet** zooms the screen to the
sheet. **Ground Area**, **Grid** and **Center** describe the sheet. Each sheet is at exact scale, with an MGRS/USNG
grid, scale bars and a declination diagram.

### Print an atlas

An atlas is a set of overlapping sheets with a cover and an index page. In **Pages**, choose a size such as
**Atlas 2 × 2**. For a ring binder, tick **Binder Margin**. Use a laptop or desktop for atlases: the browser draws
every sheet itself and needs a lot of memory.

### Choose what the map shows

Open **Layers** to switch parts of the map on or off (woodland, roads, contour lines, grid lines and so on), or to
print in black and white. Changes apply on screen and on paper. **Show All Layers** switches everything back on.

### Markers and drawings

1. Pick a color in **Default Color**.
2. Press **Add Marker**, **Add Arrow**, **Circle**, **Rectangle** or **Polygon**, and follow the hint under the
   buttons. For a polygon, tap each corner, then press **Finish Shape**.

Name or remove items in the **Markers**, **Arrows** and **Shapes** lists; **Clear All** removes everything. **Units**
switches between feet and miles or meters and kilometers. **Show Measurements** puts lengths and areas on the map.
**Include Legend Page** adds a page with the map symbols and a list of your items.

Only this browser keeps your drawings. To keep them, or move them to another computer, use **Export Drawings** and
**Import Drawings**. The file is GeoJSON, which other map software and the Fieldmap app can also open.

### Magnetic model

Printed sheets show the angles between true, grid and magnetic north for the print date. When the magnetic model
on the server gets close to its end date, the panel shows a notice with **Update Magnetic Model**. The update needs
an internet connection on the server.

## Use the terrain tools

If the server has elevation data (the elevation layer in the Map Builder), **Print Map** has a **Terrain** section.
Everything runs in the browser, with no internet.

1. Under **Analysis**, choose **Viewshed** (what can be seen from a point) or **Radio Coverage** (where a VHF or UHF
   radio can probably reach).
2. Under **Mode**, choose **Area Coverage** (everything around one point) or **Point-to-Point** (a profile between
   two points, A and B).
3. Press **Place** and tap the map to set each point. Drag a point to move it.
4. Set the heights and range:
   - Viewshed: **Observer Height (AGL)**, **Target Height (AGL)** (0 means "can I see the ground there") and
     **Radius**, up to 25 km.
   - Radio Coverage: the frequency (presets for MURS, 2 m and 70 cm amateur, GMRS/FRS and 915 MHz LoRa, or any
     value), **Antenna Height A** and **Antenna Height B**, and **Radius**.

For radio, each place is shown as clear, marginal, behind one ridge, or blocked. Open **Link Budget**, enter the
transmit power, antenna gains, cable loss and receiver sensitivity, and tick **Color by Link Margin** to color the
map by how much signal is to spare. **Point-to-Point** shows the ground profile, the line of sight (and, for radio,
the Fresnel zone), the worst obstruction, the distance, bearing and clearance.

The result prints on the sheet and on every atlas page, with a strip that records the inputs. Tick **Include
Profile Page** to add the point-to-point profile as an extra page.

> **Caution:** The result is an estimate from the shape of the ground only. Trees, buildings and weather usually
> make real coverage worse. Check on the ground before you rely on it. See
> [Terrain analysis](how-it-works.md#terrain-analysis) for the model and its limits.

## Install the app and pair a device

The [Fieldmap Android app](https://github.com/Sentinel6Systems/fieldmap-android) shows the server's maps and keeps
offline areas on the device for use with no network. Its own user guide covers the app in detail.

### Get the app from the server

No app store or account is needed.

1. Connect the device to the local network.
2. In the device's browser, open the server and go to **Admin** › **Devices**.
3. Under **Android App**, press **Download App**, open the file, and allow the browser to install apps when Android
   asks.

To update the app, download it again and install it over the old one. Offline areas and markers stay.

### Connect the app

Open the app, go to **Menu** › **Server**, and press **Find Server**, or type the **Server Address** and press
**Connect**.

### Pair a device

Pairing is only needed for [remote access](#remote-access). Pair each device once, on the local network:

1. Make sure **Remote Connectivity** is **Enabled** (Admin › Remote Access).
2. On the **Devices** page, under **Device Pairing**, press **Start Pairing**. Pairing mode stays active for 10
   minutes.
3. In the app, go to **Menu** › **Server** and press **Pair Device**.

The device then shows under **Paired Devices**. Press **Remove** beside a device to lock it out at once, for
example if it is lost.

## Location sharing

Devices can see each other's locations on the map and share markers, through the server. Location sharing is
disabled until the server owner enables it, and then each person starts sharing in their own app.

1. On the server: **Admin** › **Devices**, then **Enable** under **Location Sharing**. **Active Devices** lists who
   is sharing now.
2. In the app: **Menu** › **Location Sharing**, enter a **Display Name**, and press **Start Sharing**.
3. To share a marker, tap it and tick **Share with All Devices**.

On the local network every device can take part; from the internet, only paired devices with remote connectivity
enabled. The server does not store locations: a location disappears about two minutes after a device stops
sending. Shared markers stay on the server until someone deletes them.

> **Caution:** There is no sign-in. While location sharing is enabled, anyone on the local network can see the
> shared locations. Don't enable it on a network that strangers can join.

## Remote access

Remote access lets paired devices use the server's maps (and location sharing) over the internet. **Remote
Connectivity** is disabled until you enable it. There are two **Remote Connectivity Method** options, used one at
a time:

- **Port Forwarding** (the default): the server asks your router to open one port (UPnP or NAT-PMP), or you
  forward it yourself. No account or outside service.
- **NetBird VPN**: for internet connections where the router can't open a port, such as Starlink, mobile
  internet, or a router you can't change.

### Enable remote connectivity

1. On the local network, open **Admin** › **Remote Access**.
2. Under **Remote Connectivity**, press **Enable**. This also starts pairing mode for 10 minutes.
3. Under **Remote Connectivity Method**, keep **Port Forwarding**.
4. Read **Connectivity Status**: it says whether devices can reach the server from the internet, and if not, why.
   **Retry Port Mapping** asks the router again.
5. [Pair](#pair-a-device) each device.

Away from the local network the app connects by itself and still shows **Connected**.

If the router doesn't open the port by itself (no UPnP), tick **Manual Port Forwarding** in the **Port
Forwarding** card and add the rule the page shows ("Forward TCP port N to ADDRESS on your router") in your router's
settings. Enter the router's **Public IP Address**, or press **Detect**, then **Save**. **Generate New Port** gives
the server a different port.

**Auto-disable after 90 days without a remote connection** (on by default) switches remote connectivity off by
itself if no device has connected from the internet for 90 days. To switch it off yourself, press **Disable**: the
router's port is closed, and paired devices stay on the list for next time.

### Use NetBird VPN

You need a NetBird network: a free account at [netbird.io](https://netbird.io), or your own NetBird server. Only the
server owner needs an account; people using the devices type nothing. Use a NetBird network made only for this
server, because each paired device becomes a member of it.

1. **Create an account.** Open `app.netbird.io` and sign up. If it offers to add a machine or install NetBird,
   close that.
2. **Create a setup key.** In the NetBird dashboard, go to **Settings** › **Setup Keys** › **Create Key** (or
   **Create Setup Key**). Name it `Fieldmap`, switch on **Make this key reusable**, leave **Usage limit** empty,
   clear **Expires in** so it shows **Unlimited**, and leave **Ephemeral Peers** off. Press **Create Setup Key**
   and copy the key: NetBird shows it only once. Keep it secret.
3. **Connect the server.** On the local network, open **Admin** › **Remote Access** (Remote Connectivity must be
   enabled). Under **Remote Connectivity Method**, choose **NetBird VPN**. Paste the key into **Setup Key**, leave
   **Management URL** as it is, and press **Connect**. The first time, the server downloads NetBird, which takes a
   few minutes. When it's done, the page reports that the server is connected to the NetBird network, and the
   status reads "Method in use: NetBird VPN. Port forwarding is disabled."
4. **Devices.** Devices you pair from now on get what they need when you pair them. Devices paired earlier get it
   the next time they're on the local network with the app open.
5. **Check it.** Take a paired device off the local network (for example, Wi-Fi off, mobile data on) and open the
   app. It should show **Connected**. **Connectivity Status** on the Remote Access page then shows that a device
   connected through NetBird.

> **Note:** Change the connectivity method only while the devices are on the local network or can get to it. A
> device that is away when you change it can't connect until it has been on the local network once.

### Isolate the server in a shared NetBird account

Several Fieldmap servers can share one NetBird account. **Network Isolation** on the Remote Access page gives
this server and its devices a segment of their own: devices reach only this server's gateway port, and nothing of
other servers, their devices, or other machines in the account. It needs NetBird connected (above).

The server makes, in the NetBird account: two groups (`fieldmap-server-<id>` for the server,
`fieldmap-devices-<id>` for its devices), one access policy (`fieldmap-<id>`: devices to the server, TCP, the
gateway port only), and a setup key for its devices that puts every new device in the device group. Devices then
get this key instead of the one you typed. Nobody sets anything up on the devices. Each server joins NetBird as
`fieldmap-server-<id>`, so several servers can be told apart under **Peers**; a server that joined as
`fieldmap-box` before is renamed when you set up isolation (a name you chose yourself is kept).

1. **Create an access token.** In the NetBird dashboard, open **Users** › **Service Users**, create a service user
   named `fieldmap-setup` with the role **Admin**, then create an access token for it that expires in **1** day.
   Copy the token (it starts with `nbp_`). An Admin token can change the whole NetBird account, so the server uses
   it once and never stores it.
2. **Set it up.** On the Remote Access page, in the NetBird VPN card, paste the token into **NetBird Access Token**
   and press **Set Up Network Isolation**. The page shows the groups, the policy and the result of the check.
3. **Move devices paired earlier.** A device that joined NetBird before is moved into the device group by the
   address it connects from, once it has connected through NetBird. Leave the account's **Default** policy on for
   now, let each of those devices connect from outside once, then press **Repair Network Isolation** with a new
   token. **Devices in Group** and the warnings show which devices are not matched yet. (You can also add a device
   to `fieldmap-devices-<id>` by hand under **Peers**.)
4. **Turn off the Default policy.** A new NetBird account has a policy named **Default** that lets every peer reach
   every other peer; while it is on, the page says **Not Isolated**. The server never changes it. In the dashboard,
   open **Access Control** › **Policies**. If other computers in the account need Default, give them policies of
   their own first. Then switch **Default** off.
5. **Check.** Paste a token and press **Check Isolation** (a token with the **Auditor** role is enough). The page
   should say **Verified by policy check**. This checks NetBird's policies; it is not a traffic test.
6. **Clean up.** Revoke the setup key you typed by hand (**Setup Keys**), and delete the token or the service user.

**Later:** press **Check Isolation** about once a month (the page says **Not Recently Verified** after 30 days) and
after you change policies in the dashboard. The device key lasts a year; before it expires, press **Replace Device
Key**. Devices already paired keep working; the old key is revoked. A device you remove on the Devices page stays a
NetBird peer: delete it under **Peers** (the next check lists it). If the page reports a changed or missing group or
policy, press **Repair Network Isolation**. While isolation is set up, **Change Setup Key** is not offered: a key
typed by hand would put new devices outside the segment. To stop, run `./scripts/remote.sh netbird-forget`; it
names the groups, policy and key to delete in the dashboard.

**Stop using NetBird:** choose **Port Forwarding** under **Remote Connectivity Method**. The server keeps the key
for later. To remove NetBird from the server completely, run `./scripts/remote.sh netbird-forget` and delete
the server's peer (`fieldmap-server-<id>`) under **Peers** in the dashboard.

**A lost device:** press **Remove** beside it on the Devices page, delete it under **Peers** in the NetBird
dashboard, revoke the setup key, create a new one, and enter it on the Remote Access page with **Change Setup Key**
and **Connect** (with network isolation: **Replace Device Key** instead). The other devices keep working.

**Your own NetBird server:** create the setup key on your NetBird server's dashboard, and enter its address (for
example `https://netbird.example.com`) in **Management URL** before you press **Connect**.

## Update the server

On the server:

```bash
cd <repo-dir>
git pull
./scripts/install.sh
```

On a server installed beside the machine's own nginx, run `sudo ./scripts/install-nginx.sh` instead. If you
edited files on the server, run `git stash -u` before `git pull` and `git stash drop` after.

**Maps built on another machine:** copy the `.mbtiles` files into the server's `data/` folder and restart the tile
server:

```bash
scp data/<name>.mbtiles <user>@<server-host>:<repo-dir>/data/
ssh <user>@<server-host> 'cd <repo-dir> && ./scripts/install.sh'     # or: docker restart tileserver
```

**The app:** the installer fetches the newest published release of the Fieldmap app when the server has internet.
Devices update by downloading it again from the Devices page.

## Back up the server

Keep a copy of these in `<repo-dir>/data/`:

| What | What it holds | If it is lost |
|---|---|---|
| `*.mbtiles` | The maps | Build them again |
| `remote/` | The server's certificates, the paired devices and the NetBird setup key | Pair every device again |
| `share/` | Shared markers | Shared markers are gone |
| `admin/` | The Map Builder's last choices | Choose again |

Also keep `.env`, and keep the server's address and any Access Code on paper.

## Troubleshooting

| Problem | What to do |
|---|---|
| The server's pages don't open. | Check the device is on the local network, the address is right, and the server is on. After a restart, wait a few minutes. |
| The map is empty or gray. | Move to a region the server has. A new server has no maps: build some first. |
| A tab shows **Unavailable**. | That service is stopped. Restart the server. If it stays, check `systemctl status fieldmap-admin` (Map Builder) or `fieldmap-remote` (Remote Access, Devices). |
| **Remote Access** or **Devices** won't open. | These pages only open from the local network. |
| **Print** does nothing. | The browser blocked the print window. Use **Download PDF** and print the file. |
| An atlas stops or the browser closes. | Use a laptop or desktop, a smaller atlas, or **Standard** quality. |
| The drawings on Print Map are gone. | Only the browser that made them keeps them. Use the same browser, or import your exported file. |
| A build stops with a disk space message. | Free some space, or select fewer regions or layers. Starting the same build again resumes it. |
| The app can't pair. | Enable Remote Connectivity, press **Start Pairing**, then pair within 10 minutes. |
| A paired device can't connect from the internet. | Check Remote Connectivity is enabled and read **Connectivity Status**. Bring the device to the local network once so it learns the server's current address and connectivity method. |
| **Connectivity Status** says the router did not open a port. | Turn on UPnP or NAT-PMP in the router, or use **Manual Port Forwarding**. With two routers in a row, forward on both or put the first in bridge mode. |
| **Connectivity Status** mentions a shared address (Starlink, mobile internet). | The router can't be reached from the internet. Use NetBird VPN. |
| NetBird stays on "starting" for more than 10 minutes. | Check the server has internet. Press **Change Setup Key**, then **Connect**. |
| NetBird is connected but a device can't get in. | Bring the device to the local network once with the app open. If the setup key expired, create a new one. |
| Nobody sees my location. | Location Sharing must be enabled on the server and started in the app, and the others must start sharing too. |

`./scripts/remote.sh status` and `journalctl -u fieldmap-remote` show what the router and NetBird answered.
