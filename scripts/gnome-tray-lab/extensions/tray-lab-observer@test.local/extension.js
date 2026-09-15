import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
export default class Observer extends Extension {
 enable() { global.context.unsafe_mode = true; }
 disable() { global.context.unsafe_mode = false; }
}
