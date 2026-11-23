//go:build !windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// Em sistemas que não são Windows este arquivo só serve aos testes do fluxo de arquivos:
// responde "sim" a tudo e não cria atalhos.

func execCmd(a []string) *exec.Cmd                { return exec.Command(a[0], a[1:]...) }
func msgSimNao(t, s string) bool                  { fmt.Println("[?]", s); return os.Getenv("MONITOR_AUTO") != "nao" }
func msgInfo(t, s string)                         { fmt.Println("[i]", s) }
func msgErro(t, s string)                         { fmt.Println("[!]", s) }
func msgTentar(t, s string) bool                  { fmt.Println("[?]", s); return false }
func pythonsConhecidos() []string                 { return nil }
func abrir(exe, dir string)                       {}
func removerRegistro()                            {}
func agendarAutoExclusao(string)                  {}
func removerAtalhos(string, string)               {}
func registrarDesinstalacao(string, string) error { return nil }
func criarAtalhos(r, exe string) ([]string, error) {
	return []string{filepath.Join(r, "atalho-simulado.txt")}, os.WriteFile(filepath.Join(r, "atalho-simulado.txt"), []byte("x"), 0o644)
}
func pastasUsuario() []string {
	return filepath.SplitList(strings.TrimSpace(os.Getenv("MONITOR_TEST_BASES")))
}
