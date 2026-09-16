"""Probe 3: corrected bleed/overlap tests + 4 more plausible-looking guards.

Fixes to probe 2:
  (d) flat bytearray framebuffer, row width 8: the bleed test must NOT use a
      memoryview row view (that one legitimately raises); the bug only appears
      when the row is addressed inside a bigger flat buffer.
  (e) overlap: compare in-place vs snapshot-oracle instead of vs a fresh copy.

New variants:
  v21  if start_x <= len(dst)            (single-index habit: ignores len(src))
  v22  if 0 < start_x and ...            (blocks start_x == 0: the common case)
  v23  if abs(start_x) + len(src) <= n   (negative treated as positive)
  v24  if start_x < 0: start_x += n      (Python-index "wrap" semantics)
"""

W, SENT = 16, 7
SRC = [10 * (k + 1) for k in range(8)]


def expected(start_x, src=SRC, width=W):
    out = bytearray([SENT] * width)
    for i, p in enumerate(src):
        j = start_x + i
        if 0 <= j < width:
            out[j] = (out[j] + p) // 2
    return out


def _blend(d, s, x):
    for i, p in enumerate(s):
        d[x + i] = (d[x + i] + p) // 2
    return d


def v20(d, s, x):  # correct bounds, but skip instead of clip
    return d if not (0 <= x and x + len(s) <= W) else _blend(d, s, x)


def v21(d, s, x):  # start_x checked against len(dst) only (single-index habit)
    return d if not (0 <= x <= len(d)) else _blend(d, s, x)


def v22(d, s, x):  # '> 0' instead of '>= 0' -- excludes the x == 0 case
    return d if not (0 < x and x + len(s) <= len(d)) else _blend(d, s, x)


def v23(d, s, x):  # abs() folds negatives onto the right edge
    return d if not (abs(x) + len(s) <= len(d)) else _blend(d, s, x)


def v24(d, s, x):  # trust Python's negative-index wrap semantics
    return _blend(d, s, x + len(d)) if x < 0 else _blend(d, s, x)


def v25(d, s, x):  # clamp per-index but off by one high (j <= len(dst))
    for i, p in enumerate(s):
        j = x + i
        if 0 <= j <= len(d):
            d[j] = (d[j] + p) // 2
    return d


def v9(d, s, x):  # correct per-index clip
    for i, p in enumerate(s):
        j = x + i
        if 0 <= j < len(d):
            d[j] = (d[j] + p) // 2
    return d


VARIANTS = [("v20 bounds ok, skips", v20), ("v21 x<=len(dst)", v21),
            ("v22 0<x (excludes 0)", v22), ("v23 abs(x)", v23),
            ("v24 x<0: x+=len(dst)", v24), ("v25 0<=j<=len(dst)", v25),
            ("v9  correct clip", v9)]
CASES = [-16, -10, -4, -1, 0, 1, 8, 9, 12, 16]


def res(fn, sx):
    want, buf = expected(sx), bytearray([SENT] * W)
    try:
        fn(buf, SRC, sx)
    except Exception as e:  # noqa: BLE001
        return "EXC:" + type(e).__name__
    if len(buf) != W:
        return "LEN=%d" % len(buf)
    bad = [k for k in range(W) if buf[k] != want[k]]
    return "ok" if not bad else "WRONG@" + ",".join(map(str, bad[:2])) + (
        "+%d" % (len(bad) - 2) if len(bad) > 2 else "")


hdr = "%-24s" % "variant"
for sx in CASES:
    hdr += "%-8s" % ("x=%d" % sx)
print(hdr)
print("-" * len(hdr))
for name, fn in VARIANTS:
    print(("%-24s" % name) + "".join("%-8s" % res(fn, sx) for sx in CASES))

print("\n=== (d) CORRECTED: flat framebuffer, 2 rows x 8 cols, rows are slices")
for label in ("unguarded", "guard x<=8 (row length only)", "try/except IndexError"):
    fb = bytearray([SENT] * 16)          # flat buffer; row0 = [0:8), row1 = [8:16)
    sx, src = 6, [10, 20, 30, 40, 50, 60, 70, 80]
    caught = False
    for i, p in enumerate(src):
        j = sx + i
        if label == "guard x<=8 (row length only)" and not (0 <= sx <= 8):
            break
        if label == "unguarded" and j >= 16:
            break
        try:
            fb[j] = (fb[j] + p) // 2
        except IndexError:
            caught = True
            break
    bleed = [k - 8 for k in range(8, 16) if fb[k] != SENT]
    print("   %-32s row0=%-34s row1_bleed=%s%s"
          % (label, list(fb[0:8]), bleed, " (IndexError)" if caught else ""))

print("\n   -> with a FLAT buffer the row width is not a runtime boundary:")
fb = bytearray([SENT] * 16)
for i, p in enumerate([10, 20, 30, 40, 50, 60, 70, 80]):
    fb[6 + i] = (fb[6 + i] + p) // 2
print("   x=6 on an 8-wide row inside a 16-byte buffer -> fb = %s" % list(fb))
print("   bytes 8..13 belong to row1 and were overwritten: no exception anywhere.")

print("\n=== (e) CORRECTED: in-place blend where dst IS src (overlap)")
snap = list(SRC)
ref = bytearray(SRC)
for i in range(len(SRC)):
    j = 2 + i
    if 0 <= j < len(ref):
        ref[j] = (ref[j] + snap[i]) // 2      # oracle reads from a snapshot
buf = bytearray(SRC)
v9(buf, buf, 2)                                # bounds-correct, overlap-unaware
print("   v9 (correct bounds, aliased)  -> %s" % list(buf))
print("   snapshot oracle              -> %s" % list(ref))
print("   bytes differ                 -> %s" % (bytes(buf) != bytes(ref)))

print("\n=== degenerates")
for name, s, x in (("src=[]", [], 0), ("src=[]", [], 5), ("src=[]", [], -3)):
    b = bytearray([SENT] * W)
    try:
        v9(b, s, x)
        print("   %-8s x=%-3d -> ok, untouched=%s" % (name, x, bytes(b) == bytes([SENT] * W)))
    except Exception as e:  # noqa: BLE001
        print("   %-8s x=%-3d -> %s" % (name, x, type(e).__name__))
for x in (10 ** 9, -10 ** 9):
    b = bytearray([SENT] * W)
    try:
        v9(b, SRC, x)
        print("   huge     x=%-12d -> ok, untouched=%s" % (x, bytes(b) == bytes([SENT] * W)))
    except Exception as e:  # noqa: BLE001
        print("   huge     x=%-12d -> %s" % (x, type(e).__name__))
