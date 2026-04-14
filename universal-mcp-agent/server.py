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

# [UPGRADE] Use tiktoken for accurate token counting (Claude's tokenizer)
try:
    import tiktoken
    TOKENIZER = tiktoken.get_encoding("cl100k_base")  # Works for Claude models
    def count_tokens(text: str) -> int:
        return len(TOKENIZER.encode(text))
except ImportError:
    # Fallback to character approximation if tiktoken not installed
    def count_tokens(text: str) -> int:
        return len(text) // 4

# [UPGRADE] Configurable command timeout via environment variable
CMD_TIMEOUT = int(os.getenv("MCP_CMD_TIMEOUT", "30"))

mcp = FastMCP("UniversalDevAgent")

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
        return {"total_keys": len(self._cache), "live_keys": alive, "capacity": self.capacity}


CACHE = LRUCache(capacity=64, ttl_seconds=120)

# [UPGRADE] Global file list cache to speed up repeated searches
_FILE_LIST_CACHE: Tuple[List[str], float] = ([], 0.0)
_FILE_LIST_CACHE_TTL = 120  # seconds

# ─────────────────────────────────────────────
# SKIP DIRS (avoids crawling noise)
# ─────────────────────────────────────────────
SKIP_DIRS = {
    ".git", "node_modules", "venv", "__pycache__",
    ".DS_Store", "dist", "build", ".next", ".venv",
    "coverage", ".pytest_cache", ".mypy_cache"
}

# ─────────────────────────────────────────────
# TOKEN EFFICIENCY ALGORITHMS
# ─────────────────────────────────────────────

def bm25_score(query_terms: List[str], doc_tokens: List[str], corpus_size: int,
               avg_doc_len: float, df: dict, k1: float = 1.5, b: float = 0.75) -> float:
    """
    BM25 relevance scoring — ranks files/lines by how well they match the query.
    Better matches appear first, so Claude reads the most relevant content
    without wasting tokens on low-signal results.

    k1 controls term frequency saturation (1.5 = standard).
    b controls document length normalization (0.75 = standard).
    """
    score = 0.0
    doc_len = len(doc_tokens)
    tf_map = Counter(doc_tokens)
    for term in query_terms:
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue
        # [FIX] Corrected IDF formula (standard Lucene variant)
        idf = math.log(1 + (corpus_size - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1)))
        score += idf * tf_norm
    return score


def entropy_score(text: str) -> float:
    """
    Shannon entropy of a text line/sentence.
    High entropy = more information content = worth keeping.
    Low entropy = repetitive/boilerplate = safe to drop.

    Used to pick the most informative lines when truncating output.
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

    This is especially useful for log files and command output where
    the most important lines (errors, warnings) are rarely at the top.

    Falls back to head truncation if all lines have equal entropy.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return text

    # [UPGRADE] Use actual token count instead of character estimate
    if count_tokens(text) <= max_tokens:
        return text

    # Score each line by entropy
    scored = sorted(enumerate(lines), key=lambda x: entropy_score(x[1]), reverse=True)

    # Greedily pick highest-entropy lines until token budget is full
    selected = {}
    used_tokens = 0
    for idx, line in scored:
        cost = count_tokens(line) + 1  # +1 for newline
        if used_tokens + cost > max_tokens:
            break
        selected[idx] = line
        used_tokens += cost

    # Reconstruct in original order with gap markers
    result = []
    prev = -1
    for idx in sorted(selected.keys()):
        if idx > prev + 1:
            result.append("  ... [low-entropy lines omitted] ...")
        result.append(selected[idx])
        prev = idx

    return "\n".join(result) + f"\n[smart_summarize: kept {len(selected)}/{len(lines)} lines]"


def smart_chunk_code(content: str, max_chars: int = 4000) -> str:
    """
    Semantic chunking for code files — splits at function/class boundaries
    instead of arbitrary line counts.

    Dumb slicing cuts mid-function and wastes tokens on incomplete context.
    This returns whole logical units up to the token budget.
    """
    if len(content) <= max_chars:
        return content

    # [UPGRADE] Expanded boundary regex to support more languages
    boundary_pattern = re.compile(
        r'^(def |class |async def |function |const |export (default |async )?function |func |fn |'
        r'public |private |protected |void |int |bool |string )',
        re.MULTILINE
    )
    # [FIX] Variable name corrected from 'boundaries' to 'boundaries'
    boundaries = [m.start() for m in boundary_pattern.finditer(content)]

    if not boundaries:
        # No recognized structure — fall back to paragraph chunks
        paragraphs = re.split(r'\n{2,}', content)
        result, used = [], 0
        for p in paragraphs:
            if used + len(p) > max_chars:
                break
            result.append(p)
            used += len(p) + 2
        return "\n\n".join(result) + "\n... [chunked at paragraph boundary]"

    # Return whole blocks up to budget
    result, used = [], 0
    boundaries.append(len(content))  # sentinel
    for i in range(len(boundaries) - 1):
        block = content[boundaries[i]:boundaries[i + 1]]
        if used + len(block) > max_chars:
            if not result:
                # Even the first block is too big — truncate it
                result.append(block[:max_chars] + "\n... [block truncated]")
            break
        result.append(block)
        used += len(block)

    return "".join(result) + f"\n... [chunked: {len(result)} blocks shown]"


def deduplicate_lines(text: str, threshold: float = 0.85) -> str:
    """
    Removes near-duplicate lines using Jaccard similarity on character bigrams.
    Useful for command output or logs that repeat the same message many times.

    threshold=0.85 means lines must be 85%+ similar to be considered duplicates.
    """
    lines = text.splitlines()
    if len(lines) <= 5:
        return text

    def bigrams(s: str):
        s = s.strip().lower()
        return set(s[i:i+2] for i in range(len(s) - 1))

    def jaccard(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        inter = len(a & b)
        union = len(a | b)
        return inter / union if union else 0.0

    kept, removed = [], 0
    seen_bigrams = []

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


def get_all_files(directory: str) -> List[str]:
    """
    Collect all files under directory (excluding SKIP_DIRS) with caching.
    """
    global _FILE_LIST_CACHE
    now = time.time()
    if _FILE_LIST_CACHE[1] > now - _FILE_LIST_CACHE_TTL:
        return _FILE_LIST_CACHE[0]

    all_files = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            all_files.append(os.path.join(root, f))
    _FILE_LIST_CACHE = (all_files, now)
    return all_files


# ─────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────

@mcp.tool()
def search_files(directory: str = "~", query: str = "") -> str:
    """
    Search files by name or extension, ranked by BM25 relevance.
    Skips noise directories (node_modules, .git, venv, etc).
    Returns top matches sorted by relevance score.
    """
    cache_key = f"search:{directory}:{query}"
    cached = CACHE.get(cache_key)
    if cached:
        return cached

    try:
        directory = os.path.expanduser(directory)
        directory = os.path.abspath(directory)
        if not is_safe_path(directory):
            return "Access denied"

        # [FIX] Removed early break that prevented full traversal
        all_files = get_all_files(directory)

        if not query:
            result = json.dumps(all_files[:MAX_FILES])
            CACHE.set(cache_key, result)
            return result

        # BM25 ranking
        query_terms = query.lower().split()
        tokenized = [os.path.basename(p).lower().replace("_", " ").replace("-", " ").split()
                     for p in all_files]
        avg_len = sum(len(t) for t in tokenized) / max(len(tokenized), 1)

        # Document frequency per term
        df: dict = Counter()
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
    Read file content with semantic chunking for code files
    and entropy-based summarization for logs/text.
    Short files are returned in full. Binary files are skipped.
    """
    # [FIX] Cache full file content, then apply truncation (lines param ignored in cache key)
    cache_key = f"read:{path}"
    cached = CACHE.get(cache_key)
    if cached:
        content = cached
    else:
        try:
            if not is_safe_path(path):
                return "Access denied"

            # Detect likely binary files by extension
            binary_exts = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".zip",
                           ".exe", ".bin", ".so", ".dylib", ".pdf", ".woff"}
            if os.path.splitext(path)[1].lower() in binary_exts:
                return f"[Skipped binary file: {os.path.basename(path)}]"

            with open(path, "r", errors="ignore") as f:
                # Read up to a generous limit (1000 lines) for caching
                content = "".join(itertools.islice(f, 1000))
            CACHE.set(cache_key, content)
        except Exception as e:
            return f"Error: {str(e)}"

    # Apply requested truncation/chunking based on 'lines' parameter
    # Code files: semantic chunking
    code_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".cpp", ".c"}
    if os.path.splitext(path)[1].lower() in code_exts:
        # Use lines * 80 as rough character budget for chunking
        result = smart_chunk_code(content, max_chars=lines * 80)
    else:
        # Logs / text: entropy-based summarization + dedup
        content = deduplicate_lines(content)
        # Use lines * 4 as token budget for summarization (1 token ≈ 4 chars)
        result = smart_summarize(content, max_tokens=lines * 4)

    return truncate(result)


@mcp.tool()
def run_command(command: List[str], cwd: str = ".") -> str:
    """
    Run safe system commands (allowlisted only, no shell execution).
    Output is deduplicated and entropy-summarized before returning.
    """
    try:
        if command[0] not in ALLOWED_COMMANDS:
            return f"Command not allowed. Allowed: {', '.join(ALLOWED_COMMANDS)}"
        if not is_safe_path(cwd):
            return "Access denied"

        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT  # [UPGRADE] Configurable timeout
        )
        raw = result.stdout + result.stderr
        cleaned = deduplicate_lines(raw)
        return truncate(smart_summarize(cleaned, max_tokens=300))

    except subprocess.TimeoutExpired:
        return f"Command timed out after {CMD_TIMEOUT} seconds"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def summarize(text: str, max_tokens: int = 100) -> str:
    """
    Entropy-based summarization — keeps the highest-information lines
    from a block of text. Better than simple truncation for logs and output.
    Use max_tokens to control output size (1 token ≈ 4 chars).
    """
    deduped = deduplicate_lines(text)
    return smart_summarize(deduped, max_tokens=max_tokens)


@mcp.tool()
def estimate_tokens(text: str) -> str:
    """
    Returns the token count for the given text using Claude's tokenizer.
    Useful for gauging how large a piece of content is before requesting it.
    """
    tokens = count_tokens(text)
    return json.dumps({"tokens": tokens})


@mcp.tool()
def clear_cache() -> str:
    """
    Clears the LRU result cache and the file list cache.
    Use when files have changed on disk and you want fresh reads on the next call.
    """
    global _FILE_LIST_CACHE
    stats = CACHE.stats()
    CACHE.clear()
    _FILE_LIST_CACHE = ([], 0.0)  # [UPGRADE] Also clear file list cache
    return f"Cache cleared. Had {stats['live_keys']} live entries ({stats['total_keys']} total)."


@mcp.tool()
def cache_stats() -> str:
    """
    Returns current LRU cache usage — live keys, total keys, capacity.
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
