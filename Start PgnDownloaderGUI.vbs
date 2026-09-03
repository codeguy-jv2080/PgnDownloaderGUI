Option Explicit

Dim fileSystem, shell, projectDir, distDir, packagedExe, pythonwExe, entryPoint, exitCode
Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
projectDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = projectDir

distDir = fileSystem.GetAbsolutePathName(fileSystem.BuildPath(projectDir, "dist"))
packagedExe = fileSystem.BuildPath(distDir, "PgnDownloaderGUI\PgnDownloaderGUI.exe")
pythonwExe = fileSystem.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
entryPoint = fileSystem.BuildPath(projectDir, "main.py")

If fileSystem.FileExists(packagedExe) Then
    shell.Run Quoted(packagedExe), 0, False
ElseIf fileSystem.FileExists(pythonwExe) And fileSystem.FileExists(entryPoint) Then
    exitCode = shell.Run(Quoted(pythonwExe) & " -c " & Quoted("import fastapi, uvicorn, webview"), 0, True)
    If exitCode = 0 Then
        shell.Run Quoted(pythonwExe) & " " & Quoted(entryPoint), 0, False
    Else
        MsgBox "The app's Python dependencies are not ready." & vbCrLf & vbCrLf & _
               "Double-click setup.bat in this folder, then launch PGN Downloader again.", _
               vbExclamation, "PGN Downloader setup"
    End If
Else
    MsgBox "PGN Downloader needs its one-time setup." & vbCrLf & vbCrLf & _
           "Double-click setup.bat in this folder, then launch PGN Downloader again." & vbCrLf & vbCrLf & _
           "The application can also be launched directly from dist\PgnDownloaderGUI\PgnDownloaderGUI.exe.", _
           vbInformation, "PGN Downloader setup"
End If

Function Quoted(value)
    Quoted = Chr(34) & value & Chr(34)
End Function
