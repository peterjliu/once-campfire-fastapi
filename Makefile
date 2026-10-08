# Development entry points. Run `make check` before committing.
REVISION ?= $(shell git rev-parse HEAD)
IMAGE ?= once-campfire-fastapi:app
UV ?= uv

.PHONY: install assets serve check lint format typecheck test image kloc clean

install:
	$(UV) sync --locked

assets:
	$(UV) run python tools/build_assets.py

serve: assets
	$(UV) run python -m campfire.server

lint:
	$(UV) run ruff check campfire tests tools
	$(UV) run ruff format --check campfire tests tools

format:
	$(UV) run ruff check --fix campfire tests tools
	$(UV) run ruff format campfire tests tools

typecheck:
	$(UV) run pyright

test: assets
	mkdir -p tmp
	SECRET_KEY_BASE=tests-only $(UV) run pytest $(PYTEST_ARGS)

check: lint typecheck test

image:
	docker build --build-arg REVISION="$(REVISION)" -t $(IMAGE) .

# Backend KLOC as docs/performance-review.md counts it: cloc 2.10, Python, campfire/ only.
kloc:
	cloc --by-file --skip-uniqueness --include-lang=Python campfire

clean:
	rm -rf tmp assets/generated .pytest_cache .ruff_cache
