import json
import os
import re
import math
import time
import itertools
import subprocess
from collections import Counter, OrderedDict
from typing import List, Optional, Tuple

from mcp.server.fastmcp import FastMCP
from config import MAX_FILES, ALLOWED_COMMANDS
from utils import is_safe_path, truncate

# ─────────────────────────────────────────────
# TOKEN COUNTER
# Uses tiktoken if available; falls back to char estimate.
# Note: cl100k_base is GPT-4's encoding — counts may be off ~10-15% for Claude,
# but still far better than the divide-by-4 heuristic.
# ─────────────────────────────────────────────
try:
    import tiktoken
    _TOKENIZER = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_TOKENIZER.encode(text))

except ImportError:
    def count_tokens(text: str) -> int:
        return len(text) // 4


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
CMD_TIMEOUT = int(os.getenv("MCP_CMD_TIMEOUT", "30"))

SKIP_DIRS = {
    ".git", "node_modules", "venv", "__pycache__",
    ".DS_Store", "dist", "build", ".next", ".venv",
    "coverage", ".pytest_cache", ".mypy_cache",
}

BINARY_EXTS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".zip",
    ".exe", ".bin", ".so", ".dylib", ".pdf", ".woff",
}

CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".rs", ".java", ".cpp", ".c",
}


# ─────────────────────────────────────────────
# LRU CACHE WITH TTL
# ─────────────────────────────────────────────
class LRUCache:
    """
    Least-Recently-Used cache with time-to-live expiry.
    Prevents stale results from wasting tokens on repeated calls.
    """

    def __init__(self, capacity: int = 64, ttl_seconds: int = 120):
        self.capacity = capacity
        self.ttl = ttl_seconds
        self._cache: OrderedDict = OrderedDict()  # key -> (value, timestamp)

    def get(self, key: str):
        if key not in self._cache:
            return None
        value, ts = self._cache[key]
        if time.time() - ts > self.ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (value, time.time())
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)  # evict oldest

    def clear(self):
        self._cache.clear()

    def stats(self) -> dict:
        now = time.time()
        alive = sum(1 for _, (_, ts) in self._cache.items() if now - ts <= self.ttl)
        return {
            "total_keys": len(self._cache),
            "live_keys": alive,
            "capacity": self.capacity,
        }


CACHE = LRUCache(capacity=64, ttl_seconds=120)


# ─────────────────────────────────────────────
# FILE LIST CACHE
# FIX: Cache is now keyed per directory to prevent cross-directory contamination.
# ─────────────────────────────────────────────
_FILE_LIST_CACHE: dict = {}   # directory -> (file_list, timestamp)
_FILE_LIST_CACHE_TTL = 120    # seconds


def get_all_files(directory: str) -> List[str]:
    """
    Collect all files under `directory` (excluding SKIP_DIRS) with per-directory caching.

    FIX: Previously used a single global tuple, so a second call with a different
    directory would silently return the first directory's file list for up to TTL seconds.
    Now keyed by directory so each path has its own independent cache entry.
    """
    now = time.time()
    cached = _FILE_LIST_CACHE.get(directory)
    if cached is not None:
        file_list, ts = cached
        if now - ts < _FILE_LIST_CACHE_TTL:
            return file_list

    all_files: List[str] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            all_files.append(os.path.join(root, f))

    _FILE_LIST_CACHE[directory] = (all_files, now)
    return all_files


# ─────────────────────────────────────────────
# TOKEN EFFICIENCY ALGORITHMS
# ─────────────────────────────────────────────

def bm25_score(
    query_terms: List[str],
    doc_tokens: List[str],
    corpus_size: int,
    avg_doc_len: float,
    df: dict,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """
    BM25 relevance scoring — ranks files by how well they match the query.
    Better matches appear first, so Claude reads the most relevant content
    without wasting tokens on low-signal results.
    """
    score = 0.0
    doc_len = len(doc_tokens)
    tf_map = Counter(doc_tokens)

    for term in query_terms:
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue
        idf = math.log(
            1 + (corpus_size - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)
        )
        tf_norm = (tf * (k1 + 1)) / (
            tf + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1))
        )
        score += idf * tf_norm

    return score


def entropy_score(text: str) -> float:
    """
    Shannon entropy of a text line.
    High entropy = more information content = worth keeping.
    Low entropy = repetitive/boilerplate = safe to drop.
    """
    if not text.strip():
        return 0.0
    chars = Counter(text.lower())
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in chars.values())


def smart_summarize(text: str, max_tokens: int = 400) -> str:
    """
    Entropy-based summarization — keeps the highest-information lines
    rather than blindly truncating from the top.

    Useful for log files where errors/warnings are rarely at the top.
    Falls back gracefully when all lines have near-equal entropy.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return text

    if count_tokens(text) <= max_tokens:
        return text

    scored = sorted(enumerate(lines), key=lambda x: entropy_score(x[1]), reverse=True)

    selected: dict = {}
    used_tokens = 0
    for idx, line in scored:
        cost = count_tokens(line) + 1  # +1 for newline
        if used_tokens + cost > max_tokens:
            break
        selected[idx] = line
        used_tokens += cost

    result = []
    prev = -1
    for idx in sorted(selected.keys()):
        if idx > prev + 1:
            result.append("  ... [low-entropy lines omitted] ...")
        result.append(selected[idx])
        prev = idx

    return (
        "\n".join(result)
        + f"\n[smart_summarize: kept {len(selected)}/{len(lines)} lines]"
    )


def smart_chunk_code(content: str, max_chars: int = 4000) -> str:
    """
    Semantic chunking for code files — splits at function/class boundaries
    instead of arbitrary line counts.

    Returns whole logical units up to the character budget so Claude always
    sees complete functions rather than truncated mid-function snippets.
    """
    if len(content) <= max_chars:
        return content

    boundary_pattern = re.compile(
        r"^(def |class |async def |function |const |export (default |async )?function |"
        r"func |fn |public |private |protected |void |int |bool |string )",
        re.MULTILINE,
    )
    boundaries = [m.start() for m in boundary_pattern.finditer(content)]

    if not boundaries:
        # No recognized structure — fall back to paragraph chunks
        paragraphs = re.split(r"\n{2,}", content)
        result, used = [], 0
        for p in paragraphs:
            if used + len(p) > max_chars:
                break
            result.append(p)
            used += len(p) + 2
        return "\n\n".join(result) + "\n... [chunked at paragraph boundary]"

    boundaries.append(len(content))  # sentinel
    result, used = [], 0
    for i in range(len(boundaries) - 1):
        block = content[boundaries[i] : boundaries[i + 1]]
        if used + len(block) > max_chars:
            if not result:
                result.append(block[:max_chars] + "\n... [block truncated]")
            break
        result.append(block)
        used += len(block)

    return "".join(result) + f"\n... [chunked: {len(result)} blocks shown]"


def deduplicate_lines(text: str, threshold: float = 0.85) -> str:
    """
    Removes near-duplicate lines using Jaccard similarity on character bigrams.
    Useful for command output or logs that repeat the same message many times.
    threshold=0.85 means lines must be ≥85% similar to be considered duplicates.
    """
    lines = text.splitlines()
    if len(lines) <= 5:
        return text

    def bigrams(s: str):
        s = s.strip().lower()
        return set(s[i : i + 2] for i in range(len(s) - 1))

    def jaccard(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        union = len(a | b)
        return len(a & b) / union if union else 0.0

    kept, removed = [], 0
    seen_bigrams: List[set] = []

    for line in lines:
        bg = bigrams(line)
        is_dup = any(jaccard(bg, seen) >= threshold for seen in seen_bigrams[-20:])
        if is_dup:
            removed += 1
        else:
            kept.append(line)
            seen_bigrams.append(bg)

    result = "\n".join(kept)
    if removed:
        result += f"\n[dedup: removed {removed} near-duplicate lines]"
    return result


# ─────────────────────────────────────────────
# MCP SERVER
# ─────────────────────────────────────────────
mcp = FastMCP("UniversalDevAgent")


@mcp.tool()
def search_files(directory: str = "~", query: str = "") -> str:
    """
    Search files by name or extension, ranked by BM25 relevance.
    Skips noise directories (node_modules, .git, venv, etc.).
    Returns top matches sorted by relevance score.

    Args:
        directory: Root directory to search (default: home directory).
        query: Search term(s). Leave empty to list all files.
    """
    cache_key = f"search:{directory}:{query}"
    cached = CACHE.get(cache_key)
    if cached:
        return cached

    try:
        directory = os.path.abspath(os.path.expanduser(directory))
        if not is_safe_path(directory):
            return "Access denied"

        all_files = get_all_files(directory)

        if not query:
            result = json.dumps(all_files[:MAX_FILES])
            CACHE.set(cache_key, result)
            return result

        query_terms = query.lower().split()
        tokenized = [
            os.path.basename(p).lower().replace("_", " ").replace("-", " ").split()
            for p in all_files
        ]
        avg_len = sum(len(t) for t in tokenized) / max(len(tokenized), 1)

        df: Counter = Counter()
        for tokens in tokenized:
            for term in set(tokens):
                df[term] += 1

        scored = []
        for path, tokens in zip(all_files, tokenized):
            score = bm25_score(query_terms, tokens, len(all_files), avg_len, df)
            if score > 0 or any(q in path.lower() for q in query_terms):
                scored.append((score, path))

        scored.sort(reverse=True)
        top = [p for _, p in scored[:MAX_FILES]]

        result = json.dumps(top)
        CACHE.set(cache_key, result)
        return result

    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def read_file(path: str, lines: int = 50) -> str:
    """
    Read file content with smart processing:
    - Code files (.py, .js, etc.): semantic chunking at function/class boundaries.
    - Text/log files: entropy-based summarization + near-duplicate removal.
    - Binary files: skipped with an informative message.

    FIX: `lines` is now respected even for cached content — the cache stores
    raw content, and chunking/summarization is applied fresh on each call using
    the requested `lines` budget.

    Args:
        path:  Absolute path to the file.
        lines: Rough number of lines to return (used as chunking/summarization budget).
    """
    # Cache raw content only; apply truncation on every call so `lines` is respected.
    cache_key = f"read:{path}"
    content = CACHE.get(cache_key)

    if content is None:
        try:
            if not is_safe_path(path):
                return "Access denied"

            ext = os.path.splitext(path)[1].lower()
            if ext in BINARY_EXTS:
                return f"[Skipped binary file: {os.path.basename(path)}]"

            with open(path, "r", errors="ignore") as f:
                # Read up to 1000 lines for caching; chunking trims further below.
                content = "".join(itertools.islice(f, 1000))

            CACHE.set(cache_key, content)

        except Exception as e:
            return f"Error: {str(e)}"

    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXTS:
        # lines * 80 ≈ character budget for code chunking
        result = smart_chunk_code(content, max_chars=lines * 80)
    else:
        content = deduplicate_lines(content)
        # lines * 4 ≈ token budget for log/text summarization
        result = smart_summarize(content, max_tokens=lines * 4)

    return truncate(result)


@mcp.tool()
def run_command(command: List[str], cwd: str = ".") -> str:
    """
    Run safe, allowlisted system commands (no shell execution).
    Output is deduplicated and entropy-summarized before returning.

    FIX: Skips summarization entirely for short output (≤20 lines) to avoid
    unnecessary overhead on small results.

    Args:
        command: Command + arguments as a list, e.g. ["ls", "-la"].
        cwd:     Working directory for the command.
    """
    try:
        if command[0] not in ALLOWED_COMMANDS:
            return f"Command not allowed. Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}"

        if not is_safe_path(cwd):
            return "Access denied"

        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT,
        )
        raw = result.stdout + result.stderr

        # Skip heavy processing for short output
        if raw.count("\n") <= 20:
            return truncate(raw)

        cleaned = deduplicate_lines(raw)
        return truncate(smart_summarize(cleaned, max_tokens=300))

    except subprocess.TimeoutExpired:
        return f"Command timed out after {CMD_TIMEOUT}s"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def summarize(text: str, max_tokens: int = 100) -> str:
    """
    Entropy-based summarization — keeps the highest-information lines
    from a block of text. Better than simple truncation for logs and output.

    Args:
        text:       Input text to summarize.
        max_tokens: Token budget for the output (1 token ≈ 4 chars).
    """
    deduped = deduplicate_lines(text)
    return smart_summarize(deduped, max_tokens=max_tokens)


@mcp.tool()
def estimate_tokens(text: str) -> str:
    """
    Returns the estimated token count for the given text.
    Uses tiktoken (cl100k_base) if available, otherwise estimates via char count.
    Useful for gauging content size before requesting it.

    Args:
        text: The text to measure.
    """
    return json.dumps({"tokens": count_tokens(text)})


@mcp.tool()
def clear_cache() -> str:
    """
    Clears both the LRU result cache and the per-directory file list cache.
    Use when files have changed on disk and you want fresh reads on the next call.
    """
    global _FILE_LIST_CACHE
    stats = CACHE.stats()
    CACHE.clear()
    _FILE_LIST_CACHE = {}  # FIX: reset dict, not tuple
    return f"Cache cleared. Had {stats['live_keys']} live entries ({stats['total_keys']} total)."


@mcp.tool()
def cache_stats() -> str:
    """
    Returns current LRU cache usage: live keys, total keys, capacity.
    Useful for debugging token usage across a session.
    """
    return json.dumps(CACHE.stats())


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # stdio transport = required for Claude Desktop
    # Set MCP_TRANSPORT=sse in env to switch to SSE for other clients
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)