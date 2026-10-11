' QuantHub dashboard supervisor (hidden, auto-restart, self-locating).
' Serves the robot execution dashboard at http://127.0.0.1:8770/
' NOTE: keep this file ASCII-only; non-ASCII literals get mangled by WScript.
Option Explicit

Dim sh, fso, wd, py, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

wd  = fso.GetParentFolderName(WScript.ScriptFullName)
py  = wd & "\.venv\Scripts\python.exe"
cmd = """" & py & """ dashboard_server.py --port 8770"

If Not fso.FileExists(py) Then
  WScript.Quit 1
End If

sh.CurrentDirectory = wd
Do
  sh.Run cmd, 0, True     ' 0 = hidden window, True = wait for exit
  WScript.Sleep 5000      ' restart after 5s if the server exits/crashes
Loop
