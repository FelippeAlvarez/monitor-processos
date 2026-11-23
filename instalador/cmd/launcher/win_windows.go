package main

import (
	"os"
	"path/filepath"
	"syscall"
)

func titulo(t string) {
	k := syscall.NewLazyDLL("kernel32.dll")
	if p, err := syscall.UTF16PtrFromString(t); err == nil {
		k.NewProc("SetConsoleTitleW").Call(uintptr(unsafePtr(p)))
	}
	k.NewProc("SetConsoleOutputCP").Call(65001)
	k.NewProc("SetConsoleCP").Call(65001)
}

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
