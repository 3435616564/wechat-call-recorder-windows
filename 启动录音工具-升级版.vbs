' 本地通话录音工具·升级版 启动器（无黑窗口）
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
pyw  = base & "\runtime\Python312\pythonw.exe"
app  = base & "\app.py"
sh.CurrentDirectory = base
sh.Run """" & pyw & """ """ & app & """", 0, False
