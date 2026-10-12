#!/bin/sh
# 安装 7zfm 到当前用户 (~/.local), 不需要 root。加 --no-default 则不改默认打开方式。
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
BIN=$HOME/.local/bin
APPDIR=$DATA/7zfm

ARCHIVE_MIMES="application/x-7z-compressed application/zip application/vnd.rar application/x-rar \
application/x-tar application/x-compressed-tar application/x-bzip-compressed-tar \
application/x-bzip2-compressed-tar application/x-xz-compressed-tar application/x-zstd-compressed-tar \
application/x-lzma-compressed-tar application/gzip application/x-bzip2 application/x-bzip application/x-xz \
application/zstd application/x-lzma application/vnd.ms-cab-compressed application/x-arj application/x-lha \
application/x-lzh-compressed application/x-ms-wim application/x-cpio"
EXTRA_MIMES="application/x-cd-image application/x-iso9660-image application/x-rpm \
application/vnd.debian.binary-package application/vnd.squashfs application/java-archive"

mkdir -p "$APPDIR" "$BIN" "$DATA/applications" "$DATA/kio/servicemenus" "$DATA/icons/hicolor/scalable/apps"
[ "$HERE" = "$APPDIR" ] || cp "$HERE/7zfm.py" "$HERE/7zfm.svg" "$HERE/install.sh" "$HERE/uninstall.sh" "$HERE/README.md" "$APPDIR/"
chmod +x "$APPDIR/7zfm.py" "$APPDIR/install.sh" "$APPDIR/uninstall.sh"
ln -sf "$APPDIR/7zfm.py" "$BIN/7zfm"
cp "$APPDIR/7zfm.svg" "$DATA/icons/hicolor/scalable/apps/7zfm.svg"
EXE=$BIN/7zfm
join() { printf '%s;' "$@"; }

cat > "$DATA/applications/7zfm.desktop" <<END
[Desktop Entry]
Type=Application
Name=7-Zip File Manager
Name[zh_CN]=7-Zip 文件管理器
GenericName=Archive Manager
GenericName[zh_CN]=压缩工具
Comment[zh_CN]=7-Zip 风格的压缩包管理器
Exec=$EXE %f
Icon=7zfm
Terminal=false
Categories=Qt;Utility;Archiving;Compression;
MimeType=$(join $ARCHIVE_MIMES $EXTRA_MIMES)inode/directory;
StartupWMClass=7zfm
END

cat > "$DATA/kio/servicemenus/7zfm-compress.desktop" <<END
[Desktop Entry]
Type=Service
MimeType=all/all;
X-KDE-Submenu=7-Zip
X-KDE-Priority=TopLevel
Icon=7zfm
Actions=add;add7z;addzip;

[Desktop Action add]
Name=添加到压缩包…
Icon=archive-insert
Exec=$EXE --add %F

[Desktop Action add7z]
Name=压缩为 .7z
Icon=archive-insert
Exec=$EXE --add-to 7z %F

[Desktop Action addzip]
Name=压缩为 .zip
Icon=archive-insert
Exec=$EXE --add-to zip %F
END

cat > "$DATA/kio/servicemenus/7zfm-extract.desktop" <<END
[Desktop Entry]
Type=Service
MimeType=$(join $ARCHIVE_MIMES $EXTRA_MIMES)
X-KDE-Submenu=7-Zip
X-KDE-Priority=TopLevel
Icon=7zfm
Actions=here;sub;extract;open;test;

[Desktop Action open]
Name=打开压缩包
Icon=7zfm
Exec=$EXE %f

[Desktop Action here]
Name=解压到当前文件夹
Icon=archive-extract
Exec=$EXE --extract-here %F

[Desktop Action sub]
Name=解压到同名文件夹
Icon=archive-extract
Exec=$EXE --extract-sub %F

[Desktop Action extract]
Name=解压到…
Icon=archive-extract
Exec=$EXE --extract %F

[Desktop Action test]
Name=测试压缩包
Icon=dialog-ok-apply
Exec=$EXE --test %F
END
chmod +x "$DATA/kio/servicemenus/7zfm-compress.desktop" "$DATA/kio/servicemenus/7zfm-extract.desktop"

if [ "$1" != "--no-default" ]; then
    # 记下原来的默认程序, 卸载时恢复
    if [ ! -f "$APPDIR/previous-defaults" ]; then
        for m in $ARCHIVE_MIMES; do
            echo "$m $(xdg-mime query default "$m" 2>/dev/null)" >> "$APPDIR/previous-defaults"
        done
    fi
    xdg-mime default 7zfm.desktop $ARCHIVE_MIMES 2>/dev/null
fi
update-desktop-database "$DATA/applications" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "已安装: $EXE"
