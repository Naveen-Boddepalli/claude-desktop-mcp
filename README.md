# UniversalDevAgent — MCP Server

A local Model Context Protocol (MCP) server that gives Claude secure, token-efficient access to your filesystem and shell. Built with `FastMCP` and five token-efficiency algorithms to reduce context waste.

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Project structure](#project-structure)
3. [Setup and installation](#setup-and-installation)
4. [Connecting to Claude Desktop](#connecting-to-claude-desktop)
5. [Tools reference](#tools-reference)
6. [Token efficiency algorithms](#token-efficiency-algorithms)
7. [Security model](#security-model)
8. [Skills](#skills)
9. [Tips for efficient use](#tips-for-efficient-use)
10. [Tuning the server](#tuning-the-server)
11. [Troubleshooting](#troubleshooting)

---

## What it does

UniversalDevAgent exposes six tools to Claude via the MCP protocol:

| Tool | What it does |
|---|---|
| `search_files` | Find files by name, ranked by BM25 relevance |
| `read_file` | Read code (semantic chunks) or logs (entropy summarization) |
| `run_command` | Run allowlisted shell commands safely |
| `summarize` | Entropy-based summarization of any text block |
| `clear_cache` | Flush the LRU result cache |
| `cache_stats` | Inspect current cache usage |

All file access is sandboxed to your home directory. No shell injection is possible. Token usage is actively minimized by five built-in algorithms.

---

## Project structure

```
claude-mcp/
└── universal-mcp-agent/
    ├── server.py          ← main server (all tools + algorithms)
    ├── config.py          ← MAX_OUTPUT, ALLOWED_ROOTS, ALLOWED_COMMANDS
    ├── utils.py           ← is_safe_path(), truncate()
    ├── requirements.txt   ← Python dependencies
    ├── skills/
    │   └── SKILL.md       ← token-efficient-developer skill (caveman + MCP behavior)
    └── venv/              ← virtual environment
```

---

## Setup and installation

### Prerequisites

- Python 3.11 or later
- Claude Desktop (for connecting the server)

### Step 1 — activate the virtual environment

```bash
cd /Users/boddepallinaveen/claude-mcp/universal-mcp-agent
source venv/bin/activate
```

### Step 2 — install dependencies

```bash
pip install -r requirements.txt
```

Your `requirements.txt` should contain:

```
mcp>=1.0.0,<2.0.0
```

Pin the version as shown. The MCP Python SDK changes frequently and unpinned installs can break the server silently.

To check what version you have installed right now:

```bash
pip freeze | grep mcp
```

### Step 3 — run the server manually (optional test)

```bash
python server.py
```

If it starts without errors, the server is working. Press `Ctrl+C` to stop. You don't need to keep it running manually — Claude Desktop launches it automatically once configured.

---

## Connecting to Claude Desktop

### Step 1 — find your Claude Desktop config file

On macOS:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

### Step 2 — add the server entry

Open the config file and add this inside the `"mcpServers"` object:

```json
{
  "mcpServers": {
    "universal-dev-agent": {
      "command": "/Users/boddepallinaveen/claude-mcp/universal-mcp-agent/venv/bin/python",
      "args": [
        "/Users/boddepallinaveen/claude-mcp/universal-mcp-agent/server.py"
      ]
    }
  }
}
```

**Important:** use the full path to the Python inside your `venv/bin/` — not the system Python. This ensures the correct MCP version is used.

### Step 3 — restart Claude Desktop

Quit Claude Desktop completely (Cmd+Q) and reopen it. The server will appear in the tools panel.

### Switching transport (optional)

By default the server uses `stdio` transport, which is what Claude Desktop requires. If you want to connect from a different client that uses SSE, set the environment variable:

```bash
MCP_TRANSPORT=sse python server.py
```

---

## Tools reference

### `search_files`

Search for files by name or extension inside a directory.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `directory` | string | `~` | Root directory to search |
| `query` | string | `""` | Search term — leave empty to list all files |

**Examples:**

```
# Find all files containing "auth" in their name under your home directory
search_files(query="auth")

# Find all .env files inside a specific project
search_files(directory="~/projects/my-app", query=".env")

# List all files in a directory (no filter)
search_files(directory="~/Desktop/react-folder-for-practice/react-app/src")
```

**How it works:** Results are ranked by BM25 relevance, not alphabetically. The most relevant files appear first. Noise directories (`node_modules`, `.git`, `venv`, `__pycache__`, `dist`, `build`, `.next`, `coverage`) are automatically skipped. Results are cached for 120 seconds.

---

### `read_file`

Read the contents of a file. Automatically applies the right processing strategy based on file type.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | Absolute path to the file |
| `lines` | integer | `50` | Rough number of lines to return |

**Examples:**

```
# Quick look at a file
read_file(path="~/projects/app/src/App.jsx")

# Read more of a large file
read_file(path="~/projects/app/server.py", lines=200)

# Read a log file (entropy summarization kicks in automatically)
read_file(path="~/projects/app/logs/error.log", lines=100)
```

**File type behaviour:**

| File type | What happens |
|---|---|
| `.py` `.js` `.ts` `.jsx` `.tsx` `.go` `.rs` `.java` `.cpp` `.c` | Semantic chunking at function/class boundaries |
| `.log` `.txt` `.md` `.json` `.yaml` and all other text | Entropy summarization + near-duplicate removal |
| `.pyc` `.png` `.jpg` `.gif` `.zip` `.exe` `.bin` `.so` `.pdf` `.woff` | Skipped — returns a message instead of binary garbage |

**Tip:** increase `lines` if you need more context from a large file. The `lines` parameter acts as a budget multiplier for the chunker/summarizer, not a hard line count.

---

### `run_command`

Run a shell command safely. Only allowlisted commands can be executed. No shell injection is possible because commands are passed as a list, never as a string to `sh`.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `command` | list of strings | required | Command and its arguments as a list |
| `cwd` | string | `.` | Working directory to run the command in |

**Allowed commands:** `ls`, `pwd`, `head`, `tail`, `wc`

**Examples:**

```
# List files in a directory
run_command(command=["ls", "-la"], cwd="~/projects/my-app")

# Count lines in a file
run_command(command=["wc", "-l", "server.py"], cwd="~/claude-mcp/universal-mcp-agent")

# Show first 20 lines of a file
run_command(command=["head", "-n", "20", "server.py"], cwd="~/claude-mcp/universal-mcp-agent")

# Show last 30 lines (useful for logs)
run_command(command=["tail", "-n", "30", "error.log"], cwd="~/projects/app/logs")
```

**Note:** output is automatically deduplicated and entropy-summarized before being returned to Claude, so repeated log lines are collapsed.

---

### `summarize`

Manually summarize a block of text using entropy-based line selection. Useful when you have pasted output or content that is too long.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `text` | string | required | The text to summarize |
| `max_tokens` | integer | `100` | Approximate output size in tokens (1 token ≈ 4 characters) |

**Examples:**

```
# Summarize a large block of log output
summarize(text="<paste your log here>", max_tokens=150)

# Get a very compressed version
summarize(text="<paste output here>", max_tokens=50)
```

**How it differs from truncation:** unlike cutting the first N words, this picks the lines with the highest information density (measured by Shannon entropy). Error messages and unique content survive; repeated boilerplate is dropped first.

---

### `clear_cache`

Clears the LRU result cache. Use this when you have edited files on disk and want `read_file` or `search_files` to return fresh results instead of the cached version.

```
clear_cache()
```

Returns a message telling you how many entries were cleared.

---

### `cache_stats`

Returns the current state of the LRU cache as JSON.

```
cache_stats()
# → {"total_keys": 12, "live_keys": 9, "capacity": 64}
```

`live_keys` are entries that have not yet expired (TTL = 120 seconds). `total_keys` includes entries that have expired but not yet been evicted.

---

## Token efficiency algorithms

The server includes five algorithms that run automatically to keep Claude's context lean.

### BM25 ranking (used in `search_files`)

BM25 (Best Match 25) is a probabilistic relevance algorithm used by search engines. It scores each file by how well its filename matches your query, accounting for term frequency and how rare the term is across all files. Files with the most relevant names appear first, so Claude doesn't waste tokens reading a list of unrelated matches.

### Shannon entropy scoring (used in `read_file`, `summarize`)

Each line of text is scored by Shannon entropy — a measure of information density. A line like `ERROR: database connection refused at port 5432` scores high (many unique characters, high information). A line like `# # # # # # # # #` scores low (highly repetitive). When a file needs to be compressed to fit the token budget, high-entropy lines are kept and low-entropy lines are dropped first.

### Semantic code chunking (used in `read_file` for code files)

Instead of cutting a Python or JavaScript file at an arbitrary line number, the chunker finds `def`, `class`, `async def`, `function`, and `const` boundaries and returns whole logical blocks. This means Claude always sees complete functions rather than a function that starts on line 48 and gets cut off at line 98.

### Jaccard deduplication (used in `read_file`, `run_command`)

Near-duplicate lines are detected using Jaccard similarity on character bigrams. If a log file prints the same warning 40 times with slightly different timestamps, all but the first occurrence are removed and a count is added. The threshold is 85% similarity — lines must be almost identical to be collapsed.

### LRU cache with TTL (all tools)

Results from `search_files` and `read_file` are cached in a Least-Recently-Used (LRU) cache with a 120-second time-to-live. If Claude calls the same tool with the same arguments twice in the same session, the second call returns instantly from cache and costs zero tokens. Entries expire after 120 seconds so stale content doesn't linger. The cache holds up to 64 entries; the least recently used entry is evicted first when it's full.

---

## Security model

- **Path sandboxing:** all file access is validated against `ALLOWED_ROOTS` in `config.py`. By default this is your home directory (`~`). Any path outside this list returns `"Access denied"`.
- **Path traversal prevention:** `is_safe_path()` expands `~` and resolves `..` before checking. Paths like `../../etc/passwd` are blocked.
- **No shell injection:** `subprocess.run` is called with `shell=False` (the default when passing a list). The command is never interpolated into a shell string.
- **Command allowlist:** only `ls`, `pwd`, `head`, `tail`, and `wc` can be run. Any other command returns an error immediately.
- **Binary file skipping:** known binary extensions are detected and skipped before attempting to read, preventing garbage output.

To add a trusted directory, edit `ALLOWED_ROOTS` in `config.py`:

```python
ALLOWED_ROOTS = [
    os.path.expanduser("~"),
    "/home/claude",
    "/your/trusted/path",   # add here
]
```

To add an allowed command, edit `ALLOWED_COMMANDS` in `config.py`:

```python
ALLOWED_COMMANDS = [
    "ls", "pwd", "head", "tail", "wc",
    "cat",   # add only commands you trust
]
```

---

## Skills

`skills/SKILL.md` is a companion behavior file for Claude. It combines two things into one file:

- **Caveman output compression** — drops filler words and hedging from Claude's responses, cutting output tokens ~65% while keeping full technical accuracy.
- **MCP tool discipline** — tells Claude how to use the server's tools efficiently: search before reading, default to 50 lines, stop as soon as enough context exists.

### Loading the skill

In Claude Desktop, paste this into your conversation at the start of a session:

```
Use the skill at ~/claude-mcp/universal-mcp-agent/skills/SKILL.md
```

Or read it once with the MCP server itself:

```
read_file(path="~/claude-mcp/universal-mcp-agent/skills/SKILL.md")
```

### Intensity levels

By default the skill uses **full** caveman mode. Switch mid-session with:

| Command | Effect |
|---|---|
| `/caveman lite` | Drop filler, keep articles and full sentences |
| `/caveman full` | Drop articles, fragments OK — default |
| `/caveman ultra` | Max compression, arrows for causality, abbreviate everything |
| `stop caveman` | Back to normal prose |

Code blocks, commits, and security warnings are always written normally regardless of mode.

---

## Tips for efficient use

**Be specific with search queries.** `search_files(query="auth middleware")` returns fewer, better results than `search_files(query="js")`. BM25 works best with descriptive terms.

**Use `search_files` before `read_file`.** Always find the path first, then read it. Don't guess paths — even small typos cause errors that waste a tool call.

**Increase `lines` for large files.** The default is 50. For a 500-line Python file, use `lines=200` to get the full semantic chunks. The chunker will still stop at a function boundary, so you won't get a half-function even at high values.

**Use `tail` for logs.** New log entries are at the bottom. `run_command(command=["tail", "-n", "50", "app.log"])` is much more efficient than `read_file` on the whole log.

**Call `clear_cache` after editing files.** The cache TTL is 120 seconds. If you edit a file and want Claude to see the new version immediately, call `clear_cache()` first.

**Use `summarize` on pasted content.** If you paste a large block of text into chat and want Claude to work with a compressed version, ask it to call `summarize(text="...", max_tokens=100)`.

**Check `cache_stats` in long sessions.** If you're running many tool calls in a long session, `cache_stats()` shows you how much is cached and whether the cache is near capacity.

---

## Tuning the server

These constants in `server.py` and `config.py` can be adjusted:

| Constant | Location | Default | Effect |
|---|---|---|---|
| `MAX_OUTPUT` | `config.py` | `2000` | Hard character cap on all tool output |
| `MAX_FILES` | `config.py` | `20` | Maximum files returned by `search_files` |
| `ALLOWED_ROOTS` | `config.py` | `["~"]` | Directories the server can access |
| `ALLOWED_COMMANDS` | `config.py` | 5 commands | Commands `run_command` can execute |
| `LRUCache capacity` | `server.py` | `64` | Max cached entries |
| `LRUCache ttl_seconds` | `server.py` | `120` | Cache expiry in seconds |
| `deduplicate_lines threshold` | `server.py` | `0.85` | Similarity threshold for line dedup |
| `SKIP_DIRS` | `server.py` | 10 dirs | Directories skipped by `search_files` |

To add more directories to `SKIP_DIRS`:

```python
SKIP_DIRS = {
    ".git", "node_modules", "venv", "__pycache__",
    ".DS_Store", "dist", "build", ".next", ".venv",
    "coverage", ".pytest_cache", ".mypy_cache",
    "your_dir_here",   # add here
}
```

---

## Troubleshooting

**Claude Desktop doesn't show the server tools**

Check that the path in `claude_desktop_config.json` uses the venv Python, not the system Python. Run `which python` inside the activated venv to confirm the path:

```bash
source venv/bin/activate
which python
# → /Users/boddepallinaveen/claude-mcp/universal-mcp-agent/venv/bin/python
```

**"Access denied" on a path you own**

The path is outside `ALLOWED_ROOTS`. Add it to `ALLOWED_ROOTS` in `config.py`.

**Results are stale after editing a file**

Call `clear_cache()` to force fresh reads. The cache TTL is 120 seconds, so stale results expire on their own after 2 minutes.

**`search_files` returns nothing**

The query is too specific or the directory path is wrong. Try with no query first to confirm the directory is accessible: `search_files(directory="~/your/path")`. Then add the query.

**`run_command` returns "Command not allowed"**

The command isn't in `ALLOWED_COMMANDS`. Add it to the list in `config.py` if you trust it.

**Server crashes on startup with `ImportError`**

The venv is not activated or the MCP package isn't installed. Run:

```bash
source venv/bin/activate
pip install mcp>=1.0.0,<2.0.0
```