#!/usr/bin/env python3
"""Converte uma captura ANSI (tmux capture-pane -e) em PNG.

Sem dependencias alem de Pillow. Renderiza SGR basico (1 bold, 2 dim, 7 reverse,
30-37/39/90-97 fg) com a fonte Menlo, em fundo escuro, com supersampling 2x.

    /usr/bin/python3 tests/render_ansi.py /tmp/capture.ansi docs/screenshot-demo.png
"""
import re
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 18
SCALE = 2
PAD = 26

BG = "#1b1f27"
FG = "#d7dae0"
PANEL = "#12161d"
BORDER = "#2c333d"

PALETTE = {
    30: "#3b4048", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b",
    34: "#61afef", 35: "#c678dd", 36: "#56b6c2", 37: "#d7dae0",
    90: "#5c6370", 91: "#ef7a85", 92: "#a9d18a", 93: "#f0cf8a",
    94: "#7cc0f5", 95: "#d79ae7", 96: "#75c7d2", 97: "#ffffff",
}
DEFAULT_FG = 39

# Menlo nao tem alguns simbolos usados pela TUI; troca por um equivalente de
# mesma largura para nao sair com caixa vazia no PNG.
FALLBACK = {"\u23f8": "\u275a"}

SGR = re.compile(r"\x1b\[([0-9;]*)m")


def mix(color, other, amount):
    a = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(other[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(x + (y - x) * amount):02x}" for x, y in zip(a, b))


class Style:
    __slots__ = ("fg", "bold", "dim", "reverse")

    def __init__(self, fg=DEFAULT_FG, bold=False, dim=False, reverse=False):
        self.fg, self.bold, self.dim, self.reverse = fg, bold, dim, reverse

    def apply(self, codes):
        for code in codes:
            if code == 0:
                self.fg, self.bold, self.dim, self.reverse = DEFAULT_FG, False, False, False
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 22:
                self.bold = self.dim = False
            elif code == 7:
                self.reverse = True
            elif code == 27:
                self.reverse = False
            elif code == 39:
                self.fg = DEFAULT_FG
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.fg = code

    def colors(self):
        fg = PALETTE.get(self.fg, FG)
        if self.dim:
            fg = mix(fg, BG, 0.45)
        if self.reverse:
            return BG, fg
        return fg, None


def parse(line):
    """Divide a linha em (texto, estilo) aplicando os SGR em sequencia."""
    runs, pos, style = [], 0, Style()
    for match in SGR.finditer(line):
        if match.start() > pos:
            runs.append((line[pos:match.start()], Style(style.fg, style.bold,
                                                       style.dim, style.reverse)))
        codes = [int(c) for c in match.group(1).split(";") if c != ""]
        style.apply(codes or [0])
        pos = match.end()
    if pos < len(line):
        runs.append((line[pos:], Style(style.fg, style.bold, style.dim, style.reverse)))
    return runs


def render(ansi_path, out_path):
    with open(ansi_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().rstrip("\n").split("\n")
    lines = ["".join(FALLBACK.get(c, c) for c in line) for line in lines]
    width = max(len(SGR.sub("", line)) for line in lines)

    regular = ImageFont.truetype(FONT_PATH, FONT_SIZE * SCALE, index=0)
    bold = ImageFont.truetype(FONT_PATH, FONT_SIZE * SCALE, index=1)
    cell_w = regular.getlength("M")
    line_h = round(FONT_SIZE * SCALE * 1.45)
    ascent, descent = regular.getmetrics()
    baseline = (line_h - (ascent + descent)) // 2 + ascent

    text_w = round(width * cell_w)
    text_h = line_h * len(lines)
    canvas_w = text_w + PAD * 2 * SCALE
    canvas_h = text_h + PAD * 2 * SCALE

    img = Image.new("RGB", (canvas_w, canvas_h), PANEL)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([SCALE, SCALE, canvas_w - SCALE, canvas_h - SCALE],
                           radius=8 * SCALE, fill=BG, outline=BORDER, width=SCALE)

    x0, y0 = PAD * SCALE, PAD * SCALE
    for row, line in enumerate(lines):
        col = 0
        y = y0 + row * line_h
        for text, style in parse(line):
            fg, bg = style.colors()
            font = bold if style.bold else regular
            if bg:
                draw.rectangle([x0 + col * cell_w, y,
                                x0 + col * cell_w + len(text) * cell_w, y + line_h],
                               fill=bg)
            draw.text((x0 + col * cell_w, y + baseline), text, font=font,
                      fill=fg, anchor="ls")
            col += len(text)

    img = img.resize((canvas_w // SCALE, canvas_h // SCALE), Image.LANCZOS)
    img.save(out_path)
    print(f"{out_path} {img.width}x{img.height}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    render(sys.argv[1], sys.argv[2])
