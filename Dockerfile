FROM python:3.14.7-slim-trixie
ARG REVISION=local
LABEL org.opencontainers.image.revision=$REVISION
# libvips ships inside the pinned pyvips-binary wheel; ffmpeg and pdftoppm render previews.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg poppler-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.7
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy PATH=/opt/venv/bin:$PATH
WORKDIR /rails
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY . .
RUN python tools/build_assets.py \
 && useradd --uid 1000 --create-home campfire \
 && mkdir -p storage \
 && chown -R campfire:campfire storage \
 && chmod +x bin/server hooks/*
ENV HTTP_PORT=80 CAMPFIRE_STORAGE_PATH=/rails/storage PYTHONUNBUFFERED=1
USER campfire
EXPOSE 80
ENTRYPOINT ["/rails/bin/server"]
