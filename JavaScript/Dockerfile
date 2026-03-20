FROM node:20-alpine

WORKDIR /app

COPY package.json .
RUN npm install

COPY client.js server.js ./

CMD ["node", "server.js"]
