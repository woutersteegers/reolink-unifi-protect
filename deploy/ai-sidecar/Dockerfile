FROM node:20-slim
WORKDIR /app
COPY package.json .
RUN npm install --omit=dev
COPY index.mjs decode.mjs .
CMD ["node", "index.mjs"]
