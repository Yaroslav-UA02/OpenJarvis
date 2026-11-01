#!/bin/zsh
# Джарвіс як фонова служба macOS — працює без Терміналу, стартує з Mac,
# перезапускається після збою. На «Джарвіс» сам відкриває HUD.
#
#   zsh ~/.openjarvis/src/jarvis-ua/service.sh install   — встановити й запустити
#   zsh ~/.openjarvis/src/jarvis-ua/service.sh uninstall — повністю прибрати
#
# Після install доступні команди: jarvis-on, jarvis-off, jarvis-log, jarvis-status

set -e
APP="$HOME/Applications/Jarvis.app"
LABEL="studio.nexoria.jarvis"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.openjarvis/logs"
SRC="$HOME/.openjarvis/src/jarvis-ua"

install_app() {
  echo "→ Збираю Jarvis.app"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$LOGDIR"

  cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Jarvis</string>
  <key>CFBundleDisplayName</key><string>Jarvis</string>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleExecutable</key><string>Jarvis</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>Джарвіс слухає слово «Джарвіс» і твої команди.</string>
</dict>
</plist>
EOF

  # Виконуваний файл: підтягує лише потрібні ключі з ~/.zshrc і запускає wake.py
  cat > "$APP/Contents/MacOS/Jarvis" <<'EOF'
#!/bin/zsh
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
source <(grep -E '^export (ANTHROPIC_API_KEY|ELEVENLABS_API_KEY|ELEVENLABS_MODEL|HF_TOKEN)=' "$HOME/.zshrc")
export PYTHONUNBUFFERED=1
exec "$HOME/.openjarvis/.venv/bin/python" "$HOME/.openjarvis/src/jarvis-ua/wake.py" \
  >> "$HOME/.openjarvis/logs/jarvis.log" 2>&1
EOF
  chmod +x "$APP/Contents/MacOS/Jarvis"

  # Ad-hoc підпис — щоб macOS запам'ятала дозвіл на мікрофон саме для цього додатка
  codesign --force --deep -s - "$APP" 2>/dev/null || true
}

install_agent() {
  echo "→ Реєструю автозапуск"
  # старий автозапуск через Термінал більше не потрібен
  launchctl unload "$HOME/Library/LaunchAgents/studio.nexoria.jarvis.plist" 2>/dev/null || true
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string><string>-W</string><string>-a</string><string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd.log</string>
</dict>
</plist>
EOF
}

install_aliases() {
  if ! grep -q "jarvis-on" "$HOME/.zshrc"; then
    cat >> "$HOME/.zshrc" <<'EOF'

# ── Джарвіс як служба ──
alias jarvis-on='launchctl load ~/Library/LaunchAgents/studio.nexoria.jarvis.plist && echo "Джарвіс увімкнений"'
alias jarvis-off='launchctl unload ~/Library/LaunchAgents/studio.nexoria.jarvis.plist; pkill -f jarvis-ua/wake.py; echo "Джарвіс вимкнений"'
alias jarvis-log='tail -f ~/.openjarvis/logs/jarvis.log'
alias jarvis-status='pgrep -fl jarvis-ua/wake.py >/dev/null && echo "🟢 Джарвіс працює" || echo "🔴 Джарвіс не працює"'
alias jarvis-restart='jarvis-off; sleep 2; jarvis-on'
EOF
  fi
}

case "$1" in
  install)
    pkill -f jarvis-ua/wake.py 2>/dev/null || true
    install_app
    install_agent
    install_aliases
    launchctl load "$PLIST"
    echo
    echo "✅ Готово. Джарвіс працює у фоні і стартуватиме разом з Mac."
    echo "   Якщо macOS спитає доступ до мікрофона для «Jarvis» — натисни «Дозволити»."
    echo "   Команди (після: source ~/.zshrc): jarvis-status, jarvis-log, jarvis-off, jarvis-on, jarvis-restart"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    pkill -f jarvis-ua/wake.py 2>/dev/null || true
    rm -rf "$APP" "$PLIST"
    echo "Джарвіс-служба прибрана. Ручний запуск через jw лишився."
    ;;
  *)
    echo "Використання: zsh service.sh install | uninstall"
    ;;
esac
