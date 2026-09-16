"""Probe 2: close the gaps probe 1 left, and add the non-bounds failure axes.

Gaps/missing axes:
  (a) v5 off-by-one at x=9 -- the case where the guard ACCEPTS an OOB write.
  (b) the silent-wrap band vs the crash band for a right-bound-only guard.
  (c) container semantics: bytearray vs memoryview row-view vs array.array.
  (d) flat-framebuffer row bleed (why "swallow/assert" is not memory safety).
  (e) overlapping src/dst -- destructive read-after-write, which NO bounds
      guard fixes.
  (f) correctly-bounded guard that SKIPS instead of CLIPPING (all-or-nothing).
  (g) two independent correct-clipping implementations (multiple L1 forms).

Run: python /c/Python314/python probe2.py
"""

W = 16
SENT = 7
SRC = [10 * (k + 1) for k in range(8)]   # ramp: makes source-offset bugs visible


def expected(start_x, src=SRC, width=W):
    out = bytearray([SENT] * width)
    for i, p in enumerate(src):
        j = start_x + i
        if 0 <= j < width:
            out[j] = (out[j] + p) // 2
    return out


# ---- (a) off-by-one guard, now including the OOB-accepting input -----------
def v5(dst, src, start_x):
    if 0 <= start_x and start_x + len(src) - 1 <= len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


# ---- (f) bounds-correct but all-or-nothing (skips instead of clipping) -----
def v20(dst, src, start_x):
    if 0 <= start_x and start_x + len(src) <= len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


# ---- (g) two correct forms -------------------------------------------------
def v9(dst, src, start_x):                      # per-index
    for i, pixel in enumerate(src):
        j = start_x + i
        if 0 <= j < len(dst):
            dst[j] = (dst[j] + pixel) // 2
    return dst


def v19(dst, src, start_x):                     # span-based
    lo, hi = max(0, start_x), min(len(dst), start_x + len(src))
    for j in range(lo, hi):
        dst[j] = (dst[j] + src[j - start_x]) // 2
    return dst


CASES = [-17, -16, -10, -4, -1, 0, 8, 9, 10, 12, 15, 16]


def oracle(fn, sx, dst=None, src=SRC, ret_is_dst=True):
    want = expected(sx)
    buf = bytearray([SENT] * W) if dst is None else dst
    try:
        ret = fn(buf, src, sx)
    except Exception as e:                       # noqa: BLE001
        return "EXC:" + type(e).__name__
    if len(buf) != W:
        return "LEN=%d" % len(buf)
    bad = [k for k in range(W) if buf[k] != want[k]]
    if bad:
        return "WRONG@" + ",".join(map(str, bad[:2])) + ("+%d" % (len(bad) - 2) if len(bad) > 2 else "")
    return "ok"


print("=== (a) v5 off-by-one: x = len(dst)-len(src)+1 = 9 is the OOB-accepting input")
for sx in (8, 9, 10):
    print("   x=%-3d -> %s   (guard: 0<=x and x+%d-1<=%d -> %s)"
          % (sx, oracle(v5, sx), len(SRC), W, 0 <= sx and sx + len(SRC) - 1 <= W))

print("\n=== (b) right-bound-only guard: silent-wrap band vs crash band")
for sx in (-1, -10, -16, -17):
    r = oracle(v20, sx)          # v20's guard rejects negatives -> silent skip
    # emulate the leaky guard: if x+len<=len(dst) (no left check)
    buf = bytearray([SENT] * W)
    leaky = (sx + len(SRC) <= W)
    try:
        for i, p in enumerate(SRC):
            buf[sx + i] = (buf[sx + i] + p) // 2
        out = "wrote %s" % [k for k in range(W) if buf[k] != SENT]
    except Exception as e:       # noqa: BLE001
        out = type(e).__name__
    print("   x=%-4d leaky-guard passes=%-5s outcome=%-22s | bounds-correct-skip: %s"
          % (sx, leaky, out, r))

print("\n=== (c) container semantics for dst[-k] (silent wrap or crash?)")
import array  # noqa: E402
for name, mk in (("bytearray", lambda: bytearray([SENT] * W)),
                 ("memoryview", lambda: memoryview(bytearray([SENT] * W))),
                 ("array('B')", lambda: array.array("B", [SENT] * W))):
    b = mk()
    try:
        b[-10] = 99
        print("   %-12s dst[-10]=99 ACCEPTED (silent wrap -> row tail)" % name)
    except Exception as e:       # noqa: BLE001
        print("   %-12s dst[-10] -> %s" % (name, type(e).__name__))

print("\n=== (d) flat framebuffer 2 rows x 8 cols: does overflow bleed into row 1?")
for label, guard in (("no guard", lambda x, n: True),
                     ("guard 0<=x<=len (aware of row only)", lambda x, n: 0 <= x <= 8),
                     ("try/except IndexError", lambda x, n: True)):
    fb = bytearray([SENT] * 16)
    row0 = fb[0:8]                       # careful: slice copies; use memoryview
    mv = memoryview(fb)
    r0 = mv[0:8]
    sx, src = 6, [10, 20, 30, 40, 50, 60, 70, 80]
    err = None
    try:
        for i, p in enumerate(src):
            if guard(sx, len(src)) and label != "no guard":
                if label == "try/except IndexError":
                    pass
                r0[sx + i] = (r0[sx + i] + p) // 2
            elif label == "no guard" or label == "try/except IndexError":
                try:
                    r0[sx + i] = (r0[sx + i] + p) // 2
                except IndexError:
                    break
    except Exception as e:               # noqa: BLE001
        err = type(e).__name__
    row1_touched = [k - 8 for k in range(8, 16) if fb[k] != SENT]
    print("   %-36s err=%-12s row0=%s row1_bleed=%s"
          % (label, err, list(fb[0:8]), row1_touched))

print("\n=== (e) overlapping src/dst: destructive read-after-write")
for name, fn in (("v9 correct-bounds", v9), ("v19 span-based", v19)):
    buf = bytearray(SRC)                 # dst IS src
    before = bytes(buf)
    fn(buf, buf, 2)                      # blend the row onto itself, shifted
    print("   %-20s -> %s" % (name, list(buf)))
indep = bytearray(SRC)
ref = bytearray(SRC)
for i in range(6):                       # oracle: read from an untouched copy
    j = 2 + i
    if 0 <= j < len(ref):
        ref[j] = (ref[j] + SRC[i]) // 2
print("   %-20s -> %s   (feedback differs: %s)"
      % ("oracle w/ snapshot", list(ref), bytes(indep) != bytes(ref)))

print("\n=== (f,g) full matrix on the decisive cases")


def _blend(d, s, x):
    for i, p in enumerate(s):
        d[x + i] = (d[x + i] + p) // 2
    return d


hdr = "%-30s" % "variant"
for sx in CASES:
    hdr += "%-9s" % ("x=%d" % sx)
print(hdr)
print("-" * len(hdr))
for name, fn in (("v1 right-bound only", lambda d, s, x: d if not (x + len(s) <= W) else _blend(d, s, x)),
                 ("v2 left-bound only", lambda d, s, x: d if not (x >= 0) else _blend(d, s, x)),
                 ("v5 off-by-one len-1", v5),
                 ("v20 bounds ok, skips", v20),
                 ("v9 clip per-index", v9),
                 ("v19 clip span", v19)):
    row = "%-30s" % name
    for sx in CASES:
        row += "%-9s" % oracle(fn, sx)
    print(row)

def _blend(d, s, x):
    for i, p in enumerate(s):
        d[x + i] = (d[x + i] + p) // 2
    return d
