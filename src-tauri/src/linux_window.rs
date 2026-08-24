//! Linux window shell: stay on the taskbar, register a desktop entry, and
//! keep the window visually rounded. Do not stamp a square opaque region —
//! that turns the rounded app into a hard rectangle.

use gtk::gdk::{self, WindowTypeHint};
use gtk::prelude::*;
use std::cell::Cell;
use std::io::Write;
use std::rc::Rc;
use std::time::Duration;
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
    gtk_window.set_app_paintable(false);
    if let Some(screen) = gtk::prelude::WidgetExt::screen(&gtk_window) {
        if let Some(visual) = screen.system_visual() {
            gtk_window.set_visual(Some(&visual));
        }
    }
    let _ = window.set_skip_taskbar(false);
    apply_opaque_window_css(&gtk_window);
    fill_webview_on_resize(&gtk_window);
    Ok(())
}

/// wry/WebKitGTK on Linux can park the webview in a GtkFixed at 1×1 when
/// bounds are unset. Stretch GTK widgets *and* the native Gdk/X11 child so
/// the UI actually paints instead of a blank shell.
fn fill_webview_on_resize(gtk_window: &gtk::ApplicationWindow) {
    gtk_window.connect_size_allocate(|window, _| {
        stretch_webview(window, false);
    });
    let window = gtk_window.clone();
    let attempts = Rc::new(Cell::new(0u32));
    glib::timeout_add_local(Duration::from_millis(50), move || {
        stretch_webview(&window, true);
        let n = attempts.get() + 1;
        attempts.set(n);
        if n < 40 {
            glib::ControlFlow::Continue
        } else {
            glib::ControlFlow::Break
        }
    });
}

fn stretch_webview(gtk_window: &gtk::ApplicationWindow, allocate: bool) {
    let width = gtk_window.allocated_width().max(1);
    let height = gtk_window.allocated_height().max(1);
    for child in gtk_window.children() {
        expand_widget_tree(&child, width, height, allocate);
    }
    resize_native_children(gtk_window, width, height);
}

fn expand_widget_tree(widget: &gtk::Widget, width: i32, height: i32, allocate: bool) {
    widget.set_hexpand(true);
    widget.set_vexpand(true);
    let type_name = widget.type_().name();
    if type_name.contains("WebView") || type_name.contains("Fixed") {
        widget.set_size_request(width, height);
    }
    if let Ok(fixed) = widget.clone().downcast::<gtk::Fixed>() {
        for child in fixed.children() {
            fixed.move_(&child, 0, 0);
            child.set_size_request(width, height);
            if allocate {
                child.size_allocate(&gdk::Rectangle::new(0, 0, width, height));
            }
            if let Some(gdk_window) = child.window() {
                gdk_window.move_resize(0, 0, width, height);
            }
        }
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            expand_widget_tree(&child, width, height, allocate);
        }
    }
}

fn resize_native_children(gtk_window: &gtk::ApplicationWindow, width: i32, height: i32) {
    let Some(gdk_window) = gtk::prelude::WidgetExt::window(gtk_window) else {
        return;
    };
    for child in gdk_window.children() {
        child.move_resize(0, 0, width, height);
    }
}

fn apply_opaque_window_css(gtk_window: &gtk::ApplicationWindow) {
    let provider = gtk::CssProvider::new();
    // Do not set border-radius here: GTK then picks an RGBA visual and
    // WebKitGTK paints a black webview on X11/XWayland.
    if provider
        .load_from_data(b"window { background-color: #f8f8f7; }")
        .is_err()
    {
        return;
    }
    let Some(screen) = gtk::prelude::WidgetExt::screen(gtk_window) else {
        return;
    };
    gtk::StyleContext::add_provider_for_screen(
        &screen,
        &provider,
        gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );
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
        if path.is_file()
            && path.metadata().map(|meta| meta.len()).unwrap_or(0) == APP_ICON_PNG.len() as u64
        {
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
        .filter(|path| {
            std::path::Path::new(path)
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.to_ascii_lowercase().starts_with("codexhub"))
        })
        .or_else(|| {
            std::env::current_exe()
                .ok()
                .map(|path| path.display().to_string())
        })
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
