ARG TARGETARCH

FROM node:22-alpine AS web-build

WORKDIR /app/web-vue

COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web-vue ./
RUN npm run build


FROM node:22-alpine AS protocol-node-deps

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force


FROM python:3.13-slim AS app

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai \
    CHATGPT2API_THREAD_TOKENS=80

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
# - nodejs: 无浏览器执行 Castle SDK；npm 仅在 protocol-node-deps 阶段使用
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    nodejs \
    openssl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY package.json package-lock.json ./
COPY --from=protocol-node-deps /app/node_modules ./node_modules

COPY main.py ./
COPY config.example.yaml ./
COPY config.defaults.toml ./
COPY VERSION ./
COPY CHANGELOG.md ./
COPY GROK2API_LICENSE ./
COPY app ./app
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /app/web-vue/dist ./web_dist

EXPOSE 80

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]
