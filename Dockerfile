# The hub, and only the hub.
#
# `cairn` the CLI belongs on the machines where the agents are — it reads their
# working directories, their skills directory and their host product's settings,
# none of which exist in here. What this image carries is the one long-lived
# process, so that moving the hub is moving an image and a database file rather
# than rebuilding a runtime on the far side. See docs/design.md §11 item 3.
#
# Two stages only because the wheel needs a build backend and the runtime does
# not. cairn itself has no dependencies, so nothing is fetched at install time
# beyond what PEP 517 pulls in to build it.

FROM python:3.13-slim AS build

WORKDIR /src
# Everything the wheel is made of: the metadata, the package, and the skill that
# `[tool.hatch.build.targets.wheel.force-include]` puts inside it. README and
# LICENSE are here because pyproject names them, not for decoration.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY skills ./skills
RUN pip wheel --no-deps --no-cache-dir --wheel-dir /dist .


FROM python:3.13-slim

# A home directory that is also the data directory: a named volume mounted here
# inherits this ownership on first use, which is what keeps the common case free
# of uid juggling. See docs/deployment.md for the bind-mount alternative.
RUN useradd --system --create-home --home-dir /var/lib/cairn --uid 10001 cairn

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/*.whl && rm -f /tmp/*.whl

# The hub flushes its own banner, but anything else it ever prints would sit in
# a block buffer behind `docker logs` until the process exited.
ENV PYTHONUNBUFFERED=1

USER cairn
WORKDIR /var/lib/cairn
EXPOSE 7777

# 0.0.0.0 inside the container is not a network decision — the container's own
# interface is the only thing it binds. Which host interface it is reachable on
# is decided by the port mapping in compose.yaml, and that one is a network
# decision. cairn does not authenticate: read docs/deployment.md first.
ENTRYPOINT ["cairn", "hub"]
CMD ["--host", "0.0.0.0", "--port", "7777", "--db", "/var/lib/cairn/hub.db"]
