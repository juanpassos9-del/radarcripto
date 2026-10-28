FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
FROM node:22-alpine AS builder
WORKDIR /app
ARG NEXUS_API_ORIGIN
ARG NEXT_PUBLIC_MARKET_API_URL
ENV NEXUS_API_ORIGIN=${NEXUS_API_ORIGIN}
ENV NEXT_PUBLIC_MARKET_API_URL=${NEXT_PUBLIC_MARKET_API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
RUN mkdir -p ./public
EXPOSE 3000
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=${PORT:-10000} node server.js"]
