//! Linux window shell: stay on the taskbar, register a desktop entry, and
//! keep the window visually rounded. Do not stamp a square opaque region —
//! that turns the rounded app into a hard rectangle.

use gtk::gdk::WindowTypeHint;
use gtk::prelude::*;
use std::io::Write;
use tauri::{Manager, WebviewWindow};

const APP_ICON_PNG: &[u8] = include_bytes!("../icons/128x128.png");

pub fn install(app: &tauri::App) {
    glib::set_prgname(Some("codexhub"));
    glib::set_application_name("CodexHub");
    install_desktop_entry();
    install_hicolor_icon();
    let Some(window) = app.get_webview_window("main") else {
        log::warn!("Linux window shell skipped: main window missing");
        return;
    };
    if let Err(error) = configure_shell(&window) {
        log::warn!("Linux taskbar hint failed: {error}");
    }
}

pub fn reveal_on_taskbar(window: &WebviewWindow) {
    let _ = window.set_skip_taskbar(false);
    if let Ok(gtk_window) = window.gtk_window() {
        gtk_window.set_skip_taskbar_hint(false);
        gtk_window.set_skip_pager_hint(false);
        gtk_window.set_type_hint(WindowTypeHint::Normal);
    }
}

fn configure_shell(window: &WebviewWindow) -> Result<(), String> {
    let gtk_window = window
        .gtk_window()
        .map_err(|error| format!("gtk window: {error}"))?;
    gtk_window.set_skip_taskbar_hint(false);
    gtk_window.set_skip_pager_hint(false);
    gtk_window.set_type_hint(WindowTypeHint::Normal);
    gtk_window.set_accept_focus(true);
    let _ = window.set_skip_taskbar(false);
    Ok(())
}

fn install_hicolor_icon() {
    let Some(icon_dir) = dirs::data_local_dir().map(|dir| dir.join("icons/hicolor/128x128/apps"))
    else {
        return;
    };
    if let Err(error) = std::fs::create_dir_all(&icon_dir) {
        log::warn!("failed to create hicolor icon dir: {error}");
        return;
    }
    for name in ["codexhub.png", "com.codexhub.app.png"] {
        let path = icon_dir.join(name);
        if path.is_file() && path.metadata().map(|meta| meta.len()).unwrap_or(0) == APP_ICON_PNG.len() as u64 {
            continue;
        }
        if let Err(error) = std::fs::write(&path, APP_ICON_PNG) {
            log::warn!("failed to write {name}: {error}");
        }
    }
}

fn install_desktop_entry() {
    let Some(apps_dir) = dirs::data_local_dir().map(|dir| dir.join("applications")) else {
        return;
    };
    if let Err(error) = std::fs::create_dir_all(&apps_dir) {
        log::warn!("failed to create applications dir for Linux desktop entry: {error}");
        return;
    }
    let exec = desktop_exec_path();
    let body = format!(
        "[Desktop Entry]\n\
         Type=Application\n\
         Name=CodexHub\n\
         Comment=CodexHub desktop backend and CLI\n\
         Exec={exec}\n\
         Icon=codexhub\n\
         Terminal=false\n\
         Categories=Development;\n\
         StartupNotify=true\n\
         StartupWMClass=com.codexhub.app\n\
         X-GNOME-UsesNotifications=true\n"
    );
    for name in ["com.codexhub.app.desktop", "codexhub.desktop"] {
        let path = apps_dir.join(name);
        if let Err(error) = write_if_changed(&path, &body) {
            log::warn!("failed to write {name}: {error}");
        }
    }
}

fn desktop_exec_path() -> String {
    let raw = std::env::var("APPIMAGE")
        .ok()
        .or_else(|| std::env::current_exe().ok().map(|path| path.display().to_string()))
        .unwrap_or_else(|| "codexhub".to_string());
    if raw.chars().any(|ch| ch.is_whitespace()) {
        format!("\"{}\"", raw.replace('"', "\\\""))
    } else {
        raw
    }
}

fn write_if_changed(path: &std::path::Path, body: &str) -> std::io::Result<()> {
    if std::fs::read_to_string(path).ok().as_deref() == Some(body) {
        return Ok(());
    }
    let mut file = std::fs::File::create(path)?;
    file.write_all(body.as_bytes())?;
    Ok(())
}
