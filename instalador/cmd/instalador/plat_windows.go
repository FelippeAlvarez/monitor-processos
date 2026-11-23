package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

const criarSemJanela = 0x08000000

func execCmd(a []string) *exec.Cmd {
	c := exec.Command(a[0], a[1:]...)
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: criarSemJanela}
	return c
}

var user32 = syscall.NewLazyDLL("user32.dll")

func caixa(titulo, texto string, flags uintptr) int {
	t, _ := syscall.UTF16PtrFromString(titulo)
	x, _ := syscall.UTF16PtrFromString(texto)
	r, _, _ := user32.NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(x)), uintptr(unsafe.Pointer(t)), flags|0x40000) // topmost
	return int(r)
}

func msgSimNao(t, s string) bool { return caixa(t, s, 0x4|0x20) == 6 } // YESNO | ICONQUESTION -> IDYES
func msgInfo(t, s string)        { caixa(t, s, 0x40) }
func msgErro(t, s string)        { caixa(t, s, 0x10) }
func msgTentar(t, s string) bool { return caixa(t, s, 0x5|0x30) == 4 } // RETRYCANCEL | ICONWARNING -> IDRETRY

func pythonsConhecidos() []string {
	var r []string
	for _, base := range []string{filepath.Join(os.Getenv("LOCALAPPDATA"), "Programs", "Python"), os.Getenv("ProgramFiles")} {
		if base == "" {
			continue
		}
		m, _ := filepath.Glob(filepath.Join(base, "Python3*", "python.exe"))
		r = append(r, m...)
	}
	return r
}

func abrir(exe, dir string) {
	c := exec.Command(exe)
	c.Dir = dir
	_ = c.Start()
}

// powershell grava o script num .ps1 temporário (UTF-8 com BOM) e o executa com -File; os dados entram por
// variáveis de ambiente (sem problema de aspas ou acentos).
func powershell(script string, env ...string) error {
	f, err := os.CreateTemp("", "asa-*.ps1")
	if err != nil {
		return err
	}
	nome := f.Name()
	defer os.Remove(nome)
	_, err = f.WriteString("\ufeff" + strings.ReplaceAll(script, "\r\n", "\n"))
	if e2 := f.Close(); err == nil {
		err = e2
	}
	if err != nil {
		return err
	}
	c := execCmd([]string{"powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", nome})
	c.Env = append(os.Environ(), env...)
	out, err := c.CombinedOutput()
	if err != nil {
		return &erroPS{err, strings.TrimSpace(string(out))}
	}
	return nil
}

type erroPS struct {
	err error
	out string
}

func (e *erroPS) Error() string { return e.err.Error() + ": " + e.out }

// Cria (ou corrige) os atalhos da Área de Trabalho e do Menu Iniciar. Só reescreve um atalho existente
// se o alvo estiver errado; se estiver certo, não mexe (preserva a posição e as escolhas do usuário).
// As pastas vêm de GetFolderPath, que respeita Área de Trabalho redirecionada (OneDrive).
// Os caminhos criados/confirmados são gravados em <raiz>\atalhos.txt para o Go conferir.
const psAtalhos = `
$ErrorActionPreference='Stop'
$w=New-Object -ComObject WScript.Shell
$lista=@()
foreach($pasta in 'Desktop','Programs'){
  $dir=[Environment]::GetFolderPath($pasta)
  if([string]::IsNullOrEmpty($dir)){continue}
  $lnk=Join-Path $dir ($env:ASA_NOME+'.lnk')
  $ok=$false
  if(Test-Path -LiteralPath $lnk){ try{ $ok=($w.CreateShortcut($lnk).TargetPath -ieq $env:ASA_EXE) }catch{} }
  if(-not $ok){
    $s=$w.CreateShortcut($lnk)
    $s.TargetPath=$env:ASA_EXE
    $s.WorkingDirectory=$env:ASA_RAIZ
    $s.IconLocation=$env:ASA_EXE+',0'
    $s.Description='Monitor de Processos - Alvarez de Sa Advocacia'
    $s.Save()
  }
  $lista+=$lnk
}
[IO.File]::WriteAllLines($env:ASA_LISTA,$lista,(New-Object Text.UTF8Encoding($false)))
`

func criarAtalhos(r, exe string) ([]string, error) {
	lista := filepath.Join(r, "atalhos.txt")
	_ = os.Remove(lista)
	if err := powershell(psAtalhos, "ASA_NOME="+nomeApp, "ASA_EXE="+exe, "ASA_RAIZ="+r, "ASA_LISTA="+lista); err != nil {
		return nil, err
	}
	b, err := os.ReadFile(lista)
	if err != nil {
		return nil, err
	}
	var r2 []string
	for _, l := range strings.Split(strings.ReplaceAll(string(b), "\r", ""), "\n") {
		if strings.TrimSpace(l) != "" {
			r2 = append(r2, strings.TrimSpace(l))
		}
	}
	return r2, nil
}

const psRemover = `
$w=New-Object -ComObject WScript.Shell
foreach($pasta in 'Desktop','Programs'){
  $dir=[Environment]::GetFolderPath($pasta)
  if([string]::IsNullOrEmpty($dir)){continue}
  $lnk=Join-Path $dir ($env:ASA_NOME+'.lnk')
  if(Test-Path -LiteralPath $lnk){ try{ if($w.CreateShortcut($lnk).TargetPath -ieq $env:ASA_EXE){ Remove-Item -LiteralPath $lnk -Force } }catch{} }
}
`

func removerAtalhos(r, exe string) {
	_ = powershell(psRemover, "ASA_NOME="+nomeApp, "ASA_EXE="+exe)
}

const chaveDesinst = `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\MonitorProcessos`

func reg(args ...string) error {
	return execCmd(append([]string{"reg.exe"}, args...)).Run()
}

func registrarDesinstalacao(r, versao string) error {
	exe := filepath.Join(r, exeNome)
	itens := [][]string{
		{"DisplayName", "REG_SZ", nomeApp},
		{"DisplayVersion", "REG_SZ", versao},
		{"Publisher", "REG_SZ", "Alvarez de Sá Advocacia"},
		{"InstallLocation", "REG_SZ", r},
		{"DisplayIcon", "REG_SZ", exe},
		{"UninstallString", "REG_SZ", `"` + filepath.Join(r, instNome) + `" /desinstalar`},
		{"NoModify", "REG_DWORD", "1"},
		{"NoRepair", "REG_DWORD", "1"},
	}
	for _, i := range itens {
		if err := reg("add", chaveDesinst, "/v", i[0], "/t", i[1], "/d", i[2], "/f"); err != nil {
			return err
		}
	}
	return nil
}

func removerRegistro() { _ = reg("delete", chaveDesinst, "/f") }

// A cópia do instalador que fica em <raiz> não pode apagar a si mesma enquanto roda: um cmd separado espera e apaga.
// A linha de comando é passada crua (CmdLine) para o cmd.exe interpretar as aspas do jeito dele.
func agendarAutoExclusao(r string) {
	linha := `cmd.exe /c "ping -n 3 127.0.0.1 >nul & del /f /q "` + filepath.Join(r, instNome) + `" & rmdir "` + r + `""`
	c := exec.Command("cmd.exe")
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: criarSemJanela, CmdLine: linha}
	_ = c.Start()
}

func pastasUsuario() []string {
	h := os.Getenv("USERPROFILE")
	var b []string
	if h != "" {
		for _, s := range []string{"Downloads", "Desktop", "Documents", "Área de Trabalho", "Documentos",
			`OneDrive\Desktop`, `OneDrive\Documents`, `OneDrive\Área de Trabalho`, `OneDrive\Documentos`} {
			b = append(b, filepath.Join(h, s))
		}
		b = append(b, h)
	}
	return append(b, `C:\`)
}
