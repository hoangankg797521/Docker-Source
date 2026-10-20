FROM node:22-alpine AS editor
WORKDIR /app
COPY editor/server.js editor/package.json ./
COPY editor/public ./public
COPY editor/vendor ./vendor
COPY setup.js init.js start.js ./
COPY defaults /defaults
ENV CONFIG_DIR=/config STATE_DIR=/state PORT=3000
USER 1000:1000
EXPOSE 3000
CMD ["node", "start.js"]

FROM ghcr.io/gethomepage/homepage:v1.13.2 AS homepage
