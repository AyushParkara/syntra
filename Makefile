.PHONY: lint compile check
lint:
	uvx ruff check syntra
compile:
	python3 -m compileall -q syntra
check: compile lint
