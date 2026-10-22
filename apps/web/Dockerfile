FROM node:24.21.0-bookworm-slim
WORKDIR /workspace/apps/web
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web .
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
