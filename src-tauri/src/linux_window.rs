//! WebKitGTK 2.44+ updates the widget input region from page alpha. Any
//! unpainted or transparent pixel then punches through the undecorated
//! Wayland window. Force the whole window to keep pointer events.

use gtk::prelude::*;
use tauri::{Manager, WebviewWindow};

pub fn install_full_input_region(app: &tauri::App) {
    let Some(window) = app.get_webview_window("main") else {
        log::warn!("Linux input-region guard skipped: main window missing");
        return;
    };
    if let Err(error) = hook_window(&window) {
        log::warn!("Linux input-region guard failed: {error}");
    }
}

fn hook_window(window: &WebviewWindow) -> Result<(), String> {
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
    widget.input_shape_combine_region(None);
    if let Some(gdk_window) = widget.window() {
        let width = gdk_window.width().max(1);
        let height = gdk_window.height().max(1);
        let region = cairo::Region::create_rectangle(&cairo::RectangleInt::new(0, 0, width, height));
        gdk_window.input_shape_combine_region(&region, 0, 0);
        gdk_window.set_opaque_region(Some(&region));
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            apply_full_input_region(&child);
        }
    }
}
