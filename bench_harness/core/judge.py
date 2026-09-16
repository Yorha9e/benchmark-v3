"""
Critic Expert Judge Evaluator (LLM-as-a-Judge).

Provides independent, objective multi-level depth evaluation of candidate
code review findings against strict, standardized 4-level rubrics (L1~L4)
anchored directly on ground-truth source code snippets and verified threat mechanics.

Features:
1. Target source code inlined into every judge prompt (no blind judging).
2. Per-task threat mechanics, exploitability proofs, and gold fix requirements.
3. Explicit pseudo-fix trap blacklists with mandatory disqualification rules.
4. Mandatory Quote-Required evidence extraction to eliminate hallucinated grades.
5. Strict JSON schema output with transparent audit logging (judge_verdict.json).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from benchmark_v3.bench_harness.drivers.base import BaseDriver, DriverResponse

RUBRIC_PROMPT_TEMPLATE = """You are an expert security auditor and code reviewer serving as an objective benchmark judge.
Your mission: evaluate the technical depth and quality of a candidate model's code review finding against the ground-truth defect.

=== Target Code Under Audit: {file} ===
```python
{code_snippet}
```

=== Ground-Truth Defect Specification ===
- Defect Classification: {flaw_description}
- Runtime & Language Mechanics: {flaw_mechanics}
- Exploitation & Attack Vectors (L3 Threshold): {exploit_vectors}
- Production-Grade Defense Standard (L4 Threshold): {expected_fix}
- Pseudo-Fix Blacklist (MUST DISQUALIFY FROM L4): {pseudo_fixes}

=== Candidate Finding Under Evaluation ===
- Candidate Root Cause Analysis:
{candidate_root_cause}

- Candidate Proposed Fix:
{candidate_fix}

=== Strict 4-Level Evaluation Rubric (Non-Negotiable) ===
* L1 (1.0 pt) - Superficial: Points out a line or generic issue without explaining the underlying security or runtime mechanics.
* L2 (2.5 pts) - Mechanism Understood: Accurately articulates the core vulnerability mechanics (e.g. timing side-channel via early-exit, relative traversal or absolute path root-discard in os.path.join, slice silent clamping, or negative index toroidal wrapping). But the fix proposed is incomplete or falls into a Pseudo-Fix trap.
* L3 (3.5 pts) - Exploit / Attack Scenario Articulated: Substantially meets L2 AND explains concrete exploitation conditions, crafted inputs, or attack scenarios (e.g. byte-by-byte prefix oracle / Timeless Timing, overwriting authorized_keys/cron for RCE, malformed payload inducing silent corruption or DoS, negative start_x corrupting buffer tail).
* L4 (5.0 pts) - Production-Grade Defense: Fully meets L2/L3 AND proposes a genuine production-grade, mathematically sound defense that avoids ALL pseudo-fix traps (e.g. hmac.compare_digest with type checks, os.path.commonpath with realpath / Path.is_relative_to with O_NOFOLLOW, exact pre-slice bounds check with strict EOF, or viewport span clipping).

=== Universal Disqualifiers & Grading Rules ===
1. PSEUDO-FIX DISQUALIFIER: If the candidate proposes a known pseudo-fix listed in the blacklist (e.g., zip() truncation bypass, naive string replace("../", ""), len(blob) < count confusion, single-sided bounds check, basename() sanitiser), CAP the score at L1 (max 1.0 pt). A blacklisted pseudo-fix is a disqualifying defect even when paired with other text.
2. HALLUCINATION PENALTY: If the candidate hallucinates C-style memory corruption (e.g. claims Python slice or Python integers cause C heap overflow / memory disclosure), penalize as hallucination.
3. MANDATORY QUOTE: You MUST extract a verbatim sentence or phrase from the candidate finding as `evidence_quote` justifying why the level was awarded. If no evidence exists in the candidate text, assign L1.

Evaluate strictly. Output ONLY valid JSON in this schema:
{{
    "level": "L1" | "L2" | "L3" | "L4",
    "score": 1.0 | 2.5 | 3.5 | 5.0,
    "evidence_quote": "<verbatim quote from candidate finding demonstrating this level>",
    "reasoning": "<1-2 concise sentences justifying the level and noting any pseudo-fixes or missing elements>"
}}
"""

FLAW_DOSSIERS: dict[str, dict[str, str]] = {
    "session_tokens.py": {
        "code_snippet": (
            "def check_session_token(presented, expected):\n"
            "    if len(presented) != len(expected):\n"
            "        return False\n"
            "    for a, b in zip(presented, expected):\n"
            "        if a != b:\n"
            "            return False\n"
            "    return True\n\n"
            "def make_guest_token(user):\n"
            "    return 'guest-%s' % user"
        ),
        "description": "CWE-208: Timing Side-Channel via early-exit comparison and length oracle",
        "mechanics": (
            "1. len(presented) != len(expected) exits in O(1) time, leaking exact token length.\n"
            "2. if a != b: return False exits on first mismatch, making execution time proportional to common prefix length.\n"
            "3. Secondary context: make_guest_token has zero cryptographic entropy and is deterministically forgeable."
        ),
        "exploit_vectors": (
            "Attacker uses timing differences to build a byte-by-byte prefix oracle, reducing search space from exponential (16^32) "
            "to linear (16 * 32 = 512 probes). Exploitable over LAN/cloud co-location via statistical t-test or over WAN via HTTP/2 Timeless Timing Attacks."
        ),
        "expected_fix": (
            "Use hmac.compare_digest(presented, expected) or secrets.compare_digest with strict type checks (isinstance str/bytes). "
            "For variable-length secret tokens, Double-HMAC with an ephemeral blind key is standard."
        ),
        "pseudo_fixes": (
            "1. Removing len check and using zip() with |= XOR: Python zip truncates on shorter sequence, allowing empty string '' "
            "or short prefix to bypass authentication completely! (Critical bypass trap).\n"
            "2. Raw SHA-256 == comparison: bytes.__eq__ in CPython is still a short-circuiting memcmp.\n"
            "3. Pure Python bitwise loops: interpreter bytecode and GC jitter cannot guarantee constant machine instructions.\n"
            "4. Artificial random sleep: noise is easily filtered out by statistical averaging."
        ),
    },
    "archive_import.py": {
        "code_snippet": (
            "STORAGE_ROOT = '/var/arcade/packs'\n\n"
            "def import_pack(member_name, data):\n"
            "    dest = os.path.join(STORAGE_ROOT, member_name)\n"
            "    with open(dest, 'wb') as handle:\n"
            "        handle.write(data)\n"
            "    return dest"
        ),
        "description": "CWE-22 / CWE-36: Path Traversal and Arbitrary File Overwrite via unvalidated member_name",
        "mechanics": (
            "1. os.path.join does not normalize paths; '../' relative sequences survive and traverse out of STORAGE_ROOT.\n"
            "2. CRITICAL ANCHOR: If member_name starts with '/' (absolute path), os.path.join DISCARDS STORAGE_ROOT completely!\n"
            "3. open(dest, 'wb') truncates existing files upon open, creating a destructive arbitrary file overwrite primitive."
        ),
        "exploit_vectors": (
            "Overwriting ~/.ssh/authorized_keys for passwordless SSH root login, writing to /etc/cron.d/ for scheduled root RCE, "
            "or overwriting web templates (Jinja2/PHP) / Python .pth packages to execute arbitrary code."
        ),
        "expected_fix": (
            "Resolve paths with os.path.realpath (or Path.resolve()), verify containment using os.path.commonpath([root, dest]) == root "
            "(or Path.is_relative_to(root)), and open using os.open with O_CREAT | O_EXCL | O_NOFOLLOW to block symlinks and race conditions."
        ),
        "pseudo_fixes": (
            "1. Single-pass string replace('../', ''): easily bypassed by '....//'.\n"
            "2. Global replace('..', ''): strips '../etc/passwd' into '/etc/passwd' (absolute path).\n"
            "3. Sibling prefix bypass: dest.startswith(STORAGE_ROOT) without trailing separator allows escaping into '/var/arcade/packs_evil'.\n"
            "4. Pure abspath without realpath: fails to resolve symlinks/junctions, vulnerable to symlink traversal."
        ),
    },
    "config_codec.py": {
        "code_snippet": (
            "def decode_level(blob):\n"
            "    count = blob[0]\n"
            "    entries = []\n"
            "    off = 1\n"
            "    for _ in range(count):\n"
            "        (size,) = struct.unpack_from('>H', blob, off)\n"
            "        off += 2\n"
            "        entries.append(blob[off:off + size].decode('utf-8'))\n"
            "        off += size\n"
            "    return entries"
        ),
        "description": "CWE-1284 / CWE-20: Untrusted length field causing slice silent clamping and unhandled decoding exceptions",
        "mechanics": (
            "1. Empty input blob causes IndexError on blob[0].\n"
            "2. off + 2 > len(blob) causes struct.unpack_from to crash with struct.error.\n"
            "3. PYTHON SLICE CLAMPING TRAP: blob[off:off+size] does NOT raise IndexError when off+size > len(blob)! "
            "It silently clamps and returns whatever partial bytes exist, causing silent data corruption.\n"
            "4. Truncated slices cutting through multi-byte UTF-8 sequences trigger unhandled UnicodeDecodeError.\n"
            "5. Missing strict EOF check allows trailing smuggled payloads."
        ),
        "exploit_vectors": (
            "Crafting truncated 1-byte payloads to trigger DoS crashes, sending oversized size fields to induce silent "
            "data corruption where truncated strings are accepted as valid, or smuggling unparsed payloads past the declared count."
        ),
        "expected_fix": (
            "Validate len(blob) >= 1 at entry; validate off + 2 <= len(blob) before unpack; validate off + size <= len(blob) "
            "before slicing; catch UnicodeDecodeError and raise clean domain ValueError; enforce strict EOF (off == len(blob))."
        ),
        "pseudo_fixes": (
            "1. len(blob) < count confusion: count is entry count, not byte count; fails completely when count=1 and size=65535.\n"
            "2. Single-sided check: checking off + 2 <= len(blob) but forgetting off + size <= len(blob).\n"
            "3. Catch-all try...except returning []: fail-open pseudo-fix that hides data corruption as an empty list.\n"
            "4. Claiming C-style heap buffer overflow / Heartbleed memory disclosure: hallucination (Python slices cannot read outside buffer)."
        ),
    },
    "pixel_blend.py": {
        "code_snippet": (
            "def blend_row(dst, src, start_x):\n"
            "    for i, pixel in enumerate(src):\n"
            "        dst[start_x + i] = (dst[start_x + i] + pixel) // 2\n"
            "    return dst"
        ),
        "description": "CWE-129 / CWE-787: Negative index wrap-around and unvalidated horizontal bounds",
        "mechanics": (
            "1. PYTHON NEGATIVE INDEX TRAP: When start_x < 0, dst[start_x + i] does NOT raise an error! It legally accesses "
            "from the tail of the array (e.g. dst[-1]), silently corrupting the right side of the row without notice.\n"
            "2. Positive overflow: When start_x + len(src) > len(dst), previous pixels are committed in-place before "
            "IndexError crashes mid-loop, leaving dst in an irreversible partially blended dirty state."
        ),
        "exploit_vectors": (
            "Supplying negative start_x to silently tear and corrupt HUD or opposite scanline pixels without raising exceptions; "
            "supplying oversized start_x to trigger DoS crashes with partial state damage."
        ),
        "expected_fix": (
            "Implement 1D viewport clipping: compute intersecting span [src_start : src_end] with "
            "src_start = max(0, -start_x) and src_end = min(len(src), len(dst) - start_x), safely skipping off-screen pixels; "
            "OR perform strict atomic dual-boundary check: 0 <= start_x and start_x + len(src) <= len(dst) before writing."
        ),
        "pseudo_fixes": (
            "1. Single-sided upper check: if start_x + len(src) <= len(dst) -> accepts negative start_x (e.g. -10 + 8 = -2 <= 16), "
            "completely failing to stop negative index wrap-around!\n"
            "2. Assert-based checks: stripped under python -O in production.\n"
            "3. try...except IndexError: pass: converts a loud crash into silent truncated rendering, leaving dirty state.\n"
            "4. Dynamic array expansion: mutating dst length breaks fixed scanline width invariants."
        ),
    },
}


class CriticJudgeEvaluator:
    """Evaluates Critic findings using an independent judge model or strict heuristic fallback."""

    def __init__(self, judge_driver: BaseDriver | None = None) -> None:
        self.judge_driver = judge_driver

    def evaluate_findings_depth(
        self,
        matched_findings: dict[str, dict[str, Any]],  # flaw_file -> finding dict
    ) -> dict[str, Any]:
        """
        Evaluate depth for each matched flaw finding.
        Returns a dict with per-flaw verdict, total depth score (max 20.0), and audit metadata.
        """
        verdicts: dict[str, dict[str, Any]] = {}
        total_depth = 0.0

        for flaw_file, flaw_meta in FLAW_DOSSIERS.items():
            finding = matched_findings.get(flaw_file)
            if not finding:
                verdicts[flaw_file] = {
                    "level": "L0",
                    "score": 0.0,
                    "evidence_quote": "",
                    "reasoning": "Defect was not recalled or matched by the candidate.",
                }
                continue

            root_cause = str(finding.get("root_cause", ""))
            fix = str(finding.get("fix", ""))

            if self.judge_driver is not None:
                # LLM-as-a-Judge 专家模型独立判定 (带完整代码、机理与引证要求)
                verdict = self._evaluate_with_judge(flaw_file, flaw_meta, root_cause, fix)
            else:
                # 确定性严格量表启发式兜底 (Provisional Fallback)
                verdict = self._evaluate_strict_heuristic(flaw_file, root_cause, fix)

            verdicts[flaw_file] = verdict
            total_depth += verdict["score"]

        total_depth = round(min(20.0, total_depth), 2)
        return {
            "verdicts": verdicts,
            "total_depth": total_depth,
            "judge_model": self.judge_driver.model_id if self.judge_driver else "strict_heuristic_fallback",
            "provisional": self.judge_driver is None,
        }

    def evaluate_novel_findings(
        self,
        unmatched_findings: list[dict[str, Any]],
        codebase: dict[str, str],
    ) -> dict[str, Any]:
        """
        Evaluate candidate findings that fall outside the 4 primary ground-truth flaws.
        Awards discretionary bonus points (+2.5 per valid defect, max +5.0) for authentic
        secondary flaws (e.g. predictable guest token, unhandled exceptions) while rejecting
        stylistic or hallucinated claims.
        """
        bonus_total = 0.0
        novel_verdicts: list[dict[str, Any]] = []

        for f in unmatched_findings:
            raw_file = str(f.get("file", "")).strip().replace("\\", "/")
            file_name = Path(raw_file).name
            root_cause = str(f.get("root_cause", ""))
            fix = str(f.get("fix", ""))
            cat = str(f.get("category", ""))
            line = f.get("line", "?")

            if self.judge_driver is not None:
                code_src = codebase.get(file_name, "")
                prompt = (
                    f"You are an expert security auditor evaluating an extra finding submitted by a candidate model.\n"
                    f"This finding was NOT in our primary ground-truth flaw list.\n\n"
                    f"File Under Audit: {file_name}\n"
                    f"Code:\n```python\n{code_src}\n```\n\n"
                    f"Candidate Finding:\n"
                    f"Line: {line} | Category: {cat}\n"
                    f"Root Cause: {root_cause}\n"
                    f"Proposed Fix: {fix}\n\n"
                    f"Evaluate: Does this finding identify an AUTHENTIC, VALID secondary software defect, logic vulnerability, "
                    f"or real security weakness in the code (such as predictable guest token generation, zero entropy, DoS via unbounded parameters, or unhandled exceptions)?\n"
                    f"Reject code style preferences, harmless naming complaints, or accusations against documented preconditions.\n\n"
                    f"Return strict JSON:\n"
                    f'{{\n  "is_valid_novel_defect": true | false,\n  "bonus_score": 0.0 | 2.5,\n  "reasoning": "<1-2 sentence explanation>"\n}}'
                )
                try:
                    resp = self.judge_driver.chat([{"role": "user", "content": prompt}], temperature=0.0)
                    m = re.search(r"\{.*\}", resp.content, re.DOTALL)
                    if m:
                        parsed = json.loads(m.group(0))
                        if parsed.get("is_valid_novel_defect") and bonus_total < 5.0:
                            sc = float(parsed.get("bonus_score", 2.5))
                            bonus_total = min(5.0, bonus_total + sc)
                            novel_verdicts.append({
                                "file": file_name,
                                "line": line,
                                "bonus_score": sc,
                                "reasoning": parsed.get("reasoning", "Valid novel defect approved by judge."),
                            })
                            continue
                except Exception:
                    pass

            # 启发式规则兜底
            text = f"{cat} {root_cause} {fix}".lower()
            if file_name == "session_tokens.py" and any(k in text for k in ("guest", "entropy", "predictable", "cwe-330", "cwe-340", "forge")):
                if bonus_total < 5.0:
                    bonus_total = min(5.0, bonus_total + 2.5)
                    novel_verdicts.append({
                        "file": file_name,
                        "line": line,
                        "bonus_score": 2.5,
                        "reasoning": "额外发现了 make_guest_token 凭证零随机熵、确定性可伪造的安全缺陷 (CWE-330)。",
                    })
            elif file_name == "config_codec.py" and any(k in text for k in ("count = 255", "amplification", "dos", "255")):
                if bonus_total < 5.0:
                    bonus_total = min(5.0, bonus_total + 2.5)
                    novel_verdicts.append({
                        "file": file_name,
                        "line": line,
                        "bonus_score": 2.5,
                        "reasoning": "额外分析了 count=255 放大与未处理异常 DoS 风险。",
                    })

        return {
            "bonus_total": round(bonus_total, 2),
            "novel_verdicts": novel_verdicts,
        }

    def _evaluate_with_judge(
        self,
        flaw_file: str,
        flaw_meta: dict[str, str],
        root_cause: str,
        fix: str,
    ) -> dict[str, Any]:
        prompt = RUBRIC_PROMPT_TEMPLATE.format(
            file=flaw_file,
            code_snippet=flaw_meta["code_snippet"],
            flaw_description=flaw_meta["description"],
            flaw_mechanics=flaw_meta["mechanics"],
            exploit_vectors=flaw_meta["exploit_vectors"],
            expected_fix=flaw_meta["expected_fix"],
            pseudo_fixes=flaw_meta["pseudo_fixes"],
            candidate_root_cause=root_cause or "(empty)",
            candidate_fix=fix or "(empty)",
        )

        try:
            response = self.judge_driver.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            raw = response.content.strip()
            # 提取 JSON 块
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                level = str(parsed.get("level", "L1")).upper()
                score = float(parsed.get("score", 1.0))
                evidence = str(parsed.get("evidence_quote", ""))
                reasoning = str(parsed.get("reasoning", "Assigned by expert judge."))
                return {
                    "level": level,
                    "score": min(5.0, max(0.0, score)),
                    "evidence_quote": evidence,
                    "reasoning": reasoning,
                }
        except Exception:
            pass

        # 裁判调用失败时平滑回退到严格启发式
        return self._evaluate_strict_heuristic(flaw_file, root_cause, fix)

    def _evaluate_strict_heuristic(
        self,
        flaw_file: str,
        root_cause: str,
        fix: str,
    ) -> dict[str, Any]:
        """严格四级量表启发式判定（不放水，区分 L1 到 L4，含伪修复检测）"""
        combined = f"{root_cause}\n{fix}".lower()

        if flaw_file == "session_tokens.py":
            # 伪修复一票否决检测：提出 zip 循环且未提及长度保护
            if "zip(" in combined and "diff |=" in combined and not any(k in combined for k in ("len(", "length", "guard")):
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "zip truncation pseudo-fix",
                    "reasoning": "伪修复陷阱：使用 zip() 且未做长度保护，导致空字符串或短前缀直接绕过认证！",
                }

            has_production_fix = any(k in combined for k in ("compare_digest", "constant_time", "constant-time")) and "not use compare_digest" not in combined
            has_attack_vector = any(k in combined for k in ("prefix", "leak", "side-channel", "side channel", "timing attack", "measure", "timeless"))
            has_mechanism = any(k in combined for k in ("early exit", "early-exit", "mismatch", "short-circuit", "different time"))

            if has_production_fix and (has_attack_vector or has_mechanism):
                return {
                    "level": "L4",
                    "score": 5.0,
                    "evidence_quote": "hmac.compare_digest / constant-time",
                    "reasoning": "提出了 hmac.compare_digest 或恒定时间比较等生产级修复。",
                }
            if has_attack_vector and has_mechanism:
                return {
                    "level": "L3",
                    "score": 3.5,
                    "evidence_quote": "timing oracle / prefix recovery",
                    "reasoning": "准确分析了前缀逐字节时序泄漏与测量攻击机理。",
                }
            if has_mechanism:
                return {
                    "level": "L2",
                    "score": 2.5,
                    "evidence_quote": "early exit / execution time",
                    "reasoning": "指出了提前退出导致耗时不同的根因，但未深入分析利用或生产级修复。",
                }
            return {
                "level": "L1",
                "score": 1.0,
                "evidence_quote": "syntax / loop check",
                "reasoning": "仅提及比较逻辑有问题，缺乏机理分析。",
            }

        elif flaw_file == "archive_import.py":
            # 伪修复一票否决检测
            if "replace('../', '')" in combined or "replace('..', '')" in combined:
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "replace('../', '')",
                    "reasoning": "伪修复陷阱：单次 replace 极易被 '....//' 剥离绕过。",
                }
            if "basename" in combined and not any(
                k in combined
                for k in (
                    "not use basename", "do not use basename", "don't use basename",
                    "never use basename", "avoid basename", "instead of basename",
                )
            ):
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "os.path.basename sanitiser",
                    "reasoning": "伪修复陷阱：盲目 basename 未检验空串与 ..，在部分系统上依然逃逸（一票否决）。",
                }

            has_production_fix = any(k in combined for k in ("commonpath", "is_relative_to", "realpath", "o_nofollow")) and "startswith" not in combined
            has_attack_vector = any(k in combined for k in ("../", "..\\", "traversal", "escape", "overwrite", "authorized_keys", "cron", "rce"))
            has_mechanism = any(k in combined for k in ("relative path", "path join", "join", "outside", "root", "absolute path"))

            if has_production_fix and has_attack_vector:
                return {
                    "level": "L4",
                    "score": 5.0,
                    "evidence_quote": "commonpath / is_relative_to / realpath",
                    "reasoning": "给出了 commonpath 或 Path.is_relative_to 结合 realpath 等生产级防御建议。",
                }
            if has_attack_vector:
                return {
                    "level": "L3",
                    "score": 3.5,
                    "evidence_quote": "arbitrary overwrite / cron / ssh",
                    "reasoning": "给出了包含路径穿越或具体目标覆写的攻击触发场景。",
                }
            if has_mechanism:
                return {
                    "level": "L2",
                    "score": 2.5,
                    "evidence_quote": "os.path.join / relative escape",
                    "reasoning": "指出 os.path.join 无法防御相对路径跨目录或绝对路径覆盖。",
                }
            return {
                "level": "L1",
                "score": 1.0,
                "evidence_quote": "path join",
                "reasoning": "仅泛泛提及文件名或路径拼接问题。",
            }

        elif flaw_file == "config_codec.py":
            # 伪修复陷阱一票否决
            if "len(blob) < count" in combined or "len(blob) >= count" in combined:
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "len(blob) < count",
                    "reasoning": "伪修复陷阱：混淆了条目数量(count)与缓冲区字节长度。",
                }
            if "except exception" in combined and "[]" in combined:
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "except Exception: return []",
                    "reasoning": "伪修复陷阱：使用粗暴 try-except 吞异常返回空列表，掩盖数据损坏（Fail-Open 一票否决）。",
                }

            has_production_fix = (
                any(k in combined for k in ("off + size", "len(blob) - off", "off+size", "total_len"))
                and any(k in combined for k in ("len(blob)", "total_len", "len(view)"))
            )
            has_attack_vector = any(k in combined for k in ("truncated", "malformed", "crafted", "clamping", "desync", "dos", "crash"))
            has_mechanism = any(k in combined for k in ("slice", "size", "clamp", "unpacked", "length field", "unpack_from"))

            if has_production_fix and (has_attack_vector or has_mechanism):
                return {
                    "level": "L4",
                    "score": 5.0,
                    "evidence_quote": "off + size <= len(blob) bounds check",
                    "reasoning": "指出必须在解码前精确校验 off + size <= len(blob) 与头部边界的生产级防护。",
                }
            if has_attack_vector and has_mechanism:
                return {
                    "level": "L3",
                    "score": 3.5,
                    "evidence_quote": "slice truncation / delayed crash",
                    "reasoning": "分析了切片静默截断导致数据损坏或畸形声明长度触发崩溃的场景。",
                }
            if has_mechanism:
                return {
                    "level": "L2",
                    "score": 2.5,
                    "evidence_quote": "size length field unvalidated",
                    "reasoning": "指出 size 长度字段未做校验导致切片或解包异常的机理。",
                }
            return {
                "level": "L1",
                "score": 1.0,
                "evidence_quote": "unpack_from error",
                "reasoning": "仅提及解包可能报错。",
            }

        elif flaw_file == "pixel_blend.py":
            # 伪修复陷阱一票否决
            if ("start_x + len(src)" in combined or "start_x+len(src)" in combined) and not any(
                k in combined for k in ("start_x >= 0", "start_x > 0", "0 <=", "0 <", "max(0", "negative")
            ):
                return {
                    "level": "L1",
                    "score": 1.0,
                    "evidence_quote": "start_x + len(src) <= len(dst) without lower bound",
                    "reasoning": "伪修复陷阱：仅校验右侧上限，完全忽略负坐标 start_x < 0 静默尾部回绕（一票否决）。",
                }
            if "except indexerror" in combined and "pass" in combined:
                return {
                    "level": "L2",
                    "score": 2.5,
                    "evidence_quote": "except IndexError: pass",
                    "reasoning": "伪修复陷阱：使用 try-except pass 粗暴吞掉越界异常，留下半混合脏状态。",
                }

            has_clipping_fix = any(k in combined for k in ("max(0", "min(", "clip", "intersect", "span"))
            has_strict_validation = any(k in combined for k in ("0 <= start_x", "start_x >= 0", "start_x < 0")) and any(
                k in combined for k in ("start_x + len(src)", "start_x+len(src)")
            )
            has_production_fix = has_clipping_fix or has_strict_validation

            has_attack_vector = any(k in combined for k in ("negative", "wrap", "corrupt", "partial", "hud", "right-side", "indexerror", "dos"))
            has_mechanism = any(k in combined for k in ("start_x", "boundary", "index", "len(dst)", "negative index"))

            if has_production_fix and (has_attack_vector or has_mechanism):
                return {
                    "level": "L4",
                    "score": 5.0,
                    "evidence_quote": "viewport clipping / 0 <= start_x dual bounds",
                    "reasoning": "给出了视口相交区间裁剪或完整双向闭环边界前置校验的生产级修复。",
                }
            if has_attack_vector and has_mechanism:
                return {
                    "level": "L3",
                    "score": 3.5,
                    "evidence_quote": "negative wrap / partial state crash",
                    "reasoning": "分析了负索引静默尾部回绕污染或正向越界破坏局部状态的利用场景。",
                }
            if has_mechanism:
                return {
                    "level": "L2",
                    "score": 2.5,
                    "evidence_quote": "start_x index bounds",
                    "reasoning": "指出 start_x 未做边界约束导致 IndexError 或负索引回绕的机理。",
                }
            return {
                "level": "L1",
                "score": 1.0,
                "evidence_quote": "array index",
                "reasoning": "仅指出数组索引可能存在问题。",
            }

        return {
            "level": "L1",
            "score": 1.0,
            "evidence_quote": "general observation",
            "reasoning": "基础观察。",
        }
