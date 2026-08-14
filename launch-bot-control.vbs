Option Explicit

Dim shell, fso, projectDir, pythonw, app
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
app = fso.BuildPath(projectDir, "bot_control.pyw")

If Not fso.FileExists(pythonw) Then
    MsgBox "Не найден Python: " & pythonw, vbCritical, "Ляля Бот"
    WScript.Quit 1
End If

shell.Run """" & pythonw & """ """ & app & """", 0, False
