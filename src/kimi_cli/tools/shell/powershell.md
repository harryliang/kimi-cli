Execute a ${SHELL} command. Use this tool to explore the filesystem, inspect or edit files, run Windows scripts, collect system information, etc., whenever the agent is running on Windows.

Note that you are running on Windows, so make sure to use Windows commands, paths, and conventions.

**Output:**
The stdout and stderr streams are combined and returned as a single string. Extremely long output may be truncated. When a command fails, the exit code is provided in a system tag.

If `run_in_background=true`, the command will be started as a background task and this tool will return a task ID instead of waiting for completion. When doing that, you must provide a short `description`. You will be automatically notified when the task completes. Use `TaskOutput` if you need progress or want to wait for completion, and use `TaskStop` only if the task must be cancelled. For human users in the interactive shell, background tasks are managed through `/task` only; do not suggest `/task list`, `/task output`, `/task stop`, `/tasks`, or any other invented shell subcommands.

**Guidelines for safety and security:**
- Every tool call starts a fresh ${SHELL} session. Environment variables, `cd` changes, and command history do not persist between calls.
- Do not launch interactive programs or anything that is expected to block indefinitely; ensure each command finishes promptly. Provide a `timeout` argument for potentially long runs.
- Avoid using `..` to leave the working directory, and never touch files outside that directory unless explicitly instructed.
- Never attempt commands that require elevated (Administrator) privileges unless explicitly authorized.

**Guidelines for efficiency:**
- **PowerShell does NOT support `&&` or `||`** (these are bash operators). To run multiple commands:
  - Use `;` to execute commands sequentially (unconditional): `cmd1; cmd2`
  - Use `if ($?) { cmd2 }` to run cmd2 only if cmd1 succeeded: `cmd1; if ($?) { cmd2 }`
  - Use `if (-not $?) { cmd2 }` to run cmd2 only if cmd1 failed: `cmd1; if (-not $?) { cmd2 }`
- **PowerShell's `2>&1` behaves differently from Bash**: It wraps stderr in `ErrorRecord` objects with extra metadata. If you need clean stderr capture like Bash, consider using cmd.exe instead: `cmd /c "command 2>&1"`
- **PowerShell quoting rules differ from Bash**:
  - Single quotes `'...'` do NOT expand variables (e.g., `'$var'` stays as `$var`)
  - Double quotes `"..."` DO expand variables (e.g., `"$var"` expands to its value)
  - Backtick `` ` `` is the escape character, NOT backslash `\`
  - To include a literal `$` in double quotes, escape it with backtick: `` `"$var` `` or use single quotes
  - PowerShell does NOT support heredoc (`<<EOF`); for complex multi-line scripts, write to a file and execute
- Redirect or pipe output with `>`, `>>`, `|`, and leverage `for /f`, `if`, and `set` to build richer one-liners instead of multiple tool calls.
- Reuse built-in utilities (e.g., `findstr`, `where`) to filter, transform, or locate data in a single invocation.
- Prefer `run_in_background=true` for long-running builds, tests, watchers, or servers when you need the conversation to continue before the command finishes.
- After starting a background task, do not guess its outcome. Rely on the automatic completion notification whenever possible. Use `TaskOutput` only when you need to inspect progress or block until completion.
- If you need to tell a human shell user how to manage background tasks, only mention `/task`. Do not invent `/task list`, `/task output`, `/task stop`, or `/tasks`.

**Commands available:**
- Shell environment: `cd`, `dir`, `set`, `setlocal`, `echo`, `call`, `where`
- File operations: `type`, `copy`, `move`, `del`, `erase`, `mkdir`, `rmdir`, `attrib`, `mklink`
- Text/search: `find`, `findstr`, `more`, `sort`, `Get-Content`
- System info: `ver`, `systeminfo`, `tasklist`, `wmic`, `hostname`
- Archives/scripts: `tar`, `Compress-Archive`, `powershell`, `python`, `node`
- Other: Any other binaries available on the system PATH; run `where <command>` first if unsure.
