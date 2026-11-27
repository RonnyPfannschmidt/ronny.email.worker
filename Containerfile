# Code only.  Configuration, database, password and tokens are instance state: mounts.
# Spike: pin the three bases by digest before this publishes anything.
ARG PYTHON=3.14

FROM docker.io/library/node:22-slim AS interface
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

FROM docker.io/library/python:${PYTHON}-slim AS package
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ARG VERSION
ENV UV_PROJECT_ENVIRONMENT=/opt/mailmind UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 MAILMIND_UI_PREBUILT=1 SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
WORKDIR /build
COPY . .
COPY --from=interface /build/src/mailmind/web/static/ui src/mailmind/web/static/ui
RUN uv sync --frozen --no-default-groups --no-editable

FROM docker.io/library/python:${PYTHON}-slim
COPY --from=package /opt/mailmind /opt/mailmind
USER 10001:10001
ENV PATH=/opt/mailmind/bin:$PATH
ENTRYPOINT ["mailmindctl"]
CMD ["serve"]
