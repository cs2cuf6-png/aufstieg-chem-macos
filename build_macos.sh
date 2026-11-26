#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ошибка: build_macos.sh нужно запускать на macOS." >&2
  exit 1
fi

ARCH="$(uname -m)"
echo "==> Сборка Aufstieg.app для macOS (${ARCH})..."

if ! command -v uv >/dev/null 2>&1; then
  echo "Ошибка: не найден uv. Установите: brew install uv" >&2
  exit 1
fi

uv sync
uv run --with pyinstaller pyinstaller --noconfirm cbyd_macos.spec

APP_PATH="dist/Aufstieg.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "Ошибка: ${APP_PATH} не был создан." >&2
  exit 1
fi

echo "==> Ad-hoc подпись приложения (codesign)..."
codesign --force --deep --sign - "$APP_PATH"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# 1. Build .dmg
DMG_NAME="dist/Aufstieg-macOS-${ARCH}.dmg"
rm -f "$DMG_NAME"
echo "==> Упаковка в ${DMG_NAME}..."
DMG_STAGE="$STAGING/dmg"
mkdir -p "$DMG_STAGE"
cp -R "$APP_PATH" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -volname "Aufstieg | Химия" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG_NAME"

# 2. Build one-click .pkg installer (installs to /Applications, clears quarantine, ensures Rosetta 2 on Apple Silicon)
PKG_NAME="dist/Aufstieg-macOS-Installer-${ARCH}.pkg"
rm -f "$PKG_NAME"
echo "==> Сборка установщика ${PKG_NAME}..."
PKG_ROOT="$STAGING/pkg_root"
PKG_SCRIPTS="$STAGING/pkg_scripts"
PKG_PLIST="$STAGING/components.plist"
COMP_PKG="$STAGING/component.pkg"

mkdir -p "$PKG_ROOT/Applications" "$PKG_SCRIPTS"
cp -R "$APP_PATH" "$PKG_ROOT/Applications/"

cat > "$PKG_SCRIPTS/postinstall" <<'EOF'
#!/bin/bash
set -e
APP="/Applications/Aufstieg.app"
/usr/bin/xattr -cr "$APP" 2>/dev/null || true
/bin/chmod -R a+rX "$APP" 2>/dev/null || true
if [[ "$(/usr/bin/uname -m)" == "arm64" ]] && [[ ! -f "/Library/Apple/usr/share/rosetta/rosetta" ]]; then
  /usr/sbin/softwareupdate --install-rosetta --agree-to-license >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod +x "$PKG_SCRIPTS/postinstall"

pkgbuild --analyze --root "$PKG_ROOT" "$PKG_PLIST"
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$PKG_PLIST"

pkgbuild \
  --root "$PKG_ROOT" \
  --component-plist "$PKG_PLIST" \
  --scripts "$PKG_SCRIPTS" \
  --identifier "com.aufchem.aufstieg" \
  --version "0.1.0" \
  --install-location "/" \
  "$COMP_PKG"

productbuild \
  --package "$COMP_PKG" \
  "$PKG_NAME"

echo ""
echo "Готово!"
echo "  Приложение: ${APP_PATH}"
echo "  Установщик: ${PKG_NAME}"
echo "  Образ DMG:  ${DMG_NAME}"
if [[ ! -f "cbyd.db" ]]; then
  echo "  Примечание: cbyd.db не найден в корне проекта — приложение будет скачивать синтезы онлайн"
  echo "  (или положите cbyd.db рядом с Aufstieg.app / в ~/Documents/Aufstieg/cbyd.db)."
fi
