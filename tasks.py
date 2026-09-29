"""Benchmark tasks. `public` examples are shown to the models; `hidden` cases are only used for final grading.
`edge` is an extra case a good planner would think of; `ref` / `buggy` / `edge` are used by the --mock backend to simulate a model that sometimes ships a realistic bug."""

TASKS = [
    {
        "fn": "roman_to_int",
        "edge": (("XC",), 90),
        "spec": "roman_to_int(s: str) -> int. Convert a Roman numeral (I V X L C D M, with subtractive pairs like IV, IX, XL, XC, CD, CM) to an integer.",
        "public": [(("III",), 3), (("IV",), 4)],
        "hidden": [(("MCMXCIV",), 1994), (("LVIII",), 58), (("IX",), 9), (("XL",), 40)],
        "ref": '''def roman_to_int(s):
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for a, b in zip(s, s[1:] + " "):
        total += -v[a] if v.get(b, 0) > v[a] else v[a]
    return total''',
        "buggy": '''def roman_to_int(s):
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    return sum(v[c] for c in s)''',
    },
    {
        "fn": "merge_intervals",
        "edge": (([[1, 5], [2, 3]],), [[1, 5]]),
        "spec": "merge_intervals(intervals: list[list[int]]) -> list[list[int]]. Merge overlapping or touching closed intervals; return them sorted by start.",
        "public": [(([[1, 3], [2, 6], [8, 10]],), [[1, 6], [8, 10]]), (([[1, 4], [4, 5]],), [[1, 5]])],
        "hidden": [(([[1, 10], [2, 3]],), [[1, 10]]), (([],), []), (([[5, 6], [1, 2]],), [[1, 2], [5, 6]])],
        "ref": '''def merge_intervals(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out''',
        "buggy": '''def merge_intervals(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out''',
    },
    {
        "fn": "is_balanced",
        "edge": (("(()",), False),
        "spec": "is_balanced(s: str) -> bool. True if every (), [], {} bracket is properly opened, closed and nested. Ignore all other characters.",
        "public": [(("([]{})",), True), (("(]",), False), (("((",), False)],
        "hidden": [(("",), True), (("a(b)c",), True), (("}{",), False), (("([)]",), False)],
        "ref": '''def is_balanced(s):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for c in s:
        if c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack.pop() != pairs[c]:
                return False
    return not stack''',
        "buggy": '''def is_balanced(s):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for c in s:
        if c in "([{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack.pop() != pairs[c]:
                return False
    return True''',
    },
    {
        "fn": "rle_encode",
        "edge": (("aba",), "a1b1a1"),
        "spec": "rle_encode(s: str) -> str. Run-length encode consecutive runs as <char><count>, e.g. 'aaabcc' -> 'a3b1c2'.",
        "public": [(("aaabcc",), "a3b1c2"), (("",), "")],
        "hidden": [(("abab",), "a1b1a1b1"), (("aabbaa",), "a2b2a2"), (("z",), "z1")],
        "ref": '''from itertools import groupby

def rle_encode(s):
    return "".join(f"{k}{len(list(g))}" for k, g in groupby(s))''',
        "buggy": '''from collections import Counter

def rle_encode(s):
    return "".join(f"{k}{v}" for k, v in Counter(s).items())''',
    },
    {
        "fn": "parse_duration",
        "edge": (("12m",), 720),
        "spec": "parse_duration(s: str) -> int. Parse a duration made of <number><unit> parts with units h, m, s (e.g. '1h30m', '90s') and return total seconds.",
        "public": [(("1h30m",), 5400), (("45s",), 45)],
        "hidden": [(("2h",), 7200), (("90s",), 90), (("1h1m1s",), 3661), (("10m",), 600)],
        "ref": '''import re

def parse_duration(s):
    mult = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\\d+)([hms])", s))''',
        "buggy": '''import re

def parse_duration(s):
    mult = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\\d)([hms])", s))''',
    },
    {
        "fn": "top_k_frequent",
        "edge": ((["b", "a", "b", "a"], 1), ["a"]),
        "spec": "top_k_frequent(words: list[str], k: int) -> list[str]. Return the k most frequent words, most frequent first; ties broken alphabetically.",
        "public": [((["a", "b", "a"], 1), ["a"]), ((["x", "y", "x", "y", "z"], 2), ["x", "y"])],
        "hidden": [((["b", "a"], 2), ["a", "b"]), ((["the", "day", "is", "the", "is"], 2), ["is", "the"])],
        "ref": '''from collections import Counter

def top_k_frequent(words, k):
    c = Counter(words)
    return sorted(c, key=lambda w: (-c[w], w))[:k]''',
        "buggy": '''from collections import Counter

def top_k_frequent(words, k):
    return [w for w, _ in Counter(words).most_common(k)]''',
    },
    {
        "fn": "semver_compare",
        "edge": (("1.0.10", "1.0.9"), 1),
        "spec": "semver_compare(a: str, b: str) -> int. Compare two MAJOR.MINOR.PATCH version strings numerically. Return -1, 0 or 1.",
        "public": [(("1.0.0", "1.0.1"), -1), (("1.2.0", "1.10.0"), -1)],
        "hidden": [(("2.0.0", "2.0.0"), 0), (("10.0.0", "9.9.9"), 1), (("0.1.0", "0.0.9"), 1)],
        "ref": '''def semver_compare(a, b):
    x, y = [tuple(map(int, v.split("."))) for v in (a, b)]
    return (x > y) - (x < y)''',
        "buggy": '''def semver_compare(a, b):
    return (a > b) - (a < b)''',
    },
    {
        "fn": "flatten",
        "edge": (([[[1]]],), [1]),
        "spec": "flatten(x: list) -> list. Flatten arbitrarily nested lists/tuples into one flat list. Strings are atoms, never split.",
        "public": [(([1, [2, 3]],), [1, 2, 3]), (([1, [2, [3, [4]]]],), [1, 2, 3, 4])],
        "hidden": [(([],), []), ((["ab", ("c", ["d"])],), ["ab", "c", "d"]), (([[[]]],), [])],
        "ref": '''def flatten(x):
    out = []
    for i in x:
        if isinstance(i, (list, tuple)):
            out.extend(flatten(i))
        else:
            out.append(i)
    return out''',
        "buggy": '''def flatten(x):
    out = []
    for i in x:
        if isinstance(i, (list, tuple)):
            out.extend(i)
        else:
            out.append(i)
    return out''',
    },
]
