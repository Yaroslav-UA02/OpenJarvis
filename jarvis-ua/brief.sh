#!/bin/zsh
# Ранковий бриф за розкладом.
#   zsh ~/.openjarvis/src/jarvis-ua/brief.sh install     — щодня о 7:30
#   zsh ~/.openjarvis/src/jarvis-ua/brief.sh uninstall   — вимкнути розклад
#   zsh ~/.openjarvis/src/jarvis-ua/brief.sh run         — запустити бриф зараз
#
# Якщо о 7:30 Mac спить — бриф запуститься, щойно ти його відкриєш.

LABEL="studio.nexoria.jarvis.brief"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR=7
MINUTE=30

run_brief() {
  source <(grep -E '^export [A-Z_]+=' "$HOME/.zshrc")
  mkdir -p "$HOME/.openjarvis/logs"
  "$HOME/.openjarvis/.venv/bin/python" "$HOME/.openjarvis/src/jarvis-ua/brief.py" "$@"
}

case "$1" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.openjarvis/logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string><string>$HOME/.openjarvis/src/jarvis-ua/brief.sh</string><string>run</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$HOME/.openjarvis/logs/brief.log</string>
  <key>StandardErrorPath</key><string>$HOME/.openjarvis/logs/brief.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    grep -q "jarvis-brief" "$HOME/.zshrc" || cat >> "$HOME/.zshrc" <<'EOF'
alias jarvis-brief='zsh ~/.openjarvis/src/jarvis-ua/brief.sh run'
alias jarvis-brief-check='zsh ~/.openjarvis/src/jarvis-ua/brief.sh run --check'
EOF
    echo "✅ Бриф щодня о $HOUR:$MINUTE. Зараз: jarvis-brief, перевірка джерел: jarvis-brief-check"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "⏹  Розклад брифу вимкнено."
    ;;
  run)
    shift
    run_brief "$@"
    ;;
  *)
    echo "Використання: zsh brief.sh install | uninstall | run [--check|--text|--no-speak]"
    ;;
esac
