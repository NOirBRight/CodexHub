//! Linux window shell: keep pointer events, stay on the taskbar, and register
//! a desktop entry so GNOME can show CodexHub in the dock and the top-bar tray.

use gtk::gdk::WindowTypeHint;
use gtk::prelude::*;
use std::io::Write;
use tauri::{Manager, WebviewWindow};

pub fn install(app: &tauri::App) {
    install_desktop_entry();
    let Some(window) = app.get_webview_window("main") else {
        log::warn!("Linux window shell skipped: main window missing");
        return;
    };
    if let Err(error) = configure_shell(&window) {
        log::warn!("Linux taskbar hint failed: {error}");
    }
    if let Err(error) = hook_input_region(&window) {
        log::warn!("Linux input-region guard failed: {error}");
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

fn hook_input_region(window: &WebviewWindow) -> Result<(), String> {
    let gtk_window = window
        .gtk_window()
        .map_err(|error| format!("gtk window: {error}"))?;
    let widget: gtk::Widget = gtk_window.upcast();
    apply_full_input_region(&widget);
    widget.connect_realize(|widget| apply_full_input_region(widget));
    widget.connect_size_allocate(|widget, _| apply_full_input_region(widget));
    widget.connect_draw(|widget, _| {
        let widget = widget.clone();
        glib::idle_add_local_once(move || {
            apply_full_input_region(&widget);
        });
        glib::Propagation::Proceed
    });
    Ok(())
}

fn apply_full_input_region(widget: &gtk::Widget) {
    if let Some(gdk_window) = widget.window() {
        let width = gdk_window.width().max(1);
        let height = gdk_window.height().max(1);
        let region = cairo::Region::create_rectangle(&cairo::RectangleInt::new(0, 0, width, height));
        widget.input_shape_combine_region(Some(&region));
        gdk_window.input_shape_combine_region(&region, 0, 0);
        gdk_window.set_opaque_region(Some(&region));
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            apply_full_input_region(&child);
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
