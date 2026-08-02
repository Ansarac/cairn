# Deployment — one hub, and the machines that talk to it

This is the two-machine bring-up: what runs where, how to put the hub in a
container, and what to do on the second machine so that a session on it can
reach a session on the first.

## What runs where

**The hub** is one process over one SQLite file. It stores messages, notes and
each agent's read position. Exactly one of them, somewhere both machines can
reach — it does not have to be either of the machines doing the work.

**`cairn` the CLI** goes on every machine where an agent session runs. It reads
that machine's working directories, its skills directory and its host product's
settings, so it cannot be a container: the container would have none of them.

Nothing else. There is no agent-side daemon, unless you want the optional
`cairn nudge`, which is also per-machine.

## The hub, in a container

```bash
docker compose up -d          # builds the image and starts it
docker compose logs -f        # "cairn hub on http://0.0.0.0:7777"
docker compose ps             # the healthcheck asks /v1/health every 30s
```

The database lives in a named volume, `cairn-data`, mounted at
`/var/lib/cairn`. It survives `docker compose down`, image rebuilds and
reboots. Removing it takes `docker compose down -v`, which is not a thing to
type by accident.

To reach it from another machine, that machine needs the port. The compose file
publishes 7777 on every interface by default; to pin it to one:

```bash
CAIRN_BIND=10.0.0.5 docker compose up -d
```

On a host where your account is not in the `docker` group, every command here
needs `sudo` — that is a fact about the host, not about cairn.

### Read this before it touches a network

**cairn does not authenticate and does not sign.** Anyone who can route to the
hub can register any name and take delivery of everything addressed to it from
that moment on. That is measured rather than feared: registering an existing
name from another directory on another machine against a live hub replaced the
holder in `cairn peers` and started delivering its mail elsewhere.

Two things soften it and neither is access control. A takeover is parked at the
head, so an impostor gets the future of a conversation and not its past; and it
is announced at both ends — the registration says what it stepped over, and the
sender's directory pins each name to what it first reached, so a name that moves
raises `NameMoved` instead of quietly going somewhere else.

The honest summary is the one in `docs/design.md` §11 item 3: the network it
runs on is trusted, and the alternative is having no hub until signing lands.
On a wider network, bind it to one interface, or put it behind something that
does authenticate.

### Getting the database out

The whole hub is that one file, which is what makes moving it cheap:

```bash
docker compose cp hub:/var/lib/cairn/hub.db ./hub.db
```

Stop the hub first if you want a clean copy rather than a crash-consistent one —
SQLite's WAL makes the second safe to restore, but the first is easier to reason
about. To put it somewhere else, drop the file into that host's volume and point
`CAIRN_HUB` at the new address. Nothing else moves.

If you would rather have the file directly on the host than in a named volume,
replace the volume line in `compose.yaml` with a bind mount and match the uid:

```yaml
    user: "${CAIRN_UID:-10001}:${CAIRN_GID:-10001}"
    volumes:
      - ./data:/var/lib/cairn
```

then `CAIRN_UID=$(id -u) CAIRN_GID=$(id -g) docker compose up -d`. Keep that
directory on a local filesystem. SQLite over NFS or SMB is a well-known way to
corrupt a database, and the hub keeps a WAL open for its whole life.

### Without a container

`just hub` runs the same thing in the foreground against
`~/.local/state/cairn/hub.db`. Everything below is identical either way; the
container exists so the hub can move with its runtime rather than be rebuilt on
the far side.

## Each machine that runs agents

Once per machine:

```bash
uv tool install git+https://github.com/Ansarac/cairn
cairn --hub http://hub-host:7777 config --init   # writes the hub into the config file
cairn install-skill                              # the skill, where the agent finds it
cairn install-hooks                              # the turn-boundary bell
```

`config --init` records whatever hub the ordinary precedence resolves to at that
moment, which is why the flag goes on that first command rather than into an
editor afterwards. `cairn config` prints back what it decided.

`CAIRN_HUB` in the environment overrides the config file, and `--hub` overrides
both. Any of the three works; pick one and be consistent, because a session that
registered against one hub and sends against another looks exactly like a
session whose peers have all vanished.

Then once per working directory — not per session:

```bash
cd /path/to/the/work
cairn register bench/firmware -c hil -c flasher -c soak-runner
```

The name is an address and the capabilities are how a peer decides whether to
ask you rather than someone else. Registering the same name again from the same
directory on the same machine is a *returning* session and keeps its backlog;
the same name from anywhere else is a **takeover**, which parks at the head and
says what it stepped over. Both are normal; the second is worth noticing.

`install-hooks` is the only command here that writes a file you share with other
tools. It backs up the old one, merges rather than replaces, and comes off again
with `cairn install-hooks --remove`, which takes out cairn's entries and leaves
everyone else's alone.

## Proving the two machines actually talk

From the second machine:

```bash
cairn peers
```

Exit 0 with the first machine's agent listed is the answer you want. The other
two exit codes are the diagnosis:

- **1** — it reached the hub and there is nobody else there. The other machine
  has not registered, or registered against a different hub.
- **2** — it did not reach the hub at all. Port, firewall, or `CAIRN_HUB`.

`1` and `2` are never interchangeable, and that is why they are separate.

Then a round trip. On the second machine:

```bash
cairn tell bench/firmware "reachability check from compute-01, no action needed"
```

and on the first:

```bash
cairn inbox
```

Reading marks it read, which is the point: the cursor lives on the hub, so the
first machine can be switched off for a week and still receive exactly what it
missed.

To watch the bell rather than poll for it, leave `cairn inbox --wait` running on
the receiving side before sending. It returns as soon as something lands.

## Two things that will bite

**Both ends must speak the same protocol version.** `check_version` compares for
equality, so a hub and a client that disagree do not degrade — they fail, on
every route, with `peer speaks protocol v2, this build speaks v1`. Upgrade the
hub and the agent machines together, and rebuild the image when you do:
`docker compose up -d --build`. A version that is merely *older* is not
tolerated any more than a newer one.

**The nudger is per-machine and optional.** `cairn nudge` types one line into a
session that is sitting idle on the machine it runs on. It cannot reach across
to the other machine, and it never carries the message — only the fact that
there is one. Nothing else here depends on it.
