Option Explicit

Dim shell, fso, rootDir, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = rootDir

command = shell.ExpandEnvironmentStrings("%ComSpec%") _
    & " /D /C " & Chr(34) & Chr(34) & rootDir & "\RUNME.bat" & Chr(34) & Chr(34)

' Run the existing bootstrap/update script without showing a console window.
shell.Run command, 0, False
