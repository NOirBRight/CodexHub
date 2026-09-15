#include <gtk/gtk.h>
#include <libayatana-appindicator/app-indicator.h>
int main(int argc, char **argv) {
 gtk_init(&argc, &argv);
 AppIndicator *indicator=app_indicator_new("tray-reference", "dialog-information", APP_INDICATOR_CATEGORY_APPLICATION_STATUS);
 GtkWidget *menu=gtk_menu_new();
 gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_menu_item_new_with_label("Show Reference"));
 gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_menu_item_new_with_label("Exit Reference"));
 gtk_widget_show_all(menu);
 app_indicator_set_menu(indicator, GTK_MENU(menu));
 app_indicator_set_status(indicator, APP_INDICATOR_STATUS_ACTIVE);
 gtk_main();
 return 0;
}
