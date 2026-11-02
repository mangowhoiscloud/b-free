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

FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0
COPY --chmod=0644 docker/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=0644 docker/bravo-headers.conf /etc/nginx/bravo-headers.conf
COPY --chmod=0755 docker/40-bravo-config.sh /docker-entrypoint.d/40-bravo-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/config.json || exit 1
