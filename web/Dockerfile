# Builds the print tool and serves it with nginx, which also fronts TileServer GL.
FROM node:22-alpine AS build
WORKDIR /src
COPY print/package.json print/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY print/ ./
RUN node build.mjs

FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html/mapprinter
