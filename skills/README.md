# skills

Skills for Claude apps that support them (the claude.ai web app, Claude Code, ...). A skill is a folder with a `SKILL.md`; Claude sees only its name and description until a task matches, then loads the rest.

## router-sandbox

How to work through the router and its sandbox (start of session, the tools, limits, working in a mounted project, committing and pushing), and how to hand unfinished work from one Claude instance to the next. It replaces the old `USAGE.md`, which you had to attach to every first prompt.

| File | Loaded |
|---|---|
| `router-sandbox/SKILL.md` | whenever the skill triggers |
| `router-sandbox/references/handoff.md` | only when a task is multi-step or picks up someone else's work |

### Install

The folder itself has to be at the top of the zip:

```
cd skills
python3 -m zipfile -c router-sandbox.zip router-sandbox      # or: zip -r router-sandbox.zip router-sandbox
```

Upload `router-sandbox.zip` in the web app's skills settings. For Claude Code, copy the `router-sandbox` folder into `~/.claude/skills/` instead. To update, edit the files here, zip again and upload again. The zip is not committed (`skills/*.zip` is ignored).

### What it expects on your side

* The router and the sandbox server from this repo, with the project folders mapped as `rw` mounts.
* Optional: `[collab] log = true` in the sandbox server's config (see `sandbox/README.md`). The skill works without it; the per-project `calls.log` is then simply absent.
