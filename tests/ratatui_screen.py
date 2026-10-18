"""A small ANSI screen emulator for the native-TUI PTY tests.

The renderer paints diffs: cursor jumps, style changes, and erase-in-line all land between the
characters of one word, so substring checks on the raw byte stream are brittle. Feed the bytes
here and assert on the visible grid instead.
"""
import codecs
import threading
import unicodedata


class Screen:
    def __init__(self, rows: int, cols: int):
        self.lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.rows, self.cols = rows, cols
        self._reset_grid()
        self._state = "ground"
        self._params = ""
        self._osc_esc = False

    def _reset_grid(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.row = self.col = 0
        self.saved = (0, 0)
        self.wrap = False
        self.top, self.bottom = 0, self.rows - 1

    def resize(self, rows: int, cols: int):
        """Match a terminal resize; the app repaints every cell afterwards."""
        with self.lock:
            self.rows, self.cols = rows, cols
            self._reset_grid()

    def clear(self):
        with self.lock:
            self._reset_grid()

    def feed(self, data: bytes):
        text = self._decoder.decode(data)
        with self.lock:
            for ch in text:
                self._step(ch)

    def text(self) -> str:
        with self.lock:
            return "\n".join("".join(r).rstrip() for r in self.grid)

    def __contains__(self, needle: str) -> bool:
        return needle in self.text()

    # -- parser -----------------------------------------------------------------------------
    def _step(self, ch: str):
        state = self._state
        if state == "ground":
            if ch == "\x1b":
                self._state = "esc"
            else:
                self._print(ch)
        elif state == "esc":
            if ch == "[":
                self._state, self._params = "csi", ""
            elif ch in "]P_^X":
                self._state, self._osc_esc = "string", False
            elif ch in "()#%":
                self._state = "skip1"
            elif ch == "7":
                self.saved = (self.row, self.col)
                self._state = "ground"
            elif ch == "8":
                self.row, self.col = self.saved
                self._state = "ground"
            elif ch == "M":
                self._reverse_index()
                self._state = "ground"
            else:
                self._state = "ground"
        elif state == "skip1":
            self._state = "ground"
        elif state == "string":
            if ch == "\x07" or (self._osc_esc and ch == "\\"):
                self._state = "ground"
            self._osc_esc = ch == "\x1b"
        elif state == "csi":
            if "\x20" <= ch <= "\x3f":
                self._params += ch
            elif "\x40" <= ch <= "\x7e":
                self._state = "ground"
                self._csi(ch, self._params)
            else:
                self._state = "ground"

    def _print(self, ch: str):
        if ch == "\r":
            self.col, self.wrap = 0, False
        elif ch == "\n" or ch in "\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            self.col, self.wrap = max(0, self.col - 1), False
        elif ch == "\t":
            self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
        elif ch < " " or ch == "\x7f":
            return
        elif unicodedata.combining(ch):
            return
        else:
            width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if self.wrap or self.col + width > self.cols:
                self.col, self.wrap = 0, False
                self._linefeed()
            self.grid[self.row][self.col] = ch
            if width == 2:
                self.grid[self.row][self.col + 1] = ""
            self.col += width
            if self.col >= self.cols:
                self.col, self.wrap = self.cols - 1, True

    def _linefeed(self):
        if self.row == self.bottom:
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _reverse_index(self):
        if self.row == self.top:
            del self.grid[self.bottom]
            self.grid.insert(self.top, [" "] * self.cols)
        else:
            self.row = max(0, self.row - 1)

    def _csi(self, final: str, raw: str):
        if raw[:1] in "<=>?":  # private modes (alt screen, mouse, cursor) and kitty keyboard flags
            if raw[:1] == "?" and final in "hl" and raw[1:] in ("1049", "47", "1047"):
                self._reset_grid()
            return
        if " " <= raw[-1:] <= "/" and raw:  # intermediate bytes (cursor style etc.)
            return
        nums = [int(p) if p.isdigit() else 0 for p in raw.split(";")] if raw else []
        arg = lambda i, default=1: (nums[i] if len(nums) > i and nums[i] else default)
        self.wrap = False
        if final in "Hf":
            self.row = min(self.rows - 1, arg(0) - 1)
            self.col = min(self.cols - 1, arg(1) - 1)
        elif final == "A":
            self.row = max(0, self.row - arg(0))
        elif final in "Be":
            self.row = min(self.rows - 1, self.row + arg(0))
        elif final in "Ca":
            self.col = min(self.cols - 1, self.col + arg(0))
        elif final == "D":
            self.col = max(0, self.col - arg(0))
        elif final in "G`":
            self.col = min(self.cols - 1, arg(0) - 1)
        elif final == "d":
            self.row = min(self.rows - 1, arg(0) - 1)
        elif final == "J":
            mode = arg(0, 0)
            if mode == 0:
                self._erase(self.row, self.col, self.cols)
                for r in range(self.row + 1, self.rows):
                    self._erase(r, 0, self.cols)
            elif mode == 1:
                for r in range(self.row):
                    self._erase(r, 0, self.cols)
                self._erase(self.row, 0, self.col + 1)
            else:
                for r in range(self.rows):
                    self._erase(r, 0, self.cols)
        elif final == "K":
            mode = arg(0, 0)
            start, end = {0: (self.col, self.cols), 1: (0, self.col + 1)}.get(mode, (0, self.cols))
            self._erase(self.row, start, end)
        elif final == "X":
            self._erase(self.row, self.col, self.col + arg(0))
        elif final == "@":
            line = self.grid[self.row]
            n = min(arg(0), self.cols - self.col)
            line[self.col:] = ([" "] * n + line[self.col:])[: self.cols - self.col]
        elif final == "P":
            line = self.grid[self.row]
            n = min(arg(0), self.cols - self.col)
            line[self.col:] = line[self.col + n:] + [" "] * n
        elif final in "LM":
            for _ in range(arg(0)):
                if final == "L":
                    del self.grid[self.bottom]
                    self.grid.insert(self.row, [" "] * self.cols)
                else:
                    del self.grid[self.row]
                    self.grid.insert(self.bottom, [" "] * self.cols)
        elif final in "ST":
            for _ in range(arg(0)):
                del self.grid[self.top if final == "S" else self.bottom]
                self.grid.insert(self.bottom if final == "S" else self.top, [" "] * self.cols)
        elif final == "r":
            self.top = arg(0) - 1
            self.bottom = min(self.rows, arg(1, self.rows)) - 1
            self.row = self.col = 0
        elif final == "s":
            self.saved = (self.row, self.col)
        elif final == "u":
            self.row, self.col = self.saved

    def _erase(self, row: int, start: int, end: int):
        for c in range(max(0, start), min(self.cols, end)):
            self.grid[row][c] = " "
