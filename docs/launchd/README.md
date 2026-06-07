# Launchd KeepAlive Template

`com.kushal.claude-swarm-perpetual.plist.template` is a source template only.
It is not an installed LaunchAgent.

Fill the placeholders before any approved install:

- `__CLAUDE_SWARM_BIN__`: absolute path to the `claude-swarm` executable.
- `__CLAUDE_SWARM_HOME__`: approved swarm state directory.
- `__AGENT_SWARM_REPO__`: absolute path to the repo checkout to run from.
- `__LOG_DIR__`: approved writable log directory.

Do not put provider secrets in the plist. The API conductor now fails fast unless
`ANTHROPIC_API_KEY` is present in the process environment, but the key must come
from an operator-approved secret path outside the committed template, such as an
approved per-user environment setup or a Keychain-backed wrapper.

Example live-enablement shape, held until explicit Operator Review approval:

```sh
launchctl setenv ANTHROPIC_API_KEY '<approved-secret-from-Keychain-or-operator>'
```

Source-only checks that are safe before installation:

```sh
plutil -lint docs/launchd/com.kushal.claude-swarm-perpetual.plist.template
python -m pytest tests/test_launchd_template.py -q
```

Installing, loading, starting, stopping, or inspecting live launchd state remains
a runtime/process action and needs explicit approval with target path, overwrite
policy, secret scope, evidence target, stop plan, and rollback plan.
