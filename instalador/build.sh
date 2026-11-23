#!/bin/bash
# Gera Instalar-MonitorProcessos-vX.Y.Z.exe a partir do código do app. Uso: ./build.sh [pasta-do-app] [saida]
set -euo pipefail
cd "$(dirname "$0")"
APP="${1:-$(cd .. && pwd)}"
VER=$(python3 -c "import json,sys;print(json.load(open('$APP/sobre.json'))['versao'])")
OUT="${2:-$PWD/Instalar-MonitorProcessos-v$VER.exe}"
export GOOS=windows GOARCH=amd64 CGO_ENABLED=0
# 1) recursos (ícone, versão, manifesto)
V4=$(echo "$VER" | awk -F. '{printf "%s,%s,%s,0",$1,$2,$3}'); V4D=${V4//,/.}
for n in launcher instalador; do
  sed -e "s/FILEVERSION .*/FILEVERSION $V4/" -e "s/PRODUCTVERSION .*/PRODUCTVERSION $V4/" -e "s/0\.6\.1\.0/$V4D/g" res/$n.rc > res/_$n.rc
  sed -i "s/version=\"[0-9.]*\"/version=\"$V4D\"/" res/manifest.xml
  (cd res && llvm-rc /C 65001 /FO _$n.res _$n.rc && llvm-cvtres /machine:x64 /out:_$n.obj _$n.res >/dev/null)
  cp res/_$n.obj cmd/$n/rsrc_windows_amd64.syso
done
# 2) launcher
go build -trimpath -ldflags "-s -w" -o cmd/instalador/payload/MonitorProcessos.exe ./cmd/launcher
cp res/monitor.ico cmd/instalador/payload/monitor.ico
# 3) pacote do app (sem dados, testes, caches, local.json, .bat de instalação manual)
python3 - "$APP" cmd/instalador/payload/app.zip <<'PY'
import sys, zipfile, os
app, saida = sys.argv[1:3]
skip_dirs = {"dados", "tests", "instalador", "__pycache__", ".git"}
skip_files = {"local.json", "iniciar.bat", "iniciar-celular.bat"}
with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as z:
    for raiz, dirs, files in os.walk(app):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for f in sorted(files):
            if f in skip_files or f.endswith(".pyc") or f.endswith(".zip"):
                continue
            p = os.path.join(raiz, f)
            z.write(p, "MonitorProcessos/" + os.path.relpath(p, app).replace(os.sep, "/"))
PY
# 4) instalador (subsistema GUI: não abre janela preta)
go build -trimpath -ldflags "-s -w -H=windowsgui -X main.Versao=$VER" -o "$OUT" ./cmd/instalador
echo "$OUT"
