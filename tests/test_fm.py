import importlib.util, os, sys, tempfile, subprocess, shutil
APP = sys.argv[1]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp(prefix='cfg-', dir=os.getcwd())
os.environ['XDG_CACHE_HOME'] = tempfile.mkdtemp(prefix='cache-', dir=os.getcwd())
spec = importlib.util.spec_from_file_location('fm', APP); fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)
from PySide6.QtWidgets import QApplication
app = QApplication([]); app.setOrganizationName('7zfm-test'); app.setApplicationName('7zfm-test')

msgs = []; pw_answers = []; conflict_answers = []
fm.UI.ask_password = staticmethod(lambda parent, name, wrong: (msgs.append(('pw', name, wrong)), pw_answers.pop(0))[1])
fm.UI.ask_conflict = staticmethod(lambda parent, path: (msgs.append(('conflict', path)), conflict_answers.pop(0))[1])
fm.UI.confirm = staticmethod(lambda *a: True)
fm.UI.info = staticmethod(lambda parent, title, text: msgs.append(('info', title, text)))
fm.UI.error = staticmethod(lambda parent, title, text, detail='': msgs.append(('error', title, text, detail)))

T = tempfile.mkdtemp(prefix='work-', dir=os.getcwd())
src = os.path.join(T, 'src'); os.makedirs(os.path.join(src, 'sub', 'deep'))
open(os.path.join(src, 'a.txt'), 'w').write('hello\n'); os.chmod(os.path.join(src, 'a.txt'), 0o755)
open(os.path.join(src, 'sub', 'b 中文.txt'), 'w').write('world\n')
open(os.path.join(src, 'sub', 'deep', 'c.bin'), 'wb').write(os.urandom(200000))
os.symlink('a.txt', os.path.join(src, 'link'))
fails = []
def check(name, cond, extra=''):
    print(('ok   ' if cond else 'FAIL ') + name, extra if not cond else '')
    if not cond: fails.append(name)
def tree(d):
    return sorted(os.path.relpath(os.path.join(r, f), d) for r, ds, fs in os.walk(d) for f in fs + ds)

# --- compress in every format
for fmt in fm.FORMATS:
    o = fm.default_opts(os.path.join(T, 'out.' + fmt), fmt)
    ok = fm.compress(None, [src], o)
    check(f'compress {fmt}', ok and os.path.getsize(o['archive']) > 1000, msgs[-1:] )
    d = os.path.join(T, 'x-' + fmt)
    ok, _ = fm.extract(None, o['archive'], d, mode='overwrite')
    got = tree(d)
    check(f'extract {fmt}', ok and got == ['src', 'src/a.txt', 'src/link', 'src/sub', 'src/sub/b 中文.txt', 'src/sub/deep', 'src/sub/deep/c.bin'], got)
    check(f'content {fmt}', open(os.path.join(d, 'src/sub/deep/c.bin'), 'rb').read() == open(os.path.join(src, 'sub/deep/c.bin'), 'rb').read())
    if fmt.startswith('tar'):
        check(f'perm+symlink {fmt}', os.access(os.path.join(d, 'src/a.txt'), os.X_OK) and os.path.islink(os.path.join(d, 'src/link')))
    check(f'no stage left {fmt}', not [n for n in os.listdir(d) if n.startswith('.7zfm')])
check('no part files', not [n for n in os.listdir(T) if '7zfm-part' in n], os.listdir(T))

# --- options: method/dict/volume/password
o = fm.default_opts(os.path.join(T, 'opt.7z')); o.update(method='LZMA2', dict='4m', level=9, solid=False, threads=2, password='秘密 pw', enc_names=True)
check('compress 7z opts+pw', fm.compress(None, [src], o), msgs[-1:])
o2 = fm.default_opts(os.path.join(T, 'opt.zip'), 'zip'); o2.update(method='Deflate', password='zp', enc_method='AES256', level=7)
check('compress zip pw', fm.compress(None, [src], o2), msgs[-1:])
o3 = fm.default_opts(os.path.join(T, 'vol.7z')); o3.update(volume='50k', level=0)
check('compress volumes', fm.compress(None, [src], o3) and os.path.exists(os.path.join(T, 'vol.7z.001')) and os.path.exists(os.path.join(T, 'vol.7z.002')), msgs[-1:])
ok, _ = fm.extract(None, os.path.join(T, 'vol.7z.001'), os.path.join(T, 'x-vol'), mode='overwrite')
check('extract volumes', ok and len(tree(os.path.join(T, 'x-vol'))) == 7)

# --- password flows
pw_answers[:] = ['wrong', '秘密 pw']; msgs.clear()
ok, pw = fm.extract(None, o['archive'], os.path.join(T, 'x-pw'), mode='overwrite')
check('encrypted header: ask, wrong, retry', ok and pw == '秘密 pw' and [m[2] for m in msgs if m[0] == 'pw'] == [False, True], msgs)
pw_answers[:] = ['zp']; msgs.clear()
ok, pw = fm.extract(None, o2['archive'], os.path.join(T, 'x-zpw'), mode='overwrite')
check('zip content password', ok and pw == 'zp' and len(tree(os.path.join(T, 'x-zpw'))) == 7, msgs)
pw_answers[:] = [None]
ok, pw = fm.extract(None, o['archive'], os.path.join(T, 'x-cancel'), mode='overwrite')
check('password cancel leaves nothing', not ok and os.listdir(os.path.join(T, 'x-cancel')) == [])
pw_answers[:] = ['zp']; msgs.clear()
ok, pw = fm.test_archive(None, o2['archive'])
check('test archive', ok and msgs[-1][0] == 'info', msgs)

# --- conflicts
d = os.path.join(T, 'x-7z'); open(os.path.join(d, 'src/a.txt'), 'w').write('LOCAL')
ok, _ = fm.extract(None, os.path.join(T, 'out.7z'), d, mode='skip')
check('conflict skip', ok and open(os.path.join(d, 'src/a.txt')).read() == 'LOCAL')
ok, _ = fm.extract(None, os.path.join(T, 'out.7z'), d, mode='rename')
check('conflict rename', ok and open(os.path.join(d, 'src/a.txt')).read() == 'LOCAL' and open(os.path.join(d, 'src/a_1.txt')).read() == 'hello\n', tree(d))
conflict_answers[:] = ['overwrite']; msgs.clear()
ok, _ = fm.extract(None, os.path.join(T, 'out.7z'), d, mode='ask')
check('conflict ask->overwrite (asked once)', ok and open(os.path.join(d, 'src/a.txt')).read() == 'hello\n' and len([m for m in msgs if m[0] == 'conflict']) == 1, msgs)
open(os.path.join(d, 'src/a.txt'), 'w').write('LOCAL'); conflict_answers[:] = [None]
ok, _ = fm.extract(None, os.path.join(T, 'out.7z'), d, mode='ask')
check('conflict ask->cancel', not ok and open(os.path.join(d, 'src/a.txt')).read() == 'LOCAL' and not [n for n in os.listdir(d) if n.startswith('.7zfm')])
ok, _ = fm.extract(None, os.path.join(T, 'out.zip'), os.path.join(T, 'x-flat'), mode='overwrite', flat=True)
check('flat extract', ok and {'a.txt', 'b 中文.txt', 'c.bin'} <= set(os.listdir(os.path.join(T, 'x-flat'))), os.listdir(os.path.join(T, 'x-flat')))

# --- main window browsing
win = fm.MainWindow(T)
names = lambda: [r.name for r in win.model.rows]
check('fs listing', 'src' in names() and 'out.7z' in names() and names()[0] == 'src', names())
row = lambda n: next(r for r in win.model.rows if r.name == n)
def select(*ns):
    sm = win.view.selectionModel(); sm.clearSelection()
    for i, r in enumerate(win.model.rows):
        if r.name in ns: sm.select(win.model.index(i, 0), sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
arcp = os.path.join(T, 'edit.7z'); shutil.copy(os.path.join(T, 'out.7z'), arcp); win.refresh()
win.open_row(row('edit.7z'))
check('enter archive', win.arc is not None and names() == ['src'] and win.arc.type == '7z', names())
win.open_row(row('src')); check('archive subdir', names() == ['sub', 'a.txt', 'link'], names())
check('dir size summed', row('sub').size == 200006, row('sub').size)
win.open_row(row('sub'))
check('location', win.location() == arcp + '/src/sub' and win.addr.text() == arcp + '/src/sub', win.location())
# extract selected relative to current dir
select('b 中文.txt', 'deep'); d = os.path.join(T, 'x-rel')
ok, _ = fm.extract(win, win.arc.path, d, [r.entry.raw for r in win.selected()], win.inner, 'overwrite')
check('extract selection relative', ok and tree(d) == ['b 中文.txt', 'deep', 'deep/c.bin'], tree(d))
# drag out
urls = win.drag_urls()
check('drag urls', len(urls) == 2 and all(os.path.exists(u) for u in urls) and {os.path.basename(u) for u in urls} == {'b 中文.txt', 'deep'}, urls)
# add into subdir
extra = os.path.join(T, 'new file.txt'); open(extra, 'w').write('NEW'); extradir = os.path.join(T, 'newdir'); os.makedirs(extradir); open(os.path.join(extradir, 'n.txt'), 'w').write('n')
win.add([extra, extradir])
check('add into archive subdir', {'new file.txt', 'newdir'} <= set(names()), names())
out = subprocess.run(['7z', 'l', '-ba', arcp], capture_output=True, text=True).stdout
check('add path correct', 'src/sub/new file.txt' in out and 'src/sub/newdir/n.txt' in out, out)
# rename + delete inside archive
select('new file.txt'); fm.QInputDialog.getText = staticmethod(lambda *a, **k: ('renamed.txt', True)); win.rename()
check('rename in archive', 'renamed.txt' in names() and 'new file.txt' not in names(), (names(), msgs[-2:]))
select('renamed.txt', 'newdir'); win.delete()
check('delete in archive', names() == ['deep', 'b 中文.txt'], names())
win.go_up(); win.go_up(); check('up to archive root', names() == ['src'] and win.inner == [])
win.go_up(); check('up out of archive', win.arc is None and win.cwd == T and [r.name for r in win.selected()] == ['edit.7z'])
# root-level add
win.open_row(row('edit.7z')); win.add([extra]); check('add at archive root', 'new file.txt' in names(), names()); win.go_up()
# tar.gz transparently
win.open_row(row('out.tar.gz'))
check('tar.gz shows tar content', names() == ['src'] and win.arc.readonly and win.arc.outer_type == 'tar.gzip', (names(), win.arc.type))
msgs.clear(); win.open_row(row('src')); select('a.txt'); win.delete()
check('readonly archive refuses edit', msgs and msgs[-1][0] == 'info' and 'a.txt' in names(), msgs)
win.navigate(T)
# nested archive
nest = os.path.join(T, 'nest.zip'); subprocess.run(['7z', 'a', nest, os.path.join(T, 'out.7z'), os.path.join(T, 'src', 'a.txt')], capture_output=True)
win.navigate(nest); check('navigate to archive path', win.arc is not None and set(names()) == {'out.7z', 'a.txt'}, names())
win.open_row(row('out.7z')); check('nested archive', len(win.stack) == 2 and names() == ['src'] and win.location() == nest + '/out.7z', win.location())
win.go_up(); check('up from nested', len(win.stack) == 1 and [r.name for r in win.selected()] == ['out.7z'])
win.navigate(arcp + '/src/sub'); check('navigate into archive inner path', win.inner == ['src', 'sub'] and 'deep' in names(), (win.inner, names()))
# encrypted header in browser
pw_answers[:] = ['秘密 pw']; win.navigate(o['archive'])
check('open encrypted', names() == ['src'] and win.arc.header_encrypted and win.arc.password == '秘密 pw')
win.add([extra]); out = subprocess.run(['7z', 'l', '-ba', o['archive']], input='秘密 pw\n', capture_output=True, text=True)
check('add to encrypted keeps header encryption', 'new file.txt' in names() and subprocess.run(['7z', 'l', o['archive']], stdin=subprocess.DEVNULL, capture_output=True).returncode != 0, names())
# fs ops
win.navigate(T); fm.QInputDialog.getText = staticmethod(lambda *a, **k: ('目录 x', True)); win.mkdir()
check('mkdir', os.path.isdir(os.path.join(T, '目录 x')) and [r.name for r in win.selected()] == ['目录 x'])
fm.QInputDialog.getText = staticmethod(lambda *a, **k: ('目录 y', True)); win.rename()
check('rename fs', os.path.isdir(os.path.join(T, '目录 y')) and not os.path.exists(os.path.join(T, '目录 x')))
win.model.sort(fm.COL_SIZE, fm.Qt.SortOrder.DescendingOrder); ns = names()
check('sort keeps dirs first', win.model.rows[0].is_dir and ns.index('src') < ns.index('out.tar'), ns[:6])
win.show_hidden = False; win.refresh(); check('hidden files filtered', not any(n.startswith('.') for n in names()))
# bad archive
bad = os.path.join(T, 'bad.7z'); open(bad, 'w').write('not an archive'); msgs.clear()
check('bad archive rejected', not win.open_archive(bad) and msgs and msgs[-1][0] == 'error', msgs)
win.close()
print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL PASSED')
sys.exit(1 if fails else 0)
