; Audio Converter Windows installer (NSIS).
; Built by scripts\build_windows.bat, which passes /DVERSION=x.y.z.
; Installs to Program Files, adds a Start Menu shortcut, and ships an
; uninstaller. Everything the app needs is inside the bundle.

!ifndef VERSION
  !define VERSION "1.0.0"
!endif

Name "Audio Converter ${VERSION}"
OutFile "..\dist\AudioConverter-Setup-${VERSION}.exe"
InstallDir "$PROGRAMFILES64\AudioConverter"
RequestExecutionLevel admin

Page directory
Page instfiles

UninstPage uninstConfirm
UninstPage instfiles

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "..\dist\AudioConverter\*.*"
  CreateDirectory "$SMPROGRAMS\Audio Converter"
  CreateShortcut "$SMPROGRAMS\Audio Converter\Audio Converter.lnk" "$INSTDIR\AudioConverter.exe"
  CreateShortcut "$SMPROGRAMS\Audio Converter\Uninstall.lnk" "$INSTDIR\uninstall.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AudioConverter" \
    "DisplayName" "Audio Converter ${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AudioConverter" \
    "UninstallString" "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\Audio Converter\Audio Converter.lnk"
  Delete "$SMPROGRAMS\Audio Converter\Uninstall.lnk"
  RMDir "$SMPROGRAMS\Audio Converter"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AudioConverter"
SectionEnd
