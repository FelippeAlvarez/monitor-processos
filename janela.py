"""Janela própria do programa (v0.7.4): abre o painel numa janela de "aplicativo" do Chrome (ou do Edge), sem barra de endereço
nem abas, com perfil separado do navegador do dia a dia. Nada é instalado: usa o navegador que já existe no computador.
Se não achar Chrome nem Edge, ou se falhar, quem chama abre o navegador padrão normalmente."""
import ntpath
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _candidatos_windows(env):
    pf, pf86, local = env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""), env.get("LocalAppData", "")
    chrome = [ntpath.join(b, "Google", "Chrome", "Application", "chrome.exe") for b in (pf, pf86, local) if b]
    edge = [ntpath.join(b, "Microsoft", "Edge", "Application", "msedge.exe") for b in (pf86, pf, local) if b]
    return chrome, edge


def localizar_navegador(env=None, existe=os.path.exists, quais=shutil.which, plataforma=None):
    """(nome, caminho) do Chrome (preferido) ou do Edge; None se nenhum existir."""
    env = os.environ if env is None else env
    plataforma = plataforma or sys.platform
    if plataforma.startswith("win"):
        chrome, edge = _candidatos_windows(env)
    elif plataforma == "darwin":
        chrome = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        edge = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        chrome = [quais(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")]
        edge = [quais(n) for n in ("microsoft-edge", "microsoft-edge-stable")]
    for nome, lista in (("Chrome", chrome), ("Edge", edge)):
        for c in lista:
            if c and existe(c):
                return nome, c
    return None


def pasta_perfil(env=None, plataforma=None):
    """Perfil do navegador da janela do app. Fica FORA da pasta de dados (que pode estar numa pasta sincronizada, como o Dropbox)."""
    env = os.environ if env is None else env
    plataforma = plataforma or sys.platform
    if plataforma.startswith("win") and env.get("LocalAppData"):
        return Path(env["LocalAppData"]) / "MonitorProcessos" / "janela"
    return Path.home() / ".monitor-processos" / "janela"


def comando(exe, url, perfil):
    return [exe, f"--app={url}", f"--user-data-dir={perfil}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1366,860"]


def abrir(url, localizar=localizar_navegador, executar=subprocess.Popen, perfil=None):
    """Abre a janela própria. Devolve o nome do navegador usado, ou None se não foi possível (use o navegador padrão)."""
    achou = localizar()
    if not achou:
        return None
    nome, exe = achou
    try:
        pf = Path(perfil) if perfil else pasta_perfil()
        pf.mkdir(parents=True, exist_ok=True)
        executar(comando(exe, url, str(pf)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return nome
    except (OSError, ValueError):
        return None
