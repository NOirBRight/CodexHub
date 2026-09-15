#!/bin/bash
set -eu
scenario="$1"
for n in {1..10}; do
 gjs -m /tmp/codexhub-tray-lab/eval.js 'imports.gi.GLib.spawn_command_line_async("bash /tmp/codexhub-tray-lab/restart-app.sh"); true' >/dev/null
 sleep 3
 gjs -m /tmp/codexhub-tray-lab/eval.js 'Main.overview.hide(); Object.entries(Main.panel.statusArea).filter(([k])=>k.includes("codexhub")).forEach(([k,s])=>s.menu.open()); true' >/dev/null
 sleep 1
 gjs -m /tmp/codexhub-tray-lab/eval.js "$(cat /tmp/codexhub-tray-lab/inspect-expression.js)" > "/tmp/codexhub-tray-lab/artifacts/$scenario-$n.json"
 gjs -m /tmp/codexhub-tray-lab/check.js "/tmp/codexhub-tray-lab/artifacts/$scenario-$n.json" || true
 done
