# Builds the map (print/) and serves it with nginx, which also fronts TileServer GL. Serves the
# User Guide too, with the look and the header every page shares.
FROM node:22-alpine AS build
WORKDIR /src
COPY print/package.json print/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY print/ ./
COPY ui/ /ui/
RUN node build.mjs

FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html/map
COPY guide/ /usr/share/nginx/html/guide/
COPY ui/fieldmap.css ui/nav.js /usr/share/nginx/html/guide/
