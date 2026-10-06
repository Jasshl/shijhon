# Shijhon: the proxy in front of an unmodified Navidrome. Build with
#   docker build -t shijhon .            (docker buildx build --platform linux/amd64 … elsewhere)
# Base images are pinned by digest (multi-platform indexes, checked 2026-09-28).
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv

# Catalog adapters are separate packages (docs/deployment.md, "Catalog adapters"). Two
# ways to add them to the image, alone or together:
#   docker build --build-arg ADAPTERS="<requirement> ..." -t shijhon .
#       requirement specifiers, separated by spaces: the URL of a wheel or of a source
#       archive, or a package's name on the package index;
#   docker build --build-context adapters=/path/to/wheels -t shijhon .
#       every wheel in a folder.
# Without the second this empty stage stands in. With neither, nothing is added.
FROM scratch AS adapters

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached while only the source changes), exactly as locked.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# The adapters, if any, into the same environment: resolved together, with everything
# already installed - Shijhon and its locked dependencies - held where it is (an adapter
# that needs another version of any of it fails the build), then checked. A source archive
# is built here by its own build backend, which is fetched for that (standard metadata
# only: no [tool.uv.sources]). A specifier may be written "name @ URL", with or without the
# spaces. The build fails when something cannot be installed, and when what was installed
# adds no catalog adapter at all, or one that cannot be loaded. `shijhon catalogs` in
# the image lists what it has.
ARG ADAPTERS=""
RUN --mount=type=bind,from=adapters,target=/adapters \
    set -eu; \
    set --; \
    for wheel in /adapters/*.whl; do [ ! -e "$wheel" ] || set -- "$@" "$wheel"; done; \
    set -f; \
    for spec in $(printf '%s' "$ADAPTERS" | sed -E 's/[[:space:]]*@[[:space:]]*/@/g'); do \
        case "$spec" in \
            [A-Za-z0-9]*) set -- "$@" "$spec" ;; \
            *) echo "ADAPTERS: '$spec' is not a requirement specifier (the URL of a wheel or of a source archive, or a package's name)" >&2; exit 1 ;; \
        esac; \
    done; \
    [ "$#" -gt 0 ] || exit 0; \
    before="$(/app/.venv/bin/shijhon catalogs)"; \
    uv pip freeze --python /app/.venv/bin/python > /tmp/locked.txt; \
    uv pip install --python /app/.venv/bin/python --no-sources --constraint /tmp/locked.txt "$@" \
        || { echo "Catalog adapters: could not install $*" >&2; exit 1; }; \
    uv pip check --python /app/.venv/bin/python; \
    after="$(/app/.venv/bin/shijhon catalogs)"; \
    echo "Catalog adapters in the image:"; echo "$after"; \
    case "$after" in \
        "$before") echo "Catalog adapters: nothing that was installed is a catalog adapter ($*)" >&2; exit 1 ;; \
        *"could not be loaded"*) echo "Catalog adapters: an installed adapter cannot be loaded" >&2; exit 1 ;; \
    esac

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
# ffmpeg (Debian's) joins add-ons' DASH links into files, copying the audio as it is; the
# silent placeholders are written in-process, and nothing is transcoded.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 shijhon \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent shijhon \
    && install -d -o 10001 -g 10001 -m 0700 /data
COPY --from=build /app/.venv /app/.venv
# The license and the third-party notices are in the image with the package
# (/app/.venv/lib/python3.12/site-packages/shijhon-*.dist-info/licenses/).
LABEL org.opencontainers.image.title="Shijhon" \
      org.opencontainers.image.source="https://github.com/Jasshl/shijhon" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["shijhon"]
CMD ["--config", "/config/shijhon.toml", "serve"]
