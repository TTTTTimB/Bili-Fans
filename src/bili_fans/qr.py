"""最小 QR 码生成器：字节模式、纠错等级 M、版本 1-10。纯标准库。

只为了把 B 站登录二维码画到网页/终端里，所以只实现用得到的那部分。
算法按 ISO/IEC 18004 实现：数据编码 → Reed-Solomon 纠错 → 交织 →
落点 → 8 种掩码择优。
"""

from __future__ import annotations

from dataclasses import dataclass

# 版本 -> (每块纠错码字数, [(块数, 每块数据码字数), ...])，纠错等级 M
BLOCKS_M: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# 校正图形中心坐标
ALIGN: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

FORMAT_BITS_M = 0b00  # 纠错等级 M 的 2 位编码


# ---------------------------------------------------------------- GF(256)


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# ---------------------------------------------------------------- 编码


def _capacity_bytes(version: int) -> int:
    _, groups = BLOCKS_M[version]
    return sum(n * d for n, d in groups)


def _pick_version(length: int) -> int:
    for version in sorted(BLOCKS_M):
        # 模式 4 bit + 字符计数 8/16 bit，至少还要留出终止符的余量
        overhead = 4 + (8 if version <= 9 else 16)
        if length * 8 + overhead <= _capacity_bytes(version) * 8:
            return version
    raise ValueError(f"内容太长（{length} 字节），超出本实现的容量")


def _data_bits(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                      # 字节模式
    push(len(data), 8 if version <= 9 else 16)
    for byte in data:
        push(byte, 8)

    capacity = _capacity_bytes(version) * 8
    bits.extend([0] * min(4, capacity - len(bits)))     # 终止符
    while len(bits) % 8:
        bits.append(0)                                   # 补齐到整字节

    pad = (0xEC, 0x11)
    index = 0
    while len(bits) < capacity:
        push(pad[index % 2], 8)
        index += 1
    return bits


def _codewords(data: bytes, version: int) -> list[int]:
    bits = _data_bits(data, version)
    payload = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    ec_len, groups = BLOCKS_M[version]
    blocks: list[list[int]] = []
    pos = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(payload[pos:pos + size])
            pos += size

    divisor = _rs_divisor(ec_len)
    eccs = [_rs_remainder(block, divisor) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for ecc in eccs:
            out.append(ecc[i])
    return out


# ---------------------------------------------------------------- 落点


def _format_word(mask: int) -> int:
    data = (FORMAT_BITS_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_word(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _mask_bit(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


class _Canvas:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[0] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, row: int, col: int, value: int) -> None:
        self.modules[row][col] = value
        self.fixed[row][col] = True

    # ---------- 功能图形 ----------

    def draw_function_patterns(self) -> None:
        size = self.size
        for row, col in ((3, 3), (3, size - 4), (size - 4, 3)):
            for dr in range(-4, 5):
                for dc in range(-4, 5):
                    r, c = row + dr, col + dc
                    if not (0 <= r < size and 0 <= c < size):
                        continue
                    near = max(abs(dr), abs(dc))
                    self.put(r, c, 1 if near not in (2, 4) else 0)

        for i in range(8, size - 8):
            value = 1 if i % 2 == 0 else 0
            if not self.fixed[6][i]:
                self.put(6, i, value)
            if not self.fixed[i][6]:
                self.put(i, 6, value)

        positions = ALIGN[self.version]
        for r in positions:
            for c in positions:
                # 只有和三个定位图形重叠的位置才不画（落在时序线上的要画）
                if (r < 8 and c < 8) or (r < 8 and c > size - 9) or (r > size - 9 and c < 8):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.put(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)

        # 预留格式信息区（先占位，掩码选好后再写）
        for i in range(9):
            if not self.fixed[8][i]:
                self.put(8, i, 0)
            if not self.fixed[i][8]:
                self.put(i, 8, 0)
        for i in range(8):
            if not self.fixed[8][size - 1 - i]:
                self.put(8, size - 1 - i, 0)
            if not self.fixed[size - 1 - i][8]:
                self.put(size - 1 - i, 8, 0)
        self.put(size - 8, 8, 1)          # 固定的深色模块

        if self.version >= 7:
            for i in range(18):
                bit = (_version_word(self.version) >> i) & 1
                self.put(size - 11 + i % 3, i // 3, bit)
                self.put(i // 3, size - 11 + i % 3, bit)

    # ---------- 数据 ----------

    def draw_codewords(self, codewords: list[int]) -> None:
        bits: list[int] = []
        for cw in codewords:
            for i in range(7, -1, -1):
                bits.append((cw >> i) & 1)

        size = self.size
        index = 0
        right = size - 1
        while right > 0:
            if right == 6:
                right = 5
            for vert in range(size):
                upward = ((right + 1) & 2) == 0
                row = size - 1 - vert if upward else vert
                for j in range(2):
                    col = right - j
                    if not self.fixed[row][col]:
                        bit = bits[index] if index < len(bits) else 0
                        self.modules[row][col] = bit
                        index += 1
            right -= 2

    # ---------- 掩码与格式信息 ----------

    def apply_mask(self, mask: int) -> None:
        for row in range(self.size):
            for col in range(self.size):
                if not self.fixed[row][col] and _mask_bit(mask, row, col):
                    self.modules[row][col] ^= 1

    def draw_format(self, mask: int) -> None:
        bits = _format_word(mask)
        size = self.size

        def bit(i: int) -> int:
            return (bits >> i) & 1

        # 第一份：定位图形左上角周围（注意 put 的参数是 行, 列）
        for i in range(6):
            self.put(i, 8, bit(i))
        self.put(7, 8, bit(6))
        self.put(8, 8, bit(7))
        self.put(8, 7, bit(8))
        for i in range(9, 15):
            self.put(8, 14 - i, bit(i))

        # 第二份：右上角沿第 8 行 + 左下角沿第 8 列
        for i in range(8):
            self.put(8, size - 1 - i, bit(i))
        for i in range(8, 15):
            self.put(size - 15 + i, 8, bit(i))
        self.put(size - 8, 8, 1)

    # ---------- 评分 ----------

    def penalty(self) -> int:
        m = self.modules
        size = self.size
        score = 0

        def line_penalty(line: list[int]) -> int:
            total = 0
            run_color = line[0]
            run_len = 1
            for value in line[1:]:
                if value == run_color:
                    run_len += 1
                    if run_len == 5:
                        total += 3
                    elif run_len > 5:
                        total += 1
                else:
                    run_color, run_len = value, 1
            return total

        for row in range(size):
            score += line_penalty(m[row])
        for col in range(size):
            score += line_penalty([m[row][col] for row in range(size)])

        for row in range(size - 1):
            for col in range(size - 1):
                v = m[row][col]
                if v == m[row][col + 1] == m[row + 1][col] == m[row + 1][col + 1]:
                    score += 3

        pattern = [1, 0, 1, 1, 1, 0, 1]
        for row in range(size):
            for col in range(size - 10):
                seg = m[row][col:col + 11]
                if seg[:7] == pattern and seg[7:] == [0, 0, 0, 0]:
                    score += 40
                if seg[4:] == pattern and seg[:4] == [0, 0, 0, 0]:
                    score += 40
        for col in range(size):
            for row in range(size - 10):
                seg = [m[row + i][col] for i in range(11)]
                if seg[:7] == pattern and seg[7:] == [0, 0, 0, 0]:
                    score += 40
                if seg[4:] == pattern and seg[:4] == [0, 0, 0, 0]:
                    score += 40

        dark = sum(sum(row) for row in m)
        ratio = abs(dark * 100 // (size * size) - 50)
        score += (ratio // 5) * 10
        return score


@dataclass(frozen=True)
class QR:
    version: int
    size: int
    mask: int
    modules: tuple[tuple[int, ...], ...]

    def ascii(self, *, quiet: int = 2) -> str:
        """终端里用半块字符画出来，一行顶两行。"""
        grid = []
        for row in range(0, self.size, 2):
            line = []
            for col in range(self.size):
                upper = self.modules[row][col]
                lower = self.modules[row + 1][col] if row + 1 < self.size else 0
                line.append(
                    "█" if upper and lower else "▀" if upper else "▄" if lower else " "
                )
            grid.append("  " + "".join(line) + "  ")
        blank = "  " * (self.size + 2)
        return "\n".join([blank] * quiet + grid + [blank] * quiet)


def encode(text: str, *, mask: int | None = None) -> QR:
    """把文本编码成二维码。mask 传 0-7 可以强制指定（测试用）。"""
    data = text.encode("utf-8")
    version = _pick_version(len(data))
    codewords = _codewords(data, version)

    best_score: int | None = None
    best_canvas: _Canvas | None = None
    best_mask = 0
    for candidate in (range(8) if mask is None else [mask]):
        canvas = _Canvas(version)
        canvas.draw_function_patterns()
        canvas.draw_codewords(codewords)
        canvas.apply_mask(candidate)
        canvas.draw_format(candidate)
        score = canvas.penalty()
        if best_score is None or score < best_score:
            best_score, best_canvas, best_mask = score, canvas, candidate

    assert best_canvas is not None
    return QR(
        version=version,
        size=best_canvas.size,
        mask=best_mask,
        modules=tuple(tuple(row) for row in best_canvas.modules),
    )


def to_svg(qr: QR, *, scale: int = 8, quiet: int = 4, dark: str = "#0f172a", light: str = "#ffffff") -> str:
    """返回一个自包含的 SVG 字符串（按行合并连续黑格，体积很小）。"""
    total = (qr.size + quiet * 2) * scale
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}" shape-rendering="crispEdges">',
        f'<rect width="{total}" height="{total}" fill="{light}"/>',
    ]
    for row in range(qr.size):
        col = 0
        while col < qr.size:
            if qr.modules[row][col]:
                start = col
                while col < qr.size and qr.modules[row][col]:
                    col += 1
                x = (start + quiet) * scale
                y = (row + quiet) * scale
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{(col - start) * scale}" height="{scale}" fill="{dark}"/>'
                )
            else:
                col += 1
    parts.append("</svg>")
    return "".join(parts)
