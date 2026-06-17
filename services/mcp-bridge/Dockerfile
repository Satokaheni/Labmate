FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY dist/ ./dist/

ENV LOG_LEVEL=info

CMD ["node", "dist/index.js"]
