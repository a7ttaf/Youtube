# UMS Smart Revenue — local-first quality gate via Elite-CI.
#
# Run `make verify` before pushing. The gate refuses to ship code that does
# not lint, type-check, test, or pass the security/secrets scans.
#
# When uv is available (the project default), preflight runs inside the
# project's managed venv so ruff / mypy / pytest / sqlfluff / alembic
# resolve without needing a manual `source .venv/bin/activate`.

.PHONY: verify ci-quick ci-full ci-ship ci-debt ci-all ci-fix ci-profile \
        impact test-plan smart ship install-hooks ci-self-test docs bats \
        bats-install \
        lint-ci

# Windows gnuwin32 make sometimes launches with HOME=/, which breaks uv's
# $HOME/.cache and $HOME/.local/share lookups. We sidestep that by pointing
# uv at worktree-local cache and tool dirs whenever HOME looks broken. These
# directories are listed in ci/.gitignore so they don't pollute the repo.
ifeq ($(HOME),/)
export UV_CACHE_DIR := $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR := $(CURDIR)/.uv-python
export UV_TOOL_DIR := $(CURDIR)/.uv-tool
endif

# Prefer `uv run bash` so the venv's Scripts/bin directory is on PATH.
# Falls back to bare bash if uv is not installed.
# Detect uv presence at make-time but invoke by bare name so the runtime
# shell's PATH (not make's $(shell ...) env) resolves the binary; this
# avoids HOME-expansion bugs when make's subshell has a sparse env.
# --extra dev installs the test/lint tools (pytest, ruff, mypy) into the venv so
# the python lane's pytest/ruff steps resolve; a bare `uv run` syncs only the
# runtime deps and the gate fails infra with "pytest binary is missing".
HAS_UV := $(shell command -v uv >/dev/null 2>&1 && echo yes || echo no)
ifeq ($(HAS_UV),yes)
PREFLIGHT := bash ci/scripts/with-home.sh uv run --extra dev bash ci/preflight.sh
else
PREFLIGHT := bash ci/scripts/with-home.sh bash ci/preflight.sh
endif

verify: ci-full

ci-quick:
	$(PREFLIGHT) --mode quick

ci-full:
	$(PREFLIGHT) --mode full

ci-ship:
	$(PREFLIGHT) --mode ship

ci-debt:
	$(PREFLIGHT) --mode debt

ci-all:
	$(PREFLIGHT) --all

ci-fix:
	$(PREFLIGHT) --fix

ci-profile:
	$(PREFLIGHT) --profile

impact:
	bash ci/impact.sh

test-plan:
	bash ci/test-plan.sh

smart:
	bash ci/test-plan.sh
	$(PREFLIGHT) --mode full

ship:
ifndef MESSAGE
	@echo "Usage: make ship MESSAGE=\"your commit message\""
	@exit 1
endif
	bash ci/ship.sh "$(MESSAGE)"

install-hooks:
	bash ci/install-hooks.sh

ci-self-test:
	bash ci/self-test.sh

docs:
	@mkdir -p docs
	@bash ci/scripts/gen-checks-doc.sh > docs/CHECKS.md 2>/dev/null || \
		printf "# Checks\n\nSee ci/checks/manifest.yml\n" > docs/CHECKS.md
	@echo "Generated docs/CHECKS.md"

bats:
	bats ci/tests/

# The tests-shell lane is a blocker and refuses when bats is missing, so a
# fresh clone needs a way to get it that does not depend on the machine.
# Pinned, worktree-local, no sudo; `rm -rf .ci-gate/bats` undoes it.
bats-install:
	bash ci/scripts/install-bats.sh

lint-ci:
	@for f in ci/*.sh ci/checks/*.sh ci/lib/*.sh ci/hook-dispatch.sh; do \
		[ -f "$$f" ] && bash -n "$$f" && echo "OK: $$f"; \
	done
