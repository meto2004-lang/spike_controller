"""Qt UI over the existing tested operation/document controller.

The transport, cancellation, freshness gates and training validation stay in
spike_controller. These small adapters expose only the UI methods it needs.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import traceback

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QKeySequence,
                           QPainter, QPixmap, QShortcut, QTextCursor)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QSplitter, QTabWidget, QToolButton,
    QVBoxLayout, QWidget)

import spike_controller as backend
from spike_documents import EditorDocument
from spike_icons import ICON_PATHS, icon_pixels
from spike_qt_text import CodeEditor, Console, raw_text


class TextAdapter:
    def __init__(self, widget):
        self.widget = widget

    def get(self, *_):
        return raw_text(self.widget.document())

    def delete(self, *_):
        self.widget.clear()

    def insert(self, index, text, *_):
        cursor = self.widget.textCursor()
        if index == '1.0':
            cursor.movePosition(QTextCursor.MoveOperation.Start)
        elif index in ('end', 'end-1c'):
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.widget.setTextCursor(cursor)

    def edit_modified(self, value=None):
        if value is not None:
            self.widget.document().setModified(value)
        return self.widget.document().isModified()

    def edit_undo(self):
        self.widget.undo()

    def edit_redo(self):
        self.widget.redo()

    def focus_set(self):
        self.widget.setFocus()

    def configure(self, **options):
        if 'state' in options:
            self.widget.setReadOnly(options['state'] == 'disabled')


class LabelAdapter:
    def __init__(self, widget):
        self.widget = widget

    def configure(self, **options):
        if 'text' in options:
            self.widget.setText(options['text'])
            self.widget.setToolTip(options['text'])
        if 'foreground' in options:
            self.widget.setStyleSheet('color: ' + options['foreground'])


class ButtonAdapter:
    def __init__(self, widget):
        self.widget = widget

    def configure(self, **options):
        if 'state' in options:
            self.widget.setEnabled(options['state'] != 'disabled')


class Value:
    def __init__(self, widget):
        self.widget = widget

    def get(self):
        return self.widget.isChecked() if isinstance(self.widget, QCheckBox) else self.widget.text()

    def set(self, value):
        if isinstance(self.widget, QCheckBox):
            self.widget.setChecked(value)
        else:
            self.widget.setText(value)


class ProgressAdapter:
    def __init__(self, widget):
        self.widget = widget

    def configure(self, **options):
        if 'value' in options:
            self.widget.setValue(options['value'])
        if 'style' in options:
            style = options['style']
            color = '#e5484d' if style.startswith('Failed') else (
                '#f2a73b' if style.startswith('Cancelled') else '#4cc38a')
            self.widget.setStyleSheet('QProgressBar {border:0; background:#14151b;}'
                                     'QProgressBar::chunk {background:' + color + ';}')


class Frame(QWidget):
    def destroy(self):
        self.deleteLater()


class Notebook:
    def __init__(self, widget):
        self.widget = widget

    def add(self, frame, text):
        self.widget.addTab(frame, text)

    def select(self, frame=None):
        if frame is not None:
            self.widget.setCurrentWidget(frame)
        return str(self.widget.currentWidget())

    def tab(self, frame, text):
        index = self.widget.indexOf(frame)
        if index >= 0:
            self.widget.setTabText(index, text)
            self.widget.setTabToolTip(index, text)

    def forget(self, frame):
        self.widget.removeTab(self.widget.indexOf(frame))


class Panes:
    def __init__(self, widget):
        self.widget = widget

    def sashpos(self, _):
        return self.widget.sizes()[0]

    def winfo_height(self):
        return self.widget.height()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.allow_close = False
        self.close_handler = None

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            if self.close_handler:
                QTimer.singleShot(0, self.close_handler)


class Root:
    def __init__(self, window):
        self.window = window
        self.timers = {}
        self.next_timer = 0

    def after(self, ms, callback):
        self.next_timer += 1
        token = self.next_timer
        timer = QTimer(self.window)
        timer.setSingleShot(True)
        def fire():
            self.timers.pop(token, None)
            timer.deleteLater()
            callback()
        timer.timeout.connect(fire)
        self.timers[token] = timer
        timer.start(ms)
        return token

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, token):
        timer = self.timers.pop(token, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def cancel_timers(self):
        for token in list(self.timers):
            self.after_cancel(token)

    def protocol(self, _, callback):
        self.window.close_handler = callback

    def geometry(self):
        return f'{self.window.width()}x{self.window.height()}'

    def title(self, title):
        self.window.setWindowTitle(title)

    def destroy(self):
        self.cancel_timers()
        self.window.allow_close = True
        self.window.close()


class Dialogs:
    def __init__(self, parent):
        self.parent = parent

    def showerror(self, title, text, **_):
        QMessageBox.critical(self.parent, title, text)

    def showinfo(self, title, text, **_):
        QMessageBox.information(self.parent, title, text)

    def askyesno(self, title, text, **_):
        return QMessageBox.question(self.parent, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def askyesnocancel(self, title, text):
        answer = QMessageBox.question(self.parent, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return None
        return answer == QMessageBox.StandardButton.Yes

    def askopenfilename(self, **options):
        return QFileDialog.getOpenFileName(self.parent, options.get('title', ''),
            options.get('initialdir', ''), 'Python (*.py);;All files (*)')[0]

    def asksaveasfilename(self, **options):
        extension = options.get('defaultextension', '.py')
        path = os.path.join(options.get('initialdir', ''), options.get('initialfile', ''))
        path = QFileDialog.getSaveFileName(self.parent, options.get('title', ''), path,
                                         f'Files (*{extension});;All files (*)')[0]
        if path and not os.path.splitext(path)[1]:
            path += extension
        return path


class RaceWindow(backend.RaceGenWindow):
    def __init__(self, app):
        self.app = app
        self.win = QDialog(app.window)
        self.win.setWindowTitle('توليد ملف سباق من بيانات التدريب')
        self.win.resize(800, 600)
        self.win.destroy = self.win.close
        layout = QVBoxLayout(self.win)
        layout.addWidget(QLabel('راجع الأوامر والمقاسات قبل الحفظ أو التشغيل'))
        editor = CodeEditor()
        self.preview = TextAdapter(editor)
        layout.addWidget(editor, 1)
        buttons = QHBoxLayout()
        for text, callback in (('حفظ كملف', self.save), ('فتح في تبويب جديد', self.to_editor),
                               ('تحديث من الكونسول', self.refresh), ('إغلاق', self.win.close)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.refresh()
        self.win.show()


class QtApp(backend.App):
    def __init__(self, window):
        self.window = window
        self.dialogs = Dialogs(window)
        self.shortcuts = []
        self.race_windows = []
        super().__init__(Root(window))

    @staticmethod
    def _display_text(text):
        return text  # Qt performs shaping/bidi itself. Never use to_visual here.

    @property
    def _messages(self):
        return self.dialogs

    @property
    def _files(self):
        return self.dialogs

    def _build_style(self):
        self.BG, self.PANEL, self.ENTRY_BG = '#1e1f26', '#252835', '#14151b'
        self.FG, self.OK, self.ERR, self.WARN = '#e8e9ed', '#4cc38a', '#e5484d', '#f2a73b'
        self.window.setFont(QFont('Segoe UI', 10))
        self.window.setStyleSheet('''
            QWidget {background:#1e1f26; color:#e8e9ed;}
            QPlainTextEdit,QLineEdit {background:#14151b; border:1px solid #34394b;
                selection-background-color:#346bd5; padding:3px;}
            QPushButton,QToolButton,QComboBox {background:#34394b; border:1px solid #454d64;
                border-radius:3px; padding:5px 8px;}
            QPushButton:disabled,QToolButton:disabled {color:#70788d; background:#252835;}
            QPushButton:hover,QToolButton:hover {background:#46516c;}
            QPushButton#send {background:#346bd5;} QPushButton#stop {background:#733440;}
            QTabBar::tab {background:#252835; padding:6px 12px;}
            QTabBar::tab:selected {background:#34394b;}
            QMenu {background:#252835; border:1px solid #454d64;}
            QMenu::item {padding:6px 20px;} QMenu::item:selected {background:#346bd5;}
            QMenu::item:disabled {color:#70788d;}
            QSplitter::handle {background:#454d64; height:5px;}
        ''')

    def _icon(self, name):
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setPen(QColor('#e8e9ed'))
        for x, y in icon_pixels(name, 20):
            painter.drawPoint(x, y)
        painter.end()
        return QIcon(pixmap)

    def _button(self, text, callback, icon=None, name=''):
        button = QPushButton(text)
        if icon:
            button.setIcon(self._toolbar_images[icon][0])
        button.setObjectName(name)
        button.clicked.connect(callback)
        return button

    def _label(self, text='', stretch=False):
        label = QLabel(text)
        if stretch:
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return LabelAdapter(label)

    def _build_ui(self):
        match = re.match(r'(\d+)x(\d+)', self.cfg.get('geometry', '1220x780'))
        width, height = map(int, match.groups()) if match else (1220, 780)
        self.window.resize(width, height)
        self.window.setMinimumSize(900, 560)
        self._toolbar_images = {name: (self._icon(name),) for name in ICON_PATHS}
        self._action_entries = {}
        self._build_menu()
        central = QWidget()
        self.window.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        self.toolbar = QWidget()
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        for attr, text, icon, callback, name in (
            ('btn_connect', 'بلوتوث', 'bluetooth', self.action_connect, ''),
            ('btn_disconnect', 'قطع الاتصال', 'disconnect', self.action_disconnect, ''),
            ('btn_send', 'إرسال وتشغيل', 'play', self.action_send_run, 'send'),
            ('btn_stop', 'إيقاف', 'stop', self.action_stop, 'stop')):
            button = self._button(text, callback, icon, name)
            setattr(self, attr, ButtonAdapter(button))
            row.addWidget(button)
        for text, entries in (
            ('خيارات', [('usb', 'اتصال USB', self.action_usb_connect),
                        ('serial', 'استقبال COM', self.action_serial_toggle),
                        ('sendwait', 'إرسال مع الانتظار', self.action_send_wait),
                        ('dl', 'تحميل فقط', self.action_download_only),
                        ('stored', 'تشغيل المخزن', self.action_run_stored)]),
            ('أدوات', [('race', 'توليد ملف سباق', self.action_race_gen),
                       ('clear', 'جلسة جديدة / مسح', self.action_clear_console),
                       ('export', 'تصدير السجل', self.action_export_log)])):
            button = QToolButton()
            button.setText(text)
            menu = QMenu(button)
            for key, title, callback in entries:
                self._action_entries[key] = menu.addAction(title, callback)
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            row.addWidget(button)
        self.target_entry = QLineEdit(self.cfg.get('hub_name', ''))
        self.target_entry.setPlaceholderText('اسم الروبوت أو عنوان BLE')
        self.target_entry.setMinimumWidth(100)
        self.target_var = Value(self.target_entry)
        row.addWidget(self.target_entry, 1)
        layout.addWidget(self.toolbar)
        self.lbl_send_target = self._label(stretch=True)
        layout.addWidget(self.lbl_send_target.widget)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.paned = Panes(self.splitter)
        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.lbl_file = self._label(stretch=True)
        self.lbl_file.widget.setMinimumWidth(120)
        header.addWidget(self.lbl_file.widget, 1)
        path = QLineEdit()
        path.setReadOnly(True)
        path.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.file_path_var = Value(path)
        header.addWidget(path, 3)
        self.direction_combo = QComboBox()
        self.direction_combo.addItems(['اتجاه تلقائي', 'يسار ← يمين', 'يمين ← يسار'])
        self.direction_combo.setToolTip('اتجاه العرض فقط؛ لا يغيّر النص المحفوظ')
        self.direction_combo.currentIndexChanged.connect(self._change_direction)
        header.addWidget(self.direction_combo)
        header.addWidget(self._button('+', self.action_new))
        editor_layout.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(False)
        self.notebook = Notebook(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(lambda index: self._close_document(self.documents[index]))
        editor_layout.addWidget(self.tabs, 1)
        self.splitter.addWidget(editor_panel)
        console_panel = QWidget()
        console_layout = QVBoxLayout(console_panel)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_header = QHBoxLayout()
        console_header.addWidget(QLabel('مخرجات الروبوت'))
        console_header.addStretch()
        console_header.addWidget(self._button('نسخ المحدد', self._copy_console))
        console_header.addWidget(self._button('تحديد الكل', self._select_all_console))
        console_layout.addLayout(console_header)
        self.console_view = Console(maximum_blocks=backend.MAX_LINES)
        self.console_view.extra_actions = [('جلسة جديدة / مسح', self.action_clear_console),
                                           ('تصدير السجل', self.action_export_log)]
        self.console_w = TextAdapter(self.console_view)
        console_layout.addWidget(self.console_view, 1)
        self.splitter.addWidget(console_panel)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 1)
        editor_panel.setMinimumHeight(180)
        console_panel.setMinimumHeight(80)
        layout.addWidget(self.splitter, 1)
        status = QHBoxLayout()
        self.lbl_state = self._label('● غير متصل', stretch=True)
        self.lbl_prog = self._label(stretch=True)
        status.addWidget(self.lbl_state.widget, 1)
        status.addWidget(self.lbl_prog.widget, 1)
        for attr, label, key, callback in (('_v_ts', 'طوابع زمنية', 'timestamps', self._toggle_ts),
                                          ('_v_al', 'حفظ تلقائي', 'autolog', self._toggle_al)):
            checkbox = QCheckBox(label)
            checkbox.setChecked(self.cfg.get(key, True))
            setattr(self, attr, Value(checkbox))
            checkbox.toggled.connect(callback)
            status.addWidget(checkbox)
        self.lbl_rx, self.lbl_upload = self._label('0 سطر'), self._label('0%')
        status.addWidget(self.lbl_rx.widget)
        status.addWidget(self.lbl_upload.widget)
        layout.addLayout(status)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        self.upload_bar = ProgressAdapter(bar)
        self.upload_bar.configure(style='Upload.Horizontal.TProgressbar')
        layout.addWidget(bar)
        self._new_document()
        self.root.after_idle(self._restore_split)

    def _restore_split(self):
        height = self.splitter.height()
        fraction = self.cfg.get('editor_fraction', 0.78)
        self.splitter.setSizes([round(height * fraction), round(height * (1 - fraction))])

    def _build_menu(self):
        bar = self.window.menuBar()
        file = bar.addMenu('الملف')
        for text, callback in (('جديد', self.action_new), ('فتح', self.action_open),
            ('حفظ', self.action_save), ('حفظ باسم', self.action_save_as),
            ('إغلاق التبويب', self.action_close_tab)):
            file.addAction(text, callback)
        self.recent_menu = file.addMenu('آخر الملفات')
        self._refresh_recent_menu()
        file.addAction('فتح مجلد السجلات', self.action_open_logs)
        file.addAction('خروج', self._on_close)
        edit = bar.addMenu('التحرير')
        for label, method in (('تراجع', 'undo'), ('إعادة', 'redo'), ('قص', 'cut'),
                              ('نسخ', 'copy'), ('لصق', 'paste'), ('تحديد الكل', 'selectAll')):
            edit.addAction(label, lambda checked=False, m=method: self._focused_edit(m))
        edit.addAction('نسخ المحدد من الكونسول', self._copy_console)
        insert = bar.addMenu('إدراج')
        for label, text in (('إعدادات الروبوت', backend.SETUP_TEMPLATE),
            ('حلقة تكرار', 'while True:\n    # ...\n    wait(100)\n'),
            ('قراءة الحساس الأيسر', 'print(left_sensor.color(), left_sensor.reflection())\n')):
            insert.addAction(label, lambda checked=False, t=text: self._insert_at_cursor(t))
        tools = bar.addMenu('أدوات')
        tools.addAction('توليد ملف سباق', self.action_race_gen)
        tools.addAction('جلسة جديدة / مسح', self.action_clear_console)
        tools.addAction('تصدير السجل', self.action_export_log)
        help_menu = bar.addMenu('مساعدة')
        help_menu.addAction('شرح الاستخدام', self.action_help)
        help_menu.addAction('عن التطبيق', lambda: self.dialogs.showinfo('Hyena Control',
            'Hyena Control — PySide6 / Qt\nPybricks SPIKE Prime\nدعم Unicode والعربية الأصلي'))

    def _focused_edit(self, method):
        widget = QApplication.focusWidget()
        if widget is not None and hasattr(widget, method):
            getattr(widget, method)()

    def _bind_shortcuts(self):
        for sequence, callback in (
            ('Ctrl+N', self.action_new), ('Ctrl+O', self.action_open),
            ('Ctrl+S', self.action_save), ('Ctrl+Shift+S', self.action_save_as),
            ('Ctrl+W', self.action_close_tab), ('Ctrl+Tab', lambda: self._cycle_document(1)),
            ('Ctrl+Shift+Tab', lambda: self._cycle_document(-1)), ('Ctrl+G', self.action_race_gen),
            ('Ctrl+L', self.action_clear_console), ('Ctrl+Shift+C', self._copy_console),
            ('F5', self.action_send_run), ('Ctrl+F5', self.action_send_wait),
            ('Escape', self.action_stop), ('F1', self.action_help)):
            shortcut = QShortcut(QKeySequence(sequence), self.window)
            shortcut.activated.connect(lambda c=callback: self._run_shortcut(c))
            self.shortcuts.append(shortcut)

    def _configure_action(self, key, **options):
        action = self._action_entries[key]
        if 'state' in options:
            action.setEnabled(options['state'] != 'disabled')
        if 'label' in options:
            action.setText(options['label'])
        if 'image' in options:
            action.setIcon(options['image'])

    def _create_editor_tab(self, content, path, saved, number, suggested_name):
        frame = Frame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = CodeEditor()
        editor.setPlainText(content)
        editor.document().setModified(False)
        layout.addWidget(editor)
        adapter = TextAdapter(editor)
        document = EditorDocument(frame, adapter, editor.numbers, editor.verticalScrollBar(),
                                  path, content if saved else '', number, suggested_name)
        editor.textChanged.connect(lambda: self._on_editor_modified(document=document))
        return document

    def _update_lineno(self, document=None):
        pass  # Native gutter tracks visible blocks and scrolling itself.

    def _rehighlight(self, document=None):
        document = document or self.active_document
        if document is not None:
            document.highlight_job = None  # QSyntaxHighlighter handles incremental updates.

    def _set_file_label(self):
        super()._set_file_label()
        if self.active_document is not None:
            direction = self.editor.widget.text_direction
            choices = [Qt.LayoutDirection.LayoutDirectionAuto, Qt.LayoutDirection.LeftToRight,
                       Qt.LayoutDirection.RightToLeft]
            with QSignalBlocker(self.direction_combo):
                self.direction_combo.setCurrentIndex(choices.index(direction))

    def _change_direction(self, index):
        if self.active_document:
            self.editor.widget.set_direction([Qt.LayoutDirection.LayoutDirectionAuto,
                Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft][index])

    def _refresh_recent_menu(self):
        if not hasattr(self, 'recent_menu'):
            return
        self.recent_menu.clear()
        for path in self.cfg.get('recent', []):
            self.recent_menu.addAction(path, lambda checked=False, p=path: self._open_path(p))
        if not self.recent_menu.actions():
            self.recent_menu.addAction('(لا يوجد)').setEnabled(False)

    def _append_console_display(self, line, tag, source, stamp):
        self.console_view.append_output(line, tag, stamp if self._ts_var().get() else '',
                                        '[سلك] ' if source == 'serial' else '')

    def _copy_console(self):
        if not self.console_view.textCursor().hasSelection():
            self.console('حدّد نصاً أولاً ثم اضغط «نسخ المحدد»', 'info')
            return
        self.console_view.copy()

    def _select_all_console(self):
        self.console_view.selectAll()
        self.console_view.setFocus()

    def _insert_at_cursor(self, text):
        if not self._closing and not self._confirming_close:
            self.editor.widget.insertPlainText(text)
            self.editor.focus_set()

    def action_open_logs(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(backend.LOG_DIR))

    def action_help(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle('شرح الاستخدام')
        dialog.resize(750, 600)
        layout = QVBoxLayout(dialog)
        view = Console()
        view.append_output(backend.HELP_TEXT, 'out')
        layout.addWidget(view)
        dialog.show()
        self.help_dialog = dialog

    def action_race_gen(self):
        self.race_windows.append(RaceWindow(self))


def run(smoke=False):
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setQuitOnLastWindowClosed(True)
    window, controller = None, None
    try:
        if backend.PB_OK:
            backend._WORKER.start()
        window = MainWindow()
        controller = QtApp(window)
        window.show()
        if smoke:
            controller.console('مرحبا بالعالم — اختبار العربية', 'info')
            controller.console('robot.straight(100)', 'out')
            controller.root.after(50, controller._on_close)
        result = application.exec()
        if smoke:
            print('QT SELFTEST OK')
        return result
    except Exception as exc:
        traceback.print_exc()
        if window is not None:
            window.allow_close = True
            window.close()
        raise SystemExit(1) from exc
    finally:
        worker = backend._WORKER
        if controller is not None:
            controller._closing = True
            controller.root.cancel_timers()
            if controller.serial_stop:
                controller.serial_stop.set()
            if controller.serial_thread:
                controller.serial_thread.join(timeout=1)
            if worker.loop.is_running():
                try:
                    future = getattr(controller, '_shutdown_future', None)
                    if future is None:
                        future = asyncio.run_coroutine_threadsafe(controller._shutdown(), worker.loop)
                    future.result(timeout=7)
                except Exception:
                    traceback.print_exc()
            if controller.session_file:
                controller.session_file.close()
        if worker.loop.is_running():
            worker.loop.call_soon_threadsafe(worker.loop.stop)
        if worker.thread:
            worker.thread.join(timeout=5)
        elif not worker.loop.is_closed():
            worker.loop.close()
