; DebridPulse for Windows x64 -- the per-user installer (NSIS 3.11, the pinned
; Debian package; built by packaging/desktop/windows/build-installer.sh).
;
; * Per-user: no elevation for DebridPulse itself; the program goes to
;   %LOCALAPPDATA%\Programs\DebridPulse, its data stays in
;   %LOCALAPPDATA%\DebridPulse (the supervisor's data root), whose content
;   neither an upgrade nor the uninstaller changes (they only hold its
;   desktop.lock, below). No configuration file is written, so the first run
;   is always the setup wizard.
; * Silent: /S installs or uninstalls without a page.
; * Prerequisites, before any DebridPulse file is written:
;     - Microsoft Visual C++ x64 Redistributable >= VCREDIST_MIN, read where
;       Microsoft documents it (HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\
;       Runtimes\X64, 64-bit view: Installed=1, Major/Minor/Bld), checked
;       first, before anything is touched. It is NOT bundled (Microsoft limits
;       redistributing it to Visual Studio licensees): missing or older, an
;       interactive install offers Microsoft's official download and checks
;       again until it is there or the user cancels; a silent install (/S)
;       writes the requirement to the calling console and exits with code 3,
;       leaving any existing installation exactly as it was;
;     - Microsoft Edge WebView2 Runtime, read where Microsoft documents it
;       (EdgeUpdate Clients {F3017226-...} "pv", per machine or per user);
;       missing, Microsoft's Evergreen bootstrapper installs it per user.
;   A failed WebView2 installation ends the installation with an explanation
;   and nothing installed.
; * Uninstall removes the shortcut, the program files, the uninstall entry
;   and the Media Downloads helpers' AppContainer profile -- never the data
;   root or any download. Program files that cannot be removed or replaced
;   (in use) are reported, and the uninstall entry stays, so the user can
;   run the uninstaller or this installer again.

Unicode true
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!ifndef VERSION
  !error "VERSION is required (-DVERSION=...)"
!endif
!ifndef STAGE
  !error "STAGE (the assembled application directory) is required"
!endif
!ifndef OUTFILE
  !define OUTFILE "DebridPulse-${VERSION}-windows-x64.exe"
!endif
!ifndef VCREDIST_MIN_MAJOR
  !define VCREDIST_MIN_MAJOR 14
!endif
!ifndef VCREDIST_MIN_MINOR
  !define VCREDIST_MIN_MINOR 51   ; the WS3 inventory's minimum (ujson, bencode2, cryptography: toolset 14.51)
!endif
!define VCREDIST_MIN_BLD 0
; Microsoft's official download of the latest supported x64 Redistributable
; (learn.microsoft.com/cpp/windows/latest-supported-vc-redist).
!define VCREDIST_DOWNLOAD "https://aka.ms/vc14/vc_redist.x64.exe"
!define WEBVIEW2_CLIENT "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DebridPulse"

Name "DebridPulse"
OutFile "${OUTFILE}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\DebridPulse"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
SetCompressor /SOLID lzma
ShowInstDetails show
Icon "${STAGE}\desktop\shell\resources\icons\app.ico"
UninstallIcon "${STAGE}\desktop\shell\resources\icons\app.ico"
LicenseData "${STAGE}\LICENSE"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "DebridPulse"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "DebridPulse installer"
VIAddVersionKey "LegalCopyright" "GPL-2.0-or-later"

Page license
Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Var vcMajor
Var vcMinor
Var vcBld

; One owner of the data root at a time: the supervisor's lock -- an exclusive
; lock on the first byte of %LOCALAPPDATA%\DebridPulse\desktop.lock, held for as
; long as DebridPulse runs (desktop/supervisor.py single_instance). The
; installer and the uninstaller take that same byte before they change
; anything and hold it until they end, so DebridPulse cannot be running while
; its program files, shortcut or uninstall entry change (a running program
; keeps files open: a half-installed or half-removed DebridPulse), and a
; DebridPulse started meanwhile finds the lock taken and ends before it starts.
; A first installation creates the data root only to hold the lock, and removes
; what it created when it lets go (DebridPulse creates it on its first run).
Var dpLock
Var dpRootCreated

!macro DP_DATA_ROOT_LOCK UN
Function ${UN}TakeDataRootLock
  StrCpy $dpRootCreated 0
  ${IfNot} ${FileExists} "$LOCALAPPDATA\DebridPulse\*.*"
    CreateDirectory "$LOCALAPPDATA\DebridPulse"
    StrCpy $dpRootCreated 1
  ${EndIf}
  ; GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE (the
  ; supervisor's own open), OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL
  System::Call 'kernel32::CreateFileW(w "$LOCALAPPDATA\DebridPulse\desktop.lock", i 0xC0000000, i 3, p 0, i 4, i 0x80, p 0) p .r1'
  ${If} $1 == -1
    ${If} $dpRootCreated == 1
      RMDir "$LOCALAPPDATA\DebridPulse"
    ${EndIf}
    MessageBox MB_ICONSTOP|MB_OK "$LOCALAPPDATA\DebridPulse\desktop.lock cannot be opened, so it is not known whether DebridPulse is running. Nothing was changed." /SD IDOK
    Abort
  ${EndIf}
  System::Call 'kernel32::LockFile(p r1, i 0, i 0, i 1, i 0) i .r2'
  ${If} $2 == 0
    System::Call 'kernel32::CloseHandle(p r1)'
    MessageBox MB_ICONSTOP|MB_OK "DebridPulse is running. Quit it from its tray menu (Quit), then run this again." /SD IDOK
    Abort
  ${EndIf}
  StrCpy $dpLock $1
FunctionEnd

Function ${UN}ReleaseDataRootLock
  ${If} $dpLock != ""
    System::Call 'kernel32::UnlockFile(p $dpLock, i 0, i 0, i 1, i 0)'
    System::Call 'kernel32::CloseHandle(p $dpLock)'
    StrCpy $dpLock ""
    ${If} $dpRootCreated == 1
      Delete "$LOCALAPPDATA\DebridPulse\desktop.lock"
      RMDir "$LOCALAPPDATA\DebridPulse"
    ${EndIf}
  ${EndIf}
FunctionEnd
!macroend
!insertmacro DP_DATA_ROOT_LOCK ""
!insertmacro DP_DATA_ROOT_LOCK "un."

; Taken first, before any page or section; let go once the run has ended,
; whichever way it ended (the system also lets go when the process ends).
; Removing the program files. The lock keeps DebridPulse from starting, but a
; launch from the Start-menu shortcut meanwhile still loads the bundled
; interpreter from these files before its supervisor finds the lock taken and
; ends -- holding them open for that moment. Removal is retried while that
; lasts (10 s); files still present after that are in use by something else,
; and the caller reports it.
!macro DP_REMOVE_PROGRAM_FILES UN
Function ${UN}RemoveProgramFiles
  StrCpy $R9 0
  dp_remove:
  RMDir /r "$INSTDIR\app"
  ${IfNot} ${FileExists} "$INSTDIR\app\*.*"
    Return
  ${EndIf}
  IntOp $R9 $R9 + 1
  ${If} $R9 < 20
    DetailPrint "Some program files are in use; trying again..."
    Sleep 500
    Goto dp_remove
  ${EndIf}
FunctionEnd
!macroend
!insertmacro DP_REMOVE_PROGRAM_FILES ""
!insertmacro DP_REMOVE_PROGRAM_FILES "un."

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "DebridPulse for Windows needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}
  Call RequireVcRedist      ; before the data-root lock and any file: nothing is changed without it
  Call TakeDataRootLock
FunctionEnd
Function .onInstSuccess
  Call ReleaseDataRootLock
FunctionEnd
Function .onInstFailed
  Call ReleaseDataRootLock
FunctionEnd
Function .onGUIEnd
  Call ReleaseDataRootLock
FunctionEnd

Function un.onInit
  Call un.TakeDataRootLock
FunctionEnd
Function un.onUninstSuccess
  Call un.ReleaseDataRootLock
FunctionEnd
Function un.onUninstFailed
  Call un.ReleaseDataRootLock
FunctionEnd
Function un.onGUIEnd
  Call un.ReleaseDataRootLock
FunctionEnd

Function VcRedistSatisfied
  ; Microsoft's documented detection: the 64-bit registry view.
  SetRegView 64
  ReadRegDWORD $0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Installed"
  ReadRegDWORD $vcMajor HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Major"
  ReadRegDWORD $vcMinor HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Minor"
  ReadRegDWORD $vcBld HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" "Bld"
  SetRegView lastused
  StrCpy $1 0
  ${If} $0 == 1
    ${If} $vcMajor > ${VCREDIST_MIN_MAJOR}
      StrCpy $1 1
    ${ElseIf} $vcMajor == ${VCREDIST_MIN_MAJOR}
      ${If} $vcMinor > ${VCREDIST_MIN_MINOR}
        StrCpy $1 1
      ${ElseIf} $vcMinor == ${VCREDIST_MIN_MINOR}
      ${AndIf} $vcBld >= ${VCREDIST_MIN_BLD}
        StrCpy $1 1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  Push $1
FunctionEnd

Function RequireVcRedist
  vcCheck:
  Call VcRedistSatisfied
  Pop $0
  ${If} $0 == 1
    Return
  ${EndIf}
  ${If} $vcMajor == ""
    StrCpy $2 "none"
  ${Else}
    StrCpy $2 "$vcMajor.$vcMinor.$vcBld"
  ${EndIf}
  ${If} ${Silent}
    ; No page: say so where the caller can read it -- the standard output it
    ; gave the installer (a redirect or a pipe), else the console that started
    ; it -- and fail with a distinct code before anything is changed.
    StrCpy $4 "DebridPulse setup: the Microsoft Visual C++ x64 Redistributable ${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer is required (found: $2). Install it from Microsoft (${VCREDIST_DOWNLOAD}) and run setup again. Nothing was changed.$\r$\n"
    StrLen $5 $4
    System::Call 'kernel32::GetStdHandle(i -11) p .r6'
    System::Call 'kernel32::GetFileType(p r6) i .r7'
    ${If} $7 != 0     ; FILE_TYPE_UNKNOWN: no usable standard output
      System::Call 'kernel32::WriteFile(p r6, m r4, i r5, *i .r8, p 0)'
    ${Else}
      System::Call 'kernel32::AttachConsole(i -1) i .r3'
      ${If} $3 != 0
        FileOpen $3 "CONOUT$$" w
        FileWrite $3 $4
        FileClose $3
      ${EndIf}
    ${EndIf}
    SetErrorLevel 3
    Quit
  ${EndIf}
  MessageBox MB_ICONEXCLAMATION|MB_YESNOCANCEL "DebridPulse needs the Microsoft Visual C++ x64 Redistributable ${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer (found on this computer: $2).$\n$\nInstall it from Microsoft, then continue here:$\n$\nYes: open Microsoft's download of the Redistributable$\nNo: check again (after installing it)$\nCancel: quit setup (nothing has been changed)" IDYES vcDownload IDNO vcCheck
  SetErrorLevel 3
  Quit
  vcDownload:
  ExecShell "open" "${VCREDIST_DOWNLOAD}"
  Goto vcCheck
FunctionEnd

Function WebView2Satisfied
  ; Microsoft's documented detection: a non-empty, non-"0.0.0.0" pv, per
  ; machine (WOW6432Node) or per user.
  StrCpy $1 0
  ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_CLIENT}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $1 1
  ${EndIf}
  ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_CLIENT}" "pv"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $1 1
  ${EndIf}
  Push $1
FunctionEnd

Section "Prerequisites" SecPrerequisites
  SectionIn RO
  InitPluginsDir
  ; The Visual C++ Redistributable was required in .onInit (RequireVcRedist).
  Call WebView2Satisfied
  Pop $0
  ${If} $0 != 1
    DetailPrint "Installing the Microsoft Edge WebView2 Runtime for this account..."
    File /oname=$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe "${STAGE}\prerequisites\MicrosoftEdgeWebview2Setup.exe"
    ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $2
    Call WebView2Satisfied
    Pop $0
    ${If} $0 != 1
      MessageBox MB_ICONSTOP|MB_OK "The Microsoft Edge WebView2 Runtime could not be installed (exit $2). Nothing of DebridPulse was installed." /SD IDOK
      Abort "The Microsoft Edge WebView2 Runtime is missing."
    ${EndIf}
  ${EndIf}
SectionEnd

Section "DebridPulse" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; An upgrade replaces the program files in place; the data root is elsewhere.
  Call RemoveProgramFiles
  ${If} ${FileExists} "$INSTDIR\app\*.*"
    MessageBox MB_ICONSTOP|MB_OK "The installed DebridPulse program files could not be replaced: some of them are in use ($INSTDIR\app). Close what is using them, then run this installer again. The existing uninstall entry is kept." /SD IDOK
    Abort "Program files in use: nothing was replaced."
  ${EndIf}
  ; The uninstall entry first: whatever happens next, it can remove or repair.
  WriteUninstaller "$INSTDIR\uninstall.exe"
  Call WriteUninstallEntry
  SetOutPath "$INSTDIR\app"
  ClearErrors
  File /r "${STAGE}\*.*"
  ${If} ${Errors}
    MessageBox MB_ICONSTOP|MB_OK "Some DebridPulse program files could not be written ($INSTDIR\app). Close what is using them, then run this installer again; the uninstall entry is kept." /SD IDOK
    Abort "Program files could not be written."
  ${EndIf}
  CreateShortcut "$SMPROGRAMS\DebridPulse.lnk" "$INSTDIR\app\runtime\python\pythonw.exe" '-I "$INSTDIR\app\desktop\supervisor.py"' "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
SectionEnd

Function WriteUninstallEntry
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "DebridPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "DebridPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
FunctionEnd

Section "Uninstall"
  ; The shortcut first: no new launch from it while the files go.
  Delete "$SMPROGRAMS\DebridPulse.lnk"
  Call un.RemoveProgramFiles
  ${If} ${FileExists} "$INSTDIR\app\*.*"
    MessageBox MB_ICONSTOP|MB_OK "Some DebridPulse program files could not be removed: they are in use ($INSTDIR\app). Close what is using them, then uninstall DebridPulse again (Settings > Apps, or $INSTDIR\uninstall.exe). Your settings, database and downloads were not touched." /SD IDOK
    Abort "Program files in use: the uninstall entry is kept."
  ${EndIf}
  ; The Media Downloads helpers' AppContainer profile (executors.media.netless).
  System::Call 'userenv::DeleteAppContainerProfile(w "DebridPulse.MediaHelpers") i .r0'
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; %LOCALAPPDATA%\DebridPulse (settings, database, logs) and every download stay.
SectionEnd
