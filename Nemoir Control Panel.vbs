' Nemoir Control Panel - double-click launcher (no console window).
' Resolves every path relative to this file's own location and never embeds
' a user-specific absolute path. It runs the panel with pythonw.exe so no
' terminal window remains visible while the panel is open.
Option Explicit

Dim fso, shell, root, python

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
python = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")

If Not fso.FileExists(python) Then
    MsgBox "Local virtual environment not found:" & vbCrLf & python & vbCrLf & vbCrLf & _
           "Follow the README.md Windows PowerShell setup first.", _
           vbExclamation, "Nemoir Control Panel"
    WScript.Quit 1
End If

shell.CurrentDirectory = root
shell.Run """" & python & """ -m nemoir gui", 0, False
