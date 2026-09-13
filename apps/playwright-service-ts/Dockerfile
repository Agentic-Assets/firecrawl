FROM node:22-slim

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
ENV CI=true

RUN corepack enable && corepack prepare pnpm@11.4.0 --activate

WORKDIR /usr/src/app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

ENV PLAYWRIGHT_BROWSERS_PATH=/usr/local/share/playwright

# Browser binaries and OS packages depend on the lockfile, not application
# source. Keep this layer ahead of COPY so a collector-side edit does not
# redownload Chromium during an operator recovery.
RUN pnpm exec playwright install chromium --with-deps

COPY . .

RUN pnpm run build

RUN chown -R node:node /usr/src/app \
  && chmod -R a+rX /usr/local/share/playwright

ARG PORT
ENV PORT=${PORT}

EXPOSE ${PORT}

USER node

CMD [ "pnpm", "start" ]
