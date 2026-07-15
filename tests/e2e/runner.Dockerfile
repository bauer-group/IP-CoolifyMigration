# The e2e tests run from INSIDE the rig's network, not from Windows.
#
# Not a workaround — the honest setup. Docker Desktop gives Windows no route to
# container IPs, and Coolify hands out those IPs as the servers' addresses. A
# test that reached the servers by some other address would be exercising a
# path the tool never takes. In here, `172.23.0.x:22` means the same thing to
# the test as it does to Coolify, which is the whole point.
#
# rsync/openssh are absent on purpose beyond what asyncssh needs: the tool must
# not require them locally. If a test starts failing for want of a local rsync,
# that is the tool reaching for the wrong machine and the test is right to fail.

# The development interpreter. The 3.12 floor is a matter for the CI matrix,
# which runs the unit suite on all three; this rig exists to meet a real Coolify,
# and doing that on an interpreter nobody develops on would test the wrong thing.
FROM python:3.14-slim

WORKDIR /app

# One static binary out of the official image — no apt, no daemon. The slug
# contract test shells into the Coolify container to ask the real Laravel what
# Str::slug returns; comparing our slugify against our own idea of Laravel is
# exactly the mistake that let the eszett bug through.
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

# Deps only. The source is bind-mounted and read via PYTHONPATH, so an edit on
# Windows is live in here with no reinstall and no egg-info dropped in the tree.
#
# TRANSCRIBED FROM pyproject.toml, and test_runner_deps_match_pyproject keeps it
# that way. The first cut typed the ranges from memory and drifted on day one —
# `rich<15` here against `<16` there — which would have let the rig prove the
# tool works against a version the tool does not allow. Edit pyproject, then run
# that test; do not adjust these by hand.
RUN pip install --no-cache-dir \
      "typer>=0.15.0,<1.0.0" \
      "rich>=13.7.0,<16.0.0" \
      "httpx>=0.28.1,<1.0.0" \
      "pydantic>=2.10.0,<3.0.0" \
      "pydantic-settings>=2.14.1,<3.0.0" \
      "structlog>=24.4.0,<26.0.0" \
      "asyncssh>=2.18.0,<3.0.0" \
      "dnspython>=2.7.0,<3.0.0" \
      "pyyaml>=6.0.0,<7.0.0" \
      "platformdirs>=4.3.0,<5.0.0" \
      "questionary>=2.0.0,<3.0.0" \
      "pytest>=8.3.0,<10.0.0" \
      "pytest-asyncio>=0.24.0,<2.0.0"

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest", "-m", "e2e", "-v"]
