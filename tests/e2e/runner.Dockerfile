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
FROM python:3.12-slim

WORKDIR /app

# One static binary out of the official image — no apt, no daemon, ~50MB. The
# slug-contract test shells into the Coolify container to ask the real Laravel
# what Str::slug returns; comparing our slugify against our own idea of Laravel
# is exactly the mistake that let the eszett bug through.
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

# Deps only. The source is bind-mounted and read via PYTHONPATH, so an edit on
# Windows is live in here with no reinstall and no egg-info dropped in the tree.
RUN pip install --no-cache-dir \
      "typer>=0.15,<1" "rich>=13.9,<15" "httpx>=0.28,<1" \
      "pydantic>=2.10,<3" "pydantic-settings>=2.7,<3" "structlog>=24.4,<26" \
      "asyncssh>=2.19,<3" "dnspython>=2.7,<3" "platformdirs>=4.3,<5" \
      "questionary>=2.0,<3" "pyyaml>=6.0,<7" \
      "pytest>=8.3,<9" "pytest-asyncio>=0.25,<2"

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest", "-m", "e2e", "-v"]
