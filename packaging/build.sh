#!/bin/zsh
# Builds "dist/MX Keys Light.app" and a release zip. Usage: packaging/build.sh [version]
set -e
cd "$(dirname "$0")/.."
VERSION="${1:-1.0.0}"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
.venv/bin/pip install -q pyinstaller
QT_QPA_PLATFORM=offscreen .venv/bin/python packaging/make_icon.py
rm -rf build dist
.venv/bin/pyinstaller --noconfirm --windowed --name "MX Keys Light" \
  --icon packaging/icon.icns \
  --osx-bundle-identifier io.github.srkanyalcinkaya.mxkeys-backlight-fx \
  --hidden-import pynput.keyboard._darwin --hidden-import pynput.mouse._darwin \
  --exclude-module tkinter \
  app.py >/dev/null
APP="dist/MX Keys Light.app"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string 'MIT License'" "$APP/Contents/Info.plist" 2>/dev/null || true
codesign --force --deep -s - "$APP"            # ad-hoc signature (not notarized)
ZIP="dist/MX-Keys-Light-$VERSION-macos-$(uname -m).zip"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "built: $APP"
echo "zip:   $ZIP ($(du -h "$ZIP" | cut -f1))"
