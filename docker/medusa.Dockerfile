FROM node:24.22.1-bookworm-slim

WORKDIR /app/commerce
RUN mkdir -p apps/backend && chown node:node . apps apps/backend
COPY --chown=node:node commerce/package.json commerce/package-lock.json commerce/.npmrc ./
COPY --chown=node:node commerce/apps/backend/package.json apps/backend/package.json
USER node
RUN npm ci

COPY --chown=node:node commerce/ ./
WORKDIR /app/commerce/apps/backend
ENV MEDUSA_TELEMETRY_DISABLED=1
RUN STORE_CORS=http://localhost:8000 \
    ADMIN_CORS=http://localhost:9000 \
    AUTH_CORS=http://localhost:9000 \
    npm run build

COPY docker/start-medusa.sh /usr/local/bin/start-medusa.sh
ENV NODE_ENV=production
CMD ["sh", "/usr/local/bin/start-medusa.sh"]
