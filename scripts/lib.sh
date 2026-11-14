#!/usr/bin/env bash
# Shared functions for the terrain build scripts.
# Do not run this file directly.

TOOLS_IMAGE="tileserver-tools"

# Planetiler release used for the base map. Pinned so a new release cannot change
# the tiles unnoticed; set PLANETILER_IMAGE to try another one.
PLANETILER_IMAGE="${PLANETILER_IMAGE:-ghcr.io/onthegomap/planetiler:0.10.2}"

# Languages a build can keep labels for (LANGUAGE / --language): the ten most
# spoken by total speakers, Ethnologue 2025. Without a choice every language is
# kept. The styles try these in this order, then the local name.
BUILD_LANGUAGES="en zh hi es ar fr bn pt ru id"

# Stop unless LANGUAGE is empty or one of BUILD_LANGUAGES.
check_language() {
    [ -z "${LANGUAGE:-}" ] && return 0
    case " $BUILD_LANGUAGES " in
        *" $LANGUAGE "*) return 0 ;;
    esac
    echo "Error: unknown language '$LANGUAGE'. Use one of: $BUILD_LANGUAGES" >&2
    exit 1
}

# The world layer for the current LANGUAGE, relative to data/.
world_file() {
    echo "world-z0-8${LANGUAGE:+-$LANGUAGE}.mbtiles"
}

# Run Planetiler (OpenMapTiles profile) on one OSM file from data/sources.
# Planetiler makes no network calls: fetch_shared_sources must run first.
# Arguments: OSM_FILE_NAME OUTPUT_NAME MEMORY [more Planetiler arguments ...]
run_planetiler() {
    local osm="$1" out="$2" memory="$3"
    shift 3
    local lang=()
    [ -n "${LANGUAGE:-}" ] && lang=(--languages="$LANGUAGE")
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -e JAVA_TOOL_OPTIONS="-Xmx$memory" \
        -v "$REPO_DIR/data":/data \
        "$PLANETILER_IMAGE" \
        --force \
        --osm-path="/data/sources/$osm" \
        --tmpdir=/data/tmp \
        --output="/data/$out" \
        "${lang[@]}" \
        "$@"
}

# Build data/basemap.mbtiles from one OSM file in data/sources.
# With WORLD=yes, the region is built at zoom 9-14 only and the world layer
# (built first if missing) supplies zoom 0-8 everywhere.
# Arguments: OSM_FILE_NAME MEMORY [more Planetiler arguments ...]
build_basemap() {
    local osm="$1" memory="$2"
    shift 2
    if [ "${WORLD:-no}" != yes ]; then
        run_planetiler "$osm" basemap.mbtiles "$memory" "$@"
        return
    fi
    local world
    world="$(world_file)"
    if [ ! -s "$REPO_DIR/data/$world" ]; then
        echo "No world layer yet ($world): building it first."
        LANGUAGE="${LANGUAGE:-}" "$REPO_DIR/scripts/build-world.sh"
    else
        echo "World layer: reusing data/$world (delete it to rebuild)"
    fi
    run_planetiler "$osm" region-z9-14.mbtiles "$memory" --minzoom=9 "$@"
    merge_world_tiles "$REPO_DIR/data/$world" "$REPO_DIR/data/region-z9-14.mbtiles" \
        "$REPO_DIR/data/basemap.mbtiles"
    rm -f "$REPO_DIR/data/region-z9-14.mbtiles"
}

# Combine the world layer (zoom 0-8) and a region file (zoom 9 and up) into
# one MBTiles file. Tiles are copied as they are, not re-encoded.
# Arguments: WORLD_FILE REGION_FILE OUTPUT_FILE
merge_world_tiles() {
    echo "Merging the world layer (zoom 0-8) with the region (zoom 9+) into $(basename "$3")"
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, shutil, sqlite3, sys
world, region, out = sys.argv[1:]
tmp = out + ".tmp"
shutil.copyfile(region, tmp)
db = sqlite3.connect(tmp)
db.execute("ATTACH DATABASE ? AS w", (world,))
names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
compact = {"tiles_shallow", "tiles_data"} <= names   # Planetiler's deduplicated layout
rows = db.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM w.tiles WHERE zoom_level <= 8").fetchall()
for z, x, y, data in rows:
    if compact:
        cur = db.execute("INSERT INTO tiles_data (tile_data) VALUES (?)", (data,))
        db.execute("INSERT INTO tiles_shallow (zoom_level, tile_column, tile_row, tile_data_id) VALUES (?, ?, ?, ?)",
                   (z, x, y, cur.lastrowid))
    else:
        db.execute("INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)", (z, x, y, data))
meta = dict(db.execute("SELECT name, value FROM main.metadata"))
wmeta = dict(db.execute("SELECT name, value FROM w.metadata"))
meta["minzoom"] = "0"
meta["bounds"] = "-180,-85.05113,180,85.05113"
if "json" in meta and "json" in wmeta:
    rj, wj = json.loads(meta["json"]), json.loads(wmeta["json"])
    layers = {l["id"]: l for l in rj.get("vector_layers", [])}
    for l in wj.get("vector_layers", []):
        r = layers.setdefault(l["id"], l)
        r["minzoom"] = min(r.get("minzoom", 0), l.get("minzoom", 0))
        r.setdefault("fields", {}).update(l.get("fields", {}))
    rj["vector_layers"] = list(layers.values())
    meta["json"] = json.dumps(rj)
db.execute("DELETE FROM main.metadata")
db.executemany("INSERT INTO main.metadata (name, value) VALUES (?, ?)", meta.items())
db.commit()
db.execute("DETACH DATABASE w")
db.close()
os.replace(tmp, out)
print(f"  {len(rows)} world tiles added")
PY
}

# Free space in whole GB on the drive that holds a directory.
free_gb() {
    df -BG --output=avail "$1" | tail -1 | tr -dc '0-9'
}

# Area of a bounding box in square degrees.
# Arguments: MIN_LON MIN_LAT MAX_LON MAX_LAT
bbox_area() {
    python3 -c "print(round(max(0, $3 - $1) * max(0, $4 - $2), 1))"
}

# Stop with a clear message when a drive has less free space than a step
# needs. The numbers are estimates with a safety margin.
# Arguments: DIR NEEDED_GB WHAT
require_free_space() {
    local dir="$1" need_gb="$2" what="$3"
    mkdir -p "$dir"
    local avail_gb
    avail_gb="$(free_gb "$dir")"
    if [ "$avail_gb" -lt "$need_gb" ]; then
        cat >&2 <<EOF

ERROR: Not enough free disk space for $what.

  Needed (estimate): about $need_gb GB
  Available:         $avail_gb GB, on the drive that holds
                     $dir

What you can do:
  - Free up space on that drive, then run the same command again.
  - Or move this whole folder to a drive with more space.
  - Or build a smaller area (a smaller bounding box, or fewer
    Geofabrik areas in the region file).

Files that were already downloaded are kept and reused next time.
EOF
        exit 1
    fi
    echo "Disk check: $what needs about $need_gb GB; $avail_gb GB free. OK."
}

# Print how much working space a step used, so the estimates in
# require_free_space can be checked against real builds.
# Arguments: WORK_DIR OUTPUT_FILE
report_work_space() {
    local work_mb out_mb
    work_mb="$(du -sBM "$1" 2>/dev/null | cut -f1 | tr -dc '0-9')"
    out_mb="$(du -sBM "$2" 2>/dev/null | cut -f1 | tr -dc '0-9')"
    python3 -c "print(f'Working space used: {${work_mb:-0}/1024:.1f} GB; output file: {${out_mb:-0}/1024:.1f} GB.')"
}

# Called when a step fails. If the drive is (nearly) full, say so in plain
# words, because the underlying tool errors are cryptic.
# Argument: DIR
disk_full_hint() {
    local dir="$1"
    local avail_gb
    avail_gb="$(free_gb "$dir" 2>/dev/null || echo 999)"
    if [ "$avail_gb" -lt 2 ]; then
        cat >&2 <<EOF

The build stopped because THE DISK IS FULL ($avail_gb GB free on the
drive that holds $dir).

Free up space, then run the same command again. Downloads that finished
are kept and reused. Work files from the failed step can be deleted:
  rm -rf $dir/cache/contours $dir/cache/hillshade $dir/tmp
EOF
    fi
}

# Shared inputs that Planetiler needs besides the OSM file.
# Format: "<file name in data/sources> <url> [<fallback url> ...]"
# URLs are tried in order. Each one gets several attempts before the
# script moves on to the next. Add mirrors here when a source is unreliable.
SHARED_SOURCES=(
    "water-polygons-split-3857.zip https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip https://www.osmdata.org/download/water-polygons-split-3857.zip"
    "natural_earth_vector.sqlite.zip https://naciscdn.org/naturalearth/packages/natural_earth_vector.sqlite.zip"
    "lake_centerline.shp.zip https://dev.maptiler.download/geodata/omt/lake_centerline.shp.zip"
)

# Download one shared input, trying each URL in turn.
# Arguments: DEST_FILE URL [URL ...]
# A file that already exists and looks like a zip archive is kept.
fetch_source() {
    local dest="$1"
    shift
    local name
    name="$(basename "$dest")"

    if [ -s "$dest" ] && [ "$(head -c 2 "$dest")" = "PK" ]; then
        echo "cached: $name"
        return 0
    fi

    local url
    for url in "$@"; do
        echo "download: $url"
        # Start each URL from scratch: a mirror can hold a different
        # version of the file, so a partial file must not be resumed
        # from another source.
        rm -f "$dest.part"
        if curl -fSL --retry 3 --retry-delay 10 --retry-connrefused \
            --connect-timeout 30 -o "$dest.part" "$url" \
            && [ "$(head -c 2 "$dest.part")" = "PK" ]; then
            mv "$dest.part" "$dest"
            return 0
        fi
        echo "  failed: $url"
    done
    rm -f "$dest.part"
    echo "Error: could not download $name from any source." >&2
    return 1
}

# Download every shared input into a folder.
# Argument: DIR
fetch_shared_sources() {
    local dir="$1" entry
    mkdir -p "$dir"
    for entry in "${SHARED_SOURCES[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        local file="$1"
        shift
        fetch_source "$dir/$file" "$@"
    done
}

# OpenStreetMap extract mirrors: "<name> <url template>". In the template,
# {path} is the Geofabrik path (north-america/us/new-jersey) and {path_} is
# the same path with "-" turned into "_" in its first part
# (north_america/us/new-jersey). Movisda accepts Geofabrik paths and
# redirects them to its own file names.
OSM_MIRRORS=(
    "geofabrik https://download.geofabrik.de/{path}-latest.osm.pbf"
    "movisda https://osm.download.movisda.io/{path}-latest.osm.pbf"
    "geo2day https://geo2day.com/{path_}.pbf"
)
# Mirrors of the whole OpenStreetMap planet, for the world layer (build-world.sh).
# The OSM Foundation asks for big downloads to come from mirrors.
# shellcheck disable=SC2034  # used by filter-planet.sh
PLANET_MIRRORS=(
    "your.org https://ftpmirror.your.org/pub/openstreetmap/pbf/planet-latest.osm.pbf"
    "fau.de https://ftp.fau.de/osm-planet/pbf/planet-latest.osm.pbf"
    "init7 https://mirror.init7.net/openstreetmap/pbf/planet-latest.osm.pbf"
)
# A copy older than this many days is never downloaded.
OSM_MAX_AGE_DAYS="${OSM_MAX_AGE_DAYS:-30}"
# Size of the speed test download from each mirror.
OSM_SPEED_TEST_BYTES=2097152

# URL of one extract on one mirror.
# Arguments: TEMPLATE GEOFABRIK_PATH
osm_mirror_url() {
    local template="$1" path="$2"
    local first="${path%%/*}" rest=""
    [ "$path" != "$first" ] && rest="/${path#*/}"
    local path_="${first//-/_}$rest"
    template="${template//\{path_\}/$path_}"
    echo "${template//\{path\}/$path}"
}

# Last-Modified header of the file behind a URL; nothing if there is none.
# Argument: URL
osm_last_modified() {
    curl -sfIL --connect-timeout 15 -m 30 "$1" 2>/dev/null \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="last-modified" {d=$2} END {print d}'
}

# Age in whole days of a Last-Modified date. Prints nothing for no date.
# Argument: LAST_MODIFIED
osm_age_days() {
    local modified="$1"
    [ -n "$modified" ] || return 0
    local then_s now_s
    then_s="$(date -d "$modified" +%s 2>/dev/null)" || return 0
    now_s="$(date +%s)"
    echo $(( (now_s - then_s) / 86400 ))
}

# Rank the mirrors by download speed, measured with a short download of one
# extract from each. Sets OSM_MIRROR_ORDER (fastest first); mirrors that
# failed the test go last, so they are still tried if the others fail.
# Argument: GEOFABRIK_PATH (used for the test)
rank_osm_mirrors() {
    local path="$1" entry name template url result speed bytes
    local ranked=() failed=()
    echo "Mirror speed test: first $((OSM_SPEED_TEST_BYTES / 1048576)) MB of $path from each mirror"
    for entry in "${OSM_MIRRORS[@]}"; do
        read -r name template <<<"$entry"
        url="$(osm_mirror_url "$template" "$path")"
        result="$(curl -sfL -r "0-$((OSM_SPEED_TEST_BYTES - 1))" -o /dev/null \
            --connect-timeout 10 -m 30 -w '%{speed_download} %{size_download}' "$url" 2>/dev/null)" || result=""
        read -r speed bytes <<<"${result:-0 0}"
        speed="${speed%.*}"
        if [ "${bytes%.*}" -gt 0 ] 2>/dev/null; then
            printf '  %-10s %s\n' "$name" "$(python3 -c "print(f'{$speed/1048576:.2f} MB/s')")"
            ranked+=("$speed $name")
        else
            printf '  %-10s no answer or no such area\n' "$name"
            failed+=("$name")
        fi
    done
    OSM_MIRROR_ORDER=()
    while read -r _ name; do
        [ -n "$name" ] && OSM_MIRROR_ORDER+=("$name")
    done < <(printf '%s\n' "${ranked[@]}" | sort -rn)
    OSM_MIRROR_ORDER+=("${failed[@]}")
    echo "Mirror order: ${OSM_MIRROR_ORDER[*]}"
}

# Download one extract from the fastest mirror whose copy is at most
# OSM_MAX_AGE_DAYS old. Call rank_osm_mirrors first.
# Arguments: GEOFABRIK_PATH DEST_FILE
download_osm_extract() {
    local path="$1" dest="$2" name entry template url age modified
    for name in "${OSM_MIRROR_ORDER[@]}"; do
        for entry in "${OSM_MIRRORS[@]}"; do
            [ "${entry%% *}" = "$name" ] && template="${entry#* }"
        done
        url="$(osm_mirror_url "$template" "$path")"
        modified="$(osm_last_modified "$url")"
        age="$(osm_age_days "$modified")"
        if [ -z "$age" ]; then
            echo "  $name: not available (missing, or no date given), skipped"
            continue
        fi
        if [ "$age" -gt "$OSM_MAX_AGE_DAYS" ]; then
            echo "  $name: copy is $age day$([ "$age" -eq 1 ] || echo s) old (limit $OSM_MAX_AGE_DAYS), skipped"
            continue
        fi
        echo "download: $path from $name ($age day$([ "$age" -eq 1 ] || echo s) old)"
        # A partial download is resumed only from the same copy (same URL
        # and date); copies on different mirrors differ.
        if [ "$(cat "$dest.part.from" 2>/dev/null)" != "$url $modified" ]; then
            rm -f "$dest.part"
            echo "$url $modified" > "$dest.part.from"
        elif [ -s "$dest.part" ]; then
            echo "  resuming the partial download"
        fi
        if curl -fSL --retry 3 --retry-delay 10 --connect-timeout 30 -C - -o "$dest.part" "$url"; then
            mv "$dest.part" "$dest"
            rm -f "$dest.part.from"
            return 0
        fi
        echo "  $name: download failed"
    done
    echo "Error: no mirror has $path at most $OSM_MAX_AGE_DAYS days old." >&2
    echo "Check the path at https://download.geofabrik.de, or raise OSM_MAX_AGE_DAYS." >&2
    return 1
}

# Run the calling script again inside the tool container.
# The container has GDAL and Tippecanoe.
reexec_in_tools() {
    if [ -n "${IN_TOOLS:-}" ]; then
        return 0
    fi
    echo "Building the tool image (first run can take some minutes)..."
    docker build -q -t "$TOOLS_IMAGE" "$REPO_DIR/tools"
    # Run as the calling user so that files under data/ are not owned by
    # root. Host-side steps (curl, rm) must be able to write there too.
    exec docker run --rm \
        --user "$(id -u):$(id -g)" \
        -e HOME=/tmp \
        -e IN_TOOLS=1 \
        -e OSM_MAX_AGE_DAYS \
        -v "$REPO_DIR":/work \
        -w /work \
        "$TOOLS_IMAGE" \
        bash "/work/scripts/$(basename "$0")" "$@"
}

# Download Copernicus GLO-30 DEM tiles for a bounding box.
# Arguments: MIN_LON MIN_LAT MAX_LON MAX_LAT OUT_DIR
# The tiles are 1x1 degree GeoTIFF files.
# Tiles that do not exist (open water) are skipped.
download_dem() {
    local min_lon="$1" min_lat="$2" max_lon="$3" max_lat="$4" out_dir="$5"
    local base_url="https://copernicus-dem-30m.s3.amazonaws.com"
    mkdir -p "$out_dir"

    local lat0 lat1 lon0 lon1
    lat0=$(python3 -c "import math;print(math.floor($min_lat))")
    lat1=$(python3 -c "import math;print(math.floor($max_lat))")
    lon0=$(python3 -c "import math;print(math.floor($min_lon))")
    lon1=$(python3 -c "import math;print(math.floor($max_lon))")

    local lat lon name url dest
    for lat in $(seq "$lat0" "$lat1"); do
        for lon in $(seq "$lon0" "$lon1"); do
            name=$(python3 -c "
lat, lon = $lat, $lon
ns = 'N' if lat >= 0 else 'S'
ew = 'E' if lon >= 0 else 'W'
print(f'Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM')
")
            dest="$out_dir/$name.tif"
            if [ -f "$dest" ]; then
                echo "cached: $name"
                continue
            fi
            url="$base_url/$name/$name.tif"
            echo "download: $name"
            if ! curl -sfSL -o "$dest.part" "$url"; then
                echo "  no tile (water area), skipped"
                rm -f "$dest.part"
                continue
            fi
            mv "$dest.part" "$dest"
        done
    done

    if ! ls "$out_dir"/*.tif >/dev/null 2>&1; then
        echo "Error: no DEM tiles found for this bounding box." >&2
        exit 1
    fi
}
