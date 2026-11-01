#!/bin/zsh
# Автозапуск Джарвіса при вході в macOS.
#   Увімкнути:  zsh ~/.openjarvis/src/jarvis-ua/autostart.sh on
#   Вимкнути:   zsh ~/.openjarvis/src/jarvis-ua/autostart.sh off
#
# Запускає wake.py у вікні Терміналу (згорнутому), бо Термінал уже має дозвіл
# на мікрофон. Фоновий процес без вікна macOS може мовчки позбавити мікрофона.

LABEL="studio.nexoria.jarvis"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CMD="source ~/.zshrc; ~/.openjarvis/.venv/bin/python ~/.openjarvis/src/jarvis-ua/wake.py"

case "$1" in
  on)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/osascript</string>
    <string>-e</string>
    <string>delay 8</string>
    <string>-e</string>
    <string>tell application "Terminal" to set w to do script "$CMD"</string>
    <string>-e</string>
    <string>delay 1</string>
    <string>-e</string>
    <string>tell application "Terminal" to set miniaturized of front window to true</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "✅ Автозапуск увімкнено. Джарвіс стартує через ~8 с після входу в систему."
    ;;
  off)
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "⏹  Автозапуск вимкнено."
    ;;
  *)
    echo "Використання: zsh autostart.sh on|off"
    ;;
esac
