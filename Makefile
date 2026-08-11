.PHONY: build test lint clean

# Build sdist + wheel into dist/ and twine-check them.
# Does NOT upload — publishing is a separate, deliberate step.
build:
	./scripts/build.sh

test:
	python3 -m pytest -q

lint:
	python3 -m ruff check .

clean:
	rm -rf dist build *.egg-info
