# Operator console: build with Node 22, serve static files with unprivileged nginx.
# Base images are pinned by digest. The image holds no API address; set BRAVO_API_BASE at run time.

FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS build
WORKDIR /app
RUN npm install --global --no-fund --no-audit pnpm@12.6.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN pnpm build

# nginx 1.30.6 stable on Alpine 3.24 (multi-arch index digest, 2026-10-19). 1.27-alpine was Alpine 3.21 (EOL) with
# CRITICAL fixed CVEs in brotli-libs and OpenSSL that the image scan gate blocks.
FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
# That base still ships brotli-libs 1.2.0-r1 (CVE-2026-111497, CRITICAL, fixed in Alpine 1.2.1-r0; pulled in by
# libcurl and freetype). Take the fixed package; the constraint fails the build if the repo lacks it.
# Drop this line when the base digest ships brotli-libs >= 1.2.1-r0.
USER root
RUN apk add --no-cache 'brotli-libs>=1.2.1-r0'
COPY --chmod=0644 docker/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=0644 docker/bravo-headers.conf /etc/nginx/bravo-headers.conf
COPY --chmod=0755 docker/40-bravo-config.sh /docker-entrypoint.d/40-bravo-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/config.json || exit 1
