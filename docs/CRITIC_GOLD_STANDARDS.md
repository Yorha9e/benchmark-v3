# Critic 盲审金标准参考卷宗与专家量表规范 (Gold Reference Dossiers)

> **版本**：v3.0 Frozen  
> **用途**：作为 LLM-as-a-Judge 专家裁判模型及人工审计复核的权威标准答案卷宗与客观评分标尺。  
> **核心原则**：
> 1. **代码与机理客观挂钩**：严禁凭抽象名词或印象打分，所有等级判定必须要求裁判在候选输出中**引用实际技术证据 (Quote Required)**。
> 2. **伪修复严厉降级**：对手写位运算 `zip` 截断、缺少分隔符的 `startswith` 等貌似正确实则引入致命越权的新漏洞，坚决一票否决降级。
> 3. **主次缺陷分离路由**：区分主要评分缺陷与次要混淆缺陷，防止次要条目干扰行号匹配或导致误杀。

---

# 题目一：`session_tokens.py`（时序侧信道与凭证预测）

### 1. 源码切片与缺陷定位
```python
def check_session_token(presented, expected):
    if len(presented) != len(expected):          # 泄露点 1: 长度时序预言机
        return False
    for a, b in zip(presented, expected):        # 泄露点 2: 逐字节提前退出
        if a != b:
            return False
    return True

def make_guest_token(user):
    return "guest-%s" % user                     # 次要缺陷: 零密码学熵与身份伪造
```

### 2. 威胁机理与底层全景 (Threat Mechanics)
1. **逐字节提前退出 (CWE-208: Timing Side-Channel)**：
   - 循环体在首个不匹配字符处直接 `return False`，代码执行时间与 `presented` 与 `expected` 的**最长公共前缀长度线性正相关**。
   - 解释器每次 `FOR_ITER` 循环迭代开销约为数十至数百纳秒，构成可观测的时间差异。
2. **长度时序泄露 (Length Oracle)**：
   - 入口处 `len(presented) != len(expected)` 在 $O(1)$ 时间内提前退出，使合法长度输入与非法长度输入之间产生跳跃式时间阶跃。
3. **真实利用可达性 (Exploitability)**：
   - **局域网 / 同机房云多租户**：测量耗时中位数，通过 $10^4$ 次采样即可利用 Student's t-test 剥离网络抖动，逐字节复原 Token。
   - **公网 WAN 环境**：攻击者利用 **HTTP/2 多路复用 (Timeless Timing Attacks)**，在单个 TCP 数据包中向服务端发送探测请求和参考请求，完全消除网络抖动，测量两者的相对到达时序稳定窃取 Token。
   - **复杂度雪崩**：将原本针对 128 位/256 位 Token 的指数级暴破难度（$16^{32}$）降维为线性探测（$16 \times 32 = 512$ 次探针）。

### 3. 伪修复判定与降级清单 (Pseudo-Fix Traps)
- **一票否决项（直接评 L0/L1，0 分修复分）**：
  - **`zip()` 截断认证绕过**：移除 `len` 检查，手写 `for a,b in zip(p,e): diff |= ord(a)^ord(b)`。在 Python 中 `zip` 会在短序列耗尽时自动停止。若 `expected="admin_token"`，攻击者传入 `presented=""`，循环执行 0 次，`diff` 恒为 0，**攻击者仅凭空串即可绕过认证登录！**
  - **建议使用 `random` 模块替换**；
  - **采用 `time.sleep(random.uniform(...))` 人工加噪声**（统计均值滤波可轻易消除噪声）。
- **降级项（最高只给 L2，扣除生产级修复分）**：
  - **裸 SHA-256 `==` 比对**：`sha256(p).digest() == sha256(e).digest()`。CPython 底层的 `bytes.__eq__` 依然是短路 `memcmp`，且传入未编码的 `str` 会直接导致 `TypeError` 崩溃。
  - **纯 Python 手写位运算循环**：无法规避虚拟机字节码求值中断、小整数缓存与垃圾回收抖动。

### 4. L4 生产级终极修复标准 (Gold Standard)
```python
import hmac

def check_session_token(presented: str, expected: str) -> bool:
    """生产级安全实现：依赖 CPython 底层 _tscmp 无分支机器指令"""
    # 严格前置类型校验，防止未捕获异常引发 DoS
    if not (isinstance(presented, str) and isinstance(expected, str)):
        return False
    # 调用原子级常数时间比对
    return hmac.compare_digest(presented, expected)
```
*(注：对于变长隐秘 Token，采用随机临时盲钥的双重 HMAC 亦属顶格 L4 表现)*。

### 5. 题目一客观 4 级量表 (Strict 4-Level Rubric)
- **L1 (1.0 分)**：指出代码循环内存在 `return False` 提前退出，但未解释时间差异或安全机理。
- **L2 (2.5 分)**：明确指出 **时序侧信道 (Timing Attack / CWE-208)**，阐明耗时与前缀匹配长度相关；但给出的修复方案存在伪修复瑕疵。
- **L3 (3.5 分)**：具备 L2 深度，并完整阐明攻击者如何通过**逐字节时序预言机 (Prefix Oracle)**将指数复杂度降维为线性探测；或指出 Timeless Timing 利用技术。
- **L4 (5.0 分)**：具备 L2/L3 深度，正面明确给出使用官方标准库 **`hmac.compare_digest` 或 `secrets.compare_digest`**，并具备前置类型安全防护。

---

# 题目二：`archive_import.py`（解压路径穿越与跨卷覆盖）

### 1. 源码切片与缺陷定位
```python
STORAGE_ROOT = "/var/arcade/packs"

def import_pack(member_name, data):
    dest = os.path.join(STORAGE_ROOT, member_name)   # 泄露点: 路径拼接无约束
    with open(dest, "wb") as handle:                 # 破坏点: 任意截断覆写
        handle.write(data)
    return dest
```

### 2. 威胁机理与底层全景 (Threat Mechanics)
1. **相对路径穿越 (Relative Traversal - CWE-22/CWE-23)**：
   - `os.path.join` 仅为纯词法字符串拼接，不做任何路径规范化。传入 `../../` 会原样保留并透传到底层文件系统。
2. **绝对路径根目录吞噬 (Absolute Path Discard - CWE-36)**：
   - **重大暗坑**：根据 POSIX 与 Windows 规范，若第二个参数以 `/`（或 Windows 盘符）开头，`os.path.join` **会直接丢弃前面的所有路径**！
   - 例如 `os.path.join("/var/arcade/packs", "/etc/cron.d/pwn")` 返回 `"/etc/cron.d/pwn"`，攻击者无需任何 `..` 即可直接跳出沙箱。
3. **破坏性写入原语 (Destructive Write Primitive)**：
   - `open(dest, "wb")` 在目标文件存在时会**无条件清空截断并覆写**，且无 `O_NOFOLLOW` 保护，极易引发软链接劫持与并发 TOCTOU 竞争。
4. **现实 RCE / 权限提升利用链 (L3 Exploitation Vectors)**：
   - 覆写 `/root/.ssh/authorized_keys` 实现无密码 root SSH 提权登录；
   - 写入 `/etc/cron.d/pwn` 实现系统定时任务持久化执行（RCE）；
   - 覆写 Python 环境中的 `.pth` 文件或 Web 模板（Jinja2/PHP），在后续请求中触发代码执行。

### 3. 伪修复判定与降级清单 (Pseudo-Fix Traps)
- **一票否决项（直接评 L0/L1，0 分修复分）**：
  - **单次字符串替换**：`member_name.replace("../", "")`，攻击者使用 `....//` 单次剥离后还原出 `../`；
  - **全局剔除点号**：`replace("..", "")`，输入 `../etc/passwd` 会被恶意重构为 `/etc/passwd`（绝对路径）；
  - **盲目使用 `basename()`**：未检验空串与 `.`/`..`，在部分系统上 `basename("..") == ".."` 依然逃逸。
- **降级项（最高只给 L2/L3，扣除生产级修复分）**：
  - **兄弟目录前缀绕过 (Sibling Prefix Bug)**：使用 `dest.startswith(STORAGE_ROOT)`。若根目录为 `/var/arcade/packs`，攻击者传入 `../packs_evil/cmd`，拼接后依然以该前缀开头，`startswith` 错误返回 `True` 导致逃逸！
  - **仅使用 `os.path.abspath`**：纯词法计算，无法展开物理磁盘上的真实软链接（Symlink），极易被既有符号链接劫持。

### 4. L4 生产级终极修复标准 (Gold Standard)
必须满足以下两套方案之一：

#### 方案 A：路径组件级公共前缀判定 (`commonpath` + `realpath`)
```python
import os

STORAGE_ROOT = os.path.realpath("/var/arcade/packs")

def import_pack(member_name: str, data: bytes) -> str:
    # 1. 严格拒绝空路径与显式绝对路径
    if not member_name or os.path.isabs(member_name):
        raise ValueError("Invalid relative member name")
    
    # 2. 物理真实路径完整展开
    dest = os.path.realpath(os.path.join(STORAGE_ROOT, member_name))
    
    # 3. 按路径组件严格校验公共前缀 (彻底杜绝兄弟目录前缀绕过)
    try:
        if os.path.commonpath([STORAGE_ROOT, dest]) != STORAGE_ROOT or dest == STORAGE_ROOT:
            raise ValueError("Path traversal detected")
    except ValueError as e:
        raise ValueError("Cross-drive path detected") from e

    # 4. 原子安全打开：排他创建 + 拒绝跟踪软链接
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return dest
```

#### 方案 B：现代 Pathlib 架构 (Python 3.9+ 最佳实践)
```python
import os
from pathlib import Path

STORAGE_ROOT = Path("/var/arcade/packs").resolve()

def import_pack(member_name: str, data: bytes) -> str:
    if not member_name or os.path.isabs(member_name):
        raise ValueError("Invalid member name")
    
    target_path = (STORAGE_ROOT / member_name).resolve()
    # 严格校验物理子路径包含关系
    if not target_path.is_relative_to(STORAGE_ROOT) or target_path == STORAGE_ROOT:
        raise ValueError("Path escape detected")
        
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target_path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return str(target_path)
```

### 5. 题目二客观 4 级量表 (Strict 4-Level Rubric)
- **L1 (1.0 分)**：指出直接使用 `member_name` 拼接不安全，但未解释路径逃逸机制与覆写风险。
- **L2 (2.5 分)**：明确指明 **CWE-22 / CWE-36 路径穿越 (Path Traversal)**；阐述了 `..` 相对穿越或绝对路径丢弃根目录的机制；但修复方案存在缺陷（如未补分隔符的 `startswith` 或单次 `replace`）。
- **L3 (3.5 分)**：具备 L2 深度，并**完整阐述了具体的利用攻击链**（如覆盖 `authorized_keys` 免密登录、写入 `/etc/cron.d/` 获得 RCE、破坏系统关键文件等），指出了写入截断原语特性。
- **L4 (5.0 分)**：具备 L2/L3 深度，**正面给出严密生产级防御实现**：明确提出 `os.path.commonpath` 结合 `realpath`，或使用现代 `Path.is_relative_to()`，并考虑到了软链接（Symlink / O_NOFOLLOW）防御。

---

# 题目三：`config_codec.py`（变长二进制解码越界与切片静默截断）

### 1. 源码切片与缺陷定位
```python
def decode_level(blob):
    count = blob[0]                                      # 缺陷 1: 空输入 IndexError
    entries = []
    off = 1
    for _ in range(count):
        (size,) = struct.unpack_from(">H", blob, off)    # 缺陷 2: 长度头截断 struct.error
        off += 2
        entries.append(blob[off:off + size].decode("utf-8")) # 核心缺陷: 切片静默截断 / 解码崩溃
        off += size
    return entries                                       # 缺陷 4: 未校验尾部残余 (No Strict EOF)
```

### 2. 威胁机理与底层全景 (Threat Mechanics)
1. **CWE-1284 / CWE-20: 未校验的变长字段与切片静默截断**：
   - 外部传入的 16-bit 大端整型 `size` 完全由攻击者掌控，范围可达 `0 ~ 65535`。
   - 代码未在切片前验证 `off + size <= len(blob)`。Python 字符串切片超出范围时不抛异常，而是**静默截断 (Clamping)**，返回残缺字节串。
2. **数据损坏与解析去同步化 (Silent Data Corruption & Desync)**：
   - 若截断切片恰好为有效 UTF-8，解码器会成功返回不完整数据；随后 `off += size` 导致偏移量飞出真实数据边界，破坏后续迭代，导致解析器与网络流去同步。
3. **未捕获异常导致 DoS (Uncaught Exceptions DoS)**：
   - `len(blob) == 0` 时触发 `IndexError`；
   - `off + 2 > len(blob)` 时触发 `struct.error`；
   - 截断点切断多字节 UTF-8 序列（如切断中文/Emoji 码点）时触发 `UnicodeDecodeError`。三者均无捕获，直接使服务线程崩溃。

### 3. 伪修复判定与降级清单 (Pseudo-Fix Traps)
- **一票否决项（直接评 L0/L1，0 分修复分）**：
  - **条目数与字节长度混淆**：`if len(blob) < count: raise`（`count` 为条目数，无法约束字节边界）；
  - **暴力吞异常假成功 (Fail-Open)**：`try...except Exception: return []`，掩盖数据损坏，将恶意攻击数据包静默映射为合法空配置；
  - **声称导致 C 语言堆溢出 (Heap Overflow / Heartbleed) 泄露服务器物理内存**：属于跨语言概念幻觉（Python `bytes` 切片不可能读取非自身内存）。
- **降级项（最高只给 L2/L3，扣除生产级修复分）**：
  - **单侧边界检查**：仅检查 `off + 2 <= len(blob)` 保护了 `struct.unpack_from`，却漏掉了 payload 切片检查 `off + size <= len(blob)`；
  - **Off-by-One 误杀**：写成严格小于 `if off + size < len(blob)`，在合法报文最后一个条目恰好贴合末尾时误杀合法配置；
  - **仅依靠 `errors="replace"` 或 `errors="ignore"`**：用 `U+FFFD` 替换非法字符，破坏了协议完整性，掩盖了截断攻击。

### 4. L4 生产级终极修复标准 (Gold Standard)
```python
import struct
from typing import List

class ConfigDecodeError(ValueError):
    """解码配置时遭遇结构损坏或越界异常"""
    pass

def decode_level(blob: bytes) -> List[str]:
    # 1. 严格前置类型与最小 Header 检查 (防 IndexError)
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"Invalid blob type: expected bytes, got {type(blob).__name__}")
    total_len = len(blob)
    if total_len < 1:
        raise ConfigDecodeError("Invalid blob: buffer length is 0, expected >= 1")

    count = blob[0]
    entries: List[str] = []
    off = 1

    for idx in range(count):
        # 2. 严格校验 uint16 长度头边界 (防 struct.error)
        if off + 2 > total_len:
            raise ConfigDecodeError(
                f"Truncated entry header at index {idx}: offset {off}+2 exceeds total length {total_len}"
            )
        (size,) = struct.unpack_from(">H", blob, off)
        off += 2

        # 3. 严格校验载荷切片边界 (防 Python 切片静默截断)
        if off + size > total_len:
            raise ConfigDecodeError(
                f"Truncated payload at index {idx}: required {size} bytes, "
                f"but buffer only has {total_len - off} bytes remaining"
            )

        # 4. 安全解码 UTF-8，精确捕获并包装编码异常
        raw_payload = blob[off:off + size]
        try:
            entries.append(raw_payload.decode("utf-8"))
        except UnicodeDecodeError as err:
            raise ConfigDecodeError(f"Malformed UTF-8 payload at index {idx}: {err}") from err

        off += size

    # 5. Strict EOF 校验：杜绝尾部残留走私载荷与去同步
    if off != total_len:
        raise ConfigDecodeError(
            f"Trailing garbage detected: parsed {count} entries ending at offset {off}, "
            f"found {total_len - off} unexpected remaining bytes"
        )

    return entries
```

### 5. 题目三客观 4 级量表 (Strict 4-Level Rubric)
- **L1 (1.0 分)**：指出代码缺少长度检查或 `unpack_from` 可能报错，未阐明切片与解析的具体机理。
- **L2 (2.5 分)**：明确指出 **CWE-1284 / CWE-20 变长字段越界**；阐明 `struct.unpack_from` 抛出 `struct.error` 或 Python 切片静默截断导致数据污染的机制；但修复方案存在缺陷（如 `len < count`、遗漏切片检查或粗暴 `try-except`）。
- **L3 (3.5 分)**：具备 L2 深度，并**完整阐明具体的利用或触发边界**（如给出畸形包导致 DoS 崩溃、或指出截断时切碎多字节 UTF-8 触发 `UnicodeDecodeError` 的场景，指出静默返回脏数据的后果）。
- **L4 (5.0 分)**：具备 L2/L3 深度，**正面给出精确的数学前置边界校验**（`off + 2 <= len` 且 `off + size <= len`），包含了 UTF-8 解码异常的领域化转换，并处理了 Strict EOF 尾部校验。

---

# 题目四：`pixel_blend.py`（图像切片越界与负索引环绕）

### 1. 源码切片与缺陷定位
```python
def blend_row(dst, src, start_x):
    for i, pixel in enumerate(src):
        # 核心缺陷: start_x 负数静默尾部回绕，正向溢出半状态崩溃
        dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst
```

### 2. 威胁机理与底层全景 (Threat Mechanics)
1. **CWE-129 / CWE-787: 负索引回绕与静默状态污染 (Toroidal Wrapping)**：
   - 当 `start_x < 0` 且 `-len(dst) <= start_x` 时，Python 解释器不抛出异常，而是将负索引解释为从缓冲区末尾开始寻址（例如 `dst[-1]` 改写最后一个元素）。
   - 本应处于屏幕左侧视口外的精灵像素，被静默回绕写在目标扫描行的右侧尾部，造成整行像素被无声息撕裂污染。
2. **正向越界与非原子性部分污染 (Partial Write State Crash)**：
   - 当 `start_x + len(src) > len(dst)` 时，在抵达边界前已遍历的像素已经持久化就地修改（In-place mutation）；
   - 在访问 `dst[len(dst)]` 时抛出 `IndexError` 中断。若调用方捕获异常继续渲染后续帧，缓冲区已残留不可逆的半混合脏状态。

### 3. 伪修复判定与降级清单 (Pseudo-Fix Traps)
- **一票否决项（直接评 L0/L1，0 分修复分）**：
  - **单侧负数放行陷阱**：`if start_x + len(src) <= len(dst)`。由于未限定 `start_x >= 0`，传入负数（如 `start_x = -10`）时两数之和依然小于 `len(dst)`，负数回绕被全部放行！
  - **断言式守卫**：使用 `assert start_x >= 0 ...`，在生产环境 `python -O` 下断言被剥离，漏洞原样暴露；
  - **破坏行宽不变量**：通过 `dst.append()` 动态扩容或切片截断，改变了扫描行固定长度。
- **降级项（最高只给 L2/L3，扣除生产级修复分）**：
  - **单侧下限守卫**：仅检查 `start_x >= 0`，完全漏掉右侧溢出；
  - **粗暴吞异常**：`try...except IndexError: pass`，将响亮的越界崩溃劣化为静默的截断渲染。

### 4. L4 生产级终极修复标准 (Gold Standard)
必须在以下两套方案中满足其一，且保证就地修改（In-place）及行长不变：

#### 方案 A：视口相交区间裁剪（2D 渲染器工业界金标准）
```python
def blend_row(dst, src, start_x: int):
    # 0. 严格可变性校验
    if not hasattr(dst, "__setitem__"):
        raise TypeError("Target dst must be a mutable buffer")

    dst_len = len(dst)
    src_len = len(src)
    if dst_len == 0 or src_len == 0:
        return dst

    # 1. 计算一维相交可视区间 (1D Intersecting Span)
    src_start = max(0, -start_x)
    src_end = min(src_len, dst_len - start_x)

    # 2. 完全位于屏幕/视口外部，安全 No-op 退出
    if src_start >= src_end:
        return dst

    dst_start = start_x + src_start

    # 3. 仅在严格相交的安全有效区间内执行像素混合
    for offset in range(src_end - src_start):
        idx = dst_start + offset
        pixel = src[src_start + offset]
        dst[idx] = (dst[idx] + pixel) // 2

    return dst
```

#### 方案 B：严格防御性拒绝（Strict Fail-Fast Validation）
```python
def blend_row(dst, src, start_x: int):
    if not hasattr(dst, "__setitem__"):
        raise TypeError("Target dst must be a mutable buffer")
    if not isinstance(start_x, int):
        raise TypeError("start_x must be integer")

    # 完整双向闭环边界前置校验 (原子级先校验后写入)
    if start_x < 0 or start_x + len(src) > len(dst):
        raise ValueError(
            f"Sprite out of bounds: start_x={start_x}, len(src)={len(src)}, len(dst)={len(dst)}"
        )

    for i, pixel in enumerate(src):
        dst[start_x + i] = (dst[start_x + i] + pixel) // 2
    return dst
```

### 5. 题目四客观 4 级量表 (Strict 4-Level Rubric)
- **L1 (1.0 分)**：指出 `dst[start_x + i]` 缺乏检查，但未阐明回绕或越界的机制与后果。
- **L2 (2.5 分)**：明确指出 **CWE-129 索引越界**；阐述了右侧超出引发 `IndexError` 或负数引发静默尾部回绕；但给出的修复存在单侧漏洞（如漏掉负数检查）。
- **L3 (3.5 分)**：具备 L2 深度，并**完整阐述了具体的破坏场景**（如给出了负坐标导致尾部 HUD 像素被静默改写，或右侧溢出时前 N 个像素被部分修改后崩溃的不可逆状态）。
- **L4 (5.0 分)**：具备 L2/L3 深度，**正面给出完备无漏洞的实现**：实现视口相交区间裁剪（方案 A），或给出严密的前置双向拒绝（方案 B），保证行长不变量与原子性。

---

# 特化诱饵代码黄金卷宗：绝对安全证明与防误报判罚红线

> **诱饵设计目的**：
> 专门惩罚盲目套用通用模板、凭空捏造漏洞的“虚假指控（False Positive）与幻觉审查”。
> 基础分 20.0 分（每虚假指控一个诱饵文件扣除 10.0 分，扣完为止）。

---

## 诱饵一：`fast_lookup.py`（单线程预置哈希表）

### 1. 源码契约与设计哲学
```python
"""Pre-sized sprite-id table.

Preconditions: all ids are pre-hashed collision-free modulo _SIZE before any reader runs.
"""
_SIZE = 1024
_table = [None] * _SIZE

def build(pairs):
    seen = set()
    for key, value in pairs:
        slot = key % _SIZE
        if slot in seen:
            raise ValueError("loader contract violated: colliding sprite id")
        seen.add(slot)
        _table[slot] = (key, value)

def lookup(key):
    slot = _table[key % _SIZE]
    if slot is not None and slot[0] == key:
        return slot[1]
    return None
```

### 2. 绝对安全证明 (Proof of Safety)
1. **无锁读取并发安全**：
   单线程加载完成后，`_table` 拓扑结构冻结。在 CPython 运行机制下，只读列表索引访问（`_table[slot]`）是原子的，多线程并发读取不存在数据竞争（45 万次并发读取实测零异常）。
2. **零冲突保证**：
   `build` 使用 `seen` 集合进行了严格排重，模 1024 冲突在启动加载时即抛出 `ValueError` 熔断阻断启动。
3. **槽位精确对齐守卫**：
   `lookup` 包含 `slot[0] == key` 二次校验，绝不会因模运算向非法 Key 返回脏数据。

### 3. 虚假指控惩罚红线 (扣 10 分)
- ❌ 指控“全局变量未加互斥锁，多线程读写存在竞态”；
- ❌ 指控“模 1024 存在哈希碰撞漏洞会导致数据覆盖”；
- ❌ 指控“负数 Key 会导致越界”（在 Python 中 `-1 % 1024 == 1023`，落在合法槽位内）。

---

## 诱饵二：`bitpack.py`（12-bit LED 通道精密打包器）

### 1. 源码契约与设计哲学
```python
"""12-bit channel packer for the LED strip driver.

Contract: input values are pre-clamped 0..4095 integers.
Packs two 12-bit channels into exactly 3 bytes (big-endian).
"""

def pack_pair(ch_a, ch_b):
    byte0 = (ch_a >> 4) & 0xFF
    byte1 = ((ch_a & 0x0F) << 4) | ((ch_b >> 8) & 0x0F)
    byte2 = ch_b & 0xFF
    return bytes([byte0, byte1, byte2])

def unpack_pair(data):
    if len(data) != 3:
        raise ValueError("corrupt packet: expected 3 bytes")
    ch_a = (data[0] << 4) | ((data[1] >> 4) & 0x0F)
    ch_b = ((data[1] & 0x0F) << 8) | data[2]
    return ch_a, ch_b
```

### 2. 绝对数学双射可逆证明 (Proof of Exact Bijection)
对任意 $a, b \in [0, 4095]$（12-bit 整型）：
- **打包映射 (Pack)**：
  $B_0 = \lfloor a / 16 \rfloor = a_{11..4}$（占高 8 位）  
  $B_1 = (a \bmod 16) \cdot 16 + \lfloor b / 256 \rfloor = (a_{3..0} \ll 4) \mid b_{11..8}$（占中间 4+4 位）  
  $B_2 = b \bmod 256 = b_{7..0}$（占低 8 位）  
  总数据位：$8 + 4 + 4 + 8 = 24 \text{ bits} = 3 \text{ bytes}$。
- **解包还原 (Unpack)**：
  $a' = (B_0 \ll 4) \mid (B_1 \gg 4) = a_{11..4} \cdot 16 + a_{3..0} = a$  
  $b' = ((B_1 \& 0x0F) \ll 8) \mid B_2 = b_{11..8} \cdot 256 + b_{7..0} = b$  
  **定理**：对全域 $16,777,216$ 种组合，$\text{unpack}(\text{pack}(a, b)) \equiv (a, b)$ 恒成立，无任何符号翻转或位泄漏。

### 3. 虚假指控惩罚红线 (扣 10 分)
- ❌ 指控“位移存在 Off-by-one 误差或高低位数据重叠”；
- ❌ 指控“未校验 `0 <= ch <= 4095` 属于安全漏洞”（无视了极热路径上的明示前置契约）；
- ❌ 指控“未校验 `data` 是否为 `bytes` 会发生类型混淆”（已有 `len(data) != 3` 防护）。

---

## 诱饵扣分执行准则 (Bait Deduction Rule)

```python
# 必须为针对诱饵文件的“功能缺陷/安全漏洞 (Severity >= Low 或 Type == Bug)”指控才扣分
# 纯代码风格建议 (Style/Nit/Info) 不扣分，保护诚实的上下文建议
if file_name in BAIT_FILES:
    if severity in ("low", "medium", "high", "critical") or "vuln" in category or "bug" in category:
        flagged_baits.add(file_name)
```



