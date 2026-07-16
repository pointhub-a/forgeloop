.PHONY: test dev

test:
	python3 -m pytest -q

dev:
	python3 -m forgeloop.cli serve
