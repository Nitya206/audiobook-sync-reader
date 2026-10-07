#!/usr/bin/env python3
"""
audiobook_sync.py: EPUB + audio -> synced reader HTML

  audiobook_sync.py --epub BOOK.epub --audio AUDIO --template reader.html --out OUT.html

  --epub       one or more EPUB files
  --audio      one audio file, several files, or a folder of files
               (several files are joined into one, because the reader plays one file)
  --words      reuse an existing Parakeet JSON and skip transcription
               (add --audio FILE to still name the audio the reader loads)
  --chapters   chapter range such as 219-250 (default: detected from the audio)
  --skin       reader look: shadow (default) or meridian
  --title, --subtitle, --author   shown in the reader (default: from the EPUB)
  --book-id    key for saved progress (default: derived from --out)
  --no-repair  skip the second transcription pass over stretches Parakeet dropped

Transcription runs macparakeet-cli (Parakeet), which gives a start and end time
for every word. Work files and review.txt go to .sync/<out name>/ next to OUT.
The last line printed is DONE or FAILED. Needs Python 3.8+, no extra packages.
"""

import argparse
import bisect
import difflib
import html as htmllib
import json
import os
import posixpath
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import unquote

AUDIO_EXT = ('.mp3', '.m4a', '.m4b', '.wav', '.flac', '.ogg', '.opus', '.aac', '.mp4', '.webm')
PARAKEET_EXT = ('.avi', '.flac', '.m4a', '.mkv', '.mov', '.mp3', '.mp4', '.ogg', '.opus', '.wav', '.webm')


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log(f'FAILED: {msg}')
    sys.exit(1)


# ───────────────────────────── EPUB ─────────────────────────────

BLOCK_TAGS = {
    'p', 'div', 'li', 'ul', 'ol', 'blockquote', 'section', 'article', 'body', 'pre',
    'table', 'tr', 'td', 'th', 'header', 'footer', 'center', 'figure', 'figcaption',
}
HEAD_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
SKIP_TAGS = {'script', 'style', 'head', 'nav', 'svg'}


class BlockParser(HTMLParser):
    """
    Flattens an XHTML document into blocks in reading order:
    {'kind': 'h' | 'p', 'text', 'html', 'cls'}. `html` keeps italics and small
    caps and is None when the block has neither. `cls` is the element's class.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.parts = []     # ('t', text) or ('<', markup)
        self.inline = []    # open inline tags: (name, opening markup, closing markup)
        self.cls = ['']
        self.skip = 0
        self.in_head = False

    def flush(self):
        parts = self.parts + [('<', close) for _, _, close in reversed(self.inline) if close]
        self.parts = [('<', opening) for _, opening, _ in self.inline if opening]
        text, rich = [], []
        started = pending_space = False
        for kind, value in parts:
            if kind == '<':
                rich.append(value)
                continue
            for ch in value:
                if ch.isspace():
                    pending_space = started
                    continue
                if pending_space:
                    text.append(' ')
                    rich.append(' ')
                    pending_space = False
                text.append(ch)
                rich.append(htmllib.escape(ch, quote=False))
                started = True
        if not text:
            return
        markup = ''.join(rich)
        while True:
            trimmed = re.sub(r'<(?:em|span class="sc")>\s*</(?:em|span)>', '', markup)
            if trimmed == markup:
                break
            markup = trimmed
        self.blocks.append({
            'kind': 'h' if self.in_head else 'p',
            'text': ''.join(text),
            'html': markup if re.search(r'<(em|span)', markup) else None,
            'cls': self.cls[-1],
        })

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        cls = dict(attrs).get('class') or ''
        if tag == 'br':
            self.flush()
        elif tag == 'hr':
            self.flush()
            self.blocks.append({'kind': 'p', 'text': '***', 'html': None, 'cls': ''})
        elif tag in HEAD_TAGS:
            self.flush()
            self.in_head = True
            self.cls.append(cls)
        elif tag in BLOCK_TAGS:
            self.flush()
            self.cls.append(cls)
        elif tag in ('em', 'i'):
            self.inline.append((tag, '<em>', '</em>'))
            self.parts.append(('<', '<em>'))
        elif tag == 'span':
            small_caps = bool(re.search(r'\b(sc|smallcaps?|small-caps)\b', cls))
            opening = '<span class="sc">' if small_caps else ''
            self.inline.append((tag, opening, '</span>' if small_caps else ''))
            if opening:
                self.parts.append(('<', opening))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in HEAD_TAGS:
            self.flush()
            self.in_head = False
            if len(self.cls) > 1:
                self.cls.pop()
        elif tag in BLOCK_TAGS:
            self.flush()
            if len(self.cls) > 1:
                self.cls.pop()
        elif tag in ('em', 'i', 'span'):
            for k in range(len(self.inline) - 1, -1, -1):
                if self.inline[k][0] == tag:
                    closing = self.inline.pop(k)[2]
                    if closing:
                        self.parts.append(('<', closing))
                    break

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(('t', data))


def epub_documents(path):
    """Returns (metadata, [(toc label, blocks)]) for an EPUB in spine order."""
    z = zipfile.ZipFile(path)
    have = set(z.namelist())
    meta, names, labels = {}, [], {}
    try:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        opf_path = container.find('.//{*}rootfile').get('full-path')
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)

        def resolve(href, relative_to=base):
            return posixpath.normpath(posixpath.join(relative_to, unquote(href.split('#')[0])))

        for key in ('title', 'creator'):
            node = opf.find('.//{*}metadata/{*}' + key)
            if node is not None and node.text:
                meta[key] = node.text.strip()
        items = {i.get('id'): i for i in opf.iterfind('.//{*}manifest/{*}item')}
        for ref in opf.iterfind('.//{*}spine/{*}itemref'):
            item = items.get(ref.get('idref'))
            if item is not None:
                names.append(resolve(item.get('href')))

        # Table-of-contents labels tell front matter and back matter apart
        for item in items.values():
            if item.get('media-type') == 'application/x-dtbncx+xml':
                ncx_path = resolve(item.get('href'))
                ncx = ET.fromstring(z.read(ncx_path))
                for point in ncx.iterfind('.//{*}navPoint'):
                    text = point.find('./{*}navLabel/{*}text')
                    content = point.find('./{*}content')
                    if text is not None and content is not None and text.text:
                        target = resolve(content.get('src'), posixpath.dirname(ncx_path))
                        labels.setdefault(target, text.text.strip())
    except Exception:
        pass
    names = [n for n in names if n in have]
    if not names:
        names = sorted(n for n in have if n.lower().endswith(('.xhtml', '.html', '.htm')))

    docs = []
    for name in names:
        parser = BlockParser()
        parser.feed(z.read(name).decode('utf-8', 'replace'))
        parser.close()
        parser.flush()
        docs.append((labels.get(name, ''), parser.blocks))
    return meta, docs


HEAD_CHAPTER = re.compile(r'^chapter\s+(\d+|[IVXLCDM]+)\b\s*[:.\-–—]*\s*(.*)$', re.I)
HEAD_BARE = re.compile(r'^(\d+|[IVXLCDM]+)\.?$')
HEAD_NAMED = re.compile(r'^(prologue|epilogue|interlude|afterword|coda)\b', re.I)
HEAD_EXTRA = re.compile(r"^(introduction|foreword|preface|author.s note)\b", re.I)
ROMAN = re.compile(r'^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$')

# Scraper leftovers that are not story text
JUNK_RE = re.compile(r'\[?\s*do you want to read more chapters\s*\?*\s*\]?', re.I)

# Look-alike letters that scrapers swap in for Latin ones
HOMOGLYPHS = str.maketrans({
    'а': 'a', 'с': 'c', 'е': 'e', 'о': 'o', 'р': 'p', 'х': 'x', 'у': 'y', 'і': 'i',
    'ј': 'j', 'ѕ': 's', 'һ': 'h', 'ԁ': 'd', 'ԛ': 'q', 'ԝ': 'w', 'ո': 'n', 'ս': 'u',
    'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'Н': 'H', 'К': 'K', 'М': 'M', 'О': 'O',
    'Р': 'P', 'Т': 'T', 'Х': 'X', 'У': 'Y', 'І': 'I', 'Ј': 'J', 'Ѕ': 'S',
    'ο': 'o', 'ν': 'v', 'α': 'a', 'ι': 'i', 'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Η': 'H',
    'Ι': 'I', 'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T', 'Χ': 'X',
})


def roman_to_int(text):
    values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    total = 0
    for a, b in zip(text, text[1:] + ' '):
        total += -values[a] if values.get(b, 0) > values[a] else values[a]
    return total


def parse_numeral(text):
    """'219' -> 219, 'XIV' -> 14, anything else -> None."""
    if text.isdigit():
        return int(text)
    if text and text == text.upper() and ROMAN.match(text):
        return roman_to_int(text)
    return None


def number_words(n):
    """12 -> ['twelve'], 123 -> ['one', 'hundred', 'twenty', 'three']."""
    ones = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen '
            'fifteen sixteen seventeen eighteen nineteen').split()
    tens = 'x x twenty thirty forty fifty sixty seventy eighty ninety'.split()
    out = []
    if n >= 1000:
        out += number_words(n // 1000) + ['thousand']
        n %= 1000
    if n >= 100:
        out += [ones[n // 100], 'hundred']
        n %= 100
    if n >= 20:
        out.append(tens[n // 10])
        n %= 10
        if n:
            out.append(ones[n])
    elif n or not out:
        out.append(ones[n])
    return out


def parse_heading(text):
    """Recognises a section heading. Returns a new section dict or None."""
    if len(text) > 140:
        return None
    m = HEAD_CHAPTER.match(text)
    if m and parse_numeral(m.group(1).upper()) is not None:
        numeral = m.group(1).upper()
        return {'kind': 'chapter', 'num': parse_numeral(numeral), 'numeral': numeral,
                'sub': m.group(2).strip(), 'bare': False}
    m = HEAD_BARE.match(text)
    if m and parse_numeral(m.group(1)) is not None:
        return {'kind': 'chapter', 'num': parse_numeral(m.group(1)), 'numeral': m.group(1),
                'sub': '', 'bare': True}
    if HEAD_NAMED.match(text):
        return {'kind': 'named', 'num': None, 'name': text}
    if HEAD_EXTRA.match(text):
        return {'kind': 'extra', 'num': None, 'name': text}
    return None


def _is_latin(ch):
    return unicodedata.name(ch, '').startswith('LATIN')


def fix_accents(text):
    """Joins accents that were split from their letter: 'pin˜ on' -> 'piñon', 'Go´mez' -> 'Gómez'."""
    text = re.sub(r'([A-Za-z])˜ ?', lambda m: unicodedata.normalize('NFC', m.group(1) + '̃'), text)
    return re.sub(r'([A-Za-z])´', lambda m: unicodedata.normalize('NFC', m.group(1) + '́'), text)


def clean_text(text, notes):
    """Removes scraper junk, repairs look-alike letters, drops watermark tokens."""
    fixed = fix_accents(text)
    if fixed != text:
        notes.append(('accent joined', ', '.join(sorted(set(fixed.split()) - set(text.split()))) or 'one word'))
        text = fixed
    if JUNK_RE.search(text):
        notes.append(('junk removed', JUNK_RE.search(text).group().strip()))
        text = JUNK_RE.sub(' ', text)

    words = []
    for tok in text.split():
        letters = [c for c in tok if c.isalpha()]
        if letters and not all(_is_latin(c) for c in letters) and any(_is_latin(c) for c in letters):
            repaired = tok.translate(HOMOGLYPHS)
            if all(_is_latin(c) for c in repaired if c.isalpha()):
                notes.append(('look-alike letters fixed', f'{tok} -> {repaired}'))
                tok = repaired
            else:
                notes.append(('watermark removed', tok))
                continue
        words.append(tok)
    return ' '.join(words)


def fix_title(sub, notes):
    """Repairs common scrape damage in a chapter subtitle."""
    fixed = re.sub(r',(?=\S)', ', ', sub)
    # Lowercase L standing in for capital I at the start of a word (lnto, lrrefutable)
    fixed = re.sub(r'\bl(?=[bcdfghjkmnpqrstvwxz])', 'I', fixed)
    fixed = re.sub(r'\s+', ' ', fixed).strip()
    if fixed != sub:
        notes.append(('title fixed', f'{sub} -> {fixed}'))
    return fixed


def is_scene_break(text, cls=''):
    bare = text.replace(' ', '')
    if not bare or re.search(r'\w', bare):
        return False
    if 'break' in cls or bare == '⁂':
        return True
    return len(bare) >= 3 and not re.search(r'[^*\-_~•⁂#=]', bare)


def plain_of(markup):
    return htmllib.unescape(re.sub(r'<[^>]+>', '', markup))


def extract_sections(epub_paths):
    """
    Returns (metadata, sections). A section is a chapter, a named part such as an
    epilogue, or an epigraph page:
    {'kind', 'num', 'title', 'label', 'synopsis', 'paragraphs': [{'text', 'html', 'cls'}], 'notes'}
    """
    meta, docs = {}, []
    for path in epub_paths:
        m, d = epub_documents(path)
        meta = meta or m
        docs.extend(d)

    def heading_of(block, want_kind):
        return parse_heading(block['text']) if block['kind'] == want_kind else None

    # Headings are trusted first. Plain paragraphs count only when no heading matches.
    all_blocks = [b for _, blocks in docs for b in blocks]
    want_kind = 'h' if any((heading_of(b, 'h') or {}).get('kind') == 'chapter' for b in all_blocks) else 'p'

    sections = []
    index = {}
    cur = None

    def start(sec):
        nonlocal cur
        sec.update(blocks=[], synopsis=None)
        key = ('n', sec['num']) if sec['num'] is not None else ('s', sec.get('name', sec['kind']).lower())
        prev = index.get(key)
        # A section listed twice (table of contents, repeated title) keeps the fuller copy
        if prev is not None and prev['blocks']:
            cur = None
            return
        if prev is not None:
            if len(prev.get('sub', '')) > len(sec.get('sub', '')):
                sec['sub'] = prev['sub']
            sections[sections.index(prev)] = sec
        else:
            sections.append(sec)
        index[key] = sec
        cur = sec

    for label, blocks in docs:
        if label:
            # A new document with its own contents entry ends whatever came before
            known = parse_heading(label) or re.search(r'epigraph', label, re.I)
            if cur is not None and not known:
                cur = None
            if re.search(r'epigraph', label, re.I):
                start({'kind': 'epigraph', 'num': None, 'name': 'Epigraph'})
        elif cur is not None and cur['kind'] == 'epigraph':
            cur = None
        for block in blocks:
            head = heading_of(block, want_kind)
            if head:
                start(head)
                continue
            if cur is None:
                continue
            if block['kind'] == 'h' and want_kind == 'h':
                cur = None   # a heading that is not a section ends the section
                continue
            if cur['kind'] == 'chapter' and not cur['blocks'] and cur['synopsis'] is None \
                    and 'center' in block['cls'] and not is_scene_break(block['text'], block['cls']):
                cur['synopsis'] = block['text']
                continue
            cur['blocks'].append(block)

    out = []
    for sec in sections:
        notes = []
        paras = []
        for block in sec['blocks']:
            text, cls = block['text'], block['cls']
            if is_scene_break(text, cls):
                if paras and paras[-1]['text'] != '***':
                    paras.append({'text': '***'})
                continue
            cleaned = clean_text(text, notes)
            m = HEAD_CHAPTER.match(cleaned)
            if not paras and sec['kind'] == 'chapter' and m and parse_numeral(m.group(1)) == sec['num'] \
                    and len(cleaned) <= 140:
                continue   # title repeated as the first line of the body
            if not cleaned or not re.search(r'\w', cleaned):
                continue
            if 'break' in cls and paras and paras[-1]['text'] != '***':
                paras.append({'text': '***'})   # a spaced-out paragraph marks a scene break
            para = {'text': cleaned}
            markup = fix_accents(block['html']) if block['html'] else None
            if markup and plain_of(markup) == cleaned:
                para['html'] = markup
            role = 'right' if 'right' in cls else 'center' if 'center' in cls else None
            if role:
                para['cls'] = role
            paras.append(para)
        while paras and paras[-1]['text'] == '***':
            paras.pop()
        if not paras:
            continue

        if sec['kind'] == 'chapter':
            if sec['bare']:
                title, label = sec['numeral'], f'Chapter {sec["numeral"]}'
            else:
                sub = fix_title(sec['sub'], notes)
                title = f'Chapter {sec["numeral"]}: {sub}' if sub else f'Chapter {sec["numeral"]}'
                label = None
            spoken = ['chapter'] + number_words(sec['num']) if sec['bare'] or not sec['numeral'].isdigit() else None
        else:
            title, label, spoken = sec['name'], None, None
        out.append({
            'kind': sec['kind'], 'num': sec['num'], 'title': title, 'label': label,
            'synopsis': clean_text(sec['synopsis'], notes) if sec['synopsis'] else None,
            'spoken_title': spoken, 'paragraphs': paras, 'notes': notes,
        })

    nums = [s['num'] for s in out]
    if all(n is not None for n in nums) and nums != sorted(nums):
        out.sort(key=lambda s: s['num'])
    return meta, out


def section_name(sec):
    return sec['label'] or sec['title']


# ───────────────────────────── Tokens ─────────────────────────────

_SEPARATORS = re.compile(r'[\-‐-―/…]|\.{2,}')


def norm(word):
    word = unicodedata.normalize('NFKD', word).lower()
    return re.sub(r'[^a-z0-9]', '', word)


def tokenize(text):
    """Yields (char_index, normalized_word). Dashes and ellipses split words."""
    masked = _SEPARATORS.sub(lambda m: ' ' * len(m.group()), text)
    for m in re.finditer(r'\S+', masked):
        n = norm(m.group())
        if n:
            yield m.start(), n


class Novel:
    """Flat token list over all sections. pi is -1 for the words of a section's heading."""

    def __init__(self, sections):
        self.norm, self.ci, self.pi, self.char = [], [], [], []
        self.para_range = {}   # (ci, pi) -> (first token, one past last token)
        for ci, sec in enumerate(sections):
            start = len(self.norm)
            # The heading as a narrator would say it, then the synopsis line if there is one
            for word in sec['spoken_title'] or [n for _, n in tokenize(sec['title'])]:
                self._push(ci, -1, 0, word)
            for _, n in tokenize(sec['synopsis'] or ''):
                self._push(ci, -1, 0, n)
            self.para_range[(ci, -1)] = (start, len(self.norm))
            for pi, para in enumerate(sec['paragraphs']):
                start = len(self.norm)
                if para['text'] != '***':
                    for char_idx, n in tokenize(para['text']):
                        self._push(ci, pi, char_idx, n)
                self.para_range[(ci, pi)] = (start, len(self.norm))

    def _push(self, ci, pi, char_idx, n):
        self.norm.append(n)
        self.ci.append(ci)
        self.pi.append(pi)
        self.char.append(char_idx)


def read_words(path):
    """Parakeet JSON -> [(word, start seconds, end seconds)]."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get('ok') is False:
        fail(f'parakeet reported an error: {data.get("error")}')
    if isinstance(data, list):
        return [tuple(w) for w in data]   # a transcript this script saved after its repair pass
    return [(w['word'], w['startMs'] / 1000, w['endMs'] / 1000) for w in data.get('wordTimestamps') or []]


def spoken_tokens(words):
    """[(word, start, end)] -> (norms, starts, ends), one entry per spoken word part."""
    norms, starts, ends = [], [], []
    for word, start, end in words:
        parts = [n for _, n in tokenize(word)]
        total = sum(len(p) for p in parts)
        # Parakeet stretches a word's end to the start of the next word it heard, however
        # far away that is. A word is held to the time it can take to say.
        end = min(end, start + 0.35 + 0.11 * total)
        t = start
        for p in parts:
            dur = (end - start) * len(p) / total
            norms.append(p)
            starts.append(t)
            ends.append(t + dur)
            t += dur
    return norms, starts, ends


# ───────────────────────────── Alignment ─────────────────────────────

LEAF_CELLS = 1_000_000


def _longest_increasing(pairs):
    """pairs sorted by first item -> longest subset whose second items also increase."""
    tails, tails_at, prev = [], [], [-1] * len(pairs)
    for i, (_, t) in enumerate(pairs):
        j = bisect.bisect_left(tails, t)
        if j == len(tails):
            tails.append(t)
            tails_at.append(i)
        else:
            tails[j] = t
            tails_at[j] = i
        prev[i] = tails_at[j - 1] if j else -1
    out = []
    i = tails_at[-1] if tails_at else -1
    while i >= 0:
        out.append(pairs[i])
        i = prev[i]
    return out[::-1]


def _anchors(N, T, n0, n1, t0, t1, k):
    """k-word runs that occur exactly once in each range, kept in consistent order."""
    in_n = {}
    for i in range(n0, n1 - k + 1):
        g = tuple(N[i:i + k])
        in_n[g] = -1 if g in in_n else i
    in_t = {}
    for j in range(t0, t1 - k + 1):
        g = tuple(T[j:j + k])
        if g in in_n:
            in_t[g] = -1 if g in in_t else j
    pairs = sorted((in_n[g], j) for g, j in in_t.items() if j >= 0 and in_n[g] >= 0)
    return _longest_increasing(pairs)


def align(N, T, kmax=6, coarse=False):
    """
    Returns match[n] = index into T, or -1.

    Unique word runs anchor the two sequences. The stretches between anchors are
    aligned the same way with shorter runs, and small stretches go to difflib.
    coarse=True stops after the first anchor pass.
    """
    match = [-1] * len(N)

    def leaf(n0, n1, t0, t1):
        sm = difflib.SequenceMatcher(None, N[n0:n1], T[t0:t1], autojunk=False)
        for a, b, size in sm.get_matching_blocks():
            for i in range(size):
                match[n0 + a + i] = t0 + b + i

    def rec(n0, n1, t0, t1, k):
        if n0 >= n1 or t0 >= t1:
            return
        cells = (n1 - n0) * (t1 - t0)
        if cells <= LEAF_CELLS and not coarse:
            leaf(n0, n1, t0, t1)
            return
        found = []
        while k >= 1:
            if k == 1 and cells > 40_000_000:
                return  # too large to trust single-word anchors
            found = _anchors(N, T, n0, n1, t0, t1, k)
            if found or coarse:
                break
            k -= 1
        if not found:
            return
        pn, pt = n0, t0
        for ni, ti in found:
            if ni >= pn and ti >= pt:
                if not coarse:
                    rec(pn, ni, pt, ti, k)
                first = 0
            elif ni - pn == ti - pt and ni + k > pn:
                first = pn - ni   # overlaps the previous anchor on the same diagonal
            else:
                continue
            for i in range(first, k):
                match[ni + i] = ti + i
            pn, pt = ni + k, ti + k
        if not coarse:
            rec(pn, n1, pt, t1, k)

    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    rec(0, len(N), 0, len(T), kmax)
    return match


def drop_isolated(match):
    """
    Removes chance hits on common words. A real match has neighbours that are
    matched too, and matched to nearby places in the audio.
    """
    n = len(match)
    keep = list(match)
    for i in range(n):
        t = match[i]
        if t < 0:
            continue
        near = sum(1 for j in range(max(0, i - 3), min(n, i + 4))
                   if match[j] >= 0 and abs(match[j] - t) <= 8)
        if near < 3:
            keep[i] = -1
    return keep


def pair_leftovers(N, T, match):
    """
    Pairs the few words left between two matched neighbours. These are spelling
    variants (gray/grey, nothin/nothing), misheard names, and compounds the
    narrator's words split or join (expriest / ex priest).

    Returns (match, last) where last[n] is the final spoken index of a book word
    that covers two spoken words.
    """
    match = list(match)
    last = {}
    matched = [i for i, m in enumerate(match) if m >= 0]

    def similar(a, b):
        return difflib.SequenceMatcher(None, a, b).ratio()

    for a, b in zip(matched, matched[1:]):
        n0, n1 = a + 1, b               # unmatched book words
        t0, t1 = match[a] + 1, match[b]  # unmatched spoken words
        nn, tn = n1 - n0, t1 - t0
        if nn < 1 or tn < 1 or nn > 12 or tn > 12:
            continue
        if nn == tn and nn <= 2:
            # Same number of words in the same slot: they are the same words
            for k in range(nn):
                match[n0 + k] = t0 + k
            continue

        # Small edit-distance table with join moves
        INF = 1e9
        cost = [[INF] * (tn + 1) for _ in range(nn + 1)]
        move = [[None] * (tn + 1) for _ in range(nn + 1)]
        cost[0][0] = 0.0
        for i in range(nn + 1):
            for j in range(tn + 1):
                c = cost[i][j]
                if c >= INF:
                    continue

                def relax(di, dj, extra, how):
                    if c + extra < cost[i + di][j + dj]:
                        cost[i + di][j + dj] = c + extra
                        move[i + di][j + dj] = (di, dj, how)

                if i < nn:
                    relax(1, 0, 1.0, None)
                if j < tn:
                    relax(0, 1, 1.0, None)
                if i < nn and j < tn:
                    s = similar(N[n0 + i], T[t0 + j])
                    if s >= 0.5:
                        relax(1, 1, 1.0 - s, 'one')
                if i < nn and j + 1 < tn and similar(N[n0 + i], T[t0 + j] + T[t0 + j + 1]) >= 0.85:
                    relax(1, 2, 0.1, 'split')    # one book word, two spoken words
                if i + 1 < nn and j < tn and similar(N[n0 + i] + N[n0 + i + 1], T[t0 + j]) >= 0.85:
                    relax(2, 1, 0.1, 'joined')   # two book words, one spoken word
        i, j = nn, tn
        while move[i][j]:
            di, dj, how = move[i][j]
            i, j = i - di, j - dj
            if how == 'one':
                match[n0 + i] = t0 + j
            elif how == 'split':
                match[n0 + i] = t0 + j
                last[n0 + i] = t0 + j + 1
            elif how == 'joined':
                match[n0 + i] = match[n0 + i + 1] = t0 + j
    return match, last


def release_edges(novel, match, last, starts, ends):
    """
    Undoes matches made across a hole in the transcript.

    Where the transcript misses a sentence, a common word at the edge of the
    missing text ("the", "and") can match the same word on the far side of the
    hole. The sign is a pair of neighbours with far too little time for the book
    words between them, right beside a pair with far too much. The few words in
    between were matched across the hole and are released.
    """
    match = list(match)
    rate = speech_rate(novel, match, starts)
    cum = [0]
    for n in novel.norm:
        cum.append(cum[-1] + len(n) + 1)

    def need(a, b):   # seconds the book words strictly between a and b take to say
        return (cum[b] - cum[a + 1]) * rate

    def gap(a, b):
        return starts[match[b]] - ends[last.get(a, match[a])]

    def bloated(a, b):
        return gap(a, b) > 3.0 + 4 * need(a, b)

    released = 0
    for _ in range(400):
        idx = [i for i, m in enumerate(match) if m >= 0]
        fix = None
        for k in range(len(idx) - 1):
            a, b = idx[k], idx[k + 1]
            if need(a, b) < 1.5 or gap(a, b) >= 0.35 * need(a, b):
                continue
            # Starved pair. Look up to four matched words to either side for the bloated one.
            left = next((w for w in range(1, 5) if k - w >= 0 and bloated(idx[k - w], idx[k - w + 1])), None)
            right = next((w for w in range(1, 5) if k + w + 1 < len(idx) and bloated(idx[k + w], idx[k + w + 1])), None)
            if left and (not right or left <= right):
                fix = idx[k - left + 1:k + 1]
            elif right:
                fix = idx[k + 1:k + right + 1]
            if fix:
                break
        if not fix:
            break
        for i in fix:
            match[i] = -1
            released += 1
    return match, released


def full_align(novel, T, starts, ends):
    match, last = pair_leftovers(novel.norm, T, drop_isolated(align(novel.norm, T)))
    match, _ = release_edges(novel, match, last, starts, ends)
    return match, last


# ───────────────────────────── Timing ─────────────────────────────

MIN_PARA_MATCH = 0.3


def speech_rate(novel, match, starts):
    """Median seconds per character, measured on consecutive matched words."""
    samples = []
    for i in range(len(match) - 1):
        a, b = match[i], match[i + 1]
        if a >= 0 and b == a + 1 and novel.pi[i] == novel.pi[i + 1] and novel.ci[i] == novel.ci[i + 1]:
            d = starts[b] - starts[a]
            if 0.03 < d < 2:
                samples.append(d / (len(novel.norm[i]) + 1))
    return statistics.median(samples) if samples else 0.065


def time_paragraph(novel, match, last, starts, ends, lo, hi, rate):
    """
    Per-word (start, end) for novel tokens lo..hi-1, or None when too little matched.
    Matched words take Parakeet's times. Unmatched words are spaced by their length.
    """
    idx = [i for i in range(lo, hi) if match[i] >= 0]
    count = hi - lo
    units = [len(novel.norm[i]) + 1 for i in range(lo, hi)]
    if not idx or (count >= 6 and len(idx) / count < MIN_PARA_MATCH):
        return None

    s = [None] * count
    e = [None] * count
    for i in idx:
        s[i - lo], e[i - lo] = starts[match[i]], ends[last.get(i, match[i])]

    # Words between two matched words share the time between them
    for a, b in zip(idx, idx[1:]):
        if b - a < 2:
            continue
        t0, t1 = e[a - lo], s[b - lo]
        if t1 < t0:
            t0 = t1 = min(t0, t1)
        span = sum(units[a + 1 - lo:b - lo])
        t = t0
        for j in range(a + 1 - lo, b - lo):
            d = (t1 - t0) * units[j] / span
            s[j], e[j] = t, t + d
            t += d

    # Words before the first match and after the last run at the measured speech rate
    for j in range(idx[0] - lo - 1, -1, -1):
        e[j] = s[j + 1]
        s[j] = max(0.0, e[j] - units[j] * rate)
    for j in range(idx[-1] - lo + 1, count):
        s[j] = e[j - 1]
        e[j] = s[j] + units[j] * rate
    return list(zip(s, e))


def build_sync(sections, novel, match, last, spoken, starts, ends):
    """Returns (SYNC_DATA, announce_times, per-section stats)."""
    rate = speech_rate(novel, match, starts)
    sync = []
    stats = []
    timed = []   # (ci, pi, [(start, end)] per token) for synced paragraphs, in order

    for ci, sec in enumerate(sections):
        paras = []
        total = hit = synced = spoken_paras = 0
        unsynced = []
        for pi, para in enumerate(sec['paragraphs']):
            entry = {'text': para['text'], 'start': None, 'end': None}
            if para.get('html'):
                entry['html'] = para['html']
            if para.get('cls'):
                entry['cls'] = para['cls']
            paras.append(entry)
            if para['text'] == '***':
                continue
            lo, hi = novel.para_range[(ci, pi)]
            total += hi - lo
            hit += sum(1 for i in range(lo, hi) if match[i] >= 0)
            spoken_paras += 1
            times = time_paragraph(novel, match, last, starts, ends, lo, hi, rate) if hi > lo else None
            if times is None:
                unsynced.append(para['text'])
            else:
                synced += 1
                timed.append((ci, pi, times))
        chapter = {'title': sec['title'], 'paragraphs': paras}
        for key in ('label', 'synopsis'):
            if sec.get(key):
                chapter[key] = sec[key]
        if sec['kind'] != 'chapter':
            chapter['kind'] = sec['kind']
        sync.append(chapter)
        stats.append({'total': total, 'hit': hit, 'synced': synced, 'spoken': spoken_paras,
                      'unsynced': unsynced})

    # Paragraph bounds: starts strictly increase, and no paragraph runs into the next
    prev = None
    for ci, pi, times in timed:
        entry = sync[ci]['paragraphs'][pi]
        start, end = times[0][0], times[-1][1]
        if prev is not None:
            start = max(start, prev['start'] + 0.05)
            prev['end'] = round(max(prev['start'] + 0.05, min(prev['end'], start)), 2)
        entry['start'] = round(start, 2)
        entry['end'] = round(max(end, start + 0.05), 2)
        prev = entry

    # charMap holds [character index, seconds] for every word
    for ci, pi, times in timed:
        entry = sync[ci]['paragraphs'][pi]
        lo, _ = novel.para_range[(ci, pi)]
        cmap = []
        last_t = entry['start']
        for k, (s, _) in enumerate(times):
            t = round(min(max(s, last_t), entry['end']), 2)
            last_t = t
            cmap.append([novel.char[lo + k], t])
        cmap[0][1] = entry['start']
        entry['charMap'] = cmap

    announce = announce_times(sections, novel, match, spoken, starts, ends, sync, stats)
    time_openers(sections, novel, match, last, starts, ends, sync, announce, rate)

    # A section's last paragraph stops when the next section is announced
    for ci in range(1, len(sync)):
        tail = next((p for p in reversed(sync[ci - 1]['paragraphs']) if p['start'] is not None), None)
        if tail and tail['end'] > announce[ci] > tail['start']:
            tail['end'] = announce[ci]
            for entry in tail['charMap']:
                entry[1] = min(entry[1], tail['end'])
    return sync, announce, stats


def time_openers(sections, novel, match, last, starts, ends, sync, announce, rate):
    """
    Times the spoken heading and the synopsis line of each section, where the narrator reads them.
    Adds 'head': {start, end} and 'syn': {start, end, charMap} to the section.
    """
    for ci, sec in enumerate(sections):
        first = next((p['start'] for p in sync[ci]['paragraphs'] if p['start'] is not None), None)
        if first is None:
            continue
        lo, hi = novel.para_range[(ci, -1)]
        n_head = len(sec['spoken_title'] or [n for _, n in tokenize(sec['title'])])
        t0, t1 = announce[ci], first

        def timed(a, b):
            """Word times for heading tokens a..b-1 when they sit, in order, inside the opener."""
            if b <= a:
                return None
            times = time_paragraph(novel, match, last, starts, ends, a, b, rate)
            if times is None or times[0][0] < t0 - 0.5 or times[-1][0] > t1:
                return None
            # Snapped word starts can sit a few hundredths out of order. A real misplacement is seconds off.
            if any(nxt[0] < cur[0] - 0.5 for cur, nxt in zip(times, times[1:])):
                return None
            return times

        syn_tokens = list(tokenize(sec['synopsis'] or ''))
        head = timed(lo, min(lo + n_head, hi))
        syn = timed(lo + n_head, hi) if syn_tokens and hi - lo - n_head == len(syn_tokens) else None

        syn_start = None
        if syn is not None:
            syn_start = max(syn[0][0], t0)
            syn_end = max(syn_start + 0.05, min(syn[-1][1], t1))
            cmap, last_t = [], syn_start
            for (char_idx, _), (word_start, _) in zip(syn_tokens, syn):
                last_t = round(min(max(word_start, last_t), syn_end), 2)
                cmap.append([char_idx, last_t])
            sync[ci]['syn'] = {'start': round(syn_start, 2), 'end': round(syn_end, 2), 'charMap': cmap}
        if head is not None:
            head_start = max(head[0][0], t0)
            head_end = min(head[-1][1], syn_start if syn_start is not None else t1)
            if head_end > head_start:
                sync[ci]['head'] = {'start': round(head_start, 2), 'end': round(head_end, 2)}


def announce_times(sections, novel, match, spoken, starts, ends, sync, stats):
    """When the narrator announces each section. Falls back to just before its first line."""
    out = []
    prev_end = 0.0
    for ci in range(len(sections)):
        synced = [p for p in sync[ci]['paragraphs'] if p['start'] is not None]
        first = synced[0]['start'] if synced else None
        t, source = None, 'none'

        lo, hi = novel.para_range[(ci, -1)]
        hits = [match[i] for i in range(lo, hi) if match[i] >= 0]
        window = 30 + 0.6 * (hi - lo)   # a long synopsis line takes a while to read
        if hits and len(hits) * 2 >= hi - lo and (first is None or 0 <= first - starts[hits[0]] <= window):
            t, source = starts[hits[0]], 'heading'

        if t is None and first is not None:
            body = next((match[i] for i in range(hi, len(match))
                         if novel.ci[i] == ci and match[i] >= 0), None)
            if body is not None:
                for j in range(body - 1, max(-1, body - 26), -1):
                    if ends[j] <= prev_end:
                        break
                    if spoken[j] == 'chapter':
                        t, source = starts[j], 'spoken "chapter"'
                        break
        if t is None and first is not None:
            t, source = max(prev_end, first - 5.0), 'estimate'
        if t is None:
            t = prev_end
        if first is not None:
            t = min(t, first)
        t = round(max(t, out[-1] if out else 0.0), 2)
        out.append(t)
        stats[ci]['announce'] = source
        if synced:
            prev_end = synced[-1]['end']
    return out


# ───────────────────────────── Audio ─────────────────────────────

def find_tool(name, extra=()):
    for cand in (shutil.which(name), *extra):
        if cand and os.path.exists(cand):
            return cand
    return None


def ffmpeg_path():
    path = find_tool('ffmpeg', ('/opt/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'))
    if not path:
        fail('ffmpeg not found')
    return path


def parakeet_path():
    path = find_tool('macparakeet-cli', ('/opt/homebrew/bin/macparakeet-cli',))
    if not path:
        fail('macparakeet-cli not found')
    return path


def natural_key(path):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', os.path.basename(path))]


def collect_audio(inputs):
    files = []
    for item in inputs:
        if os.path.isdir(item):
            inside = [os.path.join(item, f) for f in os.listdir(item) if f.lower().endswith(AUDIO_EXT)]
            files.extend(sorted(inside, key=natural_key))
        elif os.path.isfile(item):
            files.append(item)
        else:
            fail(f'audio not found: {item}')
    if not files:
        fail('no audio files found')
    return files


def join_audio(files, dest, work):
    """Joins several audio files into one, because the reader plays a single file."""
    if os.path.exists(dest) and all(os.path.getmtime(dest) >= os.path.getmtime(f) for f in files):
        log(f'joined audio is current: {dest}')
        return
    listing = os.path.join(work, 'concat.txt')
    with open(listing, 'w', encoding='utf-8') as f:
        for path in files:
            escaped = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    log(f'joining {len(files)} audio files -> {dest}')
    tmp = dest + '.part.m4a'
    cmd = [ffmpeg_path(), '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', listing,
           '-vn', '-c:a', 'aac', '-b:a', '64k', '-ac', '1', tmp]
    if subprocess.run(cmd).returncode != 0:
        fail('ffmpeg could not join the audio files')
    os.replace(tmp, dest)


def parakeet_input(audio, work):
    """Parakeet refuses some extensions such as .m4b. A link with a name it accepts is enough."""
    ext = os.path.splitext(audio)[1].lower()
    if ext in PARAKEET_EXT:
        return audio
    link = os.path.join(work, 'audio' + ('.m4a' if ext in ('.m4b', '.aac') else '.mp4'))
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.abspath(audio), link)
    return link


def transcribe(audio, cache, work):
    """Runs Parakeet once and caches the word JSON."""
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(audio) \
            and os.path.getsize(cache) > 1000:
        log(f'transcript is current: {cache}')
        return
    log(f'transcribing with Parakeet: {os.path.basename(audio)}')
    tmp = cache + '.part'
    with open(tmp, 'wb') as out, open(cache + '.log', 'wb') as err:
        code = subprocess.run([parakeet_path(), 'transcribe', parakeet_input(audio, work), '--format', 'json',
                               '--no-history', '--no-diarize'], stdout=out, stderr=err).returncode
    if code != 0:
        fail(f'parakeet exited with {code}, see {cache}.log and {tmp}')
    os.replace(tmp, cache)


# ───────────────────────────── Speech onsets ─────────────────────────────
# Parakeet places the first word after a pause a quarter of a second or more
# before the narrator actually speaks, somewhere inside the silence. The audio
# itself says where each silence ends, so those words are moved to that point.

SILENCE_FILTER = 'silencedetect=noise=-38dB:d=0.25'


def detect_silences(audio, cache):
    """Silent stretches of the audio as [(start, end)], found with ffmpeg and cached."""
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(audio):
        with open(cache, encoding='utf-8') as f:
            return [tuple(x) for x in json.load(f)]
    log('finding the pauses in the audio')
    run = subprocess.run([ffmpeg_path(), '-hide_banner', '-nostats', '-i', audio, '-vn',
                          '-af', SILENCE_FILTER, '-f', 'null', '-'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors='replace')
    starts = [float(x) for x in re.findall(r'silence_start: (-?[\d.]+)', run.stderr)]
    ends = [float(x) for x in re.findall(r'silence_end: (-?[\d.]+)', run.stderr)]
    silences = [(max(0.0, a), b) for a, b in zip(starts, ends) if b > a]
    with open(cache, 'w', encoding='utf-8') as f:
        json.dump(silences, f)
    return silences


def snap_to_speech(words, silences):
    """Moves word starts out of silences, and word ends back to where the silence began."""
    if not silences:
        return words
    begins = [a for a, _ in silences]
    out = []
    for word, start, end in words:
        k = bisect.bisect_right(begins, start) - 1
        if k >= 0 and start < silences[k][1] - 0.08:
            start = silences[k][1] - 0.05          # just before the sound begins
        k = bisect.bisect_right(begins, end) - 1
        if k >= 0 and silences[k][0] + 0.15 < end <= silences[k][1] and silences[k][0] + 0.15 > start:
            end = silences[k][0] + 0.15            # the word was over when the silence began
        out.append((word, start, max(end, start + 0.05)))
    return out


# ───────────────────────────── Repair pass ─────────────────────────────
# Parakeet sometimes returns nothing for half a minute of clear speech. Whether it
# does depends on exactly where the audio it is given starts, so the stretches
# where book words found no spoken words are cut out and transcribed again, and
# each round cuts them at a different point.

REPAIR_MIN_WORDS = 5
REPAIR_MIN_SECS = 1.5
REPAIR_PADS = (1.0, 3.5, 7.0, 2.2, 5.2)   # seconds of lead-in per round
REPAIR_CLIP = 40.0
REPAIR_OVERLAP = 5.0


def find_dropouts(match, starts, ends):
    """Time spans (t0, t1) where a run of book words has no spoken words."""
    spans = []
    prev = None
    for i, m in enumerate(match):
        if m < 0:
            continue
        if prev is not None and i - prev - 1 >= REPAIR_MIN_WORDS:
            t0, t1 = ends[match[prev]], starts[m]
            if t1 - t0 >= REPAIR_MIN_SECS:
                spans.append((t0, t1))
        prev = i
    return spans


def plan_clips(spans, pad):
    """Clips to cut: [(start, length, own_from, own_to)]. Each clip owns part of its span."""
    windows = []
    for t0, t1 in spans:
        a, b = max(0.0, t0 - pad), t1 + 1.0
        if windows and a <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], b)
        else:
            windows.append([a, b])
    clips = []
    for a, b in windows:
        if b - a <= REPAIR_CLIP + REPAIR_OVERLAP:
            clips.append((a, b - a, a, b))
            continue
        step = REPAIR_CLIP - REPAIR_OVERLAP
        t = a
        while t < b:
            end = min(b, t + REPAIR_CLIP)
            own_from = a if t == a else t + REPAIR_OVERLAP / 2
            own_to = b if end == b else end - REPAIR_OVERLAP / 2
            clips.append((t, end - t, own_from, own_to))
            if end == b:
                break
            t += step
    return clips


def merge_repairs(words, spans, clips, clip_words):
    """Replaces whatever sat inside each dropout with the words from its clips."""
    starts_of = [s for s, _ in spans]

    def span_of(t):
        k = bisect.bisect_right(starts_of, t) - 1
        return k if k >= 0 and spans[k][0] <= t <= spans[k][1] else -1

    kept = [w for w in words if span_of((w[1] + w[2]) / 2) < 0]
    added = []
    for (start, _, own_from, own_to), found in zip(clips, clip_words):
        for word, s, e in found:
            s, e = start + s, start + e
            mid = (s + e) / 2
            if own_from <= mid <= own_to and span_of(mid) >= 0:
                added.append((word, s, e))
    return sorted(kept + added, key=lambda w: w[1]), len(added)


def repair_dropouts(audio, words, spans, work, round_no):
    """Cuts the dropout spans out of the audio, transcribes them, merges the words back."""
    clips = plan_clips(spans, REPAIR_PADS[round_no - 1])
    folder = os.path.join(work, f'repair{round_no}')
    out_dir = os.path.join(folder, 'out')
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(out_dir)
    ffmpeg = ffmpeg_path()
    log(f'repair pass {round_no}: {len(spans)} silent stretches, '
        f'{sum(c[1] for c in clips) / 60:.0f} min of audio in {len(clips)} clips')

    def cut(job):
        k, (start, length, _, _) = job
        dest = os.path.join(folder, f'c{k:05d}.wav')
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-ss', f'{start:.3f}', '-t', f'{length:.3f}',
                        '-i', audio, '-vn', '-ac', '1', '-ar', '16000', dest], check=False)
        return dest

    with ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(cut, enumerate(clips)))

    # One Parakeet process for all clips, in batches that fit a command line
    for k in range(0, len(files), 200):
        batch = [f for f in files[k:k + 200] if os.path.exists(f)]
        with open(os.path.join(folder, 'parakeet.log'), 'ab') as err:
            subprocess.run([parakeet_path(), 'transcribe', *batch, '--format', 'json', '--no-history',
                            '--no-diarize', '--output-dir', out_dir], stdout=err, stderr=err)

    clip_words = []
    for f in files:
        result = os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + '.json')
        try:
            clip_words.append(read_words(result))
        except (OSError, ValueError, KeyError):
            clip_words.append([])
    merged, added = merge_repairs(words, spans, clips, clip_words)
    shutil.rmtree(folder, ignore_errors=True)
    log(f'repair pass {round_no}: {added} words recovered')
    return merged


# ───────────────────────────── HTML ─────────────────────────────

def set_json_const(html, name, value_json):
    """Replaces the JSON value of `const NAME = ...` wherever its real end is."""
    m = re.search(r'\bconst\s+%s\s*=\s*' % re.escape(name), html)
    if not m:
        fail(f'template has no "const {name}"')
    _, end = json.JSONDecoder().raw_decode(html, m.end())
    return html[:m.end()] + value_json + html[end:]


def set_string_const(html, name, value):
    m = re.search(r'(\bconst\s+%s\s*=\s*)(\'[^\'\n]*\'|"[^"\n]*")' % re.escape(name), html)
    if not m:
        return html
    return html[:m.start(2)] + json.dumps(value) + html[m.end(2):]


def write_html(template, out, sync, announce, strings, audio=None):
    with open(template, encoding='utf-8') as f:
        html = f.read()
    rows = [json.dumps(ch, ensure_ascii=False, separators=(',', ':')) for ch in sync]
    data = '[\n' + ',\n'.join(rows) + '\n]'
    data = data.replace('</', '<\\/')   # keeps "</script>" in book text from ending the script
    html = set_json_const(html, 'SYNC_DATA', data)
    html = set_json_const(html, 'CHAPTER_ANNOUNCE_TIMES', json.dumps(announce))
    for name, value in strings.items():
        if value is not None:
            html = set_string_const(html, name, value)
    if audio:
        # Audio beside the HTML, or one folder up, loads by itself when the reader opens
        rel = os.path.relpath(os.path.abspath(audio), os.path.dirname(out)).replace(os.sep, '/')
        if rel.count('../') <= 1:
            html = set_string_const(html, 'AUDIO_FILE_PATH', rel)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)


# ───────────────────────────── Review ─────────────────────────────

def clock(t):
    t = int(t)
    return f'{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}'


def write_review(path, sections, sync, announce, stats, notes, n_spoken, duration, problems, extra):
    lines = []
    total = sum(s['total'] for s in stats)
    hit = sum(s['hit'] for s in stats)
    lines.append(f'{section_name(sections[0])} to {section_name(sections[-1])} ({len(sections)} sections)   '
                 f'book words {total}   spoken words {n_spoken}   audio {clock(duration)}')
    lines.append(f'matched {hit / max(1, total):.1%} of book words')
    lines.extend(extra)
    lines.append('PROBLEMS: ' + ('none' if not problems else str(len(problems))))
    lines.extend(f'  ! {p}' for p in problems)
    lines.append('')
    lines.append('section        match  synced    start     announce')
    for sec, sy, st, an in zip(sections, sync, stats, announce):
        first = next((p['start'] for p in sy['paragraphs'] if p['start'] is not None), None)
        ratio = st['hit'] / max(1, st['total'])
        flag = '  <-- check' if ratio < 0.85 or st['synced'] < st['spoken'] else ''
        lines.append(f'{section_name(sec)[:13]:<13}  {ratio:>5.0%}  {st["synced"]:>3}/{st["spoken"]:<3} '
                     f'{clock(first) if first is not None else "   -   ":>8}  '
                     f'{clock(an):>8} {st["announce"]}{flag}')
    unsynced = [(section_name(sec), t) for sec, st in zip(sections, stats) for t in st['unsynced']]
    if unsynced:
        lines.append('')
        lines.append(f'paragraphs with no timing ({len(unsynced)}), the reader fills short gaps itself:')
        lines.extend(f'  {name}: {t[:70]}' for name, t in unsynced[:40])
        if len(unsynced) > 40:
            lines.append(f'  ... and {len(unsynced) - 40} more')
    if notes:
        lines.append('')
        lines.append(f'text cleanup ({len(notes)}):')
        seen = set()
        for kind, detail in notes:
            if (kind, detail) not in seen:
                seen.add((kind, detail))
                lines.append(f'  {kind}: {detail}')
    text = '\n'.join(lines) + '\n'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return text


# ───────────────────────────── Main ─────────────────────────────

def choose_sections(sections, spoken, wanted):
    """Keeps the sections the audio covers. `wanted` is an optional (first, last) chapter range."""
    novel = Novel(sections)
    match = align(novel.norm, spoken, coarse=True)
    per = [[0, 0] for _ in sections]
    for i, m in enumerate(match):
        per[novel.ci[i]][1] += 1
        if m >= 0:
            per[novel.ci[i]][0] += 1
    covered = [bool(t) and h / t >= 0.15 for h, t in per]

    if wanted:
        inside = [i for i, s in enumerate(sections) if s['num'] is not None and wanted[0] <= s['num'] <= wanted[1]]
        if not inside:
            fail(f'the EPUB has no chapters in {wanted[0]}-{wanted[1]}')
        first, last = inside[0], inside[-1]
        # A narrated prologue or epilogue next to the range comes along
        while first > 0 and sections[first - 1]['num'] is None and covered[first - 1]:
            first -= 1
        while last + 1 < len(sections) and sections[last + 1]['num'] is None and covered[last + 1]:
            last += 1
    else:
        hits = [i for i, c in enumerate(covered) if c]
        if not hits:
            fail('the audio does not match any chapter in the EPUB')
        first, last = hits[0], hits[-1]

    keep = []
    for i, sec in enumerate(sections):
        if first <= i <= last:
            if sec['kind'] == 'extra' and not covered[i]:
                continue   # an introduction or afterword the narrator does not read
            if wanted and sec['num'] is not None and not wanted[0] <= sec['num'] <= wanted[1]:
                continue
            keep.append(sec)
        elif i < first and sec['kind'] == 'epigraph' and not any(s['num'] is not None for s in sections[:first]):
            keep.append(sec)   # the book's opening epigraph stays even when it is not read aloud
    return keep


def main():
    ap = argparse.ArgumentParser(description='EPUB + audio -> synced reader HTML')
    ap.add_argument('--epub', nargs='+', required=True)
    ap.add_argument('--audio', nargs='+')
    ap.add_argument('--words', help='existing Parakeet JSON, skips transcription')
    ap.add_argument('--template', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--chapters', help='range such as 219-250')
    ap.add_argument('--skin')
    ap.add_argument('--book-id')
    ap.add_argument('--title')
    ap.add_argument('--subtitle')
    ap.add_argument('--author')
    ap.add_argument('--no-repair', action='store_true')
    args = ap.parse_args()

    if not args.audio and not args.words:
        fail('give --audio or --words')
    for path in args.epub + [args.template]:
        if not os.path.isfile(path):
            fail(f'not found: {path}')

    out = os.path.abspath(args.out)
    stem = os.path.splitext(os.path.basename(out))[0]
    work = os.path.join(os.path.dirname(out), '.sync', stem)
    os.makedirs(work, exist_ok=True)

    # 1. Words with timestamps
    audio = None
    joined = None
    if args.audio:
        files = collect_audio(args.audio)
        audio = files[0]
        if len(files) > 1:
            audio = joined = os.path.join(os.path.dirname(out), stem + '.m4a')
            join_audio(files, audio, work)
    if args.words:
        words_path = args.words
    else:
        words_path = os.path.join(work, 'words.json')
        transcribe(audio, words_path, work)
    words = read_words(words_path)
    if not words:
        fail(f'no word timestamps in {words_path}')
    silences = detect_silences(audio, os.path.join(work, 'silences.json')) if audio and os.path.isfile(audio) else []

    def timed_tokens(word_list):
        return spoken_tokens(snap_to_speech(word_list, silences))

    spoken, starts, ends = timed_tokens(words)
    log(f'spoken words: {len(spoken)}')

    # 2. Book text
    meta, sections = extract_sections(args.epub)
    if not any(s['kind'] == 'chapter' for s in sections):
        fail('no chapter headings found in the EPUB')
    log(f'sections in EPUB: {len(sections)}, {section_name(sections[0])} to {section_name(sections[-1])}')

    wanted = None
    if args.chapters:
        m = re.fullmatch(r'\s*(\d+)\s*(?:-\s*(\d+))?\s*', args.chapters)
        if not m:
            fail('--chapters must look like 219-250')
        wanted = (int(m.group(1)), int(m.group(2) or m.group(1)))
    sections = choose_sections(sections, spoken, wanted)
    log(f'sections to sync: {len(sections)}, {section_name(sections[0])} to {section_name(sections[-1])}')
    notes = [(f'{section_name(s)} {kind}', detail) for s in sections for kind, detail in s['notes']]

    # 3. Align, re-transcribe what Parakeet dropped, align again
    novel = Novel(sections)
    match, last = full_align(novel, spoken, starts, ends)
    extra = []
    repaired_path = os.path.join(work, 'words.repaired.json')
    can_repair = audio and not args.no_repair and os.path.isfile(audio)
    state_path = os.path.join(work, 'repair.json')
    tried = -1   # dropouts left after the last repair, so a rerun does not repeat a finished job
    if can_repair and os.path.exists(repaired_path) \
            and os.path.getmtime(repaired_path) >= os.path.getmtime(words_path):
        words = read_words(repaired_path)
        log(f'repaired transcript found: {repaired_path}')
        spoken, starts, ends = timed_tokens(words)
        match, last = full_align(novel, spoken, starts, ends)
        try:
            with open(state_path, encoding='utf-8') as f:
                tried = json.load(f).get('left', -1)
        except (OSError, ValueError):
            pass
    if can_repair and len(find_dropouts(match, starts, ends)) > max(tried, 2):
        source = parakeet_input(audio, work)
        before = sum(m >= 0 for m in match)
        for round_no in range(1, len(REPAIR_PADS) + 1):
            spans = find_dropouts(match, starts, ends)
            if len(spans) < 3:
                break
            words = repair_dropouts(source, words, spans, work, round_no)
            spoken, starts, ends = timed_tokens(words)
            match, last = full_align(novel, spoken, starts, ends)
        with open(repaired_path, 'w', encoding='utf-8') as f:
            json.dump(words, f)
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump({'left': len(find_dropouts(match, starts, ends))}, f)
        after = sum(m >= 0 for m in match)
        extra.append(f'repair pass: matched book words {before} -> {after}')
    left = find_dropouts(match, starts, ends)
    if left:
        extra.append(f'stretches still without spoken words: {len(left)}, '
                     f'{sum(b - a for a, b in left) / 60:.1f} min in total')
    sync, announce, stats = build_sync(sections, novel, match, last, spoken, starts, ends)

    # 4. Checks
    problems = []
    total = sum(s['total'] for s in stats)
    hit = sum(s['hit'] for s in stats)
    if hit / max(1, total) < 0.6:
        problems.append('under 60% of book words matched, the EPUB and audio may be different editions')
    for sec, st in zip(sections, stats):
        if sec['kind'] == 'epigraph':
            continue
        if st['spoken'] and st['synced'] == 0:
            problems.append(f'{section_name(sec)} has no synced paragraphs')
        elif st['total'] and st['hit'] / st['total'] < 0.5:
            problems.append(f'{section_name(sec)} matched only {st["hit"] / st["total"]:.0%}')
    seq = [p['start'] for ch in sync for p in ch['paragraphs'] if p['start'] is not None]
    if any(b <= a for a, b in zip(seq, seq[1:])):
        problems.append('paragraph start times are not strictly increasing')
    if any(p['end'] <= p['start'] for ch in sync for p in ch['paragraphs'] if p['start'] is not None):
        problems.append('a paragraph ends before it starts')

    # 5. Output
    strings = {
        'BOOK_ID': args.book_id or re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-'),
        'BOOK_TITLE': args.title or meta.get('title'),
        'BOOK_SUBTITLE': args.subtitle,
        'BOOK_AUTHOR': args.author or meta.get('creator'),
        'SKIN': args.skin,
    }
    write_html(args.template, out, sync, announce, strings, audio)
    review = write_review(os.path.join(work, 'review.txt'), sections, sync, announce, stats,
                          notes, len(spoken), ends[-1], problems, extra)
    log('')
    log(review)
    if joined:
        log(f'load this audio in the reader: {joined}')
    log(f'DONE -> {out}')


if __name__ == '__main__':
    main()
