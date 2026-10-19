' FreeRouter Autostart - start the service once at login.
' ASCII ONLY: cscript/wscript reads .vbs as ANSI, so non-ASCII comments break parsing.
'
' Hardened after a real failure: at logon the external drive (L:) is often not ready
' yet, so "shell.CurrentDirectory = L:\..." + shell.Run failed with 800A0046
' (permission denied) and popped a Windows Script Host dialog. This version:
'   1) never shows a dialog (On Error Resume Next),
'   2) waits for run.py to become visible before launching,
'   3) avoids CurrentDirectory entirely (cmd /d handles the workdir),
'   4) skips launching if the service is already up.
On Error Resume Next

Dim shell, fso, baseDir, pyExe, tools, f, i
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

' --- locate python (prefer the Hermes-managed interpreter) ---
pyExe = "python"
tools = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\hermes\tools"
If fso.FolderExists(tools) Then
    For Each f In fso.GetFolder(tools).SubFolders
        If Left(f.Name, 7) = "python-" Then
            If fso.FileExists(f.Path & "\python.exe") Then pyExe = f.Path & "\python.exe"
        End If
    Next
End If

Function IsServiceUp()
    Dim wmi, procs, p, cl
    IsServiceUp = False
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    Set procs = wmi.ExecQuery("Select * from Win32_Process Where Name='python.exe'")
    For Each p In procs
        cl = ""
        If Not IsNull(p.CommandLine) Then cl = LCase(p.CommandLine)
        If InStr(cl, "run.py") > 0 Or InStr(cl, "app.main:app") > 0 Then
            IsServiceUp = True
            Exit Function
        End If
    Next
End Function

' --- wait for the drive/project to be ready (up to 90s) ---
For i = 1 To 90
    If fso.FileExists(baseDir & "\run.py") Then Exit For
    WScript.Sleep 1000
Next

If Not IsServiceUp() Then
    shell.Run "cmd /c cd /d """ & baseDir & """ && """ & pyExe & """ run.py", 0, False
End If
