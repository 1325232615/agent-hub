' QuantHub daemon supervisor (hidden, auto-restart, self-locating).
' Registered by a shortcut in the user's Startup folder, so the daemon
' starts at logon and is relaunched if the python process ever exits.
' To change the target agent, edit the AGENT line below.
Option Explicit

Dim sh, fso, wd, py, agent, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

AGENT = "data_env"
wd  = fso.GetParentFolderName(WScript.ScriptFullName)
py  = wd & "\.venv\Scripts\python.exe"
cmd = """" & py & """ hub_daemon.py --agent " & AGENT

If Not fso.FileExists(py) Then
  WScript.Quit 1
End If

sh.CurrentDirectory = wd
Do
  sh.Run cmd, 0, True     ' 0 = hidden window, True = wait for exit
  WScript.Sleep 5000      ' restart after 5s if the daemon exits/crashes
Loop
