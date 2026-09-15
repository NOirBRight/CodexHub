Object.entries(Main.panel.statusArea).filter(([k]) => k.startsWith('appindicator-')).map(([k, s]) => ({
  id: k,
  open: s.menu.isOpen,
  items: s.menu._getMenuItems().filter(i => i.constructor.name !== 'PopupSeparatorMenuItem').map(i => ({
    text: i.label?.text,
    fg: i.label?.get_theme_node().get_foreground_color().to_string(),
    bg: s.menu.box.get_theme_node().get_background_color().to_string(),
    style: i.label?.get_style()
  }))
}))
