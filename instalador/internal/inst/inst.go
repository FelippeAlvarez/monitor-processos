// Package inst reúne a lógica portável do instalador e do launcher do Monitor de Processos
// (extração do zip, troca da pasta app, localização do Python, cópia de dados).
// Nada aqui depende do Windows, para poder ser testado em qualquer sistema.
package inst

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// ExtractZip extrai o zip em dest, removendo o prefixo "MonitorProcessos/" se houver.
// Recusa caminhos que saiam de dest (zip-slip) e ignora dados/, local.json e __pycache__:
// o instalador nunca leva (nem sobrescreve) dados do usuário.
func ExtractZip(data []byte, dest string) error {
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return err
	}
	dest = filepath.Clean(dest)
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return err
	}
	for _, f := range zr.File {
		nome := strings.TrimPrefix(filepath.ToSlash(f.Name), "MonitorProcessos/")
		if nome == "" || nome == "." {
			continue
		}
		if nome == "local.json" || strings.HasPrefix(nome, "dados/") || nome == "dados" || strings.Contains(nome, "__pycache__") {
			continue
		}
		alvo := filepath.Join(dest, filepath.FromSlash(nome))
		if alvo != dest && !strings.HasPrefix(alvo, dest+string(os.PathSeparator)) {
			return fmt.Errorf("caminho inseguro no pacote: %q", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(alvo, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(alvo), 0o755); err != nil {
			return err
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(alvo, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(out, rc)
		rc.Close()
		if e2 := out.Close(); err == nil {
			err = e2
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// SwapApp troca raiz/app pela pasta "novo" (já extraída), guardando a versão atual em raiz/app.anterior.
// Se algo falhar, devolve a versão atual ao lugar. Nunca toca em raiz/dados.
func SwapApp(raiz, novo string) error {
	app := filepath.Join(raiz, "app")
	ant := filepath.Join(raiz, "app.anterior")
	if _, err := os.Stat(filepath.Join(novo, "app.py")); err != nil {
		return errors.New("pacote novo sem app.py")
	}
	if err := os.RemoveAll(ant); err != nil {
		return fmt.Errorf("não consegui limpar a cópia anterior: %w", err)
	}
	_, errApp := os.Stat(app)
	tinha := errApp == nil
	if tinha {
		if err := os.Rename(app, ant); err != nil {
			return fmt.Errorf("não consegui guardar a versão atual (programa aberto?): %w", err)
		}
	}
	if err := os.Rename(novo, app); err != nil {
		if tinha {
			_ = os.Rename(ant, app)
		}
		return fmt.Errorf("não consegui colocar a versão nova: %w", err)
	}
	return nil
}

var reVersao = regexp.MustCompile(`Python (\d+)\.(\d+)`)

// PythonOK diz se a saída de "python --version" é Python 3.9 ou superior.
func PythonOK(saida string) bool {
	m := reVersao.FindStringSubmatch(saida)
	if m == nil {
		return false
	}
	maior, _ := strconv.Atoi(m[1])
	menor, _ := strconv.Atoi(m[2])
	return maior == 3 && menor >= 9
}

// EncontrarPython procura um Python 3.9+. executa roda o comando (com "--version") e devolve a saída.
// extras são caminhos completos de python.exe já localizados pelo chamador.
// Devolve o comando (ex.: ["py","-3"]) ou nil. O "python" falso da Microsoft Store não responde
// "Python 3.x" e por isso é descartado.
func EncontrarPython(executa func(args []string) (string, error), extras []string) []string {
	cands := [][]string{{"py", "-3"}, {"python"}, {"python3"}}
	for _, e := range extras {
		cands = append(cands, []string{e})
	}
	for _, c := range cands {
		saida, err := executa(append(append([]string{}, c...), "--version"))
		if err == nil && PythonOK(saida) {
			return c
		}
	}
	return nil
}

// EscreverSeDiferente grava data em caminho (via arquivo temporário + renomeação). Devolve true se gravou.
func EscreverSeDiferente(caminho string, data []byte, modo os.FileMode) (bool, error) {
	if atual, err := os.ReadFile(caminho); err == nil && bytes.Equal(atual, data) {
		return false, nil
	}
	tmp := caminho + ".novo"
	if err := os.WriteFile(tmp, data, modo); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, caminho); err != nil {
		_ = os.Remove(tmp)
		return false, err
	}
	return true, nil
}

// CopiarPasta copia origem para destino (sem apagar nada na origem). Não sobrescreve arquivos existentes.
func CopiarPasta(origem, destino string) error {
	return filepath.Walk(origem, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(origem, p)
		alvo := filepath.Join(destino, rel)
		if info.IsDir() {
			return os.MkdirAll(alvo, 0o755)
		}
		if _, err := os.Stat(alvo); err == nil {
			return nil
		}
		in, err := os.Open(p)
		if err != nil {
			return err
		}
		defer in.Close()
		out, err := os.OpenFile(alvo, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o644)
		if err != nil {
			return err
		}
		_, err = io.Copy(out, in)
		if e2 := out.Close(); err == nil {
			err = e2
		}
		return err
	})
}

// MelhorDados escolhe, entre os caminhos de processos.db dados, o mais recentemente modificado.
func MelhorDados(bancos []string) string {
	melhor, quando := "", int64(-1)
	for _, b := range bancos {
		if st, err := os.Stat(b); err == nil && st.ModTime().UnixNano() > quando {
			melhor, quando = b, st.ModTime().UnixNano()
		}
	}
	return melhor
}
