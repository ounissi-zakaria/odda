import random
import re

import pytest

from odda.scanner.fastscan import FastScan

_EXACT_CORPUS: tuple[tuple[str, int], ...] = (
    (r"ab+c", 0),
    (r"a(b|c)d", 0),
    (r"a|ab", 0),
    (r"ab|a", 0),
    (r"foo|foobar", 0),
    (r"foobar|foo", 0),
    (r"a+?b", 0),
    (r"a{2,4}", 0),
    (r"a{2}", 0),
    (r"a{2,}", 0),
    (r"(ab)+", 0),
    (r"(?:ab)*c", 0),
    (r"(?:ab|ba)+x", 0),
    (r"[ab]+", 0),
    (r"[a-c]{3,}", 0),
    (r"[^a]", 0),
    (r"[^\d\s]", 0),
    (r"\w+", 0),
    (r"\d+", 0),
    (r"\s+", 0),
    (r"\W+", 0),
    (r"\S+", 0),
    (r"\w+-\d+", 0),
    (r"a+", 0),
    (r"(?:foo)+", 0),
    (r"abab", 0),
    (r"aa", 0),
    (r"aba", 0),
    (r"^ab", 0),
    (r"ab$", 0),
    (r"^ab$", 0),
    (r"^ab", re.MULTILINE),
    (r"ab$", re.MULTILINE),
    (r"^ab$", re.MULTILINE),
    (r"\Aab", 0),
    (r"ab\Z", 0),
    (r"^ab\Z", 0),
    (r"(?m)^err", 0),
    (r"(?i)café", 0),
    (r"café", re.IGNORECASE),
    (r"(?i)K", 0),
    (r"(?i:SET)-cookie", 0),
    (r"(?i)[a-z]+x", 0),
    (r"x.y", re.DOTALL),
    (r"(?s)x.y", 0),
    (r"x.*y", 0),
    (r"x.*?y", 0),
    (r"日本", 0),
    (r"é{2}", 0),
    (r"[éü]", 0),
    (r"[^é]", 0),
    (r"Traceback \(most recent call last\):", re.MULTILINE),
    (r"^\s*at [\w$.]+\(\w+\.java:\d+\)", re.MULTILINE),
    (r"\(node:internal/[\w/]+:\d+:\d+\)", re.MULTILINE),
    (r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", re.MULTILINE),
    (r'["\']?client_?[Ss]ecret["\']?\s*[:=]\s*["\'][^"\'\n]{8,}["\']', re.MULTILINE),
    (r'["\']?api_?[Kk]ey["\']?\s*[:=]\s*["\'][^"\'\n]{8,}["\']', re.MULTILINE),
    (r"Sorry, this shop is currently unavailable", 0),
)

_CRAFTED_BODIES: tuple[str, ...] = (
    "",
    "a",
    "ab",
    "abc",
    "aab",
    "abab",
    "abababab",
    "aaab",
    "banana",
    "xay xby",
    "ab\nab",
    "\nab",
    "ab\n",
    "abc\n\n",
    "a\nb\nab\n",
    "ab\ncd\nef",
    "abc\r\n",
    "err one\nno err\n err two\n",
    "Traceback (most recent call last):",
    "  at Foo.bar(File.java:42)",
    "(node:internal/util:1:2)",
    "-----BEGIN RSA PRIVATE KEY-----",
    '"client_secret": "supersecret123"',
    '"api_key": "AKIAISHENANIGANS12"',
    "<Code>NoSuchBucket</Code>...<BucketName>b</BucketName>",
    "Sorry, this shop is currently unavailable",
    "café CAFÉ Café cafe",
    "CAFÉ-AU-LAIT café",
    "日本語のテキスト japan",
    "éé ü 中五 éu",
    "K k K ſ ﬁ ss ß",  # noqa: RUF001 — fold-divergence corpus data
    "INITO İI ıi inito",  # noqa: RUF001 — fold-divergence corpus data
    "(?i:SET)-cookie literal",
    "  spaced\tout\n\r\n",
    "   ",
    "\n\n\n",
    "aaaa",
    "aabaa",
    "xxabxx",
    "foofoobar foobar foo",
    "word-42 word- 99 a-1",
    "énom-1 digénom",
)

_LITERAL_SPLICES = (
    "ab",
    "foo",
    "café",
    "É",
    "日本",
    "é{2}",
    "err",
    "Traceback",
    "secret",
    "client_secret",
    "BEGIN",
    "NoSuchBucket",
    "unavailable",
    "K",
    "İ",
    "ı",  # noqa: RUF001 — fold-divergence corpus data
    "word-9",
)


def _corpus_bodies() -> list[str]:
    rng = random.Random(20261106)
    bodies = list(_CRAFTED_BODIES)
    pools = (  # noqa: RUF001 — multibyte splice pools
        "ab\n",
        "abc é中\n",
        "xyz üKſ\n",  # noqa: RUF001 — fold-divergence corpus data
        "aA \t\r\n",
        "foo é1\n日本x",
    )
    for _ in range(240):
        pool = rng.choice(pools)
        chunks = [
            "".join(rng.choice(pool) for _ in range(rng.randrange(0, 12)))
            for _ in range(rng.randrange(1, 4))
        ]
        if rng.random() < 0.6:
            chunks.insert(rng.randrange(len(chunks) + 1), rng.choice(_LITERAL_SPLICES))
        bodies.append("".join(chunks))
    return bodies


def test_engine_corpus_spans_match_stdlib_finditer() -> None:
    compiled = [re.compile(src, flags) for src, flags in _EXACT_CORPUS]
    fs = FastScan.build(compiled)
    assert fs.patterns == tuple(compiled)
    assert fs.prefiltered == frozenset(), "corpus patterns must be engine-exact"
    for body in _corpus_bodies():
        spans = fs.find_all(body)
        assert len(spans) == len(compiled)
        for i, pattern in enumerate(compiled):
            want = [m.span() for m in pattern.finditer(body)]
            assert spans[i] == want, (
                f"pattern {pattern.pattern!r} (flags={pattern.flags})"
                f" diverged on body {body!r}:"
                f" engine={spans[i]} stdlib={want}"
            )


def test_case_insensitive_patterns_round_trip() -> None:
    cases = [
        (r"(?i)café", 0, "CAFÉ café Cafe CAFÉ"),
        (r"(?i)secret", 0, "SeCrEt SECRET secret"),
        (r"set-cookie:", re.IGNORECASE, "set-cookie: x=1\nSet-Cookie: y=2"),
        (r"(?i)[a-z]+x", 0, "ABCX abcX İx ıx"),  # noqa: RUF001 — fold corpus
        (r"(?i)k", 0, "K k K ſ ﬁ"),  # noqa: RUF001 — fold corpus data
        (r"(?i:CAFÉ)-au-lait", 0, "café-au-lait CAFÉ-AU-LAIT"),
    ]
    patterns = [re.compile(src, flags) for src, flags, _ in cases]
    fs = FastScan.build(patterns)
    assert fs.prefiltered == frozenset()
    for i, (src, flags, body) in enumerate(cases):
        want = [m.span() for m in re.compile(src, flags).finditer(body)]
        assert fs.find_all(body)[i] == want


def test_multiline_anchors_round_trip() -> None:
    cases = [
        (r"^ab", 0, ["ab", "\nab", "x\nab", "ab\nab"]),
        (r"^ab", re.MULTILINE, ["ab", "\nab", "x\nab", "ab\nab", "ab\nab\n"]),
        (r"ab$", 0, ["ab", "ab\n", "ab\n\n", "ab\nab", "xab\n"]),
        (r"ab$", re.MULTILINE, ["ab", "ab\n", "ab\n\n", "ab\nab", "xab\n", "ab\r\n"]),
        (r"^ab$", 0, ["ab", "ab\n", "\nab"]),
        (r"^ab$", re.MULTILINE, ["ab", "ab\n", "\nab", "ab\nab\n", "ab\nx\nab\n"]),
        (r"(?m)^x", 0, ["x", "a\nx", "a\nx\nx", "\nx"]),
        (r"\Aab", 0, ["ab", "ab\nab", "\nab"]),
        (r"ab\Z", 0, ["ab", "ab\n", "ab\nab"]),
    ]
    patterns = [re.compile(src, flags) for src, flags, _ in cases]
    fs = FastScan.build(patterns)
    assert fs.prefiltered == frozenset()
    for i, (src, flags, bodies) in enumerate(cases):
        pat = re.compile(src, flags)
        for body in bodies:
            got = fs.find_all(body)[i]
            want = [m.span() for m in pat.finditer(body)]
            assert got == want, (
                f"{src!r} flags={flags} on {body!r}: engine={got} stdlib={want}"
            )


_REFUSED_CORPUS: tuple[tuple[str, int, str], ...] = (
    (
        r"(?m)^# HELP ((?:prometheus|go|process)_\w+)[^\r\n]*\r?\n# TYPE \1 \w+",
        0,
        "# HELP go_goroutines total\n# TYPE go_goroutines counter",
    ),
    (
        r"<script\b[^>]{0,120}\ssrc\s*=\s*[\"'][^\"']*(?<![\w-])polyfill\.io",
        0,
        '<script defer src="https://polyfill.io/3.x"></script>',
    ),
    (r"\bAKIA[0-9A-Z]{16}\b", 0, "key=AKIA1234567890ABCDEF;"),
    (r"a(?=b)", 0, "ab"),
    (r"(?<=x)secret", 0, "xsecret"),
    (r"(?<!x)secret", 0, "secret"),
    (r"er++", 0, "error reader"),
    (r"(?>ab)c", 0, "abc"),
    (r"(?P<n>ab)(?P=n)", 0, "abab"),
    (
        r"(?s)<Code>\s*NoSuchBucket\s*</Code>.{0,500}?<BucketName>[^<]{1,255}",
        re.MULTILINE,
        "<Code>NoSuchBucket</Code>...<BucketName>b</BucketName>",
    ),
    (
        r"(?is)Telerik\.Web\.UI\.WebResource\.axd[^\"'>]{0,600}?Version(?:%3[dD]|=)",
        0,
        "Telerik.Web.UI.WebResource.axdblahblahVersion=2013.1",
    ),
    (
        r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
        r"([\"'])[A-Za-z0-9+/=_.\-]{8,64}\1",
        0,
        'config = dict(api_key = "sk_live_abcdefghijklmnop")',
    ),
    (
        r"(?is)<script(?=[^>]{0,200}\ssrc\s*=\s*[\"'])[^>]{0,400}>",
        0,
        '<script defer src="https://good.example/x.js"></script>',
    ),
    (r"token(?![A-Za-z0-9_])", 0, "auth: token"),
)


def test_prefiltered_patterns_confirm_to_stdlib_finditer_spans() -> None:
    patterns = [re.compile(src, flags) for src, flags, _ in _REFUSED_CORPUS]
    fs = FastScan.build(patterns)
    assert fs.prefiltered == frozenset(range(len(patterns)))
    rng = random.Random(20261106)
    for i, (src, _flags, hit_body) in enumerate(_REFUSED_CORPUS):
        pat = patterns[i]
        assert pat.search(hit_body), f"corpus bug: {src!r} must match its body"
        bodies = [
            hit_body,
            hit_body + " padding",
            "padding " + hit_body,
            hit_body + "\n" + hit_body,
            "no match here at all",
        ]
        for _ in range(60):
            pool = "abc \n<script>\npolyfill.io\nAKIA\n0123 xsecret Help TYPE\n"
            pool += "\"'= api_key sk_live token:\nVersion=\r\n"
            bodies.append(
                "".join(rng.choice(pool) for _ in range(rng.randrange(0, 80)))
            )
        for body in bodies:
            spans = fs.find_all(body)[i]
            want = [match.span() for match in pat.finditer(body)]
            assert spans == want, (
                f"confirmed prefilter for {src!r} diverged on {body!r}:"
                f" engine={spans[:4]}... stdlib={want[:4]}..."
            )


def test_wide_bounded_repeats_widen_in_the_prefilter_source() -> None:
    from odda.scanner.fastscan import _prefilter_expression

    wide, _flags = _prefilter_expression(
        re.compile(r"(?s)<Code>.{0,500}?<BucketName>[^<]{1,255}")
    )
    assert wide == r"<Code>.*<BucketName>[^<]+"

    ahead, _flags = _prefilter_expression(re.compile(r"x(?=[a-z]{0,300})y{2,700}"))
    assert ahead.startswith("x(?=")
    assert "(?<=" not in ahead

    exact, _flags = _prefilter_expression(re.compile(r"\bAKIA[0-9A-Z]{16}\b"))
    assert exact == r"\bAKIA[0-9A-Z]{16}\b"

    conditional = r"(a)(?(1)b|c)d{0,900}"
    fallback, _flags = _prefilter_expression(re.compile(conditional))
    assert fallback == conditional


def test_unapproximable_patterns_are_refused_at_build() -> None:
    for src in [r"a*", r"x?", r"(foo)*", r"a|", r""]:
        with pytest.raises(ValueError, match=re.escape(src) or "pattern 0"):
            FastScan.build([re.compile(src)])


def test_empty_pattern_set_scans_nothing() -> None:
    fs = FastScan.build(())
    assert fs.patterns == ()
    assert fs.prefiltered == frozenset()
    assert fs.find_all("anything") == []


def test_multichunk_reversal_spans_match_stdlib_finditer() -> None:
    from odda.scanner import fastscan

    chunk = fastscan.GIL_CHUNK
    compiled = [re.compile(src, flags) for src, flags in _EXACT_CORPUS]
    fs = FastScan.build(compiled)
    for body in (
        "." * (chunk - 1) + "ab" + "." * (chunk + 2),
        "." * (chunk - 1) + "a" + "..." + "ab",
        ("ab" + "." * (chunk - 2)) * 3,
        "é." * (chunk // 2) + "ab" + "é中" * 8,
        "." * (2 * chunk + 7),
        "ab" + ("日." * (chunk // 2)) + "ab",
    ):
        spans = fs.find_all(body)
        for i, pattern in enumerate(compiled):
            want = [m.span() for m in pattern.finditer(body)]
            assert spans[i] == want, (
                f"pattern {pattern.pattern!r} diverged on len={len(body)}:"
                f" engine={spans[i][:4]}... stdlib={want[:4]}..."
            )


def test_multi_pattern_scan_keeps_patterns_independent() -> None:
    fs = FastScan.build(
        [re.compile(p) for p in (r"one", r"\btwo\b", r"three|threex", r"none")]
    )
    body = "one two threex one two"
    spans = fs.find_all(body)
    assert spans[0] == [(0, 3), (15, 18)]
    assert spans[1] == [(4, 7), (19, 22)]
    assert spans[2] == [(8, 13)]
    assert spans[3] == []
