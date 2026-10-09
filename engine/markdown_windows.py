"""Bounded structural fallback on original text; never semantic answer validation."""
import re

KINDS = frozenset({"metadata", "body", "code", "references", "unknown"})
MAX_CANDIDATES = 128
MAX_WINDOW_CHARS = 3000

def structures(lines):
    kinds = ["body"] * len(lines)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is None:
            # An unclosed fence is ambiguous, not proof that the rest is metadata.
            kinds = ["unknown"] * len(lines)
        else:
            kinds[:end + 1] = ["metadata"] * (end + 1)
    fence, references = None, False
    for i, line in enumerate(lines):
        if kinds[i] == "metadata":
            continue
        stripped = line.strip()
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            if fence is None: fence = match[1][0]
            elif match[1][0] == fence: fence = None
            kinds[i] = "code"
            continue
        if fence:
            kinds[i] = "code"
            continue
        if re.match(r"^#{1,6}\s", stripped):
            references = bool(re.match(r"^#{1,6}\s+(references|bibliography|参考文献|参考链接)\s*$", stripped, re.I))
            kinds[i] = "references" if references else "metadata"
        elif references or re.match(r"^\[[^]]+\]:\s*https?://", stripped):
            kinds[i] = "references"
    return kinds

def choose(text, query, score, context_lines=2, limit=12):
    lines = text.split("\n")
    kinds = structures(lines)
    # Intent cues only select structure. They contain no document-specific terms or answers.
    metadata = bool(re.search(r"标题|作者|日期|出版|\b(title|author|date|doi|published)\b", query or "", re.I))
    if re.search(r"方法|原理|机制|优缺点|局限|比较|条件|成本|\b(method|mechanism|limitations|comparison|conditions|cost)\b", query or "", re.I):
        metadata = False
    code = bool(re.search(r"代码|命令|脚本|\b(code|command|script)\b", query or "", re.I))
    preferred = {"metadata"} if metadata else ({"code", "body"} if code else {"body", "unknown"})
    available = [i for i, kind in enumerate(kinds) if kind in preferred and lines[i].strip()]
    fallback = not available
    pool = available or [i for i, line in enumerate(lines) if line.strip()]
    # Scan locally; retain bounded ranked anchors. No source is sent to a provider here.
    best = sorted(pool, key=lambda i: (-score(lines[i][:MAX_WINDOW_CHARS]), i))[:MAX_CANDIDATES]
    ranked = []
    for i in best:
        start = max(0, i - context_lines)
        if not fallback:
            while start < i and kinds[start] not in preferred: start += 1
        end = min(len(lines), start + limit)
        if not fallback:
            for j in range(i + 1, end):
                if kinds[j] not in preferred and lines[j].strip(): end = j; break
        block = "\n".join(lines[start:end]).strip()[:MAX_WINDOW_CHARS]
        ranked.append((score(lines[i][:MAX_WINDOW_CHARS]), score(block), -i, block, kinds[i]))
    chosen = max(ranked, default=(0, 0, 0, "", "unknown"))
    body = next((i + 1 for i, kind in enumerate(kinds) if kind == "body" and lines[i].strip()), None)
    return (text.strip()[:MAX_WINDOW_CHARS] if len(lines) <= limit and not metadata else chosen[3]), {"structureType": chosen[4], "fallbackReason": "only-nonpreferred" if fallback else "ranked-original-window",
                       "bodyStartLine": body, "sampledCandidates": len(best)}

def quality_hints(excerpt):
    # Missing-variable grammar is a warning, never a reconstruction or a claim about the loss layer.
    patterns = [r"\bparameter (?:considered|used) is (?:for|in)\b", r"\b(?:defined|given) (?:by|as)\s*[:=]\s*(?:[.;]|$)",
                r"参数(?:为|是)\s*[，。；]", r"\$\s*\$"]
    return ["POSSIBLE_MATH_LOSS"] if any(re.search(p, excerpt or "", re.I | re.M) for p in patterns) else []
