FROM node:22.17.0-alpine
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.12.4 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN pnpm install --filter @ecc/frontend --frozen-lockfile
COPY --chown=node:node frontend ./frontend
USER node
CMD ["pnpm", "--filter", "@ecc/frontend", "dev"]