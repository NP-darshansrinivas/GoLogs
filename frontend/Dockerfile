# GoLogs frontend container image.
#
# Per PRD §16, docker-compose serves the frontend via `npm run dev` (Vite's
# dev server) rather than a production nginx build — the compose stack is
# explicitly for demo portability ("so a reviewer can `docker compose up`
# and see the UI"), not a production deployment target. A real production
# build (`npm run build` -> static files behind a proper web server) is a
# separate concern, out of scope for the demo compose stack.

FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
