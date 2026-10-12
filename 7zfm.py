#!/usr/bin/env python3
"""7zfm - 7-Zip 风格的文件管理器 / 压缩工具 (Qt6 界面, 调用系统 7z 命令)。"""

import argparse
import atexit
import codecs
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import time

from PySide6.QtCore import (QAbstractTableModel, QEventLoop, QFileSystemWatcher, QMimeData,
                            QMimeDatabase, QModelIndex, QProcess, QSettings, QSize, Qt, QTimer,
                            QUrl, Signal, QFile)
from PySide6.QtGui import (QAction, QDesktopServices, QDrag, QFontDatabase, QGuiApplication,
                           QIcon, QKeySequence)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QStyle,
                               QToolButton, QTreeView, QVBoxLayout, QWidget, QAbstractItemView)

APP_NAME = '7zfm'
APP_TITLE = '7-Zip 文件管理器'
VERSION = '1.0'

SEVENZ = shutil.which('7z') or shutil.which('7zz') or shutil.which('7za') or '7z'

# 外层只是单文件压缩器的 tar 包: 扩展名 -> 7z 的格式名
TAR_WRAP = {'.tar.gz': 'gzip', '.tgz': 'gzip', '.tar.bz2': 'bzip2', '.tbz2': 'bzip2', '.tbz': 'bzip2',
            '.tar.xz': 'xz', '.txz': 'xz', '.tar.zst': 'zstd', '.tzst': 'zstd',
            '.tar.lzma': 'lzma', '.tlz': 'lzma', '.tar.z': 'Z'}
STREAM_TYPES = {'gzip', 'bzip2', 'xz', 'zstd', 'lzma', 'lzma86', 'Z'}
ARCHIVE_EXTS = {'.7z', '.zip', '.rar', '.tar', '.gz', '.bz2', '.xz', '.zst', '.lzma', '.z', '.cab',
                '.iso', '.wim', '.arj', '.lzh', '.lha', '.cpio', '.rpm', '.deb', '.jar', '.apk',
                '.xpi', '.squashfs', '.dmg', '.vhd', '.vhdx', '.vmdk', '.qcow2', '.img', '.msi',
                '.chm', '.nsis', '.xar', '.pkg', '.001', '.zipx', '.epub', '.appimage'} | set(TAR_WRAP)
OPEN_AS_ARCHIVE = ARCHIVE_EXTS - {'.epub', '.img', '.msi', '.jar', '.apk', '.appimage', '.pkg', '.chm'}

COL_NAME, COL_SIZE, COL_PACKED, COL_MTIME, COL_ATTR, COL_ENC, COL_CRC, COL_METHOD = range(8)
HEADERS = ['名称', '大小', '压缩后大小', '修改时间', '属性', '加密', 'CRC', '算法']
ARCHIVE_ONLY_COLS = (COL_PACKED, COL_ENC, COL_CRC, COL_METHOD)


# ---------------------------------------------------------------- 工具函数

def lower_ext(name):
    low = name.lower()
    for ext in TAR_WRAP:
        if low.endswith(ext):
            return ext
    return os.path.splitext(low)[1]


def is_archive_name(name, exts=ARCHIVE_EXTS):
    return lower_ext(name) in exts


def tar_wrap_type(name):
    return TAR_WRAP.get(lower_ext(name))


def arc_stem(name):
    """压缩包去掉扩展名后的名字: a.tar.gz -> a, a.7z.001 -> a。"""
    low = name.lower()
    if re.search(r'\.\d{3}$', low):
        name = name[:-4]
        low = name.lower()
    for ext in TAR_WRAP:
        if low.endswith(ext):
            return name[:-len(ext)] or name
    stem, ext = os.path.splitext(name)
    return stem if ext and stem else name


def fmt_size(n):
    if n is None:
        return ''
    return f'{n:,}'.replace(',', ' ')


def human_size(n):
    if n is None:
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n} B' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s.lower())]


def unique_path(path):
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.lexists(f'{base}_{i}{ext}'):
        i += 1
    return f'{base}_{i}{ext}'


_TMPDIRS = []


def mk_tmp():
    cache = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), APP_NAME)
    os.makedirs(cache, exist_ok=True)
    d = tempfile.mkdtemp(prefix='tmp-', dir=cache)
    _TMPDIRS.append(d)
    return d


@atexit.register
def _cleanup_tmp():
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def write_listfile(names):
    path = os.path.join(mk_tmp(), 'list.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(names) + '\n')
    return '@' + path


def theme_icon(*names, std=None):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QApplication.style().standardIcon(std) if std is not None else QIcon()


_mimedb = None
_icon_cache = {}


def file_icon(name, is_dir):
    global _mimedb
    if is_dir:
        key = 'folder'
        names = ('folder', 'inode-directory')
    else:
        if _mimedb is None:
            _mimedb = QMimeDatabase()
        mt = _mimedb.mimeTypeForFile(name, QMimeDatabase.MatchMode.MatchExtension)
        key = mt.iconName()
        names = (key, mt.genericIconName(), 'text-x-generic', 'unknown')
    ic = _icon_cache.get(key)
    if ic is None:
        ic = theme_icon(*names)
        if ic.isNull():
            sp = QStyle.StandardPixmap.SP_DirIcon if is_dir else QStyle.StandardPixmap.SP_FileIcon
            ic = QApplication.style().standardIcon(sp)
        _icon_cache[key] = ic
    return ic


# ---------------------------------------------------------------- 可替换的交互 (测试时打桩)

class PasswordDialog(QDialog):
    def __init__(self, parent, title, wrong=False):
        super().__init__(parent)
        self.setWindowTitle('输入密码')
        lay = QVBoxLayout(self)
        msg = title or '请输入密码:'
        if wrong:
            msg = '密码错误，请重新输入。\n' + msg
        lay.addWidget(QLabel(msg))
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setMinimumWidth(300)
        lay.addWidget(self.edit)
        show = QCheckBox('显示密码')
        show.toggled.connect(lambda on: self.edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        lay.addWidget(show)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class UI:
    """所有需要用户回答的地方都走这里。"""

    @staticmethod
    def ask_password(parent, name, wrong):
        dlg = PasswordDialog(parent, f'压缩包 {name} 已加密，请输入密码:' if name else '', wrong)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.edit.text():
            return dlg.edit.text()
        return None

    @staticmethod
    def ask_conflict(parent, path):
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('确认文件替换')
        box.setText(f'目标位置已存在同名文件:\n{path}\n\n对本次操作中所有同名文件:')
        b_over = box.addButton('全部覆盖', QMessageBox.ButtonRole.AcceptRole)
        b_skip = box.addButton('全部跳过', QMessageBox.ButtonRole.AcceptRole)
        b_ren = box.addButton('自动重命名', QMessageBox.ButtonRole.AcceptRole)
        box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return {b_over: 'overwrite', b_skip: 'skip', b_ren: 'rename'}.get(box.clickedButton())

    @staticmethod
    def confirm(parent, title, text):
        return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes

    @staticmethod
    def info(parent, title, text):
        QMessageBox.information(parent, title, text)

    @staticmethod
    def error(parent, title, text, detail=''):
        if detail:
            TextDialog(parent, title, (text + '\n\n' if text else '') + detail).exec()
        else:
            QMessageBox.warning(parent, title, text)


class TextDialog(QDialog):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        self.edit = QPlainTextEdit(text)
        self.edit.setReadOnly(True)
        self.edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        lay.addWidget(self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = bb.addButton('复制', QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# ---------------------------------------------------------------- 运行 7z 并显示进度

PROGRESS_RE = re.compile(r'^\s*(\d{1,3})%(?:\s+\d+)?(?:\s+[-+=UTR.]\s(.*))?\s*$')
SCAN_RE = re.compile(r'^\s*\d+[KMG]?\s+Scan\b')
BANNER_RE = re.compile(r'^(7-Zip |\s*64-bit |\s*32-bit |Scanning the drive|\d+ files?, |\d+ folders?, )')


class Result:
    def __init__(self, rc, log, cancelled):
        self.rc = rc
        self.log = log
        self.cancelled = cancelled

    @property
    def ok(self):
        return not self.cancelled and self.rc in (0, 1)

    def errors(self):
        lines = [l for l in self.log.split('\n') if l.strip() and not BANNER_RE.match(l)
                 and 'Enter password' not in l]
        return '\n'.join(lines)


class ProgressDialog(QDialog):
    """依次连成管道运行 stages, 解析 7z 的百分比输出。"""

    def __init__(self, parent, title, stages, stdin_text=None, cwd=None, delay=0):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self.delay = delay
        self.stdin_text = stdin_text
        self.cancelled = False
        self.paused = False
        self.log = []
        self.bufs = {}
        self.decoders = {}
        self.done = {}
        self.t0 = time.monotonic()

        lay = QVBoxLayout(self)
        self.l_time = QLabel('已用时间: 0:00')
        self.l_file = QLabel(' ')
        self.l_file.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        lay.addWidget(self.l_time)
        lay.addWidget(self.l_file)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch()
        self.b_pause = QPushButton('暂停')
        self.b_pause.clicked.connect(self.toggle_pause)
        b_cancel = QPushButton('取消')
        b_cancel.clicked.connect(self.cancel)
        row.addWidget(self.b_pause)
        row.addWidget(b_cancel)
        lay.addLayout(row)

        self.procs = []
        for argv in stages:
            p = QProcess(self)
            p.setProgram(argv[0])
            p.setArguments(argv[1:])
            if cwd:
                p.setWorkingDirectory(cwd)
            self.procs.append(p)
        last = len(self.procs) - 1
        for i, p in enumerate(self.procs):
            if i < last:
                p.setStandardOutputProcess(self.procs[i + 1])
            else:
                p.readyReadStandardOutput.connect(lambda p=p, i=i: self._read(p, i, False))
            p.readyReadStandardError.connect(lambda p=p, i=i: self._read(p, i, True))
            p.finished.connect(lambda code, status, i=i: self._finished(i, code, status))
            p.errorOccurred.connect(lambda err, i=i, argv0=stages[i][0]: self._error(i, err, argv0))

        self.loop = QEventLoop(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def run(self):
        for p in self.procs:
            p.start()
        first = self.procs[0]
        if self.stdin_text:
            first.write(self.stdin_text.encode())
        first.closeWriteChannel()
        self.timer.start(500)
        if self.delay and len(self.done) < len(self.procs):
            # 很快就能跑完的操作 (列目录) 不闪对话框
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            QTimer.singleShot(self.delay, self.loop.quit)
            self.loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            QApplication.restoreOverrideCursor()
        if len(self.done) < len(self.procs):
            self.show()
            self.loop.exec()
        self.timer.stop()
        self.hide()
        for key in list(self.bufs):
            self._feed(key, '\n')
        rc = max(self.done.values(), default=255)
        log = re.sub(r'\n{3,}', '\n\n', '\n'.join(self.log)).strip()
        self.deleteLater()
        return Result(rc, log, self.cancelled)

    def _read(self, p, i, err):
        data = bytes(p.readAllStandardError() if err else p.readAllStandardOutput())
        key = (i, err)
        dec = self.decoders.get(key)
        if dec is None:
            dec = self.decoders[key] = codecs.getincrementaldecoder('utf-8')('replace')
        self._feed(key, dec.decode(data))

    def _feed(self, key, text):
        buf = self.bufs.get(key, '') + text.replace('\b', '\r')
        parts = re.split(r'[\r\n]', buf)
        self.bufs[key] = parts[-1]
        for seg in parts[:-1] + [parts[-1]]:
            m = PROGRESS_RE.match(seg)
            if m:
                self.bar.setRange(0, 100)
                self.bar.setValue(min(100, int(m.group(1))))
                if m.group(2):
                    fm = self.l_file.fontMetrics()
                    self.l_file.setText(fm.elidedText(m.group(2), Qt.TextElideMode.ElideMiddle, 480))
        for seg in parts[:-1]:
            if PROGRESS_RE.match(seg) or SCAN_RE.match(seg):
                continue
            self.log.append(seg.rstrip())

    def _finished(self, i, code, status):
        self.done[i] = code if status == QProcess.ExitStatus.NormalExit else 255
        self._check_done()

    def _error(self, i, err, argv0):
        if err == QProcess.ProcessError.FailedToStart:
            self.log.append(f'无法启动程序: {argv0}')
            self.done[i] = 255
            for p in self.procs:
                if p.state() != QProcess.ProcessState.NotRunning:
                    p.kill()
            self._check_done()

    def _check_done(self):
        if len(self.done) >= len(self.procs):
            self.loop.quit()

    def _tick(self):
        s = int(time.monotonic() - self.t0)
        self.l_time.setText(f'已用时间: {s // 60}:{s % 60:02d}')

    def _signal(self, sig):
        for p in self.procs:
            if p.state() != QProcess.ProcessState.NotRunning and p.processId():
                try:
                    os.kill(p.processId(), sig)
                except OSError:
                    pass

    def toggle_pause(self):
        self.paused = not self.paused
        self._signal(signal.SIGSTOP if self.paused else signal.SIGCONT)
        self.b_pause.setText('继续' if self.paused else '暂停')

    def cancel(self):
        self.cancelled = True
        self._signal(signal.SIGCONT)
        for p in self.procs:
            if p.state() != QProcess.ProcessState.NotRunning:
                p.kill()

    def reject(self):
        self.cancel()

    def closeEvent(self, ev):
        ev.ignore()
        self.cancel()


def run_job(parent, title, stages, password=None, cwd=None, delay=0, arc_name='', create=False):
    """运行并在需要时反复询问密码。返回 (Result, password)。"""
    while True:
        stdin = None
        if password is not None:
            stdin = password + '\n' + (password + '\n' if create else '')
        res = ProgressDialog(parent, title, stages, stdin, cwd, delay).run()
        if res.cancelled or create or res.rc == 0:
            return res, password
        need = password is None and 'Enter password' in res.log
        wrong = password is not None and 'Wrong password' in res.log
        if not (need or wrong):
            return res, password
        pw = UI.ask_password(parent, arc_name, wrong)
        if pw is None:
            res.cancelled = True
            return res, password
        password = pw


def report(parent, title, res):
    """操作结束后报告错误; 成功或被取消时不打扰。返回是否成功。"""
    if res.cancelled:
        return False
    if res.rc == 0:
        return True
    UI.error(parent, title, '操作完成，但有警告:' if res.rc == 1 else '操作失败:', res.errors())
    return res.rc == 1


# ---------------------------------------------------------------- 压缩包内容

class Entry:
    __slots__ = ('name', 'raw', 'is_dir', 'size', 'packed', 'mtime', 'attrs', 'enc', 'crc',
                 'method', 'children')

    def __init__(self, name, raw, is_dir):
        self.name = name
        self.raw = raw
        self.is_dir = is_dir
        self.size = self.packed = None
        self.mtime = self.attrs = self.enc = self.crc = self.method = ''
        self.children = {} if is_dir else None


def parse_listing(text):
    """解析 `7z l -slt` 的输出, 返回 (属性 dict, 根 Entry)。"""
    head, sep, body = text.partition('\n----------\n')
    props = {}
    m = re.search(r'^--\n(.*?)(?:\n\n|\Z)', head, re.S | re.M)
    if m:
        for line in m.group(1).split('\n'):
            k, eq, v = line.partition(' = ')
            if eq:
                props.setdefault(k, v)
    root = Entry('', '', True)
    for block in body.split('\n\n') if sep else ():
        rec = {}
        for line in block.split('\n'):
            k, eq, v = line.partition(' = ')
            if eq:
                rec.setdefault(k, v)
        raw = rec.get('Path')
        if not raw:
            continue
        parts = [p for p in raw.split('/') if p not in ('', '.')]
        if not parts:
            continue
        attrs = rec.get('Attributes', '')
        is_dir = rec.get('Folder') == '+' or attrs.startswith('D') or raw.endswith('/')
        node = root
        for i, part in enumerate(parts[:-1]):
            child = node.children.get(part)
            if child is None or not child.is_dir:
                child = node.children[part] = Entry(part, '/'.join(parts[:i + 1]), True)
            node = child
        e = node.children.get(parts[-1])
        if e is None or not (is_dir and e.is_dir):
            e = node.children[parts[-1]] = Entry(parts[-1], raw, is_dir)
        e.raw = raw.rstrip('/') or raw
        e.size = int(rec['Size']) if rec.get('Size', '').isdigit() else None
        e.packed = int(rec['Packed Size']) if rec.get('Packed Size', '').isdigit() else None
        e.mtime = rec.get('Modified', '')[:19]
        e.attrs = attrs
        e.enc = rec.get('Encrypted', '')
        e.crc = rec.get('CRC', '')
        e.method = rec.get('Method', '')
    _sum_dirs(root)
    return props, root


def _sum_dirs(node):
    size = packed = 0
    for c in node.children.values():
        if c.is_dir:
            _sum_dirs(c)
        size += c.size or 0
        packed += c.packed or 0
    node.size, node.packed = size, packed


def count_entries(node):
    files = dirs = 0
    stack = [node]
    while stack:
        for c in stack.pop().children.values():
            if c.is_dir:
                dirs += 1
                stack.append(c)
            else:
                files += 1
    return files, dirs


class Archive:
    def __init__(self, path, display=None, readonly=False, password=None):
        self.path = path
        self.display = display or path
        self.readonly = readonly
        self.password = password
        self.header_encrypted = False
        self.props = {}
        self.root = Entry('', '', True)
        self.outer_type = ''

    @property
    def type(self):
        return self.props.get('Type', '')

    def load(self, parent):
        asked_before = self.password is not None
        res, pw = run_job(parent, '正在读取压缩包', [[SEVENZ, 'l', '-slt', '-sccUTF-8', self.path]],
                          self.password, delay=400, arc_name=os.path.basename(self.display))
        if res.cancelled:
            return False
        if res.rc >= 2 and '\n----------\n' not in res.log:
            UI.error(parent, '无法打开', f'无法作为压缩包打开:\n{self.display}', res.errors())
            return False
        if pw is not None and not asked_before:
            self.header_encrypted = True
        self.password = pw
        self.props, self.root = parse_listing(res.log)
        return True

    def lookup(self, parts):
        node = self.root
        for p in parts:
            node = node.children.get(p) if node.is_dir else None
            if node is None:
                return None
        return node if node.is_dir else None


# ---------------------------------------------------------------- 核心操作 (无界面逻辑, 便于测试)

class Cancelled(Exception):
    pass


class ConflictPolicy:
    def __init__(self, parent, mode):
        self.parent = parent
        self.mode = mode  # ask / overwrite / skip / rename

    def resolve(self, path):
        if self.mode == 'ask':
            choice = UI.ask_conflict(self.parent, path)
            if choice is None:
                raise Cancelled()
            self.mode = choice
        return self.mode


def merge_move(src, dst, policy, errors):
    """把 src 目录里的内容移动合并进 dst。"""
    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        try:
            if not os.path.lexists(d):
                os.rename(s, d)
                continue
            s_dir = os.path.isdir(s) and not os.path.islink(s)
            d_dir = os.path.isdir(d) and not os.path.islink(d)
            if s_dir and d_dir:
                merge_move(s, d, policy, errors)
                continue
            mode = policy.resolve(d)
            if mode == 'skip':
                continue
            if mode == 'rename':
                os.rename(s, unique_path(d))
            elif s_dir or d_dir:
                errors.append(f'无法覆盖 (文件与文件夹同名): {d}')
            else:
                os.replace(s, d)
        except OSError as e:
            errors.append(f'{d}: {e.strerror}')


def extract(parent, arc_path, dest, names=None, strip=(), mode='ask', password=None, flat=False,
            title=None, quiet=False, pipe_tar=True, dedupe_root=False):
    """提取到 dest。先解到 dest 里的临时目录, 再合并移动, 这样可以统一处理
    同名文件询问和 "相对当前文件夹" 的路径。返回 (是否成功, password)。"""
    name = os.path.basename(arc_path)
    try:
        os.makedirs(dest, exist_ok=True)
        stage = tempfile.mkdtemp(prefix='.7zfm-', dir=dest)
    except OSError as e:
        UI.error(parent, '提取', f'无法写入目标文件夹:\n{dest}\n{e.strerror}')
        return False, password
    try:
        tail = ['-sccUTF-8', '-aou' if flat else '-aoa', '-o' + stage]
        sel = [write_listfile(names)] if names else []
        cmd = 'e' if flat else 'x'
        if pipe_tar and names is None and tar_wrap_type(name):
            stages = [[SEVENZ, 'x', '-so', '-bsp2', '-sccUTF-8', arc_path],
                      [SEVENZ, cmd, '-si', '-ttar'] + tail]
        else:
            stages = [[SEVENZ, cmd, '-bsp1'] + tail + [arc_path] + sel]
        res, password = run_job(parent, title or f'正在提取 {name}', stages, password, arc_name=name)
        if res.cancelled:
            return False, password
        src = os.path.join(stage, *strip)
        if not strip or not os.path.isdir(src):
            src = stage
        if dedupe_root and src == stage and not flat:
            # 包里只有一个与目标文件夹同名的顶层文件夹时, 不再多套一层
            only = os.listdir(stage)
            inner_dir = os.path.join(stage, only[0]) if len(only) == 1 else ''
            if only == [os.path.basename(dest.rstrip('/'))] and os.path.isdir(inner_dir) \
                    and not os.path.islink(inner_dir):
                src = inner_dir
        errors = []
        try:
            merge_move(src, dest, ConflictPolicy(parent, mode), errors)
        except Cancelled:
            return False, password
        if errors:
            res.rc = max(res.rc, 2)
            res.log += '\n' + '\n'.join(errors)
        ok = res.rc == 0
        if not quiet or not ok:
            ok = report(parent, '提取', res)
        return ok, password
    finally:
        shutil.rmtree(stage, ignore_errors=True)


LEVELS = [(0, '仅存储'), (1, '极速压缩'), (3, '快速压缩'), (5, '标准压缩'), (7, '最大压缩'), (9, '极限压缩')]
FORMATS = ['7z', 'zip', 'tar', 'tar.gz', 'tar.xz', 'tar.bz2', 'tar.zst']
METHODS = {'7z': ['LZMA2', 'LZMA', 'PPMd', 'BZip2'], 'zip': ['Deflate', 'Deflate64', 'BZip2', 'LZMA', 'PPMd']}
ZSTD_LEVEL = {0: 1, 1: 1, 3: 3, 5: 9, 7: 15, 9: 19}


def default_opts(archive, fmt='7z'):
    return {'archive': archive, 'fmt': fmt, 'level': 5, 'method': '', 'dict': '', 'solid': True,
            'threads': 0, 'volume': '', 'password': '', 'enc_method': 'AES256', 'enc_names': False,
            'delete_after': False, 'extra': ''}


def compress(parent, sources, o):
    """按选项 o 把 sources (绝对路径) 压缩。返回是否成功。"""
    fmt, out, level = o['fmt'], o['archive'], o['level']
    lf = write_listfile(sources)
    mt = [f"-mmt={o['threads']}"] if o['threads'] else []
    extra = o['extra'].split()
    part = None
    if fmt in ('7z', 'zip', 'tar'):
        cmd = [SEVENZ, 'a', '-t' + fmt, '-bsp1', '-sccUTF-8']
        if fmt != 'tar':
            cmd.append(f'-mx={level}')
            if o['method'] and level:
                cmd.append(('-m0=' if fmt == '7z' else '-mm=') + o['method'])
            lzma = o['method'] in ('LZMA', 'LZMA2') or (fmt == '7z' and not o['method'])
            if o['dict'] and level and lzma:
                cmd.append('-md=' + o['dict'])
            if fmt == '7z':
                cmd.append('-ms=on' if o['solid'] else '-ms=off')
            if o['password']:
                cmd.append('-p')
                if fmt == '7z' and o['enc_names']:
                    cmd.append('-mhe=on')
                if fmt == 'zip':
                    cmd.append('-mem=' + o['enc_method'])
        else:
            cmd.append('-snl')
        if o['volume']:
            cmd.append('-v' + o['volume'])
        if o['delete_after']:
            cmd.append('-sdel')
        stages = [cmd + mt + extra + [out, lf]]
    else:
        # tar.* : 7z 打 tar 流, 再交给压缩器; 先写到临时文件名, 成功后再改名
        part = os.path.join(os.path.dirname(out), f'.{os.path.basename(out)}.7zfm-part')
        first = [SEVENZ, 'a', '-ttar', '-snl', '-so', '-an', '-bsp2', '-sccUTF-8'] + extra + [lf]
        if fmt == 'tar.zst':
            second = ['zstd', '-q', '-f', '-T' + str(o['threads']), f'-{ZSTD_LEVEL[level]}', '-o', part]
        else:
            kind = {'tar.gz': 'gzip', 'tar.xz': 'xz', 'tar.bz2': 'bzip2'}[fmt]
            second = [SEVENZ, 'a', '-si', '-t' + kind, f'-mx={level}', '-bd'] + mt + [part]
        stages = [first, second]
    use_pw = bool(o['password']) and part is None and fmt != 'tar'
    res, _ = run_job(parent, f'正在压缩 {os.path.basename(out)}', stages,
                     o['password'] if use_pw else None, create=True)
    if part:
        if res.ok:
            os.replace(part, out)
        elif os.path.lexists(part):
            os.remove(part)
    return report(parent, '压缩', res)


def archive_add(parent, arc, inner, paths):
    """把磁盘上的 paths 加进压缩包 arc 的 inner 目录。"""
    cwd = None
    if inner:
        # 7z 只能按相对路径决定包内位置: 用符号链接搭出同样的目录结构
        cwd = mk_tmp()
        target = os.path.join(cwd, *inner)
        os.makedirs(target)
        for p in paths:
            os.symlink(p, os.path.join(target, os.path.basename(p.rstrip('/'))))
        sources = [inner[0]]
    else:
        sources = paths
    cmd = [SEVENZ, 'a', '-bsp1', '-sccUTF-8']
    if arc.password is not None:
        cmd.append('-p')
        if arc.header_encrypted:
            cmd.append('-mhe=on')
    res, _ = run_job(parent, '正在添加到压缩包', [cmd + [arc.path, write_listfile(sources)]],
                     arc.password, cwd=cwd, create=True)
    return report(parent, '添加', res)


def archive_edit(parent, arc, title, args):
    """7z d / rn 之类直接改包的命令。"""
    cmd = [SEVENZ, args[0], '-bsp1', '-sccUTF-8']
    if arc.password is not None:
        cmd.append('-p')
    res, _ = run_job(parent, title, [cmd + [arc.path] + args[1:]], arc.password, create=True)
    return report(parent, title, res)


def test_archive(parent, path, names=None, password=None, quiet=False):
    cmd = [SEVENZ, 't', '-bsp1', '-sccUTF-8', path] + ([write_listfile(names)] if names else [])
    res, password = run_job(parent, f'正在测试 {os.path.basename(path)}', [cmd], password,
                            arc_name=os.path.basename(path))
    if res.cancelled:
        return False, password
    if res.rc == 0:
        if not quiet:
            summary = [l for l in res.log.split('\n')
                       if re.match(r'(Folders|Files|Size|Compressed|Archives|Physical Size)\b', l)]
            UI.info(parent, '测试', f'{os.path.basename(path)}\n未发现错误。\n\n' + '\n'.join(summary))
        return True, password
    report(parent, '测试', res)
    return False, password


# ---------------------------------------------------------------- 对话框

class PathRow(QWidget):
    def __init__(self, text, pick_dir=True, save=False):
        super().__init__()
        self.pick_dir, self.save = pick_dir, save
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(text)
        self.edit.setMinimumWidth(420)
        btn = QToolButton()
        btn.setText('…')
        btn.clicked.connect(self.browse)
        lay.addWidget(self.edit)
        lay.addWidget(btn)

    def browse(self):
        cur = self.edit.text()
        if self.pick_dir:
            start = cur.split('*')[0]
            while start and not os.path.isdir(start):
                start = os.path.dirname(start.rstrip('/'))
            p = QFileDialog.getExistingDirectory(self, '选择文件夹', start or '/')
        else:
            p, _ = QFileDialog.getSaveFileName(self, '压缩包', cur, options=QFileDialog.Option.DontConfirmOverwrite)
        if p:
            self.edit.setText(p)


class CompressDialog(QDialog):
    def __init__(self, parent, archive, n_sources):
        super().__init__(parent)
        self.setWindowTitle('添加到压缩包')
        s = QSettings()
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f'压缩包 ({n_sources} 个项目):'))
        self.path = PathRow(archive, pick_dir=False)
        lay.addWidget(self.path)

        grid = QGridLayout()
        left = QFormLayout()
        self.fmt = QComboBox()
        self.fmt.addItems(FORMATS)
        self.level = QComboBox()
        for v, t in LEVELS:
            self.level.addItem(t, v)
        self.method = QComboBox()
        self.dict = QComboBox()
        self.dict.addItem('默认', '')
        for d in ('1m', '4m', '16m', '32m', '64m', '128m', '256m', '512m', '1024m'):
            self.dict.addItem(d.upper().replace('M', ' MB'), d)
        self.solid = QCheckBox('固实压缩 (压缩率更高, 更新更慢)')
        self.threads = QSpinBox()
        self.threads.setRange(0, os.cpu_count() or 1)
        self.threads.setSpecialValueText('自动')
        self.volume = QComboBox()
        self.volume.setEditable(True)
        self.volume.addItems(['', '10m', '100m', '700m', '1g', '4g'])
        left.addRow('压缩格式:', self.fmt)
        left.addRow('压缩等级:', self.level)
        left.addRow('压缩方法:', self.method)
        left.addRow('字典大小:', self.dict)
        left.addRow('CPU 线程数:', self.threads)
        left.addRow('分卷大小:', self.volume)
        left.addRow('', self.solid)
        grid.addLayout(left, 0, 0)

        right = QVBoxLayout()
        enc = QGroupBox('加密')
        ef = QFormLayout(enc)
        self.pw1 = QLineEdit()
        self.pw2 = QLineEdit()
        for e in (self.pw1, self.pw2):
            e.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_pw = QCheckBox('显示密码')
        self.show_pw.toggled.connect(self._show_pw)
        self.enc_method = QComboBox()
        self.enc_names = QCheckBox('加密文件名')
        ef.addRow('输入密码:', self.pw1)
        ef.addRow('重新输入:', self.pw2)
        ef.addRow('', self.show_pw)
        ef.addRow('加密算法:', self.enc_method)
        ef.addRow('', self.enc_names)
        right.addWidget(enc)
        self.delete_after = QCheckBox('压缩后删除源文件')
        right.addWidget(self.delete_after)
        right.addStretch()
        grid.addLayout(right, 0, 1)
        lay.addLayout(grid)

        pf = QFormLayout()
        self.extra = QLineEdit()
        self.extra.setPlaceholderText('附加的 7z 开关, 例如 -xr!*.tmp')
        pf.addRow('参数:', self.extra)
        lay.addLayout(pf)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText('确定')
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        fmt = s.value('compress/fmt', '7z')
        self.fmt.setCurrentText(fmt if fmt in FORMATS else '7z')
        self.level.setCurrentIndex(max(0, self.level.findData(int(s.value('compress/level', 5)))))
        self.solid.setChecked(s.value('compress/solid', 'true') == 'true')
        self.fmt.currentTextChanged.connect(self._format_changed)
        self._format_changed(self.fmt.currentText())

    def _show_pw(self, on):
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self.pw1.setEchoMode(mode)
        self.pw2.setEnabled(not on)

    def _format_changed(self, fmt):
        self.method.clear()
        self.method.addItem('默认', '')
        for m in METHODS.get(fmt, []):
            self.method.addItem(m, m)
        self.method.setEnabled(fmt in METHODS)
        self.dict.setEnabled(fmt in METHODS)
        self.level.setEnabled(fmt != 'tar')
        self.solid.setEnabled(fmt == '7z')
        can_enc = fmt in ('7z', 'zip')
        for w in (self.pw1, self.pw2, self.show_pw, self.enc_method):
            w.setEnabled(can_enc)
        self.enc_names.setEnabled(fmt == '7z')
        self.enc_method.clear()
        if fmt == 'zip':
            self.enc_method.addItem('AES-256', 'AES256')
            self.enc_method.addItem('ZipCrypto (兼容旧软件, 不安全)', 'ZipCrypto')
        else:
            self.enc_method.addItem('AES-256', 'AES256')
        direct = fmt in ('7z', 'zip', 'tar')
        self.volume.setEnabled(direct)
        self.delete_after.setEnabled(direct)
        # 跟着格式换扩展名
        p = self.path.edit.text()
        low = p.lower()
        for ext in sorted(('.' + f for f in FORMATS), key=len, reverse=True):
            if low.endswith(ext):
                p = p[:-len(ext)]
                break
        self.path.edit.setText(p + '.' + fmt)

    def accept(self):
        if self.pw1.isEnabled() and not self.show_pw.isChecked() and self.pw1.text() != self.pw2.text():
            QMessageBox.warning(self, '密码', '两次输入的密码不一致。')
            return
        out = self.options()['archive']
        if not os.path.isabs(out) or not os.path.isdir(os.path.dirname(out)):
            QMessageBox.warning(self, '压缩包', '请填写完整的压缩包路径，且所在文件夹必须存在。')
            return
        if os.path.exists(out):
            direct = self.fmt.currentText() in ('7z', 'zip', 'tar')
            q = ('压缩包已存在，文件将被添加/更新到其中。继续吗？' if direct
                 else '文件已存在，将被覆盖。继续吗？')
            if not UI.confirm(self, '压缩包已存在', f'{out}\n\n{q}'):
                return
        s = QSettings()
        s.setValue('compress/fmt', self.fmt.currentText())
        s.setValue('compress/level', self.level.currentData())
        s.setValue('compress/solid', 'true' if self.solid.isChecked() else 'false')
        super().accept()

    def options(self):
        fmt = self.fmt.currentText()
        o = default_opts(os.path.expanduser(self.path.edit.text().strip()), fmt)
        o.update(level=self.level.currentData(), method=self.method.currentData() or '',
                 dict=self.dict.currentData() or '', solid=self.solid.isChecked(),
                 threads=self.threads.value(), extra=self.extra.text().strip())
        if self.volume.isEnabled():
            o['volume'] = self.volume.currentText().strip().lower()
        if self.pw1.isEnabled():
            o.update(password=self.pw1.text(), enc_method=self.enc_method.currentData(),
                     enc_names=self.enc_names.isChecked() and self.enc_names.isEnabled())
        o['delete_after'] = self.delete_after.isChecked() and self.delete_after.isEnabled()
        return o


OVERWRITE_MODES = [('ask', '覆盖前询问'), ('overwrite', '不提示直接覆盖'), ('skip', '跳过已存在的文件'),
                   ('rename', '自动重命名')]


class ExtractDialog(QDialog):
    def __init__(self, parent, dest, multi=False):
        super().__init__(parent)
        self.setWindowTitle('提取')
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('提取到:' + ('  ( * 代表各压缩包的名称)' if multi else '')))
        self.path = PathRow(dest)
        lay.addWidget(self.path)
        form = QFormLayout()
        self.path_mode = QComboBox()
        self.path_mode.addItem('完整路径', False)
        self.path_mode.addItem('不含路径 (全部解到同一层)', True)
        self.overwrite = QComboBox()
        for k, t in OVERWRITE_MODES:
            self.overwrite.addItem(t, k)
        self.overwrite.setCurrentIndex(max(0, self.overwrite.findData(QSettings().value('extract/overwrite', 'ask'))))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText('没有加密就留空')
        form.addRow('路径模式:', self.path_mode)
        form.addRow('覆盖模式:', self.overwrite)
        form.addRow('密码:', self.pw)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText('确定')
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accept(self):
        if not os.path.isabs(os.path.expanduser(self.path.edit.text().strip())):
            QMessageBox.warning(self, '提取', '请填写完整的目标路径。')
            return
        QSettings().setValue('extract/overwrite', self.overwrite.currentData())
        super().accept()

    def values(self):
        return (os.path.expanduser(self.path.edit.text().strip()), self.path_mode.currentData(),
                self.overwrite.currentData(), self.pw.text() or None)


def extract_archives(parent, arcs):
    """弹出提取对话框, 然后依次提取磁盘上的若干压缩包。"""
    multi = len(arcs) > 1
    base = os.path.dirname(arcs[0])
    dest = os.path.join(base, '*' if multi else arc_stem(os.path.basename(arcs[0]))) + '/'
    dlg = ExtractDialog(parent, dest, multi)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return True
    pattern, flat, mode, pw = dlg.values()
    all_ok = True
    for a in arcs:
        ok, pw = extract(parent, a, pattern.replace('*', arc_stem(os.path.basename(a))),
                         mode=mode, password=pw, flat=flat, dedupe_root=True)
        all_ok = all_ok and ok
        if not ok and a != arcs[-1] and not UI.confirm(parent, '提取', '继续处理剩下的压缩包吗？'):
            break
    return all_ok


class PropsDialog(QDialog):
    def __init__(self, parent, title, pairs):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        for k, v in pairs:
            lab = QLabel(str(v))
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            lab.setMinimumWidth(360)
            form.addRow(k + ':', lab)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# ---------------------------------------------------------------- 文件列表

class Row:
    __slots__ = ('name', 'is_dir', 'size', 'packed', 'mtime', 'attrs', 'enc', 'crc', 'method',
                 'path', 'entry')

    def __init__(self, name, is_dir, size=None, packed=None, mtime='', attrs='', enc='', crc='',
                 method='', path=None, entry=None):
        self.name, self.is_dir, self.size, self.packed = name, is_dir, size, packed
        self.mtime, self.attrs, self.enc, self.crc, self.method = mtime, attrs, enc, crc, method
        self.path, self.entry = path, entry


class FileModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.sort_col, self.sort_order = COL_NAME, Qt.SortOrder.AscendingOrder
        self.draggable = True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid():
            f |= Qt.ItemFlag.ItemIsDragEnabled
        return f | Qt.ItemFlag.ItemIsDropEnabled

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == COL_NAME:
                return r.name
            if c == COL_SIZE:
                return fmt_size(r.size)
            if c == COL_PACKED:
                return fmt_size(r.packed)
            return (None, None, None, r.mtime, r.attrs, r.enc, r.crc, r.method)[c]
        if role == Qt.ItemDataRole.DecorationRole and c == COL_NAME:
            return file_icon(r.name, r.is_dir)
        if role == Qt.ItemDataRole.TextAlignmentRole and c in (COL_SIZE, COL_PACKED):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self._sort()
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_col, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        self._sort()
        self.layoutChanged.emit()

    def _sort(self):
        keys = {COL_NAME: lambda r: natural_key(r.name),
                COL_SIZE: lambda r: (r.size or 0, natural_key(r.name)),
                COL_PACKED: lambda r: (r.packed or 0, natural_key(r.name)),
                COL_MTIME: lambda r: (r.mtime, natural_key(r.name)),
                COL_ATTR: lambda r: r.attrs, COL_ENC: lambda r: r.enc, COL_CRC: lambda r: r.crc,
                COL_METHOD: lambda r: r.method}
        key = keys[self.sort_col]
        rev = self.sort_order == Qt.SortOrder.DescendingOrder
        dirs = sorted((r for r in self.rows if r.is_dir), key=key, reverse=rev)
        files = sorted((r for r in self.rows if not r.is_dir), key=key, reverse=rev)
        self.rows = dirs + files


class FileView(QTreeView):
    go_up = Signal()
    open_current = Signal()
    dropped = Signal(list)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setItemsExpandable(False)
        self.setAllColumnsShowFocus(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSortingEnabled(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_current.emit()
        elif ev.key() == Qt.Key.Key_Backspace:
            self.go_up.emit()
        else:
            super().keyPressEvent(ev)

    def startDrag(self, actions):
        urls = self.win.drag_urls()
        if not urls:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(u) for u in urls])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _accepts(self, ev):
        return ev.mimeData().hasUrls() and ev.source() is not self

    def dragEnterEvent(self, ev):
        ev.acceptProposedAction() if self._accepts(ev) else ev.ignore()

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction() if self._accepts(ev) else ev.ignore()

    def dropEvent(self, ev):
        if not self._accepts(ev):
            return ev.ignore()
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        ev.acceptProposedAction()
        if paths:
            # 等拖放事件结束后再弹对话框
            QTimer.singleShot(0, lambda: self.dropped.emit(paths))


# ---------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    def __init__(self, start=None):
        super().__init__()
        self.settings = QSettings()
        self.cwd = os.path.expanduser('~')
        self.stack = []      # [(Archive, [包内路径各段])], 嵌套打开的压缩包
        self.show_hidden = self.settings.value('view/hidden', 'false') == 'true'
        self.kio_procs = []

        self.model = FileModel()
        self.view = FileView(self)
        self.view.setModel(self.model)
        self.view.doubleClicked.connect(lambda idx: self.open_row(self.model.rows[idx.row()]))
        self.view.open_current.connect(self.open_selected)
        self.view.go_up.connect(self.go_up)
        self.view.dropped.connect(self.on_drop)
        self.view.customContextMenuRequested.connect(self.context_menu)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self.update_status())

        self.addr = QLineEdit()
        self.addr.returnPressed.connect(lambda: self.navigate(self.addr.text()))
        up = QToolButton()
        up.setIcon(theme_icon('go-up', std=QStyle.StandardPixmap.SP_ArrowUp))
        up.setToolTip('上一级 (Backspace)')
        up.setAutoRaise(True)
        up.clicked.connect(self.go_up)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.addWidget(up)
        bar.addWidget(self.addr)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.view)
        self.setCentralWidget(central)

        self.watcher = QFileSystemWatcher(self)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(400)
        self.refresh_timer.timeout.connect(self.refresh)
        self.watcher.directoryChanged.connect(lambda *_: self.refresh_timer.start())

        self._build_actions()
        self.resize(900, 600)
        geo = self.settings.value('window/geometry')
        if geo:
            self.restoreGeometry(geo)
        hdr = self.settings.value('window/header')
        if hdr:
            self.view.header().restoreState(hdr)
        else:
            for col, w in ((COL_NAME, 300), (COL_SIZE, 100), (COL_PACKED, 100), (COL_MTIME, 150), (COL_ATTR, 110), (COL_ENC, 40), (COL_CRC, 80)):
                self.view.setColumnWidth(col, w)
            self.view.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        self.navigate(start or self.settings.value('window/cwd', self.cwd))

    # ----- 菜单 / 工具栏

    def _act(self, text, slot, icon=None, shortcut=None, tip=None):
        a = QAction(text, self)
        if icon:
            a.setIcon(theme_icon(*icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(lambda *_: slot())
        return a

    def _build_actions(self):
        A = self._act
        self.a_add = A('添加', self.add, ('archive-insert', 'list-add'), 'Insert', '添加到压缩包')
        self.a_extract = A('提取', self.extract, ('archive-extract', 'document-save-as'), 'Ctrl+E', '提取文件')
        self.a_test = A('测试', self.test, ('dialog-ok-apply', 'task-complete', 'emblem-checked'), 'Ctrl+T', '测试压缩包')
        self.a_copy = A('复制', self.copy_to, ('edit-copy',), 'F5', '复制到…')
        self.a_move = A('移动', self.move_to, ('transform-move', 'go-jump', 'edit-cut'), 'F6', '移动到…')
        self.a_delete = A('删除', self.delete, ('edit-delete',), 'Delete')
        self.a_info = A('信息', self.properties, ('document-properties',), 'Alt+Return', '属性')
        self.a_open = A('打开', self.open_selected, ('document-open',))
        self.a_open_arc = A('作为压缩包打开', self.open_as_archive, ('archive-extract',))
        self.a_open_ext = A('用默认程序打开', self.open_external, ('system-run',))
        self.a_rename = A('重命名', self.rename, ('edit-rename',), 'F2')
        self.a_mkdir = A('新建文件夹', self.mkdir, ('folder-new',), 'F7')
        self.a_refresh = A('刷新', self.refresh, ('view-refresh',), 'Ctrl+R')
        self.a_up = A('上一级', self.go_up, ('go-up',), 'Alt+Up')
        self.a_home = A('主文件夹', lambda: self.navigate(os.path.expanduser('~')), ('go-home',), 'Alt+Home')
        self.a_select_all = A('全选', self.view.selectAll, ('edit-select-all',), 'Ctrl+A')
        self.a_hidden = A('显示隐藏文件', self.toggle_hidden, None, 'Ctrl+H')
        self.a_hidden.setCheckable(True)
        self.a_hidden.setChecked(self.show_hidden)
        self.a_addr = A('定位到地址栏', lambda: (self.addr.setFocus(), self.addr.selectAll()), None, 'Ctrl+L')

        self.hash_menu = QMenu('校验和', self)
        for label, algo in (('CRC-32', 'CRC32'), ('MD5', 'MD5'), ('SHA-1', 'SHA1'), ('SHA-256', 'SHA256'),
                            ('SHA-512', 'SHA512'), ('全部', '*')):
            self.hash_menu.addAction(A(label, lambda algo=algo: self.hash(algo)))

        mb = self.menuBar()
        m = mb.addMenu('文件(&F)')
        m.addActions([self.a_open, self.a_open_arc, self.a_open_ext])
        m.addSeparator()
        m.addActions([self.a_rename, self.a_copy, self.a_move, self.a_delete])
        m.addSeparator()
        m.addAction(self.a_mkdir)
        m.addMenu(self.hash_menu)
        m.addAction(self.a_info)
        m.addSeparator()
        m.addAction(A('退出', self.close, ('application-exit',), 'Ctrl+Q'))
        m = mb.addMenu('编辑(&E)')
        m.addAction(self.a_select_all)
        m.addAction(A('取消全选', self.view.clearSelection))
        m = mb.addMenu('查看(&V)')
        m.addActions([self.a_up, self.a_home, self.a_addr])
        m.addSeparator()
        m.addActions([self.a_hidden, self.a_refresh])
        m = mb.addMenu('工具(&T)')
        m.addActions([self.a_add, self.a_extract, self.a_test])
        m.addSeparator()
        m.addAction(A('基准测试', self.benchmark, ('speedometer',)))
        m = mb.addMenu('帮助(&H)')
        m.addAction(A('关于', self.about, ('help-about',)))

        tb = self.addToolBar('工具栏')
        tb.setObjectName('main')
        tb.setMovable(False)
        tb.setIconSize(QSize(32, 32))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        tb.addActions([self.a_add, self.a_extract, self.a_test])
        tb.addSeparator()
        tb.addActions([self.a_copy, self.a_move, self.a_delete, self.a_info])
        self.addAction(self.a_addr)

    def context_menu(self, pos):
        m = QMenu(self)
        rows = self.selected()
        if rows:
            m.addAction(self.a_open)
            if len(rows) == 1 and not rows[0].is_dir:
                m.addActions([self.a_open_arc, self.a_open_ext] if not self.stack else [self.a_open_arc])
            m.addSeparator()
        m.addActions([self.a_extract, self.a_test, self.a_add])
        m.addSeparator()
        if rows:
            m.addActions([self.a_copy, self.a_move, self.a_rename, self.a_delete])
            m.addSeparator()
        if not self.stack:
            m.addAction(self.a_mkdir)
            if rows:
                m.addMenu(self.hash_menu)
        m.addAction(self.a_info)
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ----- 位置与列表

    @property
    def arc(self):
        return self.stack[-1][0] if self.stack else None

    @property
    def inner(self):
        return self.stack[-1][1] if self.stack else []

    def location(self):
        if not self.stack:
            return self.cwd
        return '/'.join([self.arc.display] + self.inner)

    def selected(self):
        idxs = self.view.selectionModel().selectedRows()
        return [self.model.rows[i.row()] for i in sorted(idxs, key=lambda i: i.row())]

    def refresh(self, select=None):
        keep = select if select is not None else {r.name for r in self.selected()}
        if self.stack:
            node = self.arc.lookup(self.inner)
            if node is None:
                self.stack[-1] = (self.arc, [])
                node = self.arc.root
            rows = [Row(e.name, e.is_dir, e.size, e.packed, e.mtime, e.attrs, e.enc, e.crc, e.method,
                        entry=e) for e in node.children.values()]
        else:
            rows = self._scan_dir()
        self.model.set_rows(rows)
        for c in ARCHIVE_ONLY_COLS:
            self.view.setColumnHidden(c, not self.stack)
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        if not self.stack:
            self.watcher.addPath(self.cwd)
        self.addr.setText(self.location())
        self.setWindowTitle(f'{self.location()} - {APP_TITLE}')
        if keep:
            sm = self.view.selectionModel()
            first = None
            for i, r in enumerate(self.model.rows):
                if r.name in keep:
                    idx = self.model.index(i, 0)
                    sm.select(idx, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
                    first = first or idx
            if first:
                self.view.setCurrentIndex(first)
                self.view.scrollTo(first)
        self.update_status()

    def _scan_dir(self):
        rows = []
        try:
            with os.scandir(self.cwd) as it:
                for de in it:
                    if not self.show_hidden and de.name.startswith('.'):
                        continue
                    try:
                        st = de.stat()
                    except OSError:
                        try:
                            st = de.stat(follow_symlinks=False)
                        except OSError:
                            continue
                    is_dir = stat.S_ISDIR(st.st_mode)
                    rows.append(Row(de.name, is_dir, None if is_dir else st.st_size,
                                    mtime=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)),
                                    attrs=stat.filemode(st.st_mode), path=de.path))
        except OSError as e:
            self.statusBar().showMessage(f'无法读取文件夹: {e.strerror}', 5000)
        return rows

    def update_status(self):
        sel = self.selected()
        if sel:
            total = sum(r.size or 0 for r in sel)
            msg = f'已选择 {len(sel)} 个项目    {fmt_size(total)} 字节 ({human_size(total)})'
        else:
            msg = f'{len(self.model.rows)} 个项目'
        if self.arc:
            msg += f'    [{self.arc.outer_type or self.arc.type}' + ('，只读]' if self.arc.readonly else ']')
        self.statusBar().showMessage(msg)

    def navigate(self, text):
        path = os.path.abspath(os.path.expanduser(text.strip() or '/'))
        rest = []
        while not os.path.exists(path):
            path, tail = os.path.split(path)
            rest.insert(0, tail)
        if os.path.isdir(path):
            if rest:
                self.statusBar().showMessage('路径不存在，已定位到最近的上级文件夹', 4000)
            self.stack = []
            self.cwd = path
            self.refresh(select=set())
            return True
        self.stack = []
        self.cwd = os.path.dirname(path)
        if not self.open_archive(path):
            self.refresh(select={os.path.basename(path)})
            return False
        while rest and self.arc.lookup(self.inner + [rest[0]]):
            self.inner.append(rest.pop(0))
        self.refresh(select=set())
        return True

    def open_archive(self, path, display=None, readonly=False, password=None):
        """把磁盘上的 path 作为压缩包压入浏览栈。"""
        arc = Archive(path, display or path, readonly, password)
        if not arc.load(self):
            return False
        kids = list(arc.root.children.values())
        if arc.type in STREAM_TYPES and len(kids) == 1 and not kids[0].is_dir and (
                kids[0].name.lower().endswith('.tar') or tar_wrap_type(os.path.basename(arc.display))):
            # tar.gz 之类: 先把里面的 tar 解到缓存, 直接显示 tar 的内容
            tmp = mk_tmp()
            ok, pw = extract(self, path, tmp, mode='overwrite', password=arc.password,
                             title='正在解开 ' + os.path.basename(arc.display), quiet=True,
                             pipe_tar=False)
            inner_tar = os.path.join(tmp, kids[0].name)
            if ok and os.path.isfile(inner_tar):
                tar = Archive(inner_tar, arc.display, True, None)
                if tar.load(self):
                    tar.outer_type = f'tar.{arc.type}'
                    arc = tar
        self.stack.append((arc, []))
        self.refresh(select=set())
        return True

    def go_up(self):
        if self.stack:
            arc, inner = self.stack[-1]
            if inner:
                name = inner.pop()
            else:
                self.stack.pop()
                name = os.path.basename(arc.display)
            self.refresh(select={name})
        else:
            parent = os.path.dirname(self.cwd)
            if parent != self.cwd:
                name = os.path.basename(self.cwd)
                self.cwd = parent
                self.refresh(select={name})

    def open_selected(self):
        rows = self.selected()
        if len(rows) == 1:
            self.open_row(rows[0])

    def open_row(self, row, force_archive=False, force_external=False):
        if row.is_dir:
            if self.stack:
                self.inner.append(row.name)
            else:
                self.cwd = row.path
            return self.refresh(select=set())
        as_archive = force_archive or (not force_external and is_archive_name(row.name, OPEN_AS_ARCHIVE))
        if not self.stack:
            if as_archive and self.open_archive(row.path):
                return
            if not force_archive:
                QDesktopServices.openUrl(QUrl.fromLocalFile(row.path))
            return
        # 压缩包里的文件: 解到缓存再打开
        tmp = mk_tmp()
        ok, pw = extract(self, self.arc.path, tmp, [row.entry.raw], self.inner, 'overwrite',
                         self.arc.password, title=f'正在提取 {row.name}', quiet=True)
        if pw is not None:
            self.arc.password = pw
        path = os.path.join(tmp, row.name)
        if not ok or not os.path.exists(path):
            return
        if as_archive and self.open_archive(path, self.location() + '/' + row.name, readonly=True):
            return
        if not force_archive:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_as_archive(self):
        rows = self.selected()
        if len(rows) == 1 and not rows[0].is_dir:
            self.open_row(rows[0], force_archive=True)

    def open_external(self):
        rows = self.selected()
        if len(rows) == 1:
            self.open_row(rows[0], force_external=True)

    def toggle_hidden(self):
        self.show_hidden = self.a_hidden.isChecked()
        self.settings.setValue('view/hidden', 'true' if self.show_hidden else 'false')
        self.refresh()

    def reload_archive(self):
        if self.arc and self.arc.load(self):
            self.refresh()

    def _writable_archive(self):
        if self.arc.readonly:
            UI.info(self, APP_TITLE, '这个压缩包是从别的压缩包里临时解出来的 (或是 tar.gz 一类)，'
                                     '不能直接修改。\n请先把它提取到磁盘上。')
            return False
        return True

    # ----- 操作

    def add(self, paths=None):
        if self.stack:
            if not self._writable_archive():
                return
            if paths is None:
                paths, _ = QFileDialog.getOpenFileNames(self, '选择要添加到压缩包的文件',
                                                        os.path.dirname(self.stack[0][0].path))
            if paths:
                archive_add(self, self.arc, self.inner, paths)
                self.reload_archive()
            return
        if paths is None:
            paths = [r.path for r in self.selected()]
        if not paths:
            return UI.info(self, '添加', '请先选择要压缩的文件或文件夹。')
        base = arc_stem(os.path.basename(paths[0])) if len(paths) == 1 else (os.path.basename(self.cwd) or 'archive')
        dlg = CompressDialog(self, os.path.join(self.cwd, base + '.7z'), len(paths))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            o = dlg.options()
            compress(self, paths, o)
            self.refresh(select={os.path.basename(o['archive'])})

    def extract(self):
        rows = self.selected()
        if self.stack:
            outer = self.stack[0][0].path
            dest = os.path.join(os.path.dirname(outer), arc_stem(os.path.basename(outer))) + '/'
            dlg = ExtractDialog(self, dest)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            dest, flat, mode, pw = dlg.values()
            names = [r.entry.raw for r in rows] if rows else (
                [e.raw for e in self.arc.lookup(self.inner).children.values()] if self.inner else None)
            ok, pw = extract(self, self.arc.path, dest, names, self.inner, mode,
                             pw or self.arc.password, flat, dedupe_root=names is None)
            if pw is not None:
                self.arc.password = pw
            return
        arcs = [r.path for r in rows if not r.is_dir]
        if not arcs:
            return UI.info(self, '提取', '请先选择要提取的压缩包。')
        extract_archives(self, arcs)
        self.refresh()

    def test(self):
        rows = self.selected()
        if self.stack:
            names = [r.entry.raw for r in rows] or None
            ok, pw = test_archive(self, self.arc.path, names, self.arc.password)
            if pw is not None:
                self.arc.password = pw
            return
        arcs = [r.path for r in rows if not r.is_dir]
        if not arcs:
            return UI.info(self, '测试', '请先选择要测试的压缩包。')
        pw = None
        for a in arcs:
            ok, pw = test_archive(self, a, password=pw)

    def _pick_dir(self, title):
        start = os.path.dirname(self.stack[0][0].path) if self.stack else self.cwd
        return QFileDialog.getExistingDirectory(self, title, start)

    def copy_to(self):
        rows = self.selected()
        if not rows:
            return
        dest = self._pick_dir('复制到')
        if not dest:
            return
        if self.stack:
            ok, pw = extract(self, self.arc.path, dest, [r.entry.raw for r in rows], self.inner,
                             'ask', self.arc.password)
            if pw is not None:
                self.arc.password = pw
        else:
            self._kio('copy', [r.path for r in rows], dest)

    def move_to(self):
        rows = self.selected()
        if not rows:
            return
        if self.stack:
            return UI.info(self, '移动', '不支持把文件移出压缩包，请用「复制」提取后再在包内删除。')
        dest = self._pick_dir('移动到')
        if dest:
            self._kio('move', [r.path for r in rows], dest)

    def _kio(self, verb, paths, dest):
        """文件复制/移动交给 KDE 的 KIO: 自带进度、同名处理和撤销。"""
        exe = shutil.which('kioclient') or shutil.which('kioclient6') or shutil.which('kioclient5')
        if not exe:
            return UI.error(self, APP_TITLE, '找不到 kioclient，无法复制/移动文件。')
        p = QProcess(self)
        self.kio_procs.append(p)
        p.finished.connect(lambda *_: (self.kio_procs.remove(p), self.refresh()))
        p.start(exe, [verb] + paths + [dest])

    def delete(self):
        rows = self.selected()
        if not rows:
            return
        what = f'“{rows[0].name}”' if len(rows) == 1 else f'这 {len(rows)} 个项目'
        if self.stack:
            if not self._writable_archive():
                return
            if UI.confirm(self, '删除', f'确定要从压缩包中永久删除{what}吗？'):
                archive_edit(self, self.arc, '正在删除', ['d', write_listfile([r.entry.raw for r in rows])])
                self.reload_archive()
            return
        if not UI.confirm(self, '删除', f'确定要把{what}移到回收站吗？'):
            return
        failed = [r.name for r in rows if not QFile.moveToTrash(r.path)]
        if failed:
            UI.error(self, '删除', '以下项目无法移到回收站:\n' + '\n'.join(failed[:20]))
        self.refresh()

    def rename(self):
        rows = self.selected()
        if len(rows) != 1:
            return
        r = rows[0]
        if self.stack and not self._writable_archive():
            return
        new, ok = QInputDialog.getText(self, '重命名', '新名称:', text=r.name)
        new = new.strip()
        if not ok or not new or new == r.name:
            return
        if '/' in new:
            return UI.error(self, '重命名', '名称里不能包含 “/”。')
        if self.stack:
            if new in self.arc.lookup(self.inner).children:
                return UI.error(self, '重命名', f'压缩包里已存在 “{new}”。')
            head = r.entry.raw.rsplit('/', 1)[0] + '/' if '/' in r.entry.raw else ''
            archive_edit(self, self.arc, '正在重命名', ['rn', '--', r.entry.raw, head + new])
            self.reload_archive()
        else:
            target = os.path.join(self.cwd, new)
            if os.path.lexists(target):
                return UI.error(self, '重命名', f'已存在 “{new}”。')
            try:
                os.rename(r.path, target)
            except OSError as e:
                UI.error(self, '重命名', e.strerror)
        self.refresh(select={new})

    def mkdir(self):
        if self.stack:
            return UI.info(self, '新建文件夹', '不支持在压缩包里新建空文件夹。')
        name, ok = QInputDialog.getText(self, '新建文件夹', '文件夹名称:', text='新建文件夹')
        name = name.strip()
        if not ok or not name:
            return
        try:
            os.mkdir(os.path.join(self.cwd, name))
        except OSError as e:
            return UI.error(self, '新建文件夹', e.strerror)
        self.refresh(select={name})

    def hash(self, algo):
        paths = [r.path for r in self.selected()]
        if self.stack or not paths:
            return
        res, _ = run_job(self, '正在计算校验和',
                         [[SEVENZ, 'h', '-scrc' + algo, '-bsp1', '-sccUTF-8', write_listfile(paths)]])
        if res.cancelled:
            return
        TextDialog(self, '校验和', res.errors()).exec()

    def properties(self):
        rows = self.selected()
        if len(rows) == 1:
            r = rows[0]
            pairs = [('名称', r.name), ('类型', '文件夹' if r.is_dir else '文件')]
            if r.size is not None:
                pairs.append(('大小', f'{fmt_size(r.size)} 字节 ({human_size(r.size)})'))
            if r.packed is not None:
                pairs.append(('压缩后大小', f'{fmt_size(r.packed)} 字节'))
            pairs += [(k, v) for k, v in (('修改时间', r.mtime), ('属性', r.attrs), ('加密', r.enc),
                                          ('CRC', r.crc), ('算法', r.method)) if v]
            pairs.append(('位置', self.location()))
            return PropsDialog(self, '属性', pairs).exec()
        if rows:
            total = sum(r.size or 0 for r in rows)
            return PropsDialog(self, '属性', [('已选择', f'{len(rows)} 个项目'),
                                            ('大小合计', f'{fmt_size(total)} 字节 ({human_size(total)})'),
                                            ('位置', self.location())]).exec()
        if self.arc:
            files, dirs = count_entries(self.arc.root)
            size, packed = self.arc.root.size, self.arc.root.packed
            names = {'Path': '路径', 'Type': '类型', 'Physical Size': '物理大小', 'Headers Size': '文件头大小',
                     'Method': '算法', 'Solid': '固实', 'Blocks': '数据块', 'Comment': '注释',
                     'Volumes': '分卷数', 'Multivolume': '多卷', 'Characteristics': '特征'}
            pairs = [('压缩包', self.arc.display)]
            pairs += [(names.get(k, k), v) for k, v in self.arc.props.items() if k != 'Path' and v]
            pairs += [('文件数', files), ('文件夹数', dirs), ('原始大小', f'{fmt_size(size)} 字节 ({human_size(size)})')]
            try:
                phys = os.path.getsize(self.stack[0][0].path) if len(self.stack) == 1 else None
            except OSError:
                phys = None
            if phys and size:
                pairs.append(('压缩率', f'{phys * 100 / size:.1f}%'))
            return PropsDialog(self, '压缩包信息', pairs).exec()
        PropsDialog(self, '属性', [('文件夹', self.cwd), ('项目数', len(self.model.rows))]).exec()

    def benchmark(self):
        res, _ = run_job(self, '基准测试 (约一分钟)', [[SEVENZ, 'b', '-bsp1']])
        if not res.cancelled:
            TextDialog(self, '基准测试', res.log).exec()

    def about(self):
        res = ProgressDialog(self, '', [[SEVENZ]], delay=3000).run()
        line = next((l for l in res.log.split('\n') if l.startswith('7-Zip')), SEVENZ)
        QMessageBox.about(self, '关于', f'<b>{APP_TITLE} {VERSION}</b><br><br>'
                                      f'7-Zip 风格的 Qt 界面，压缩与解压由系统的 7z 完成。<br><br>'
                                      f'引擎: {line}<br>程序: {SEVENZ}')

    # ----- 拖放

    def drag_urls(self):
        rows = self.selected()
        if not rows:
            return []
        if not self.stack:
            return [r.path for r in rows]
        tmp = mk_tmp()
        ok, pw = extract(self, self.arc.path, tmp, [r.entry.raw for r in rows], self.inner,
                         'overwrite', self.arc.password, title='正在准备拖放的文件', quiet=True)
        if pw is not None:
            self.arc.password = pw
        if not ok:
            return []
        return [p for p in (os.path.join(tmp, r.name) for r in rows) if os.path.lexists(p)]

    def on_drop(self, paths):
        if self.stack:
            if self._writable_archive() and UI.confirm(
                    self, '添加', f'把 {len(paths)} 个项目添加到压缩包\n{self.location()} 吗？'):
                self.add(paths)
        elif len(paths) == 1 and os.path.isfile(paths[0]) and is_archive_name(paths[0], OPEN_AS_ARCHIVE):
            self.navigate(paths[0])
        else:
            self.add(paths)

    def closeEvent(self, ev):
        self.settings.setValue('window/geometry', self.saveGeometry())
        self.settings.setValue('window/header', self.view.header().saveState())
        self.settings.setValue('window/cwd', self.cwd)
        super().closeEvent(ev)


# ---------------------------------------------------------------- 命令行 (右键菜单用)

def default_archive_base(paths):
    parent = os.path.dirname(paths[0])
    if len(paths) == 1:
        name = os.path.basename(paths[0])
        base = name if os.path.isdir(paths[0]) else (os.path.splitext(name)[0] or name)
    else:
        base = os.path.basename(parent) or 'archive'
    return os.path.join(parent, base)


def cli_action(args):
    paths = [os.path.abspath(p.rstrip('/') or '/') for p in args.paths]
    missing = [p for p in paths if not os.path.lexists(p)]
    if missing or not paths:
        UI.error(None, APP_TITLE, '找不到文件:\n' + '\n'.join(missing) if missing else '没有指定文件。')
        return 1
    if args.add:
        dlg = CompressDialog(None, default_archive_base(paths) + '.7z', len(paths))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        return 0 if compress(None, paths, dlg.options()) else 1
    if args.add_to:
        out = default_archive_base(paths) + '.' + args.add_to
        if os.path.lexists(out):
            out = unique_path(out[:-len(args.add_to) - 1]) + '.' + args.add_to
        return 0 if compress(None, paths, default_opts(out, args.add_to)) else 1
    if args.extract:
        return 0 if extract_archives(None, paths) else 1
    rc, pw = 0, None
    for p in paths:
        if args.test:
            ok, pw = test_archive(None, p, password=pw)
        else:
            dest = os.path.dirname(p)
            if args.extract_sub:
                dest = os.path.join(dest, arc_stem(os.path.basename(p)))
            ok, pw = extract(None, p, dest, mode='ask', password=pw, dedupe_root=args.extract_sub)
        rc = rc or (0 if ok else 1)
    return rc


def main():
    ap = argparse.ArgumentParser(prog=APP_NAME, description=APP_TITLE)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--add', action='store_true', help='打开“添加到压缩包”对话框')
    g.add_argument('--add-to', choices=FORMATS, metavar='格式', help='直接压缩成指定格式')
    g.add_argument('--extract', action='store_true', help='打开“提取”对话框')
    g.add_argument('--extract-here', action='store_true', help='提取到压缩包所在文件夹')
    g.add_argument('--extract-sub', action='store_true', help='提取到以压缩包命名的子文件夹')
    g.add_argument('--test', action='store_true', help='测试压缩包')
    ap.add_argument('paths', nargs='*')
    args = ap.parse_args()

    app = QApplication(sys.argv[:1])
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setDesktopFileName(APP_NAME)
    app.setWindowIcon(theme_icon(APP_NAME, 'ark', 'package-x-generic'))
    if not shutil.which(SEVENZ):
        UI.error(None, APP_TITLE, '找不到 7z 命令，请先安装 7zip 软件包。')
        return 1
    if any((args.add, args.add_to, args.extract, args.extract_here, args.extract_sub, args.test)):
        return cli_action(args)
    win = MainWindow(args.paths[0] if args.paths else None)
    win.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
