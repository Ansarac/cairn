---
name: Bug report
about: Something went wrong — a message that never arrived, a bell that stayed silent, a wrong exit code
title: ''
labels: bug
assignees: ''
---

> **Redact before you post, and read this twice for cairn specifically.** Two
> kinds of thing leak out of this tool. Hub URLs carry internal hostnames and
> ports (`http://hub-host:7777`) — keep the shape, replace the contents with
> `REDACTED`. And **message bodies, notes and agent names are your work**: they
> say what is on which rig, what failed, and what somebody decided. Paraphrase
> them, or replace them with something of the same shape. Nothing here needs the
> real text to be reproducible.

## What happened

<!-- One or two sentences. Include the exit code if the command failed:
     0 = it worked, 1 = it worked and the answer is "nothing" (empty inbox, no
     peers), 2 = the hub could not be reached, 3 = the command cannot be carried
     out as asked, 130 = interrupted.

     1 and 2 mean opposite things. If you saw one where you expected the other,
     say so — that is a bug on its own. -->

## What you expected

## Command you ran

```console
$ cairn ...
```

## `cairn config` and `cairn whoami`

<!-- REQUIRED. Between them these answer most of what we would otherwise have to
     ask: which hub this directory talks to, where the config and state live, and
     what name this directory registered. Redact the hub host. -->

```console
$ cairn config
$ cairn whoami
```

## Which side is this?

<!-- Delivery bugs have two ends and they fail differently. Tell us which end you
     are describing, and whether you can see the other one at all. -->

- Sender / receiver / both:
- Same machine, or across machines:
- `cairn peers` shows the other agent: <!-- yes / no / not checked -->

## If a bell or a nudge did not fire

<!-- The bell is a hook in another program, so its failures are silent by design
     — every failure path prints `{}` and exits 0. These three tell us where it
     stopped. -->

- Hook output at the turn boundary, if your host records it:
- `cairn bell` run by hand in the same directory:
- `cairn nudge` running on that machine: <!-- yes / no -->

## Environment

- cairn version: <!-- `cairn --version` -->
- OS:
- Python: <!-- `python3 --version` -->
- Installed via: <!-- uv tool install / from a checkout / other -->
- Agent product and version on each end:
- Hub: <!-- run how? `just hub`, a container, something else -->
- Hub and client are the same version: <!-- yes / no / unsure -->

## Anything else
