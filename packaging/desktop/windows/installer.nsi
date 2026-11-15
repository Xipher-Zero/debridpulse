; DebridPulse for Windows x64 -- the per-user installer (NSIS 3.11, the pinned
; Debian package; built by packaging/desktop/windows/build-installer.sh).
;
; * Per-user: no elevation for DebridPulse itself; the program goes to
;   %LOCALAPPDATA%\Programs\DebridPulse, its data stays in
;   %LOCALAPPDATA%\DebridPulse (the supervisor's data root), which neither an
;   upgrade nor the uninstaller touches. No configuration file is written, so
;   the first run is always the setup wizard.
; * Silent: /S installs or uninstalls without a page.
; * Prerequisites, before any DebridPulse file is written:
;     - Microsoft Visual C++ x64 Redistributable >= VCREDIST_MIN, read where
;       Microsoft documents it (HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\
;       Runtimes\X64, 64-bit view: Installed=1, Major/Minor/Bld); missing or
;       older, Microsoft's own vc_redist.x64.exe runs elevated (UAC) for THAT
;       prerequisite only, /install /quiet /norestart;
;     - Microsoft Edge WebView2 Runtime, read where Microsoft documents it
;       (EdgeUpdate Clients {F3017226-...} "pv", per machine or per user);
;       missing, Microsoft's Evergreen bootstrapper installs it per user.
;   A refused elevation, a failed prerequisite or one that needs a restart
;   ends the installation with an explanation and nothing installed.
; * Uninstall removes the program files, the shortcuts, the uninstall entry
;   and the Media Downloads helpers' AppContainer profile -- never the data
;   root or any download.

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

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "DebridPulse for Windows needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}
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
  Call VcRedistSatisfied
  Pop $0
  ${If} $0 != 1
    DetailPrint "Microsoft Visual C++ x64 Redistributable ${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer is required (found $vcMajor.$vcMinor.$vcBld)."
    MessageBox MB_ICONINFORMATION|MB_OKCANCEL "DebridPulse needs the Microsoft Visual C++ x64 Redistributable (${VCREDIST_MIN_MAJOR}.${VCREDIST_MIN_MINOR} or newer).$\n$\nWindows will ask for permission to install this Microsoft component. DebridPulse itself installs without administrator rights." /SD IDOK IDOK vcInstall
    Abort "Installation cancelled: the Microsoft Visual C++ Redistributable is required."
    vcInstall:
    File /oname=$PLUGINSDIR\vc_redist.x64.exe "${STAGE}\prerequisites\vc_redist.x64.exe"
    ; Elevation for this Microsoft component only (UAC "runas").
    ExecShellWait "runas" "$PLUGINSDIR\vc_redist.x64.exe" "/install /quiet /norestart" SW_HIDE
    Call VcRedistSatisfied
    Pop $0
    ${If} $0 != 1
      MessageBox MB_ICONSTOP|MB_OK "The Microsoft Visual C++ Redistributable was not installed (permission refused, or its installer failed or needs a restart). Nothing of DebridPulse was installed; run this installer again afterwards." /SD IDOK
      Abort "The Microsoft Visual C++ Redistributable is missing."
    ${EndIf}
  ${EndIf}
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
  RMDir /r "$INSTDIR\app"
  SetOutPath "$INSTDIR\app"
  File /r "${STAGE}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateShortcut "$SMPROGRAMS\DebridPulse.lnk" "$INSTDIR\app\runtime\python\pythonw.exe" '-I "$INSTDIR\app\desktop\supervisor.py"' "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "DebridPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "DebridPulse"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\app\desktop\shell\resources\icons\app.ico"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ; The Media Downloads helpers' AppContainer profile (executors.media.netless).
  System::Call 'userenv::DeleteAppContainerProfile(w "DebridPulse.MediaHelpers") i .r0'
  Delete "$SMPROGRAMS\DebridPulse.lnk"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; %LOCALAPPDATA%\DebridPulse (settings, database, logs) and every download stay.
SectionEnd
