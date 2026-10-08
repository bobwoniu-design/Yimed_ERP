import jQuery from "jquery";
import Alert from "bootstrap/js/dist/alert";
import Button from "bootstrap/js/dist/button";
import Carousel from "bootstrap/js/dist/carousel";
import Collapse from "bootstrap/js/dist/collapse";
import Dropdown from "bootstrap/js/dist/dropdown";
import Modal from "bootstrap/js/dist/modal";
import Popover from "bootstrap/js/dist/popover";
import Scrollspy from "bootstrap/js/dist/scrollspy";
import Tab from "bootstrap/js/dist/tab";
import Toast from "bootstrap/js/dist/toast";
import Tooltip from "bootstrap/js/dist/tooltip";
import Util from "bootstrap/js/dist/util";

window.jQuery = jQuery;
window.$ = jQuery;

// Bootstrap 4.6 dist 的插件类带 _jQueryInterface 静态方法，但 NAME 是模块内
// 局部变量、并非类静态属性（仅 popover/tooltip 例外），导致 `plugin?.NAME`
// 判定失败、$.fn.modal 等接口从未注册，所有弹窗静默失效。
// 这里按 Bootstrap 官方 NAME 映射显式注册，不依赖类静态属性。
const BOOTSTRAP_PLUGIN_NAMES = {
	Alert: "alert",
	Button: "button",
	Carousel: "carousel",
	Collapse: "collapse",
	Dropdown: "dropdown",
	Modal: "modal",
	Popover: "popover",
	Scrollspy: "scrollspy",
	Tab: "tab",
	Toast: "toast",
	Tooltip: "tooltip",
};

const bootstrapPlugins = { Alert, Button, Carousel, Collapse, Dropdown, Modal, Popover, Scrollspy, Tab, Toast, Tooltip };

for (const [pluginKey, plugin] of Object.entries(bootstrapPlugins)) {
	const name = BOOTSTRAP_PLUGIN_NAMES[pluginKey] || (plugin && plugin.NAME);
	if (name && plugin && typeof plugin._jQueryInterface === "function") {
		jQuery.fn[name] = plugin._jQueryInterface;
		jQuery.fn[name].Constructor = plugin;
	}
}

// data-api 委托兜底：本环境存在多个 bundle 各自带 jQuery 副本，bootstrap dist
// 模块内部的 document 委托绑定所用的实例会在后续 bundle 加载后失效，导致
// [data-toggle="dropdown"] 等声明式组件点击无反应。这里用原生 document 捕获监听器
// 兜底：不依赖任何 jQuery 实例的委托缓存，不受后续 bundle 覆盖 window.jQuery 或
// frappe 视图切换时 $(document).off('click') 清理的影响（SPA 路由切换视图后依然有效）。
// 插件调用使用本模块闭包持有的 jQuery 实例（其 fn 上已注册 bootstrap 插件）。
if (typeof document.addEventListener === "function") {
	document.addEventListener(
		"click",
		function (event) {
			if (event.button !== 0) return;
			// 优先使用当前 window.jQuery（probe 实测该实例的 bootstrap 插件可用），
			// 否则回退本模块实例；两者都无插件时交由其他 bundle 的监听器处理
			const $win = window.jQuery;
			const $bs = $win && typeof $win.fn.dropdown === "function" ? $win : jQuery;
			if (typeof $bs.fn.dropdown !== "function") return;
			// 多个 bundle 各自打包本模块（desk/list/form/controls/report...），
			// 同一次点击会被各自的监听器重复处理，导致 dropdown 被反复 toggle。
			// 用事件对象上的标记保证一次点击只由第一个可用实例处理。
			if (event.__bsDropdownApiHandled) return;
			event.__bsDropdownApiHandled = true;
			const toggle = event.target.closest('[data-toggle="dropdown"]');
			if (toggle) {
				event.preventDefault();
				event.stopPropagation();
				$bs(toggle).dropdown("toggle");
				return;
			}
			// 点击 dropdown 之外时关闭已打开的菜单
			if (event.target.closest(".dropdown-menu")) return;
			$bs('[data-toggle="dropdown"]').each(function () {
				const $menu = $bs(this).next(".dropdown-menu");
				if ($menu.length && $menu.hasClass("show")) $bs(this).dropdown("hide");
			});
		},
		true
	);
}

export {
	Util,
	Alert,
	Button,
	Carousel,
	Collapse,
	Dropdown,
	Modal,
	Popover,
	Scrollspy,
	Tab,
	Toast,
	Tooltip,
};
