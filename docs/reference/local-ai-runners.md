# Local AI runners

Shijhon can run Codex CLI or Claude Code on the server as its [server AI](../playlists.md#server-ai).
They need more setup than an [agent connected over MCP](../agent-access.md) or an API
connection.

## Setting one up

1. Install the runner where Shijhon runs, so that Shijhon can start it. With Docker, that
   means inside Shijhon's container: the standard image doesn't include either runner, and
   one installed on the host isn't visible in the container. On NixOS, the module puts it
   in Shijhon's sandbox ([NixOS reference](nixos.md#local-ai-runners)).
2. Sign it in as Shijhon's user, with the runner's own sign-in. Signing in on another
   computer doesn't connect it to Shijhon.
3. Name the command in the configuration file. Only this file sets which program runs,
   never the dashboard:

   ```toml
   [playlists]
   allow_cli = true
   codex_command = "codex"
   claude_command = "claude"
   ```

   Use a command on the service's `PATH` or a full path, without arguments.
4. Choose the runner as the **Connection** under **Server AI** on **Playlists**, as an
   administrator.

## How runs are limited

- **Codex CLI** runs in a scratch folder, with a read-only sandbox, a session that isn't
  kept, user configuration and rules ignored, its shell, apps, plugins and web search
  turned off, and a fixed answer format.
- **Claude Code** runs with its built-in tools, MCP servers, customizations, hooks and
  saved sessions turned off. A managed policy can still force hooks on, so use an
  installation whose policy you know.

These limits don't make a runner tool-free: read-only still lets it read files. Run
Shijhon where its files hold nothing but what it needs, plus the runner's sign-in.

A run that takes too long, and any run still going when Shijhon stops, is ended. A runner
that doesn't accept one of Shijhon's options fails the run, rather than running without
it. The runners were checked with Codex CLI 0.162.1 and Claude Code 2.1.296.

Their own documentation: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
[Claude managed hooks](https://code.claude.com/docs/en/hooks#disable-or-remove-hooks).
