#!/bin/bash
# Build "Tmux Dashboard.saver" from DashSaverView.swift and install it for this user.
set -euo pipefail
cd "$(dirname "$0")"
OUT="build/Tmux Dashboard.saver"
mkdir -p build && [ -e "$OUT" ] && mv "$OUT" "build/previous-$(date +%Y%m%d-%H%M%S).saver"
mkdir -p "$OUT/Contents/MacOS"
SDK=$(xcrun --show-sdk-path)
xcrun swiftc -O -parse-as-library -module-name TmuxDashboard -target arm64-apple-macos13.0 -sdk "$SDK" \
    -c DashSaverView.swift -o build/DashSaverView.o
xcrun clang -bundle -target arm64-apple-macos13.0 -isysroot "$SDK" build/DashSaverView.o \
    -L "$SDK/usr/lib/swift" -L /usr/lib/swift -Wl,-rpath,/usr/lib/swift \
    -framework ScreenSaver -framework WebKit -framework Cocoa \
    -o "$OUT/Contents/MacOS/TmuxDashboard"
cp Info.plist "$OUT/Contents/Info.plist"
codesign --force -s - "$OUT"
DEST="$HOME/Library/Screen Savers/Tmux Dashboard.saver"
[ -e "$DEST" ] && mv "$DEST" "build/installed-before-$(date +%Y%m%d-%H%M%S).saver"
cp -R "$OUT" "$DEST"
echo "installed: $DEST"
