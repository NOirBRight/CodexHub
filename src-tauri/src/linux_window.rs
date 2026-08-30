//! Linux window shell: stay on the taskbar, keep the full GTK pointer-input
//! region, and reconcile the single user launcher used by AppImage installs.

use gtk::gdk::{self, WindowTypeHint};
use gtk::prelude::*;
use std::cell::Cell;
use std::io::Write;
use std::rc::Rc;
use std::time::Duration;
use tauri::{Manager, WebviewWindow};

const APP_ICON_PNG: &[u8] = include_bytes!("../icons/128x128.png");
const APPIMAGE_DESKTOP_FILE: &str = "com.codexhub.app.desktop";
const LEGACY_DESKTOP_FILE: &str = "codexhub.desktop";

pub fn install(app: &tauri::App) {
    glib::set_prgname(Some("com.codexhub.app"));
    glib::set_application_name("CodexHub");
    gdk::set_program_class("com.codexhub.app");
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
        apply_full_input_region(&gtk_window.clone().upcast());
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
    apply_window_icon(&gtk_window);
    apply_opaque_window_css(&gtk_window);
    hook_full_input_region(&gtk_window);
    fill_webview_on_resize(&gtk_window);
    Ok(())
}

/// WebKitGTK can derive a GTK input shape from page alpha after realize,
/// resize, or redraw. An empty or partial shape sends physical clicks to the
/// window below CodexHub, so restore the full native input region at each of
/// those lifecycle boundaries. This intentionally does not change the visual
/// opaque region.
fn hook_full_input_region(gtk_window: &gtk::ApplicationWindow) {
    let widget: gtk::Widget = gtk_window.clone().upcast();
    apply_full_input_region(&widget);
    widget.connect_realize(apply_full_input_region);
    widget.connect_size_allocate(|widget, _| apply_full_input_region(widget));
    widget.connect_draw(|widget, _| {
        let widget = widget.clone();
        glib::idle_add_local_once(move || apply_full_input_region(&widget));
        glib::Propagation::Proceed
    });
}

fn apply_full_input_region(widget: &gtk::Widget) {
    if widget.has_window() {
        if let Some(gdk_window) = widget.window() {
            let region = cairo::Region::create_rectangle(&cairo::RectangleInt::new(
                0,
                0,
                gdk_window.width().max(1),
                gdk_window.height().max(1),
            ));
            widget.input_shape_combine_region(Some(&region));
            gdk_window.input_shape_combine_region(&region, 0, 0);
        }
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            apply_full_input_region(&child);
        }
    }
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

fn apply_window_icon(gtk_window: &gtk::ApplicationWindow) {
    gtk::Window::set_default_icon_name("codexhub");
    gtk_window.set_icon_name(Some("codexhub"));
    if let Ok(pixbuf) = gdk_pixbuf::Pixbuf::from_read(std::io::Cursor::new(APP_ICON_PNG)) {
        gtk::Window::set_default_icon(&pixbuf);
        gtk_window.set_icon(Some(&pixbuf));
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
    let appimage = appimage_path();
    if let Err(error) = reconcile_desktop_entries(&apps_dir, appimage.as_deref()) {
        log::warn!("failed to reconcile Linux desktop entry: {error}");
    }
}

fn reconcile_desktop_entries(
    apps_dir: &std::path::Path,
    appimage: Option<&std::path::Path>,
) -> std::io::Result<()> {
    if !apps_dir.exists() {
        if appimage.is_none() {
            return Ok(());
        }
        std::fs::create_dir_all(apps_dir)?;
    }

    let canonical = apps_dir.join(APPIMAGE_DESKTOP_FILE);
    let legacy = apps_dir.join(LEGACY_DESKTOP_FILE);
    archive_managed_desktop_entry(&legacy)?;

    let Some(appimage) = appimage else {
        // deb/rpm packages own their stable system launcher. Remove only files
        // created by older CodexHub runtimes so package upgrades leave one
        // effective launcher without touching user customizations.
        archive_managed_desktop_entry(&canonical)?;
        return Ok(());
    };

    if canonical.is_file() {
        let existing = std::fs::read_to_string(&canonical)?;
        if !is_managed_desktop_entry(&existing) {
            return Ok(());
        }
    }

    let exec = quote_desktop_exec(appimage);
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
         X-GNOME-UsesNotifications=true\n\
         X-CodexHub-Managed=true\n"
    );
    write_if_changed(&canonical, &body)
}

fn appimage_path() -> Option<std::path::PathBuf> {
    std::env::var("APPIMAGE")
        .ok()
        .filter(|path| {
            std::path::Path::new(path)
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.to_ascii_lowercase().starts_with("codexhub"))
        })
        .map(std::path::PathBuf::from)
}

fn quote_desktop_exec(path: &std::path::Path) -> String {
    let raw = path.display().to_string();
    if raw.chars().any(|ch| ch.is_whitespace()) {
        format!("\"{}\"", raw.replace('"', "\\\""))
    } else {
        raw
    }
}

fn archive_managed_desktop_entry(path: &std::path::Path) -> std::io::Result<()> {
    let body = match std::fs::read_to_string(path) {
        Ok(body) => body,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if is_managed_desktop_entry(&body) {
        let Some(file_name) = path.file_name().and_then(|name| name.to_str()) else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "desktop entry filename is not UTF-8",
            ));
        };
        for index in 0..100 {
            let suffix = if index == 0 {
                String::new()
            } else {
                format!(".{index}")
            };
            let backup = path.with_file_name(format!("{file_name}.codexhub-legacy-backup{suffix}"));
            match std::fs::hard_link(path, &backup) {
                Ok(()) => {
                    std::fs::remove_file(path)?;
                    return Ok(());
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "too many legacy desktop entry backups",
        ));
    }
    Ok(())
}

fn is_managed_desktop_entry(body: &str) -> bool {
    const REQUIRED_LINES: [&str; 10] = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=CodexHub",
        "Comment=CodexHub desktop backend and CLI",
        "Icon=codexhub",
        "Terminal=false",
        "Categories=Development;",
        "StartupNotify=true",
        "StartupWMClass=com.codexhub.app",
        "X-GNOME-UsesNotifications=true",
    ];
    let lines: Vec<&str> = body.lines().collect();
    if !matches!(lines.len(), 11 | 12)
        || !REQUIRED_LINES
            .iter()
            .all(|required| lines.contains(required))
        || lines
            .iter()
            .filter(|line| line.starts_with("Exec=") && line.len() > "Exec=".len())
            .count()
            != 1
    {
        return false;
    }
    let known_lines = lines.iter().all(|line| {
        REQUIRED_LINES.contains(line)
            || (line.starts_with("Exec=") && line.len() > "Exec=".len())
            || *line == "X-CodexHub-Managed=true"
    });
    let managed_marker = lines.contains(&"X-CodexHub-Managed=true");
    let legacy_exec = lines
        .iter()
        .find_map(|line| line.strip_prefix("Exec="))
        .is_some_and(is_legacy_managed_exec);
    known_lines && (managed_marker || (lines.len() == 11 && legacy_exec))
}

fn is_legacy_managed_exec(raw: &str) -> bool {
    let command = raw
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .unwrap_or(raw);
    let lower = command.to_ascii_lowercase();
    if lower == "/usr/bin/codexhub"
        || (lower.contains("/src-tauri/target/") && lower.ends_with("/codexhub"))
    {
        return true;
    }
    std::path::Path::new(&lower)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("codexhub") && name.ends_with(".appimage"))
}

fn write_if_changed(path: &std::path::Path, body: &str) -> std::io::Result<()> {
    if std::fs::read_to_string(path).ok().as_deref() == Some(body) {
        return Ok(());
    }
    let mut file = std::fs::File::create(path)?;
    file.write_all(body.as_bytes())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{reconcile_desktop_entries, APPIMAGE_DESKTOP_FILE, LEGACY_DESKTOP_FILE};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    struct TestDir(PathBuf);

    impl TestDir {
        fn new(name: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock should be after epoch")
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "codexhub-linux-window-{name}-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir_all(&path).expect("create test dir");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn legacy_generated_entry(exec: &str) -> String {
        format!(
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
        )
    }

    #[test]
    fn packaged_install_removes_both_runtime_generated_launchers() {
        let root = TestDir::new("package-upgrade");
        for name in [APPIMAGE_DESKTOP_FILE, LEGACY_DESKTOP_FILE] {
            fs::write(
                root.path().join(name),
                legacy_generated_entry("/usr/bin/codexhub"),
            )
            .expect("seed legacy launcher");
        }

        reconcile_desktop_entries(root.path(), None).expect("reconcile package launchers");

        assert!(!root.path().join(APPIMAGE_DESKTOP_FILE).exists());
        assert!(!root.path().join(LEGACY_DESKTOP_FILE).exists());
        assert!(root
            .path()
            .join(format!("{APPIMAGE_DESKTOP_FILE}.codexhub-legacy-backup"))
            .exists());
        assert!(root
            .path()
            .join(format!("{LEGACY_DESKTOP_FILE}.codexhub-legacy-backup"))
            .exists());
    }

    #[test]
    fn packaged_install_preserves_a_user_customized_launcher() {
        let root = TestDir::new("custom-launcher");
        let custom = legacy_generated_entry("/custom/codexhub");
        fs::write(root.path().join(LEGACY_DESKTOP_FILE), &custom).expect("seed custom launcher");

        reconcile_desktop_entries(root.path(), None).expect("reconcile package launchers");

        assert_eq!(
            fs::read_to_string(root.path().join(LEGACY_DESKTOP_FILE))
                .expect("read custom launcher"),
            custom
        );
    }

    #[test]
    fn appimage_upgrade_rewrites_one_stable_launcher_and_removes_the_alias() {
        let root = TestDir::new("appimage-upgrade");
        fs::write(
            root.path().join(APPIMAGE_DESKTOP_FILE),
            legacy_generated_entry("/downloads/CodexHub-old.AppImage"),
        )
        .expect("seed old canonical launcher");
        fs::write(
            root.path().join(LEGACY_DESKTOP_FILE),
            legacy_generated_entry("/downloads/CodexHub-old.AppImage"),
        )
        .expect("seed old launcher alias");

        reconcile_desktop_entries(
            root.path(),
            Some(Path::new("/downloads/CodexHub-new.AppImage")),
        )
        .expect("reconcile appimage launcher");

        let launcher = fs::read_to_string(root.path().join(APPIMAGE_DESKTOP_FILE))
            .expect("read canonical launcher");
        assert!(launcher.contains("Exec=/downloads/CodexHub-new.AppImage"));
        assert!(launcher.contains("X-CodexHub-Managed=true"));
        assert!(!root.path().join(LEGACY_DESKTOP_FILE).exists());
    }

    #[test]
    fn unreadable_legacy_launcher_fails_closed_instead_of_silently_duplicating() {
        let root = TestDir::new("invalid-legacy-launcher");
        fs::write(root.path().join(LEGACY_DESKTOP_FILE), [0xff, 0xfe])
            .expect("seed invalid launcher");

        let error = reconcile_desktop_entries(root.path(), None)
            .expect_err("invalid launcher must fail reconciliation");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(root.path().join(LEGACY_DESKTOP_FILE).exists());
    }
}
