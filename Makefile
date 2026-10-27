.PHONY: test check doctor
test:
	bash scripts/test.sh
check:
	python3 scripts/release_check.py
doctor:
	bash scripts/deploy.sh doctor
