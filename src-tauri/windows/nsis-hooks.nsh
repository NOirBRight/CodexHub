!macro NSIS_HOOK_PREUNINSTALL
  ; Updates replace the executable in place and must retain its registration.
  ${If} $UpdateMode <> 1
    ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" cleanup-autostart-on-uninstall' $0
    ${If} $0 = 0
      DetailPrint "CodexHub autostart cleanup: owned registration absent or removed."
    ${Else}
      ; Fail closed: uninstall continues, but any uncertain task is preserved.
      DetailPrint "CodexHub autostart cleanup: registration preserved; ownership verification failed."
    ${EndIf}
  ${EndIf}
!macroend
