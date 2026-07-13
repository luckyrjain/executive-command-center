FROM node:22.17.0-alpine
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.12.4 --activate
COPY package.json pnpm-workspace.yaml ./
COPY frontend/package.json ./frontend/package.json
RUN pnpm install --filter @ecc/frontend --no-frozen-lockfile
COPY frontend ./frontend
CMD ["pnpm", "--filter", "@ecc/frontend", "dev"]
