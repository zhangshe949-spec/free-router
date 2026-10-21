' Free-Router watchdog launcher (hidden window). Path-independent: the script
' derives the repo root from its own location, so it can live anywhere.
' Register for logon autostart, e.g.:
'   reg add HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v FreeRouterWatchdog ^
'       /t REG_SZ /d "wscript.exe //B \"<abs-path>\watchdog.vbs\"" /f
' (Always register the real absolute path; the registry does not expand %VARS%.)
Option Explicit
' Locate pythonw.exe: 1) repo-local venv  2) PATH  3) none
Function FindPythonW(fso, sh, root)
    Dim cands, i, out, exec
    cands = Array(root & "\.venv\Scripts\pythonw.exe", _
                  root & "\venv\Scripts\pythonw.exe", _
                  root & "\..\.venv\Scripts\pythonw.exe")
    For i = 0 To UBound(cands)
        If fso.FileExists(cands(i)) Then FindPythonW = cands(i) : Exit Function
    Next
    On Error Resume Next
    Set exec = sh.Exec("cmd /c where pythonw.exe")
    Do While exec.Status = 0 : WScript.Sleep 50 : Loop
    out = Trim(exec.StdOut.ReadAll)
    On Error GoTo 0
    If out <> "" Then FindPythonW = Split(out, vbCrLf)(0) : Exit Function
    FindPythonW = ""
End Function

Dim fso, sh, here, root, py, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)   ' <repo>/scripts
root = fso.GetParentFolderName(here)                      ' <repo>
py = FindPythonW(fso, sh, root)
If py = "" Then WScript.Quit 1
cmd = Chr(34) & py & Chr(34) & " " & Chr(34) & here & "\watchdog.py" & Chr(34)
sh.Run cmd, 0, False
