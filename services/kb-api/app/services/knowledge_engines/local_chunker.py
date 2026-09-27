"""本地分段器：MinerU 解析产物（markdown）按文档库既有分段规则切块。

规则与 ProcessingConfig 对齐（RAGFlow 时代的配置项全部保留）：
- chunk_method=one：整篇一个分段；
- chunk_method=auto：【自动】分层瀑布分段（无大模型），三级策略逐级兜底——
  ① 结构切分：fence 感知地扫描 markdown 标题（# ~ ######），按标题树切节，
     节内保留标题行；超长节拆分出的续块补「标题路径」行（一级 > 二级 > 三级），
     保证脱离上下文的 chunk 仍可溯源；
  ② 递归长度修正：超长节按 段落（空行，围栏代码块整体为一块）→ 原子行
     （连续表格行/图片行不拆）→ 句子（delimiter 集合）→ 字符滑窗（重叠 limit/8）
     逐级递归，目标长度 chunk_token_num；过短邻块（< limit/4）同节内合并；
  ③ 统计兜底：全文无标题时按 TextTiling-lite 切分——句子块（3 句/块）的
     字符 1/2-gram 向量做余弦相似度，谷值深度 ≥ μ+σ/2 且为局部极小处判定
     主题边界，产出仍走 ② 的长度修正；
- 其他方法（naive/book/laws/manual/paper/presentation/table）统一按 naive
  近似处理：delimiter 集合切句 + chunk_token_num 滚动窗口聚合（单句不硬拆）；
- overlap：分段重叠度——滚动聚合（naive 切句聚合、递归修正的单元聚合）闭合
  分段时，携带上一分段尾部总 Token ≤ overlap 的完整单元（句子/段落/行），
  提升跨分段语义连续性；0 表示不重叠；
- 表格原子化：HTML 表格（MinerU 复杂表格产物，单元格可能含句末标点）与
  markdown 管道表格先整体摘出为占位符再切分——任何策略（含 naive 切句与
  字符滑窗兜底）都不会从表格中间打断（宁可整块超出也不切断）；表格占位
  不进入相邻分段的重叠尾，避免整表跨分段重复；
- 表格结束后强制分段：表格作为分段结尾（表格前引文可同段），后续正文从
  新分段开始，下一分段即使有 overlap 也不回带表格内容；表格下标（表注）
  吸附——占位后首个非空行若为注释类短行（注：/备注/说明/表 N/Table N/
  * /①…，≤100 字符），并入表格块随表格同分段；
- replace_whitespace / remove_urls_emails：文本预处理规则（分段前执行），
  语义与 Dify CleanProcessor 对齐——前者把 3+ 连续换行折叠为 2 个换行、
  2+ 连续空格/制表符（含全角空格）折叠为单空格（保留换行结构，不影响标题
  识别）；后者删除所有电子邮箱地址与 http(s) 裸 URL（markdown 链接/图片
  整体占位保护后还原，避免链接文字/图片引用被误删）；
- enable_children：父分段按 children_delimiter 切子块（子块行 parent_id 指向
  父分段，列表页只展示父分段）；仅 naive 生效；
- auto_keywords / auto_questions：引擎侧生成能力，本地解析不生成，忽略；
- layout_recognize：MinerU 恒做版面识别，该选项保留在配置中但不区分行为。

markdown 清理：剔除 data:image 内联图（base64 会撑爆存储），保留普通图片引用。
"""
import math
import re

_DATA_URI_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*data:image/[^)]*\)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# HTML 表格（MinerU 复杂表格产物）整体匹配；表格是原子单元，绝不从中间切断
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_TABLE_PH_RE = re.compile(r"^\x00tbl(\d+)\x00$")
_TABLE_PH_ANY_RE = re.compile(r"\x00tbl\d+\x00")
# 表注（表格下标）识别：占位后首个非空行为短行且以注释类前缀开头（注：/备注/说明/表 N/Table N/* /①…）
_TABLE_NOTE_RE = re.compile(r"注(?=[:：\s\d])|备注(?=[:：\s\d])|说明(?=[:：\s\d])|表\s*\d|Table\s*\d|"
                            r"Note(?=[:：\s])|[*＊①②③④⑤]", re.IGNORECASE)

# 文本预处理规则（Dify pre_processing_rules 语义）
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[\t\f\r\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]{2,}")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
_MD_IMAGE_RE = re.compile(r"!\[.*?\]\((https?://[^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")


def _pre_process(text: str, cfg: dict) -> str:
    """分段前的文本预处理：替换连续空白 / 删除 URL 与邮箱。"""
    if cfg.get("replace_whitespace"):
        text = _MULTI_NEWLINE_RE.sub("\n\n", text)
        text = _MULTI_SPACE_RE.sub(" ", text)
    if cfg.get("remove_urls_emails"):
        text = _EMAIL_RE.sub("", text)
        # markdown 链接/图片整体占位保护（链接文字本身可能是 URL），删完裸 URL 后还原
        placeholders: list[str] = []

        def _protect(m: "re.Match[str]") -> str:
            placeholders.append(m.group(0))
            return f"\x00md{len(placeholders) - 1}\x00"

        text = _MD_LINK_RE.sub(_protect, text)
        text = _MD_IMAGE_RE.sub(_protect, text)
        text = _URL_RE.sub("", text)
        for i, raw in enumerate(placeholders):
            text = text.replace(f"\x00md{i}\x00", raw)
    return text


def clean_markdown(md: str) -> str:
    if not md:
        return ""
    lines = []
    for line in md.splitlines():
        if "data:image/" in line:
            line = _DATA_URI_IMG_RE.sub("", line)
            if not line.strip():
                continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_table_note(line: str) -> bool:
    """表注（表格下标）判定：短行（≤100 字符）且以注释类前缀开头，非标题/非表格行。"""
    s = line.strip()
    return (len(s) <= 100 and bool(_TABLE_NOTE_RE.match(s))
            and not _HEADING_RE.match(s) and not _TABLE_ROW_RE.match(s))


def _extract_tables(text: str) -> tuple[str, list[str]]:
    """表格原子化：HTML 表格（MinerU 复杂表格产物）与 markdown 管道表格整体摘出为占位符。

    表格是原子单元（宁可整块超出也不从中间切断）：占位符独立成行参与常规切分，
    绝不会被句子切分 / 字符滑窗从标签中间打断；切分完成后还原，分段内完整保留
    表格结构，前端按 markdown/HTML 渲染为表格。
    表注（下标）吸附：占位后的首个非空行若为表注（注：/备注/说明/表 N 等短行），
    并入表格块——下标随表格同分段、位于分段结尾。"""
    tables: list[str] = []

    def _keep(block: str) -> str:
        tables.append(block.strip())
        return f"\n\n\x00tbl{len(tables) - 1}\x00\n\n"

    text = _HTML_TABLE_RE.sub(lambda m: _keep(m.group(0)), text)
    out: list[str] = []
    pipe: list[str] = []
    for line in text.split("\n"):
        if _TABLE_ROW_RE.match(line):
            pipe.append(line)
            continue
        if pipe:
            out.append(_keep("\n".join(pipe)))
            pipe = []
        out.append(line)
    if pipe:
        out.append(_keep("\n".join(pipe)))
    # 表注吸附：占位行后的首个非空行若为表注，并入表格块（还原时随表格输出）
    merged: list[str] = []
    i = 0
    while i < len(out):
        m = _TABLE_PH_RE.match(out[i].strip())
        if m:
            j = i + 1
            while j < len(out) and not out[j].strip():
                j += 1
            if j < len(out) and _is_table_note(out[j]):
                tables[int(m.group(1))] += "\n" + out[j].strip()
                merged.append(out[i])
                i = j + 1
                continue
        merged.append(out[i])
        i += 1
    return "\n".join(merged), tables


def _restore_tables(text: str, tables: list[str]) -> str:
    return re.sub(r"\x00tbl(\d+)\x00", lambda m: tables[int(m.group(1))], text)


def _split_units_on_tables(units: list[str]) -> list[str]:
    """单元流按表格占位再拆：占位独立成单元——delimiter 不含换行时占位可能与邻文
    同句，拆开才能保证「表格结束后必须分段」。"""
    out: list[str] = []
    for u in units:
        if _TABLE_PH_ANY_RE.search(u):
            out.extend(p for p in re.split(r"(\x00tbl\d+\x00)", u) if p)
        else:
            out.append(u)
    return out


def est_tokens(text: str) -> int:
    """token 估算：非 ASCII ≈ 1 token/字，ASCII ≈ 1 token/4 字符。"""
    if not text:
        return 0
    ascii_len = sum(1 for ch in text if ord(ch) < 128)
    return (len(text) - ascii_len) + (ascii_len + 3) // 4


def _split_sentences(text: str, delimiters: str) -> list[str]:
    """按分隔符集合切句，分隔符保留在句尾；空句丢弃。"""
    if not delimiters:
        return [text] if text.strip() else []
    parts, buf = [], []
    for ch in text:
        buf.append(ch)
        if ch in delimiters:
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _split_sections(text: str) -> list[dict]:
    """① 结构切分：按 markdown 标题树切节（fence 感知，代码块内的 # 不算标题）。

    返回 [{"path": [一级标题, ..., 当前标题], "text": 节正文（含标题行）}]，
    首个标题之前的前言作为 path 为空的首节；全文无标题时返回单个 path 为空的节。
    """
    sections: list[dict] = []
    stack: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_path: list[str] = []
    in_fence = False

    def flush():
        body = "\n".join(cur).strip()
        if body:
            sections.append({"path": cur_path, "text": body})

    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            flush()
            cur = [line]
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            cur_path = [t for _, t in stack]
        else:
            cur.append(line)
    flush()
    return sections


def _top_blocks(text: str) -> list[str]:
    """顶层块切分：空行分段，围栏代码块整体为一块（围栏内允许空行）。"""
    blocks, cur, in_fence = [], [], False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


def _atomic_lines(text: str) -> list[str]:
    """原子行切分：连续表格行聚为一个单元，其余逐行（图片/公式行天然单行）。"""
    units, table = [], []
    for line in text.split("\n"):
        if _TABLE_ROW_RE.match(line):
            table.append(line)
            continue
        if table:
            units.append("\n".join(table))
            table = []
        if line.strip():
            units.append(line)
    if table:
        units.append("\n".join(table))
    return units


def _sliding_window(text: str, limit: int) -> list[str]:
    """字符滑窗兜底：无可用边界时硬切，重叠 limit/8 保上下文连续。
    est_tokens 对 CJK ≈ 1 token/字、ASCII ≈ 1/4，故 limit 字符窗口必不超 token 上限。"""
    overlap = max(8, limit // 8)
    step = max(1, limit - overlap)
    out = []
    for start in range(0, len(text), step):
        piece = text[start:start + limit].strip()
        if piece:
            out.append(piece)
    return out


def _tail_units(units: list[str], overlap: int) -> list[str]:
    """取 units 尾部总 Token ≤ overlap 的最长后缀（按完整单元携带，0 则为空）。
    表格占位不进重叠尾：还原后的整表远超 overlap 预算，避免相邻分段重复整表。"""
    if overlap <= 0:
        return []
    tail, total = [], 0
    for u in reversed(units):
        if _TABLE_PH_RE.match(u.strip()):
            break
        t = est_tokens(u)
        if total + t > overlap:
            break
        tail.insert(0, u)
        total += t
    return tail


def _pack(units: list[str], joiner: str, limit: int, split_deeper, overlap: int = 0) -> list[str]:
    """滚动聚合 ≤ limit；单个超长单元交下一级拆分；闭合分段时携带尾部 overlap 重叠。
    表格占位单元之后强制分段（表格作为分段结尾）；表格占位不进重叠尾（整表不跨分段重复）。"""
    units = _split_units_on_tables(units)
    out: list[str] = []
    cur: list[str] = []

    def flush() -> list[str]:
        nonlocal cur
        if cur:
            out.append(joiner.join(cur).strip())
        return _tail_units(cur, overlap)

    for u in units:
        if est_tokens(u) > limit:
            cur = flush()
            out.extend(split_deeper(u))
            continue
        if cur and est_tokens(joiner.join(cur)) + est_tokens(u) > limit:
            tail = flush()
            if tail and est_tokens(joiner.join(tail)) + est_tokens(u) > limit:
                tail = []  # 重叠尾带新单元已超限：放弃本次重叠，避免尾部重复成块
            cur = tail
        cur.append(u)
        if _TABLE_PH_RE.match(u.strip()):
            cur = flush()  # 表格结束强制分段：_tail_units 遇占位即断，重叠尾为空，下一分段不回带表格
    if cur:
        out.append(joiner.join(cur).strip())
    return out


def _rec_split(text: str, limit: int, delimiters: str, level: int = 0, overlap: int = 0) -> list[str]:
    """② 递归长度修正：段落 → 原子行 → 句子 → 滑窗，逐级兜底直到 ≤ limit。"""
    text = text.strip()
    if not text:
        return []
    # 含表格占位时不整块直返：占位视角 est 极小（还原后远超），且需拆块实现「表格后强制分段」
    if est_tokens(text) <= limit and not _TABLE_PH_ANY_RE.search(text):
        return [text]
    levels = (
        lambda t: (_top_blocks(t), "\n\n"),
        lambda t: (_atomic_lines(t), "\n"),
        lambda t: (_split_sentences(t, delimiters), ""),
    )
    if level < len(levels):
        units, joiner = levels[level](text)
        if len(units) > 1:
            return _pack(units, joiner, limit,
                         lambda t: _rec_split(t, limit, delimiters, level + 1, overlap), overlap)
        return _rec_split(text, limit, delimiters, level + 1, overlap)
    return _sliding_window(text, limit)


def _merge_small(pieces: list[str], min_tokens: int, limit: int) -> list[str]:
    """过短邻块（< min_tokens）向前合并，合并后仍不超 limit；表格块之后不合并（强制分段）。"""
    out: list[str] = []
    for p in pieces:
        if out and est_tokens(out[-1]) < min_tokens \
                and not _TABLE_PH_ANY_RE.search(out[-1]) \
                and est_tokens(out[-1]) + est_tokens(p) <= limit:
            out[-1] = out[-1] + "\n\n" + p
        else:
            out.append(p)
    return out


def _ngrams(text: str) -> dict:
    """字符 1/2-gram 词频向量（CJK 无需分词）。"""
    chars = [c for c in text if not c.isspace()]
    counts: dict[str, int] = {}
    for n in (1, 2):
        for i in range(len(chars) - n + 1):
            g = "".join(chars[i:i + n])
            counts[g] = counts.get(g, 0) + 1
    return counts


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[g] * b[g] for g in a.keys() & b.keys())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _texttile_segments(text: str, delimiters: str, block: int = 2) -> list[str]:
    """③ 统计兜底（TextTiling-lite）：滑动窗口间隙评分——每个句子间隙取左右各 block 句
    的 1/2-gram 余弦相似度（避免固定分块骑跨边界）。含 ≥2 个自然段时仅以段落边界为候选
    （作者常用分段表达话题切换，同时抑制段内噪声谷值），否则全部间隙候选。
    谷值深度 ≥ max(μ+σ/2, 0.1)、间隙相似度 ≤ 0.5（绝对上限，防止高相似均匀文本因相对
    波动被误切）且为局部极小处判为主题边界；均匀文本不切，交长度修正层处理。"""
    sentences: list[str] = []
    para_gaps: set[int] = set()
    for line in text.split("\n"):
        if not line.strip():
            continue
        sentences.extend(_split_sentences(line, delimiters))
        para_gaps.add(len(sentences) - 1)
    para_gaps.discard(len(sentences) - 1)  # 文末不是间隙
    if len(sentences) < block * 3:
        return [text]
    scores = []
    for gap in range(len(sentences) - 1):
        left = "".join(sentences[max(0, gap - block + 1):gap + 1])
        right = "".join(sentences[gap + 1:gap + 1 + block])
        scores.append(_cosine(_ngrams(left), _ngrams(right)))
    if not scores:
        return [text]
    candidates = sorted(para_gaps) if para_gaps else list(range(len(scores)))
    depths: dict[int, float] = {}
    for i in candidates:
        s = scores[i]
        lp = rp = s
        j = i - 1
        while j >= 0 and scores[j] > lp:
            lp = scores[j]
            j -= 1
        j = i + 1
        while j < len(scores) and scores[j] > rp:
            rp = scores[j]
            j += 1
        depths[i] = (lp - s) + (rp - s)
    # 候选 ≥4 时统计阈值（μ+σ/2）才有意义；候选过少时 μ+σ/2 会在两个等大深谷之间
    # 取中间值而漏切，退化为仅用绝对下限 0.1。
    if len(depths) >= 4:
        mean = sum(depths.values()) / len(depths)
        std = math.sqrt(sum((d - mean) ** 2 for d in depths.values()) / len(depths))
        threshold = max(mean + std / 2, 0.1)
    else:
        threshold = 0.1
    cuts = []
    for i, d in depths.items():
        if d < threshold or scores[i] > 0.5:
            continue
        left_ok = i == 0 or scores[i] <= scores[i - 1]
        right_ok = i + 1 == len(scores) or scores[i] <= scores[i + 1]
        if left_ok and right_ok:
            cuts.append(i)
    bounds = [0] + [i + 1 for i in sorted(cuts)] + [len(sentences)]
    segments = ["".join(sentences[a:b]).strip() for a, b in zip(bounds, bounds[1:])]
    return [s for s in segments if s] or [text]


def _auto_chunk(text: str, delimiters: str, limit: int, overlap: int = 0) -> list[str]:
    """【自动】分层瀑布：结构切分 → 递归长度修正 → 统计兜底。"""
    sections = _split_sections(text)
    if not any(s["path"] for s in sections):
        sections = [{"path": [], "text": seg}
                    for seg in _texttile_segments(text, delimiters)]
    min_tokens = max(32, limit // 4)
    chunks: list[str] = []
    for sec in sections:
        pieces = _merge_small(_rec_split(sec["text"], limit, delimiters, overlap=overlap), min_tokens, limit)
        path_line = " > ".join(sec["path"])
        for i, piece in enumerate(pieces):
            if i > 0 and path_line:
                piece = f"{path_line}\n{piece}"
            chunks.append(piece)
    return chunks


def chunk_markdown(md: str, cfg: dict) -> list[dict]:
    """按分段规则切块。返回 [{"content": str, "children": [str] | None}]，
    顺序即 position；children 仅在父子分段启用且子块多于 1 个时给出。"""
    text = _pre_process(clean_markdown(md), cfg)
    if not text:
        return []
    # 表格原子化：先摘出占位参与切分（任何策略都不会从表格中间打断），完成后还原
    text, tables = _extract_tables(text)
    method = str(cfg.get("chunk_method") or "naive").lower()
    if method == "one":
        return [{"content": _restore_tables(text, tables), "children": None}]

    delimiters = cfg.get("delimiter") or "\n。！？；"
    limit = max(1, int(cfg.get("chunk_token_num") or 512))
    # 分段重叠度：缺省 25（与 Pydantic/前端默认一致），并夹取到 limit-1 以内
    raw_overlap = cfg.get("overlap")
    overlap = min(max(0, int(raw_overlap)), limit - 1) if raw_overlap is not None else min(25, limit - 1)
    if method == "auto":
        chunks = _auto_chunk(text, delimiters, limit, overlap)
    else:
        chunks = []
        cur: list[str] = []
        # 句子流按表格占位拆开（delimiter 不含换行时占位与邻文同句），保证表格后强制分段
        for sentence in _split_units_on_tables(_split_sentences(text, delimiters)):
            if cur and est_tokens("".join(cur)) + est_tokens(sentence) > limit:
                chunks.append("".join(cur).strip())
                tail = _tail_units(cur, overlap)
                if tail and est_tokens("".join(tail)) + est_tokens(sentence) > limit:
                    tail = []  # 重叠尾带新句子已超限：放弃本次重叠，避免尾部重复成块
                cur = tail
            cur.append(sentence)
            if _TABLE_PH_RE.match(sentence.strip()):
                chunks.append("".join(cur).strip())
                cur = []  # 表格结束强制分段：下一分段不带表格内容、不带重叠尾
        if cur:
            chunks.append("".join(cur).strip())

    enable_children = bool(cfg.get("enable_children")) and method == "naive"
    child_delim = cfg.get("children_delimiter") or "\n"
    result = []
    for chunk in chunks:
        children = None
        if enable_children:
            # 先按子分隔符切、再逐块还原：占位符独立成行，子块切割不会破坏表格
            pieces = [p.strip() for p in chunk.split(child_delim) if p.strip()]
            if len(pieces) > 1:
                children = [_restore_tables(p, tables) for p in pieces]
        result.append({"content": _restore_tables(chunk, tables), "children": children})
    return result
