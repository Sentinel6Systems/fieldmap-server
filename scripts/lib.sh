#!/usr/bin/env bash
# Shared functions for the terrain build scripts.
# Do not run this file directly.

TOOLS_IMAGE="tileserver-tools"

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
