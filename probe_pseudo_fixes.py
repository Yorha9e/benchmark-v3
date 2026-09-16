"""Probe: do the 'obvious' bounds fixes for blend_row actually hold up?

Contract assumed (the one a *sprite blender for a framebuffer row* implies):
CLIPPING. Source pixels that land outside dst are dropped; every dst byte
outside the blitted span must come back bit-identical; len(dst) is invariant;
the caller's buffer is mutated in place.

Discriminating setup: dst = bytearray([7]*16) sentinel, src = 10..80 ramp
(a ramp is required, otherwise a source-offset bug is invisible).
A correct clip writes (7+p)//2 for the right source pixels and leaves every
other byte at exactly 7.

Run: python3 probe_pseudo_fixes.py
"""

W = 16
SENT = 7
SRC = [10 * (k + 1) for k in range(8)]  # [10,20,...,80]


def expected(start_x, src=SRC, width=W):
    """Independent oracle: clip the blit to [0, width)."""
    out = bytearray([SENT] * width)
    for i, p in enumerate(src):
        j = start_x + i
        if 0 <= j < width:
            out[j] = (out[j] + p) // 2
    return out


# ---------------------------------------------------------------- variants
def v0_original(dst, src, start_x):
    for i, pixel in enumerate(src):
        dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v1_guard_full(dst, src, start_x):          # forgets start_x >= 0
    if start_x + len(src) <= len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v2_guard_left(dst, src, start_x):          # forgets the right bound
    if start_x >= 0:
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v3_guard_len(dst, src, start_x):           # ignores start_x entirely
    if len(src) <= len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v4_strict_lt(dst, src, start_x):           # '<' where '<=' is needed
    if 0 <= start_x and start_x + len(src) < len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v5_offbyone(dst, src, start_x):            # len-1 <= len: accepts one past end
    if 0 <= start_x and start_x + len(src) - 1 <= len(dst):
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v6_clamp(dst, src, start_x):               # teleports the sprite on-screen
    sx = max(0, min(start_x, len(dst) - len(src)))
    for i, pixel in enumerate(src):
        dst[sx + i] = (dst[sx + i] + pixel) // 2
    return dst


def v7_modulo(dst, src, start_x):              # torus wrap: never crashes
    for i, pixel in enumerate(src):
        j = (start_x + i) % len(dst)
        dst[j] = (dst[j] + pixel) // 2
    return dst


def v8_swallow(dst, src, start_x):             # loud crash -> silent truncation
    try:
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    except IndexError:
        pass
    return dst


def v9_per_index(dst, src, start_x):           # the minimal CORRECT fix
    for i, pixel in enumerate(src):
        j = start_x + i
        if 0 <= j < len(dst):
            dst[j] = (dst[j] + pixel) // 2
    return dst


def v10_zip_slice(dst, src, start_x):          # pythonic; negative slice = tail
    for i, (_d, p) in enumerate(zip(dst[start_x:], src)):
        dst[start_x + i] = (dst[start_x + i] + p) // 2
    return dst


def v11_copy(dst, src, start_x):               # breaks in-place aliasing
    out = list(dst)
    for i, pixel in enumerate(src):
        if 0 <= start_x + i < len(out):
            out[start_x + i] = (out[start_x + i] + pixel) // 2
    return out


def v12_min_only(dst, src, start_x):           # right side clipped, left not
    n = min(len(src), len(dst) - start_x)
    for i in range(n):
        dst[start_x + i] = (dst[start_x + i] + src[i]) // 2
    return dst


def v13_zero_clamp(dst, src, start_x):         # left clamp only, no right guard
    sx = max(start_x, 0)
    for i, pixel in enumerate(src):
        dst[sx + i] = (dst[sx + i] + pixel) // 2
    return dst


def v14_grow(dst, src, start_x):               # destroys the row-width invariant
    while len(dst) < start_x + len(src):
        dst.append(0)
    for i, pixel in enumerate(src):
        dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


def v17_raise(dst, src, start_x):              # strict-reject contract
    if start_x < 0 or start_x + len(src) > len(dst):
        raise ValueError("blit out of range")
    for i, pixel in enumerate(src):
        dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst


VARIANTS = [
    ("v0  original (no fix)", v0_original),
    ("v1  if x+len<=len(dst)", v1_guard_full),
    ("v2  if x>=0", v2_guard_left),
    ("v3  if len(src)<=len(dst)", v3_guard_len),
    ("v4  right bound strict <", v4_strict_lt),
    ("v5  x+len-1 <= len(dst)", v5_offbyone),
    ("v6  clamp start_x", v6_clamp),
    ("v7  (x+i) %% len(dst)", v7_modulo),
    ("v8  try/except IndexError", v8_swallow),
    ("v9  per-index 0<=j<len", v9_per_index),
    ("v10 zip(dst[x:], src)", v10_zip_slice),
    ("v11 copy -> list(dst)", v11_copy),
    ("v12 min(...) only", v12_min_only),
    ("v13 if x<0: x=0", v13_zero_clamp),
    ("v14 grow dst to fit", v14_grow),
    ("v17 raise ValueError", v17_raise),
]

CASES = [0, 8, 12, -4, -8, 16, -20]


def check(fn, sx):
    want = expected(sx)
    dst = bytearray([SENT] * W)
    try:
        ret = fn(dst, SRC, sx)
    except Exception as e:                       # noqa: BLE001
        return "EXC:" + type(e).__name__
    parts = []
    if ret is not dst:
        parts.append("ALIAS")
    if len(dst) != W:
        parts.append("LEN=%d" % len(dst))
    if bytes(dst) != bytes(want):
        bad = [k for k in range(W) if dst[k] != want[k]]
        s = "WRONG@" + ",".join(map(str, bad[:3]))
        if len(bad) > 3:
            s += ",+%d more" % (len(bad) - 3)
        parts.append(s)
    return "|".join(parts) if parts else "ok"


def main():
    print("cases (start_x, expected effect)   [ramp src 10..80, sentinel dst 7]")
    for sx in CASES:
        e = expected(sx)
        touched = [k for k in range(W) if e[k] != SENT]
        print("  x=%-4d touches dst[%s]" % (sx, ",".join(map(str, touched)) or "-"))
    print()

    head = "%-27s" % "variant"
    for sx in CASES:
        head += "%-14s" % ("x=%d" % sx)
    print(head)
    print("-" * len(head))
    for name, fn in VARIANTS:
        row = "%-27s" % name
        for sx in CASES:
            row += "%-14s" % check(fn, sx)
        print(row)

    # ---- assert-only guard: same source, two optimization levels -------------
    print("\nassert-only guard (same source compiled at optimize=0 / 1):")
    code = (
        "def v15_assert(dst, src, start_x):\n"
        "    assert 0 <= start_x and start_x + len(src) <= len(dst)\n"
        "    for i, pixel in enumerate(src):\n"
        "        dst[start_x + i] = (dst[start_x + i] + pixel) // 2\n"
        "    return dst\n"
    )
    for opt in (0, 1):
        ns = {}
        exec(compile(code, "<v15>", "exec", optimize=opt), ns)  # noqa: S102
        dst = bytearray([SENT] * W)
        try:
            ns["v15_assert"](dst, SRC, 12)
            outcome = "no error, dst[12:]=%s  <-- guard vanished" % list(dst[12:])
        except Exception as e:  # noqa: BLE001
            outcome = "%s" % type(e).__name__
        print("  optimize=%d -> %s" % (opt, outcome))

    # ---- literal special-case ------------------------------------------------
    print("\nliteral special-case  if start_x == -10: return dst   (the reported value):")

    def v16_special(dst, src, start_x):
        if start_x == -10:
            return dst
        for i, pixel in enumerate(src):
            dst[start_x + i] = (dst[start_x + i] + pixel) // 2
        return dst

    for sx in (-10, -11):
        dst = bytearray([SENT] * W)
        try:
            v16_special(dst, SRC, sx)
            print("  x=%-4d -> dst changed: %s" % (sx, bytes(dst) != bytes([SENT] * W)))
        except Exception as e:  # noqa: BLE001
            print("  x=%-4d -> %s" % (sx, type(e).__name__))

    # ---- what a silent left wrap actually does (v1, x=-10) -------------------
    print("\nsilent left wrap, v1 with x=-10 (src len 8, dst len 16):")
    dst = bytearray([SENT] * W)
    v1_guard_full(dst, SRC, -10)
    print("  guard passed? %s" % (-10 + len(SRC) <= W))
    print("  dst after   : %s" % list(dst))
    print("  -> wrote the row's TAIL (indices 6..13) with the sprite's head.")


if __name__ == "__main__":
    main()
