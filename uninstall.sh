#!/bin/sh
# 卸载 7zfm, 并恢复安装前的默认打开方式。
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
APPDIR=$DATA/7zfm
if [ -f "$APPDIR/previous-defaults" ]; then
    while read -r mime app; do
        [ -n "$app" ] && xdg-mime default "$app" "$mime" 2>/dev/null
    done < "$APPDIR/previous-defaults"
fi
rm -f "$HOME/.local/bin/7zfm" "$DATA/applications/7zfm.desktop" \
      "$DATA/kio/servicemenus/7zfm-compress.desktop" "$DATA/kio/servicemenus/7zfm-extract.desktop" \
      "$DATA/icons/hicolor/scalable/apps/7zfm.svg" \
      "$APPDIR/7zfm.py" "$APPDIR/7zfm.svg" "$APPDIR/install.sh" "$APPDIR/previous-defaults" "$APPDIR/uninstall.sh"
rmdir "$APPDIR" 2>/dev/null
update-desktop-database "$DATA/applications" 2>/dev/null
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1
echo "已卸载 7zfm"
