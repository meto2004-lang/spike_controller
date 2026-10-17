"""Native Unicode editing: no reshaping, reversed strings, or inserted bidi marks."""
from __future__ import annotations

import keyword
import re
import unicodedata

from PySide6.QtCore import QMimeData, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPainter, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor, QTextFormat)
from PySide6.QtWidgets import QApplication, QMenu, QPlainTextEdit, QTextEdit, QWidget

TIMESTAMP = int(QTextFormat.Property.UserProperty) + 1


def raw_text(document):
    # Unlike toPlainText(), toRawText() preserves NBSP and other Unicode text.
    return document.toRawText().replace('\u2029', '\n')


def direction_for(text):
    for char in text:
        bidi = unicodedata.bidirectional(char)
        if bidi in ('R', 'AL'):
            return Qt.LayoutDirection.RightToLeft
        if bidi == 'L':
            return Qt.LayoutDirection.LeftToRight
    return Qt.LayoutDirection.LeftToRight


class PythonHighlighter(QSyntaxHighlighter):
    """Formatting only: does not rewrite text or add entries to undo history."""
    def highlightBlock(self, text):
        colors = {'kw': '#79b8ff', 'num': '#f2a73b', 'str': '#a5d6a7', 'com': '#8d97ad'}
        pattern = re.compile(r'''(?P<com>\#.*)|(?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<num>\b\d+(?:\.\d+)?\b)|(?P<kw>\b\w+\b)''')
        for match in pattern.finditer(text):
            kind = match.lastgroup
            if kind == 'kw' and match.group() not in keyword.kwlist + ['True', 'False', 'None']:
                continue
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colors[kind]))
            # QTextDocument positions are UTF-16, not Python Unicode indices.
            start = len(text[:match.start()].encode('utf-16-le')) // 2
            length = len(match.group().encode('utf-16-le')) // 2
            self.setFormat(start, length, fmt)


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_numbers(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFont('DejaVu Sans Mono', 12))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.set_direction(Qt.LayoutDirection.LayoutDirectionAuto)
        self.highlighter = PythonHighlighter(self.document())
        self.numbers = LineNumbers(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(self.scroll_gutter)
        self.update_gutter()

    def set_direction(self, direction):
        self.text_direction = direction
        option = self.document().defaultTextOption()
        option.setTextDirection(direction)
        option.setAlignment(Qt.AlignmentFlag.AlignLeading)
        self.document().setDefaultTextOption(option)
        self.viewport().update()

    def gutter_width(self):
        return 14 + self.fontMetrics().horizontalAdvance('9') * len(str(max(1, self.blockCount())))

    def update_gutter(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)
        self.numbers.update()

    def scroll_gutter(self, rect, dy):
        if dy:
            self.numbers.scroll(0, dy)
        else:
            self.numbers.update(0, rect.y(), self.numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_gutter()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.numbers.setGeometry(QRect(rect.left(), rect.top(), self.gutter_width(), rect.height()))

    def paint_numbers(self, event):
        painter = QPainter(self.numbers)
        painter.fillRect(event.rect(), QColor('#252835'))
        painter.setPen(QColor('#8d97ad'))
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            height = round(self.blockBoundingRect(block).height())
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(0, top, self.numbers.width() - 6, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + 1))
            block = block.next()
            top += height

    def paintEvent(self, event):
        # QPlainTextDocumentLayout ignores per-block alignment. Position its
        # native QTextLines instead: shaping, hit testing and caret mapping stay
        # in Qt, with no document-format edits or undo-history pollution.
        margin = self.document().documentMargin()
        available = self.viewport().width() - 2 * margin
        block = self.firstVisibleBlock()
        while block.isValid():
            rect = self.blockBoundingGeometry(block).translated(self.contentOffset())
            if rect.top() > self.viewport().height():
                break
            self.blockBoundingRect(block)  # Ensure the native layout is current.
            direction = (direction_for(block.text()) if self.text_direction ==
                         Qt.LayoutDirection.LayoutDirectionAuto else self.text_direction)
            layout = block.layout()
            for index in range(layout.lineCount()):
                line = layout.lineAt(index)
                x = margin
                if direction == Qt.LayoutDirection.RightToLeft:
                    x += max(0, available - line.naturalTextWidth())
                line.setPosition(QPointF(x, line.y()))
            block = block.next()
        super().paintEvent(event)

    def createMimeDataFromSelection(self):
        data = QMimeData()
        data.setText(self.textCursor().selectedText().replace('\u2029', '\n'))
        return data

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Tab and not event.modifiers():
            self.insertPlainText('    ')
            return
        super().keyPressEvent(event)


class Console(QTextEdit):
    """Read-only native RTL paragraphs; timestamps are format metadata only."""
    def __init__(self, parent=None, maximum_blocks=20000):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        # QTextEdit honors per-paragraph direction/alignment. QPlainTextEdit's
        # layout ignores block alignment and cannot right-align mixed log lines.
        self.document().setMaximumBlockCount(maximum_blocks + 1)
        self.setFont(QFont('DejaVu Sans Mono', 11))
        self.extra_actions = []

    def append_output(self, text, tag, stamp='', prefix=''):
        scroll = self.verticalScrollBar()
        at_end = scroll.value() >= scroll.maximum() - 1
        old_scroll = scroll.value()
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        colors = {'out': '#e8e9ed', 'info': '#79b8ff', 'ok': '#4cc38a', 'err': '#e5484d'}
        for index, line in enumerate(text.split('\n')):
            block = cursor.blockFormat()
            direction = direction_for(line)
            block.setLayoutDirection(direction)
            block.setAlignment(Qt.AlignmentFlag.AlignRight if direction == Qt.LayoutDirection.RightToLeft
                               else Qt.AlignmentFlag.AlignLeft)
            cursor.setBlockFormat(block)
            if index == 0 and stamp:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor('#8d97ad'))
                fmt.setProperty(TIMESTAMP, True)
                cursor.insertText(stamp + ' ', fmt)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colors.get(tag, colors['out'])))
            fmt.setProperty(TIMESTAMP, False)
            cursor.insertText((prefix if index == 0 else '') + line, fmt)
            cursor.insertBlock()
        if at_end:
            scroll.setValue(scroll.maximum())
        else:
            scroll.setValue(old_scroll)

    def selected_output(self):
        selection = self.textCursor()
        start, end = selection.selectionStart(), selection.selectionEnd()
        parts = []
        block = self.document().findBlock(start)
        while block.isValid() and block.position() < end:
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                lo = max(start, fragment.position())
                hi = min(end, fragment.position() + fragment.length())
                if hi > lo and not fragment.charFormat().property(TIMESTAMP):
                    cursor = QTextCursor(self.document())
                    cursor.setPosition(lo)
                    cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
                    parts.append(cursor.selectedText())
                iterator += 1
            newline = block.position() + block.length() - 1
            if start <= newline < end and block.next().isValid():
                parts.append('\n')
            block = block.next()
        return ''.join(parts)

    def copy(self):
        if self.textCursor().hasSelection():
            QApplication.clipboard().setText(self.selected_output())

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        action = menu.addAction('نسخ المحدد', self.copy)
        action.setEnabled(self.textCursor().hasSelection())
        menu.addAction('تحديد الكل', self.selectAll)
        menu.addAction('إلغاء التحديد', self.clear_selection)
        if self.extra_actions:
            menu.addSeparator()
            for label, callback in self.extra_actions:
                menu.addAction(label, callback)
        menu.exec(event.globalPos())

    def clear_selection(self):
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
