#!/usr/bin/env bash
# Shared functions for the terrain build scripts.
# Do not run this file directly.

TOOLS_IMAGE="tileserver-tools"

# Where the finished .mbtiles files go: data/, or data/$FIELDMAP_STAGE when a build stages
# its files there to swap them in together at the end (build-region.sh --staged).
# OUT_SUB is the same place relative to data/, for the containers that mount data/.
OUT_DIR="$REPO_DIR/data${FIELDMAP_STAGE:+/$FIELDMAP_STAGE}"
OUT_SUB="${FIELDMAP_STAGE:+$FIELDMAP_STAGE/}"

# Label on every container a build starts, so that a build can be stopped from outside
# (docker kill $(docker ps -q --filter label=fieldmap-build)).
BUILD_LABEL="fieldmap-build"

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
    docker run --rm --label "$BUILD_LABEL" \
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
        run_planetiler "$osm" "${OUT_SUB}basemap.mbtiles" "$memory" "$@"
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
        "$OUT_DIR/basemap.mbtiles"
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
    exec docker run --rm --label "$BUILD_LABEL" \
        --user "$(id -u):$(id -g)" \
        -e HOME=/tmp \
        -e IN_TOOLS=1 \
        -e FIELDMAP_STAGE \
        -e OSM_MAX_AGE_DAYS \
        -e ELEVATION_MAXZOOM -e ELEVATION_ENCODING -e ELEVATION_FORMAT \
        -e HILLSHADE_MAXZOOM -e HILLSHADE_FORMAT -e CONTOUR_JOBS -e DEM_SOURCE \
        -e DETAIL_MAX_KM2 -e DETAIL_MAXZOOM \
        -v "$REPO_DIR":/work \
        -w /work \
        "$TOOLS_IMAGE" \
        bash "/work/scripts/$(basename "$0")" "$@"
}

# Elevation data for the terrain builds (contours, hillshade, elevation), chosen with
# DEM_SOURCE:
#   copernicus  Copernicus GLO-30: 30 m, the whole world, the surface with its trees
#               and buildings. The default.
#   usgs        USGS 3DEP 1/3 arc-second: about 10 m, bare ground, mostly from lidar.
#               United States only: a cell with no USGS tile, and gaps inside a tile,
#               use Copernicus. About 350 MB to download per one-degree cell.
# Sets DEM_SOURCE, USGS_DIR and USGS_ARGS (the option the Python tools take).
terrain_source() {
    DEM_SOURCE="${DEM_SOURCE:-copernicus}"
    USGS_DIR="$REPO_DIR/data/cache/dem-usgs"
    case "$DEM_SOURCE" in
        copernicus) USGS_ARGS=() ;;
        usgs) USGS_ARGS=(--usgs-dir "$USGS_DIR") ;;
        *) echo "Error: DEM_SOURCE must be 'copernicus' or 'usgs', not '$DEM_SOURCE'." >&2; exit 1 ;;
    esac
}

# Default highest zoom of the hillshade or elevation tiles. Zoom 12 (38 m pixels at the
# equator) suits the 30 m data. The 10 m USGS data gets zoom 14 for the hillshade and 13
# for the elevation tiles; each zoom more is four times the tiles.
# Argument: hillshade | elevation
terrain_default_zoom() {
    if [ "${DEM_SOURCE:-copernicus}" = usgs ]; then
        [ "$1" = hillshade ] && echo 14 || echo 13
    else
        echo 12
    fi
}

# Region outline for the terrain builds (contours, hillshade, elevation): the union of
# the Geofabrik .poly outlines of the areas, cut to the box, or the box alone when there
# are no areas or an outline cannot be loaded. Writes WORK_DIR/outline (see
# tools/region-outline.py) and prints the outline and cell counts.
# Arguments: WORK_DIR MIN_LON MIN_LAT MAX_LON MAX_LAT [AREA ...]
terrain_outline() {
    local work="$1"; shift
    local bbox=("$1" "$2" "$3" "$4"); shift 4
    local areas=()
    if [ $# -gt 0 ]; then
        areas=(--areas "$@")
    fi
    terrain_source
    mkdir -p "$work/outline"
    python3 "$REPO_DIR/tools/region-outline.py" prepare --out "$work/outline" \
        --dem-dir "$REPO_DIR/data/cache/dem" --poly-cache "$REPO_DIR/data/cache/outlines" \
        "${USGS_ARGS[@]}" --bbox "${bbox[@]}" "${areas[@]}"
}

# USGS 3DEP 1/3 arc-second tile of a cell, from the Copernicus cell name. USGS names a
# tile by its north-west corner: the cell N48 W123 is USGS_13_n49w123.
usgs_tile() {
    local name="$1" lat lon
    lat=$((10#${name:23:2})); [ "${name:22:1}" = S ] && lat=$((-lat))
    lon=$((10#${name:30:3})); [ "${name:29:1}" = W ] && lon=$((-lon))
    lat=$((lat + 1))
    printf 'USGS_13_%s%02d%s%03d\n' "$([ "$lat" -ge 0 ] && echo n || echo s)" "${lat#-}" \
        "$([ "$lon" -lt 0 ] && echo w || echo e)" "${lon#-}"
}

# 0 when a cell has elevation data to build from. Argument: Copernicus cell name
cell_has_data() {
    [ -f "$REPO_DIR/data/cache/dem/$1.tif" ] ||
        { [ "$DEM_SOURCE" = usgs ] && [ -f "$USGS_DIR/$(usgs_tile "$1").tif" ]; }
}

# Number of the outline's cells that have elevation data (run after download_dem): cells
# found to be open sea on the way are left out, so the working-space checks only count land.
# Argument: WORK_DIR
terrain_land_cells() {
    local _part name n=0
    while read -r _part name <&3; do
        if cell_has_data "$name"; then n=$((n + 1)); fi
    done 3< "$1/outline/cells.txt"
    echo "$n"
}

# Print the expected output size and time of terrain steps for a prepared outline.
# Arguments: WORK_DIR STEP ...
terrain_estimate() {
    local work="$1"; shift
    python3 "$REPO_DIR/tools/region-outline.py" estimate --out "$work/outline" \
        --dem-dir "$REPO_DIR/data/cache/dem" "${USGS_ARGS[@]}" \
        --elevation-maxzoom "${ELEVATION_MAXZOOM:-$(terrain_default_zoom elevation)}" \
        --hillshade-maxzoom "${HILLSHADE_MAXZOOM:-$(terrain_default_zoom hillshade)}" \
        --hillshade-format "${HILLSHADE_FORMAT:-webp}" "$@"
}

# Download one file, with retries. Returns 0 when it is saved and 1 when the server has
# no such file (it answers 404 or 403); stops the build after four failed tries.
# Arguments: URL DEST
fetch_file() {
    local url="$1" dest="$2" code tries=0
    while :; do
        code="$(curl -sSL -o "$dest.part" -w '%{http_code}' "$url" || true)"
        case "$code" in
            200)
                mv "$dest.part" "$dest"
                return 0 ;;
            403|404)
                rm -f "$dest.part"
                return 1 ;;
            *)
                rm -f "$dest.part"
                tries=$((tries + 1))
                if [ "$tries" -ge 4 ]; then
                    echo "Error: could not download ${dest##*/} (HTTP ${code:-none}). Check the connection and run the same command again; finished downloads are kept." >&2
                    exit 1
                fi
                echo "retry $tries: ${dest##*/} (HTTP ${code:-none})"
                sleep $((tries * 5)) ;;
        esac
    done
}

# Download the elevation data of the cells that touch the outline (1x1 degree GeoTIFF files).
# Copernicus GLO-30: cells without a file (open sea, mostly) are added to
# data/cache/dem/ocean-cells.txt and not requested again.
# With DEM_SOURCE=usgs, the USGS tile of each cell first, into data/cache/dem-usgs: cells
# USGS has no tile for go into no-tile.txt there, and each tile into full-tiles.txt or
# gap-tiles.txt (it has pixels without data: sea, or land USGS has not covered). The
# Copernicus cell is then fetched only where there is no USGS tile or it has gaps.
# Argument: WORK_DIR
download_dem() {
    local work="$1"
    local out_dir="$REPO_DIR/data/cache/dem" base_url="https://copernicus-dem-30m.s3.amazonaws.com"
    local usgs_url="https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"
    local ocean="$out_dir/ocean-cells.txt" no_tile="$USGS_DIR/no-tile.txt"
    local full="$USGS_DIR/full-tiles.txt" gaps="$USGS_DIR/gap-tiles.txt"
    mkdir -p "$out_dir"
    if [ ! -f "$ocean" ]; then
        printf '%s\n' "# Copernicus GLO-30 cells that have no file (open sea, mostly), found by the terrain" \
            "# builds; they are not requested again. Delete a line to try that cell again." > "$ocean"
    fi
    if [ "$DEM_SOURCE" = usgs ]; then
        mkdir -p "$USGS_DIR"
        [ -f "$no_tile" ] || printf '%s\n' "# Cells USGS 3DEP has no 1/3 arc-second tile for, found by the terrain builds; they" \
            "# are not requested again. Delete a line to try that tile again." > "$no_tile"
        [ -f "$full" ] || printf '%s\n' "# USGS tiles without gaps: their cells need no Copernicus data underneath." > "$full"
        [ -f "$gaps" ] || printf '%s\n' "# USGS tiles with pixels without data: Copernicus data is laid underneath." > "$gaps"
    fi
    local _part name tile todo=0 usgs_todo=0
    while read -r _part name <&3; do
        if [ "$DEM_SOURCE" = usgs ]; then
            tile="$(usgs_tile "$name")"
            [ -f "$USGS_DIR/$tile.tif" ] || grep -qxF "$tile" "$no_tile" || usgs_todo=$((usgs_todo + 1))
            grep -qxF "$tile" "$full" && continue
        fi
        [ -f "$out_dir/$name.tif" ] || todo=$((todo + 1))
    done 3< "$work/outline/cells.txt"
    # Up to about 30 MB a Copernicus cell and 450 MB a USGS tile; cells that turn out to
    # have no file take nothing.
    require_free_space "$REPO_DIR/data" "$(( todo * 3 / 100 + usgs_todo * 45 / 100 + 1 ))" \
        "downloading up to $((todo + usgs_todo)) elevation cells"
    local fetched=0 cached=0 none=0 hires=0 need
    while read -r _part name <&3; do
        need=yes
        if [ "$DEM_SOURCE" = usgs ]; then
            tile="$(usgs_tile "$name")"
            if [ ! -f "$USGS_DIR/$tile.tif" ] && ! grep -qxF "$tile" "$no_tile"; then
                if fetch_file "$usgs_url/${tile#USGS_13_}/$tile.tif" "$USGS_DIR/$tile.tif"; then
                    echo "download: $tile"
                else
                    echo "$tile" >> "$no_tile"
                fi
            fi
            if [ -f "$USGS_DIR/$tile.tif" ]; then
                hires=$((hires + 1))
                if ! grep -qxF "$tile" "$full" "$gaps"; then
                    if python3 "$REPO_DIR/tools/region-outline.py" tile-full "$USGS_DIR/$tile.tif"; then
                        echo "$tile" >> "$full"
                    else
                        echo "$tile" >> "$gaps"
                    fi
                fi
                if grep -qxF "$tile" "$full"; then need=no; fi
            fi
        fi
        [ "$need" = yes ] || continue
        if [ -f "$out_dir/$name.tif" ]; then
            cached=$((cached + 1))
        elif fetch_file "$base_url/$name/$name.tif" "$out_dir/$name.tif"; then
            echo "download: $name"
            fetched=$((fetched + 1))
        else
            # The bucket answers 404 (or 403) for cells it has no file for.
            echo "$name" >> "$ocean"
            echo "no file (open sea): $name"
            none=$((none + 1))
        fi
    done 3< "$work/outline/cells.txt"
    if [ "$DEM_SOURCE" = usgs ]; then
        echo "High-resolution cells (USGS 3DEP 1/3 arc-second): $hires."
    fi
    echo "Copernicus cells: $fetched downloaded, $cached already here, $none with no file (open sea)."
}

# One mosaic (a VRT, no copy) of the downloaded cells per part of the outline, listed in
# WORK_DIR/outline/mosaics.txt (see tools/region-outline.py). Run after download_dem.
# Argument: WORK_DIR
terrain_mosaic() {
    python3 "$REPO_DIR/tools/region-outline.py" mosaic --out "$1/outline" --dem-dir "$REPO_DIR/data/cache/dem" \
        "${USGS_ARGS[@]}"
}
