# Un solo profilo, sempre — hot reload attivo (no build dev/prod separate).
# frontend/ è montato a runtime da docker-compose.yml (bind-mount): questa
# immagine installa solo le dipendenze e gira il dev server Vite.
FROM node:20-alpine AS runtime

ENV CHOKIDAR_USEPOLLING=true \
    BACKEND_HOST=bot

WORKDIR /app

# `@suite/design-system` is an unpublished workspace package (dep "*" in package.json,
# normally a local symlink). It cannot be resolved from the registry and lives outside
# this build context (Life/packages). So it is vendored as a tarball (see
# scripts/deploy/docker-prepare.sh -> `npm pack`) into ./vendor and installed via file:.
# Selective COPY (not `COPY . .`) so the rewritten package.json is not clobbered.
COPY package.json ./
COPY vendor/ ./vendor/
RUN npm pkg set dependencies.@suite/design-system="file:./vendor/suite-design-system.tgz" \
 && npm install --no-audit --no-fund --legacy-peer-deps

# Baseline app source (first `docker compose up --build`, before the
# bind-mount takes over).
COPY index.html vite.config.js ./
COPY public ./public
COPY src ./src

EXPOSE 3335

CMD ["sh", "-c", "npm run dev -- --host 0.0.0.0 --port ${FRONTEND_PORT:-3335}"]
