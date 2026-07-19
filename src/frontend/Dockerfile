# Multi-stage build for React frontend

# Stage 1: Build
FROM node:20-alpine as builder

WORKDIR /build

# Accept build arguments
ARG VITE_API_BASE_URL=
ARG SSO_LOGIN_URL=
ARG VITE_EDITION=ee
ARG VITE_DEFAULT_LANGUAGE=zh-CN
ARG NODE_HEAP_MB=6144
# Upload size hint baked into the JS bundle; keep in sync with the runtime
# UPLOAD_MAX_MB below (compose feeds both from the same UPLOAD_MAX_MB env var)
ARG VITE_UPLOAD_MAX_MB=50
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV SSO_LOGIN_URL=$SSO_LOGIN_URL
ENV VITE_EDITION=$VITE_EDITION
ENV VITE_DEFAULT_LANGUAGE=$VITE_DEFAULT_LANGUAGE
ENV VITE_UPLOAD_MAX_MB=$VITE_UPLOAD_MAX_MB
ENV NODE_OPTIONS=--max-old-space-size=${NODE_HEAP_MB}

# Copy package files
COPY package*.json ./

# Install dependencies (including dev dependencies for build)
RUN npm install

# Copy source code
COPY . .

# Build production bundle
RUN npm run build

# Stage 2: Serve with Nginx
# Pinned to 1.27-alpine on purpose: unpinned `nginx:alpine` drifts to mainline
# (e.g. 1.31.x), whose newer write syscall is blocked by older hosts' Docker
# seccomp profile → nginx dies on startup with pwrite() nginx.pid "Operation not
# permitted". Stay on the stable 1.27 line to keep rebuilds portable.
FROM nginx:1.27-alpine

# Runtime port for nginx → backend proxy (overridden via docker-compose environment)
ENV BACKEND_PORT=3001
# nginx client_max_body_size for /api/ uploads, in MB (overridden via docker-compose)
ENV UPLOAD_MAX_MB=50

# Copy base nginx config (includes conf.d/)
COPY nginx.conf /etc/nginx/nginx.conf

# Copy server block template (rendered at startup via entrypoint)
COPY default.conf.template /etc/nginx/templates/default.conf.template

# Copy built assets from builder
COPY --from=builder /build/dist /usr/share/nginx/html

# Health check
HEALTHCHECK --interval=30s --timeout=3s \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:80/ || exit 1

# Expose port
EXPOSE 80

# Render template with envsubst (only the listed vars are substituted; nginx $vars are left intact)
CMD ["/bin/sh", "-c", "envsubst '$BACKEND_PORT $UPLOAD_MAX_MB' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
