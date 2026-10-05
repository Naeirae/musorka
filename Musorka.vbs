Option Explicit
Dim fso, shell, base, pyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = base & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
  MsgBox "Сначала один раз запустите install_and_run.bat, чтобы установить зависимости. После этого Мусорка будет запускаться двойным щелчком по Musorka.vbs без окна командной строки.", 48, "Мусорка"
  WScript.Quit 1
End If
cmd = Chr(34) & pyw & Chr(34) & " " & Chr(34) & base & "\main.py" & Chr(34)
shell.Run cmd, 0, False
