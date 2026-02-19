FROM node:22-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY tsconfig.json ./tsconfig.json
COPY src ./src
COPY migrations ./migrations

RUN npm run build
RUN npm prune --omit=dev

FROM node:22-alpine

WORKDIR /app
ENV NODE_ENV=production

RUN addgroup -S relay && adduser -S relay -G relay

COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY --from=build /app/migrations ./migrations
COPY package*.json ./

USER relay

EXPOSE 8080

CMD ["node", "dist/server.js"]
