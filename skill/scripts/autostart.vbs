' Free-Router logon launcher (hidden, idempotent: exits if /health already OK).
' Path-independent: repo root is derived from this script's own location.
' Serving is done by scripts/run_server.py which must redirect stdout/stderr
' BEFORE importing uvicorn (pythonw has no console -> uvicorn logging crashes).
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

Dim fso, sh, here, root, py, cmd, http, alive
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)   ' <repo>/scripts
root = fso.GetParentFolderName(here)

alive = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:8787/health", False
http.Send
If http.Status = 200 Then alive = True
On Error GoTo 0
If alive Then WScript.Quit 0

py = FindPythonW(fso, sh, root)
If py = "" Then WScript.Quit 1
cmd = Chr(34) & py & Chr(34) & " " & Chr(34) & here & "\run_server.py" & Chr(34)
sh.Run cmd, 0, False
