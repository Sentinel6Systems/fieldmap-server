# Tool image for the terrain build scripts.
# It contains GDAL, Tippecanoe, and Osmium.
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.9.2

# The base image lists an Apache Arrow apt source whose signing key no
# longer verifies, which makes apt-get update fail. The Arrow libraries are
# already installed in the image and nothing here needs that source, so
# remove it first.
RUN grep -rl "apache.jfrog.io" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null \
        | xargs -r rm -f \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libsqlite3-dev \
        zlib1g-dev \
        curl \
        ca-certificates \
        osmium-tool \
    && git clone --depth 1 --branch 2.62.0 https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
    && make -C /tmp/tippecanoe -j"$(nproc)" \
    && make -C /tmp/tippecanoe install \
    && rm -rf /tmp/tippecanoe \
    && apt-get purge -y build-essential git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
