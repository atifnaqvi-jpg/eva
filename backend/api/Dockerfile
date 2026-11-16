# Widget bundle build (WIDG-01/02). A throwaway stage: node and the 60 MB of
# node_modules never reach the runtime image, only dist/ does.
FROM node:24-alpine AS widget

WORKDIR /widget
COPY widget/package.json widget/package-lock.json ./
RUN npm ci --omit=optional
COPY widget/build.mjs widget/tsconfig.json ./
COPY widget/src ./src
COPY widget/demo ./demo
RUN node build.mjs


FROM python:3.12.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# `age` encrypts tenant BYOK keys (SEC-03, ARCHITECTURE §16.2). A binary rather
# than a Python crypto dependency: STACK.md locks age/sops for the vault, and the
# vault is a shell-out surface of exactly two commands (`age` and `age-keygen`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends age \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Built loader + iframe app, mounted at /widget by app/main.py.
COPY --from=widget /widget/dist /srv/widget

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
