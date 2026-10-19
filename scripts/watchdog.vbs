' FreeRouter Watchdog - restart the service if it goes down.
' ASCII ONLY: cscript/wscript reads .vbs as ANSI, so non-ASCII comments break parsing.
'
' Hardened after a real failure: at logon, setting CurrentDirectory to the external
' drive (L:) before it was ready made shell.Run fail with 800A0046 and popped a
' Windows Script Host dialog. This version never shows a dialog, runs as a single
' instance, waits for the drive, and never relies on CurrentDirectory.
On Error Resume Next

Dim shell, fso, baseDir, svcArgs, pyExe, tools, f, i, n
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
svcArgs = "run.py"

' ---------- single instance guard ----------
Dim wmi, procs, p, cl
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("Select * from Win32_Process Where Name='wscript.exe'")
n = 0
For Each p In procs
    cl = ""
    If Not IsNull(p.CommandLine) Then cl = LCase(p.CommandLine)
    If InStr(cl, "watchdog.vbs") > 0 Then n = n + 1
Next
If n > 1 Then WScript.Quit          ' another watchdog is already alive

' ---------- locate python ----------
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
    Dim wq, ps, pp, cc
    IsServiceUp = False
    Set wq = GetObject("winmgmts:\\.\root\cimv2")
    Set ps = wq.ExecQuery("Select * from Win32_Process Where Name='python.exe'")
    For Each pp In ps
        cc = ""
        If Not IsNull(pp.CommandLine) Then cc = LCase(pp.CommandLine)
        If InStr(cc, "run.py") > 0 Or InStr(cc, "app.main:app") > 0 Then
            IsServiceUp = True
            Exit Function
        End If
    Next
End Function

Sub EnsureService()
    If IsServiceUp() Then Exit Sub
    If Not fso.FileExists(baseDir & "\run.py") Then Exit Sub   ' drive not ready yet
    shell.Run "cmd /c cd /d """ & baseDir & """ && """ & pyExe & """ " & svcArgs, 0, False
End Sub

' give autostart a head start at login
WScript.Sleep 20000

Do While True
    EnsureService
    WScript.Sleep 30000         ' check every 30s (fast self-heal; WMI query is cheap)
Loop
